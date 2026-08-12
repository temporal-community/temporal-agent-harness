import type {
  ChroniclerAudioArtifactReceipt,
  ChroniclerAudioDestinationApproval,
  ChroniclerAudioSnapshot
} from "$lib/bridge/api";
import type { ChroniclerTranscriptSource } from "$lib/bridge/source";
import type { AgentInboundMessage } from "$lib/api/types";
import type { TranscriptItem } from "$lib/state/transcript";

export interface AudioDraft {
  draft_id: string;
  draft_digest: string;
  source_kind: "existing" | "synthetic";
  source_identity: string;
  source_content: string;
  source_hash: string;
  recap_script: string;
  voice: "Charon";
  wav_path: string;
  synthetic_markdown_path: string | null;
  bridge_id: string;
  root_id: string;
  folder_binding_id: string;
}

export interface AudioDraftResponse {
  draft: AudioDraft;
}

export type AudioSnapshot = ChroniclerAudioSnapshot;

export interface AudioComposerState {
  sourceKey: string | null;
  draft: AudioDraft | null;
  snapshot: AudioSnapshot | null;
}

export function audioChildMayExist(items: readonly TranscriptItem[]): boolean {
  return items.some((item) =>
    item.kind === "tool"
    && item.toolName === "generate_audio"
    && (item.status === "running" || item.status === "done" || item.status === "failed")
  );
}

export function selectAudioSource(
  state: AudioComposerState,
  sourceKey: string
): AudioComposerState {
  if (state.sourceKey === sourceKey) return state;
  return { sourceKey, draft: null, snapshot: null };
}

export function requestAudioChanges(state: AudioComposerState): AudioComposerState {
  return { sourceKey: state.sourceKey, draft: null, snapshot: null };
}

export function transitionAudioGeneration(
  current: ChroniclerAudioSnapshot | null,
  next: ChroniclerAudioSnapshot,
  playbackUrl: string | null,
  revokePlayback: () => void
): { snapshot: ChroniclerAudioSnapshot; playbackUrl: string | null } {
  const generationChanged = current?.status.generation_id !== next.status.generation_id;
  if (generationChanged || next.state !== "completed") {
    revokePlayback();
    return { snapshot: next, playbackUrl: null };
  }
  return { snapshot: next, playbackUrl };
}

export function transitionPreparedDraft(
  observedDraftDigest: string | null,
  nextDraft: AudioDraft,
  snapshot: ChroniclerAudioSnapshot | null,
  playbackUrl: string | null,
  revokePlayback: () => void
): {
  observedDraftDigest: string;
  snapshot: ChroniclerAudioSnapshot | null;
  playbackUrl: string | null;
} {
  if (
    observedDraftDigest !== null
    && observedDraftDigest !== nextDraft.draft_digest
    && snapshot?.state !== "running"
  ) {
    revokePlayback();
    return {
      observedDraftDigest: nextDraft.draft_digest,
      snapshot: null,
      playbackUrl: null
    };
  }
  return {
    observedDraftDigest: nextDraft.draft_digest,
    snapshot,
    playbackUrl
  };
}

export function transitionAudioStart(
  draft: AudioDraft,
  playbackUrl: string | null,
  revokePlayback: () => void
): { startedDraftDigest: string; snapshot: null; playbackUrl: null } {
  revokePlayback();
  return {
    startedDraftDigest: draft.draft_digest,
    snapshot: null,
    playbackUrl: null
  };
}

export function acceptPolledAudioSnapshot(
  snapshot: ChroniclerAudioSnapshot,
  blockedGenerationId: string | null
):
  | { accepted: false; blockedGenerationId: string }
  | {
      accepted: true;
      blockedGenerationId: null;
      snapshot: ChroniclerAudioSnapshot;
    } {
  if (blockedGenerationId === snapshot.status.generation_id) {
    return { accepted: false, blockedGenerationId };
  }
  return { accepted: true, blockedGenerationId: null, snapshot };
}

export function generationBlockedByStart(
  snapshot: ChroniclerAudioSnapshot | null,
  lastObservedGenerationId: string | null
): string | null {
  return snapshot?.status.generation_id ?? lastObservedGenerationId;
}

export async function runAudioCancellation(
  snapshot: ChroniclerAudioSnapshot,
  cancel: (childWorkflowId: string) => Promise<unknown>,
  loadTerminal: () => Promise<ChroniclerAudioSnapshot>,
  onSnapshot: (snapshot: ChroniclerAudioSnapshot) => void
): Promise<void> {
  onSnapshot({
    ...snapshot,
    state: "running",
    status: {
      ...snapshot.status,
      phase: "canceling",
      detail: "Waiting for authoritative cancellation…"
    },
    result: null
  });
  await cancel(snapshot.child_workflow_id);
  onSnapshot(await loadTerminal());
}

