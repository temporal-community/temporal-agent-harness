<script module lang="ts">
  import { WorkflowNotFoundError } from "$lib/bridge/api";
  import type { AgentInboundMessage as SubmittedAudioMessage } from "$lib/api/types";

  export function audioControlsAreReadOnly(following: boolean, closed: boolean): boolean {
    return !following || closed;
  }

  export function isCurrentAudioStatusRequest(
    request: { sessionId: string; revision: number },
    current: { sessionId: string; revision: number }
  ): boolean {
    return request.sessionId === current.sessionId && request.revision === current.revision;
  }

  export function audioStatusFailure(
    cause: unknown,
    childMayExist: boolean,
    childObserved = false,
    startupGraceActive = false
  ): { stopPolling: boolean; error: string | null } {
    if (cause instanceof WorkflowNotFoundError) {
      return childMayExist && (childObserved || !startupGraceActive)
        ? {
            stopPolling: true,
            error: "The audio generation child no longer matches this session. Start a new audio review."
          }
        : { stopPolling: false, error: null };
    }
    return {
      stopPolling: false,
      error: cause instanceof Error ? cause.message : String(cause)
    };
  }

  export class AudioStatusStartupGuard {
    private childObserved = false;
    private startupDeadline: number | null = null;
    private readonly graceMs: number;
    private readonly now: () => number;

    constructor(graceMs = 5_000, now: () => number = () => Date.now()) {
      this.graceMs = graceMs;
      this.now = now;
    }

    expectChild(): void {
      if (this.childObserved || this.startupDeadline !== null) return;
      this.startupDeadline = this.now() + this.graceMs;
    }

    accept<T>(snapshot: T): T {
      this.childObserved = true;
      this.startupDeadline = null;
      return snapshot;
    }

    failure(cause: unknown, childMayExist: boolean): { stopPolling: boolean; error: string | null } {
      if (childMayExist) this.expectChild();
      return audioStatusFailure(
        cause,
        childMayExist,
        this.childObserved,
        this.startupDeadline !== null && this.now() < this.startupDeadline
      );
    }

    reset(): void {
      this.childObserved = false;
      this.startupDeadline = null;
    }
  }

  export function resetAudioStatusPollingUnavailable(): false {
    return false;
  }

  export function timelineAudioSnapshot<T>(
    following: boolean,
    snapshot: T | null,
    sourceSelectionActive = false
  ): T | null {
    return following && !sourceSelectionActive ? snapshot : null;
  }

  export function draftInvalidationAfterSubmit(
    currentInvalidation: string | null,
    reviewedDraftDigest: string | null,
    accepted: boolean
  ): string | null {
    return accepted ? reviewedDraftDigest : currentInvalidation;
  }

  export async function submitAudioMessage(
    onSend: (message: SubmittedAudioMessage) => void | Promise<void>,
    message: SubmittedAudioMessage
  ): Promise<{ accepted: boolean; error: string | null }> {
    try {
      await onSend(message);
      return { accepted: true, error: null };
    } catch (cause) {
      return {
        accepted: false,
        error: cause instanceof Error ? cause.message : String(cause)
      };
    }
  }
</script>