export function acceptCancellationStatus(
  snapshot: ChroniclerAudioSnapshot,
  cancellationPending: boolean
): boolean {
  return !cancellationPending
    || snapshot.state !== "running"
    || snapshot.status.phase === "canceling";
}

export function changeAudioSource(
  draftDigest: string | null,
  playbackUrl: string | null,
  revokePlayback: () => void
): {
  invalidatedDraftDigest: string | null;
  startedDraftDigest: null;
  snapshot: null;
  playbackUrl: null;
} {
  revokePlayback();
  return {
    invalidatedDraftDigest: draftDigest,
    startedDraftDigest: null,
    snapshot: null,
    playbackUrl: null
  };
}

export interface AudioRouting {
  bridge_id: string;
  root_id: string;
  folder_binding_id: string;
}

function exactAudioPaths(draft: AudioDraft): string[] {
  return [
    draft.wav_path,
    ...(draft.synthetic_markdown_path ? [draft.synthetic_markdown_path] : [])
  ];
}

export function destinationApprovalFromSnapshot(
  snapshot: ChroniclerAudioSnapshot,
  routing: AudioRouting
): ChroniclerAudioDestinationApproval | null {
  const revision = snapshot.pending_destination_revision;
  if (!revision) return null;
  return {
    generation_id: revision.generation_id,
    content_digest: revision.content_digest,
    destination_revision: revision.destination_revision,
    wav_path: revision.wav_path,
    synthetic_markdown_path: revision.synthetic_markdown_path,
    ...routing
  };
}

export async function authorizeDestinationApproval(
  snapshot: ChroniclerAudioSnapshot,
  routing: AudioRouting,
  authority: Pick<AudioStartAuthority, "preflightAudioDestinations"> & {
    verifyAudioArtifact?(receipt: ChroniclerAudioArtifactReceipt): Promise<void>;
  }
): Promise<ChroniclerAudioDestinationApproval> {
  const approval = destinationApprovalFromSnapshot(snapshot, routing);
  if (!approval) throw new Error("No destination revision is awaiting approval.");
  const approved = snapshot.approved_package ?? snapshot.result?.approved_package;
  if (!approved) {
    await authority.preflightAudioDestinations(
      [
        approval.wav_path,
        ...(approval.synthetic_markdown_path ? [approval.synthetic_markdown_path] : [])
      ],
      approval.folder_binding_id
    );
    return approval;
  }
  const changedPaths = [
    ...(approval.wav_path !== approved.wav_path ? [approval.wav_path] : []),
    ...(approval.synthetic_markdown_path
      && approval.synthetic_markdown_path !== approved.synthetic_markdown_path
      ? [approval.synthetic_markdown_path]
      : [])
  ];
  const unchangedRoles = [
    ...(approval.wav_path === approved.wav_path ? ["wav" as const] : []),
    ...(approval.synthetic_markdown_path
      && approval.synthetic_markdown_path === approved.synthetic_markdown_path
      ? ["synthetic_transcript" as const]
      : [])
  ];
  for (const role of unchangedRoles) {
    const receipt = snapshot.receipts.find((item) =>
      item.artifact_role === role
      && item.generation_id === approved.generation_id
      && item.package_revision <= approved.package_revision
      && item.folder_binding_id === approved.folder_binding_id
      && item.relative_path === (role === "wav"
        ? approved.wav_path
        : approved.synthetic_markdown_path)
    );
    if (!receipt) throw new Error("The unchanged audio artifact has no authoritative receipt.");
    if (!authority.verifyAudioArtifact) {
      throw new Error("The unchanged audio artifact cannot be verified in this browser.");
    }
    await authority.verifyAudioArtifact(receipt);
  }
  await authority.preflightAudioDestinations(
    changedPaths,
    approval.folder_binding_id
  );
  return approval;
}

export function prepareExistingAudioMessage(
  source: ChroniclerTranscriptSource,
  routing: AudioRouting
): AgentInboundMessage {
  return {
    type: "prepare_audio",
    payload: {
      source: {
        source_kind: "existing",
        source_identity: source.sessionId,
        source_content: source.content,
        source_hash: source.contentHash
      },
      ...routing
    }
  };
}

export function prepareSyntheticAudioMessage(
  topic: string,
  routing: AudioRouting
): AgentInboundMessage {
  return {
    type: "prepare_audio",
    payload: {
      source: { source_kind: "synthetic", topic },
      ...routing
    }
  };
}

export function startAudioMessage(draft: AudioDraft): AgentInboundMessage {
  return {
    type: "start_audio",
    payload: {
      draft_id: draft.draft_id,
      draft_digest: draft.draft_digest,
      bridge_id: draft.bridge_id,
      root_id: draft.root_id,
      folder_binding_id: draft.folder_binding_id,
      preflighted_paths: exactAudioPaths(draft)
    }
  };
}

interface AudioStartAuthority {
  readChroniclerTranscriptSource(sessionId: string): Promise<ChroniclerTranscriptSource>;
  preflightAudioDestinations(
    relativePaths: readonly string[],
    folderBindingId: string
  ): Promise<void>;
}

export async function authorizeAudioStart(
  draft: AudioDraft,
  authority: AudioStartAuthority
): Promise<AgentInboundMessage> {
  if (draft.source_kind === "existing") {
    const current = await authority.readChroniclerTranscriptSource(draft.source_identity);
    if (
      current.content !== draft.source_content
      || current.contentHash !== draft.source_hash
      || current.folderBindingId !== draft.folder_binding_id
    ) {
      throw new Error("The selected transcript changed. Prepare a new audio review.");
    }
  }
  await authority.preflightAudioDestinations(
    exactAudioPaths(draft),
    draft.folder_binding_id
  );
  return startAudioMessage(draft);
}

export function reprepareAudioMessage(
  draft: AudioDraft,
  changeRequest: string
): AgentInboundMessage {
  return {
    type: "prepare_audio",
    payload: {
      change_request: changeRequest.trim(),
      base_draft_digest: draft.draft_digest,
      bridge_id: draft.bridge_id,
      root_id: draft.root_id,
      folder_binding_id: draft.folder_binding_id
    }
  };
}

export function latestAudioDraft(items: TranscriptItem[]): AudioDraft | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind !== "agent") continue;
    const draft = item.output?.draft;
    if (
      typeof draft === "object"
      && draft !== null
      && "draft_id" in draft
      && typeof draft.draft_id === "string"
      && "draft_digest" in draft
      && typeof draft.draft_digest === "string"
    ) return draft as unknown as AudioDraft;
  }
  return null;
}

export interface AudioRecoveryIdentity extends AudioRouting {
  generation_id: string;
  content_digest: string;
  destination_digest: string;
  package_digest: string;
}

export interface AudioRecoveryAuthority extends AudioRecoveryIdentity {
  package_revision?: number;
  wav_path?: string;
  synthetic_markdown_path?: string | null;
}

export function recoverAudioMessage(identity: AudioRecoveryIdentity): AgentInboundMessage {
  return {
    type: "recover_audio",
    payload: {
      generation_id: identity.generation_id,
      content_digest: identity.content_digest,
      destination_digest: identity.destination_digest,
      package_digest: identity.package_digest,
      bridge_id: identity.bridge_id,
      root_id: identity.root_id,
      folder_binding_id: identity.folder_binding_id
    }
  };
}

export function authoritativeAudioRecoveryIdentity(
  snapshot: ChroniclerAudioSnapshot | null,
  items: TranscriptItem[]
): AudioRecoveryAuthority | null {
  const approved = snapshot?.approved_package ?? snapshot?.result?.approved_package;
  if (!approved) return latestAudioRecoveryIdentity(items);
  return {
    generation_id: approved.generation_id,
    content_digest: approved.content_digest,
    destination_digest: approved.destination_digest,
    package_digest: approved.package_digest,
    bridge_id: approved.bridge_id,
    root_id: approved.root_id,
    folder_binding_id: approved.folder_binding_id,
    package_revision: approved.package_revision,
    wav_path: approved.wav_path,
    synthetic_markdown_path: approved.synthetic_markdown_path
  };
}

export function audioRecoveryMessage(
  snapshot: ChroniclerAudioSnapshot | null,
  items: TranscriptItem[]
): AgentInboundMessage | null {
  const outcome = snapshot?.result?.outcome;
  if (outcome !== "failed" && outcome !== "needs_recovery") return null;
  if (
    outcome === "failed"
    && !snapshot?.approved_package
    && !snapshot?.result?.approved_package
  ) return null;
  const identity = authoritativeAudioRecoveryIdentity(snapshot, items);
  return identity ? recoverAudioMessage(identity) : null;
}

export function latestAudioRecoveryIdentity(
  items: TranscriptItem[]
): AudioRecoveryIdentity | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind !== "tool" || item.toolName !== "generate_audio" || !item.input) continue;
    const input = item.input;
    const keys: Array<keyof AudioRecoveryIdentity> = [
      "generation_id",
      "content_digest",
      "destination_digest",
      "package_digest",
      "bridge_id",
      "root_id",
      "folder_binding_id"
    ];
    if (keys.every((key) => typeof input[key] === "string")) {
      return Object.fromEntries(keys.map((key) => [key, input[key]])) as unknown as AudioRecoveryIdentity;
    }
  }
  return null;
}