<script lang="ts">
  import {
    HttpChroniclerAudioApi,
    chroniclerAudioChildWorkflowId,
    type ChroniclerAudioSnapshot
  } from "$lib/bridge/api";
  import { AudioPlaybackLoadLifecycle, AudioPlaybackSession } from "$lib/bridge/playback";
  import {
    audioCancellationAvailability,
    browserBridgeRuntime
  } from "$lib/bridge/runtime";
  import type { ChroniclerTranscriptDiscovery, ChroniclerTranscriptSource } from "$lib/bridge/source";
  import type { AgentInboundMessage } from "$lib/api/types";
  import type { TranscriptItem } from "$lib/state/transcript";
  import {
    acceptPolledAudioSnapshot,
    acceptCancellationStatus,
    audioChildMayExist,
    audioRecoveryMessage,
    authorizeAudioStart,
    authorizeDestinationApproval,
    changeAudioSource,
    destinationApprovalFromSnapshot,
    generationBlockedByStart,
    latestAudioDraft,
    prepareExistingAudioMessage,
    prepareSyntheticAudioMessage,
    reprepareAudioMessage,
    runAudioCancellation,
    transitionAudioGeneration,
    transitionAudioStart,
    transitionPreparedDraft,
    type AudioDraft,
    type AudioRouting
  } from "./audioUi";
  import ChroniclerAudioWorkspace from "./ChroniclerAudioWorkspace.svelte";
  import { AudioSourceDiscoveryLifecycle } from "./audioDiscovery";
  import { startAudioLiveEffects } from "./audioLive";
  import {
    AudioApprovalPreflightLifecycle,
    AudioDestinationApprovalLifecycle,
    type AudioPreflightState
  } from "./audioPreflight";
  import type { AudioGenerationPresentation } from "./audioPresentation";

  interface Props {
    items: TranscriptItem[];
    sessionId: string;
    following?: boolean;
    closed?: boolean;
    onSend?: (message: AgentInboundMessage) => void | Promise<void>;
    onPresentationChange?: (presentation: AudioGenerationPresentation | null) => void;
  }

  let { items, sessionId, following = true, closed = false, onSend, onPresentationChange }: Props = $props();
  const readOnly = $derived(audioControlsAreReadOnly(following, closed));
  const responseDraft = $derived(latestAudioDraft(items));
  const childMayExist = $derived(audioChildMayExist(items));
  let invalidatedDraftDigest = $state<string | null>(null);
  let startedDraftDigest = $state<string | null>(null);
  let blockedGenerationId = $state<string | null>(null);
  let lastObservedGenerationId = $state<string | null>(null);
  const draft = $derived(
    responseDraft?.draft_digest === invalidatedDraftDigest
      || responseDraft?.draft_digest === startedDraftDigest
      ? null
      : responseDraft
  );
  let discovery = $state<ChroniclerTranscriptDiscovery | null>(null);
  let snapshot = $state<ChroniclerAudioSnapshot | null>(null);
  let sourceSelectionActive = $state(false);
  const visibleSnapshot = $derived(
    timelineAudioSnapshot(following, snapshot, sourceSelectionActive)
  );
  const recoveryMessage = $derived(audioRecoveryMessage(visibleSnapshot, items));
  const presentationToolId = $derived.by(() => {
    if (!visibleSnapshot) return undefined;
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (
        item?.kind === "tool"
        && item.toolName === "generate_audio"
        && item.input?.generation_id === visibleSnapshot.status.generation_id
      ) return item.toolId;
    }
    return undefined;
  });
  let bridgeStatus = $state(browserBridgeRuntime.status);
  let playbackUrl = $state<string | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);
  let cancellationPending = $state(false);
  let approvalAuthority = $state<AudioPreflightState>({
    ready: false,
    detail: "Connect the active browser bridge to approve audio."
  });
  let destinationAuthority = $state<AudioPreflightState>({
    ready: false,
    detail: "Connect the active browser bridge to approve destinations."
  });
  const audioApi = new HttpChroniclerAudioApi();
  const playback = new AudioPlaybackSession((receipt) =>
    browserBridgeRuntime.verifyAudioPlayback(receipt)
  );
  const playbackLifecycle = new AudioPlaybackLoadLifecycle(
    playback,
    (url) => { playbackUrl = url; },
    (detail) => { error = detail; }
  );
  const sourceDiscovery = new AudioSourceDiscoveryLifecycle(
    () => browserBridgeRuntime.discoverChroniclerTranscriptSources(),
    (value) => { discovery = value; },
    (cause) => { error = cause instanceof Error ? cause.message : String(cause); }
  );
  const packagePreflight = new AudioApprovalPreflightLifecycle(
    (paths, binding) => browserBridgeRuntime.preflightAudioDestinations(paths, binding),
    (value) => { approvalAuthority = value; }
  );
  const destinationPreflight = new AudioDestinationApprovalLifecycle(
    (value, activeRouting) => authorizeDestinationApproval(
      value,
      activeRouting,
      browserBridgeRuntime
    ),
    (value) => { destinationAuthority = value; }
  );
  const cancellation = $derived(audioCancellationAvailability(bridgeStatus));
  const destinationApproval = $derived.by(() => {
    if (!visibleSnapshot || !bridgeStatus.handleBindingId) return null;
    return destinationApprovalFromSnapshot(visibleSnapshot, {
      bridge_id: bridgeStatus.bridgeId,
      root_id: bridgeStatus.rootId,
      folder_binding_id: bridgeStatus.handleBindingId
    });
  });

  $effect(() => {
    onPresentationChange?.(!readOnly && visibleSnapshot ? {
      snapshot: visibleSnapshot,
      generationId: visibleSnapshot.status.generation_id,
      toolId: presentationToolId,
      cancellation,
      destinationApproval,
      destinationAuthority,
      recoveryAvailable: recoveryMessage !== null,
      onApproveDestination: !readOnly ? approveDestination : undefined,
      onCancel: !readOnly ? cancel : undefined,
      onRecover: !readOnly && !busy && recoveryMessage ? recover : undefined
    } : null);
  });

  $effect(() => () => { onPresentationChange?.(null); });
  let observedSessionId = $state<string | null>(null);
  let observedDraftDigest = $state<string | null>(null);
  let statusRequestRevision = $state(0);
  let statusPollingUnavailable = $state(false);
  const audioStatusStartup = new AudioStatusStartupGuard();

  $effect(() => {
    if (!responseDraft) return;
    if (
      sourceSelectionActive
      && responseDraft.draft_digest !== invalidatedDraftDigest
    ) sourceSelectionActive = false;
    const draftChanged = observedDraftDigest !== null
      && observedDraftDigest !== responseDraft.draft_digest;
    const transition = transitionPreparedDraft(
      observedDraftDigest,
      responseDraft,
      snapshot,
      playbackUrl,
      () => playbackLifecycle.invalidate()
    );
    observedDraftDigest = transition.observedDraftDigest;
    if (startedDraftDigest !== responseDraft.draft_digest) startedDraftDigest = null;
    if (draftChanged) blockedGenerationId = null;
    snapshot = transition.snapshot;
    playbackUrl = transition.playbackUrl;
  });

  $effect(() => {
    if (readOnly) {
      void sourceDiscovery.update({ ...bridgeStatus, phase: "disconnected", handleBindingId: null });
      return;
    }
    void sourceDiscovery.update(bridgeStatus);
  });

  $effect(() => {
    if (readOnly || !draft) {
      approvalAuthority = {
        ready: false,
        detail: "Connect the active browser bridge to approve audio."
      };
      return;
    }
    void packagePreflight.update(bridgeStatus, {
      paths: [
        draft.wav_path,
        ...(draft.synthetic_markdown_path ? [draft.synthetic_markdown_path] : [])
      ],
      folderBindingId: draft.folder_binding_id
    });
  });

  $effect(() => {
    if (readOnly || !destinationApproval) {
      destinationPreflight.invalidate();
      destinationAuthority = {
        ready: false,
        detail: "Connect the active browser bridge to approve destinations."
      };
      return;
    }
    if (!visibleSnapshot) return;
    void destinationPreflight.update(bridgeStatus, visibleSnapshot, routing());
  });

  $effect(() => {
    if (observedSessionId === null) {
      observedSessionId = sessionId;
      return;
    }
    if (observedSessionId === sessionId) return;
    statusRequestRevision += 1;
    playbackLifecycle.invalidate();
    playbackUrl = null;
    snapshot = null;
    invalidatedDraftDigest = null;
    observedDraftDigest = null;
    blockedGenerationId = null;
    lastObservedGenerationId = null;
    statusPollingUnavailable = false;
    audioStatusStartup.reset();
    sourceSelectionActive = false;
    observedSessionId = sessionId;
  });

  function routing(): AudioRouting {
    if (!bridgeStatus.handleBindingId) throw new Error("Connect the campaign folder first.");
    return {
      bridge_id: bridgeStatus.bridgeId,
      root_id: bridgeStatus.rootId,
      folder_binding_id: bridgeStatus.handleBindingId
    };
  }

  async function send(message: AgentInboundMessage): Promise<boolean> {
    if (readOnly) throw new Error("Audio controls are read-only for this session.");
    if (!onSend) throw new Error("Audio controls are unavailable.");
    busy = true;
    error = null;
    try {
      const result = await submitAudioMessage(onSend, message);
      error = result.error;
      return result.accepted;
    } finally {
      busy = false;
    }
  }

  async function prepareExisting(source: ChroniclerTranscriptSource): Promise<void> {
    if (!await browserBridgeRuntime.isChroniclerTranscriptSourceCurrent(source)) {
      error = "The selected transcript changed. Refresh the archive before preparing audio.";
      return;
    }
    const reviewedDraftDigest = responseDraft?.draft_digest ?? null;
    const accepted = await send(prepareExistingAudioMessage(source, routing()));
    invalidatedDraftDigest = draftInvalidationAfterSubmit(
      invalidatedDraftDigest,
      reviewedDraftDigest,
      accepted
    );
  }

  async function prepareTopic(topic: string): Promise<void> {
    const reviewedDraftDigest = responseDraft?.draft_digest ?? null;
    const accepted = await send(prepareSyntheticAudioMessage(topic, routing()));
    invalidatedDraftDigest = draftInvalidationAfterSubmit(
      invalidatedDraftDigest,
      reviewedDraftDigest,
      accepted
    );
  }

  async function approve(nextDraft: AudioDraft): Promise<void> {
    try {
      const accepted = await send(await authorizeAudioStart(nextDraft, browserBridgeRuntime));
      if (!accepted) return;
      statusPollingUnavailable = resetAudioStatusPollingUnavailable();
      audioStatusStartup.reset();
      audioStatusStartup.expectChild();
      blockedGenerationId = generationBlockedByStart(snapshot, lastObservedGenerationId);
      const transition = transitionAudioStart(
        nextDraft,
        playbackUrl,
        () => playbackLifecycle.invalidate()
      );
      startedDraftDigest = transition.startedDraftDigest;
      snapshot = transition.snapshot;
      playbackUrl = transition.playbackUrl;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  }

  function selectDifferentSource(): void {
    const transition = changeAudioSource(
      responseDraft?.draft_digest ?? null,
      playbackUrl,
      () => playbackLifecycle.invalidate()
    );
    invalidatedDraftDigest = transition.invalidatedDraftDigest;
    sourceSelectionActive = true;
    startedDraftDigest = transition.startedDraftDigest;
    snapshot = transition.snapshot;
    playbackUrl = transition.playbackUrl;
    blockedGenerationId = null;
    error = null;
  }

  async function requestChanges(nextDraft: AudioDraft, changeRequest: string): Promise<void> {
    const accepted = await send(reprepareAudioMessage(nextDraft, changeRequest));
    invalidatedDraftDigest = draftInvalidationAfterSubmit(
      invalidatedDraftDigest,
      nextDraft.draft_digest,
      accepted
    );
  }

  async function cancel(childWorkflowId: string): Promise<void> {
    if (!snapshot || snapshot.child_workflow_id !== childWorkflowId) return;
    busy = true;
    cancellationPending = true;
    error = null;
    try {
      await runAudioCancellation(
        snapshot,
        (workflowId) => browserBridgeRuntime.cancelAudioGeneration(workflowId),
        () => audioApi.getAudioStatus(childWorkflowId),
        (next) => {
          const transition = transitionAudioGeneration(
            snapshot,
            next,
            playbackUrl,
            () => playbackLifecycle.invalidate()
          );
          snapshot = transition.snapshot;
          lastObservedGenerationId = transition.snapshot.status.generation_id;
          playbackUrl = transition.playbackUrl;
        }
      );
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      cancellationPending = false;
      busy = false;
    }
  }

  async function recover(): Promise<void> {
    if (!recoveryMessage) {
      error = "The approved package identity is unavailable for recovery.";
      return;
    }
    await send(recoveryMessage);
  }

  async function loadPlayback(): Promise<void> {
    const wav = snapshot?.receipts.find((receipt) => receipt.artifact_role === "wav");
    if (!wav) {
      error = "The completed generation has no verified WAV receipt.";
      return;
    }
    await playbackLifecycle.load(wav);
  }

  async function refreshStatus(): Promise<void> {
    const request = { sessionId, revision: statusRequestRevision };
    if (childMayExist) audioStatusStartup.expectChild();
    try {
      const next = await audioApi.getAudioStatus(chroniclerAudioChildWorkflowId(sessionId));
      if (!isCurrentAudioStatusRequest(request, { sessionId, revision: statusRequestRevision })) {
        return;
      }
      if (!acceptCancellationStatus(next, cancellationPending)) return;
      const guarded = acceptPolledAudioSnapshot(next, blockedGenerationId);
      if (!guarded.accepted) return;
      audioStatusStartup.accept(guarded.snapshot);
      blockedGenerationId = guarded.blockedGenerationId;
      const transition = transitionAudioGeneration(
        snapshot,
        guarded.snapshot,
        playbackUrl,
        () => playbackLifecycle.invalidate()
      );
      snapshot = transition.snapshot;
      lastObservedGenerationId = transition.snapshot.status.generation_id;
      playbackUrl = transition.playbackUrl;
      if (snapshot.state === "completed" && !playbackUrl) await loadPlayback();
    } catch (cause) {
      if (!isCurrentAudioStatusRequest(request, { sessionId, revision: statusRequestRevision })) {
        return;
      }
      const failure = audioStatusStartup.failure(cause, childMayExist);
      if (failure.stopPolling) statusPollingUnavailable = true;
      if (failure.error) error = failure.error;
    }
  }

  async function approveDestination(): Promise<void> {
    if (!snapshot || !destinationApproval) return;
    busy = true;
    error = null;
    try {
      const approval = await authorizeDestinationApproval(
        snapshot,
        routing(),
        browserBridgeRuntime
      );
      const next = await audioApi.approveAudioDestination(
        snapshot.child_workflow_id,
        approval
      );
      const transition = transitionAudioGeneration(
        snapshot,
        next,
        playbackUrl,
        () => playbackLifecycle.invalidate()
      );
      snapshot = transition.snapshot;
      lastObservedGenerationId = transition.snapshot.status.generation_id;
      playbackUrl = transition.playbackUrl;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    const cleanup = startAudioLiveEffects(following, childMayExist && !statusPollingUnavailable, {
      mountBridge: () => browserBridgeRuntime.mount(),
      subscribeBridge: () => browserBridgeRuntime.subscribe((status) => { bridgeStatus = status; }),
      refreshStatus,
      startPolling: (refresh) => window.setInterval(refresh, 1_000),
      stopPolling: (timer) => window.clearInterval(timer as number)
    });
    return () => {
      playbackLifecycle.invalidate();
      playbackUrl = null;
      cleanup();
    };
  });
</script>

<ChroniclerAudioWorkspace
  {discovery}
  {draft}
  snapshot={visibleSnapshot}
  {cancellation}
  {playbackUrl}
  {error}
  {busy}
  {approvalAuthority}
  {destinationAuthority}
  recoveryAvailable={recoveryMessage !== null}
  showGenerationCard={false}
  {readOnly}
  onPrepareExisting={prepareExisting}
  onPrepareTopic={prepareTopic}
  onApprove={approve}
  onRequestChanges={requestChanges}
  onCancel={cancel}
  onRecover={recover}
  onRetryPlayback={loadPlayback}
  {destinationApproval}
  onApproveDestination={approveDestination}
  onChangeSource={selectDifferentSource}
/>
