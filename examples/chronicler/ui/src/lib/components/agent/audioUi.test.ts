import { describe, expect, it, vi } from "vitest";
import {
  acceptPolledAudioSnapshot,
  acceptCancellationStatus,
  audioChildMayExist,
  generationBlockedByStart,
  authoritativeAudioRecoveryIdentity,
  runAudioCancellation,
  authorizeAudioStart,
  authorizeDestinationApproval,
  changeAudioSource,
  destinationApprovalFromSnapshot,
  prepareExistingAudioMessage,
  latestAudioDraft,
  latestAudioRecoveryIdentity,
  recoverAudioMessage,
  reprepareAudioMessage,
  prepareSyntheticAudioMessage,
  requestAudioChanges,
  selectAudioSource,
  startAudioMessage,
  transitionAudioStart,
  transitionAudioGeneration,
  transitionPreparedDraft,
  type AudioComposerState,
  type AudioDraft
} from "./audioUi";

describe("audio review state", () => {
  it("allows audio status polling only once generate_audio has begun or reached a recoverable terminal state", () => {
    const item = (status: string) => [{
      kind: "tool",
      toolName: "generate_audio",
      status
    }] as never;

    expect(audioChildMayExist([])).toBe(false);
    expect(audioChildMayExist(item("requested"))).toBe(false);
    expect(audioChildMayExist(item("awaiting"))).toBe(false);
    expect(audioChildMayExist(item("approved"))).toBe(false);
    expect(audioChildMayExist(item("denied"))).toBe(false);
    expect(audioChildMayExist(item("running"))).toBe(true);
    expect(audioChildMayExist(item("done"))).toBe(true);
    expect(audioChildMayExist(item("failed"))).toBe(true);
  });

  it("invalidates derived review and generation state when the source changes", () => {
    const current: AudioComposerState = {
      sourceKey: "existing:session-1",
      draft: { draft_id: "draft-1" } as AudioComposerState["draft"],
      snapshot: { child_workflow_id: "chronicler-audio--agent-1" } as AudioComposerState["snapshot"]
    };

    expect(selectAudioSource(current, "synthetic:The black bell")).toEqual({
      sourceKey: "synthetic:The black bell",
      draft: null,
      snapshot: null
    });
  });

  it("preserves the selected source while reopening preparation for changes", () => {
    const current: AudioComposerState = {
      sourceKey: "existing:session-1",
      draft: { draft_id: "draft-1" } as AudioComposerState["draft"],
      snapshot: null
    };

    expect(requestAudioChanges(current)).toEqual({
      sourceKey: "existing:session-1",
      draft: null,
      snapshot: null
    });
  });

  it("builds exact initial prepare envelopes for existing and topic sources", () => {
    const routing = {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    };
    expect(prepareExistingAudioMessage({
      sessionId: "session-1",
      campaignId: "campaign-1",
      title: "The Black Bell",
      content: "The party crossed the bridge.",
      contentHash: "source-hash",
      folderBindingId: "binding-1"
    }, routing)).toEqual({
      type: "prepare_audio",
      payload: {
        source: {
          source_kind: "existing",
          source_identity: "session-1",
          source_content: "The party crossed the bridge.",
          source_hash: "source-hash"
        },
        ...routing
      }
    });
    expect(prepareSyntheticAudioMessage("The black bell", routing)).toEqual({
      type: "prepare_audio",
      payload: {
        source: { source_kind: "synthetic", topic: "The black bell" },
        ...routing
      }
    });
  });

  it("builds start authority from only the exact reviewed draft destinations", () => {
    const draft = {
      draft_id: "draft-1",
      draft_digest: "draft-digest",
      wav_path: "audio/draft-1.wav",
      synthetic_markdown_path: "audio/draft-1.md",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    } as AudioComposerState["draft"];

    expect(startAudioMessage(draft!)).toEqual({
      type: "start_audio",
      payload: {
        draft_id: "draft-1",
        draft_digest: "draft-digest",
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "binding-1",
        preflighted_paths: ["audio/draft-1.wav", "audio/draft-1.md"]
      }
    });
  });

  it("reprepares contextually against the current draft without changing its source", () => {
    const draft = {
      draft_digest: "draft-digest",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    } as AudioComposerState["draft"];

    expect(reprepareAudioMessage(draft!, "Make the narration more ominous.")).toEqual({
      type: "prepare_audio",
      payload: {
        change_request: "Make the narration more ominous.",
        base_draft_digest: "draft-digest",
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "binding-1"
      }
    });
  });

  it("selects the latest complete structured audio draft reply", () => {
    const older = { draft_id: "draft-1", draft_digest: "digest-1" };
    const latest = { draft_id: "draft-2", draft_digest: "digest-2" };
    expect(latestAudioDraft([
      { kind: "agent", output: { draft: older } },
      { kind: "agent", output: { other: true } },
      { kind: "agent", output: { draft: latest } }
    ] as never)).toMatchObject(latest);
  });

  it("builds recovery authority from the unchanged approved tool package", () => {
    expect(recoverAudioMessage({
      generation_id: "generation-1",
      content_digest: "content-digest",
      destination_digest: "destination-digest",
      package_digest: "package-digest",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    })).toEqual({
      type: "recover_audio",
      payload: {
        generation_id: "generation-1",
        content_digest: "content-digest",
        destination_digest: "destination-digest",
        package_digest: "package-digest",
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "binding-1"
      }
    });
  });

  it("finds recovery authority only on the specialized generate_audio tool", () => {
    const identity = {
      generation_id: "generation-1",
      content_digest: "content-digest",
      destination_digest: "destination-digest",
      package_digest: "package-digest",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    };
    expect(latestAudioRecoveryIdentity([
      { kind: "tool", toolName: "other", input: identity },
      { kind: "tool", toolName: "generate_audio", input: identity }
    ] as never)).toEqual(identity);
  });

  it("prefers the authoritative revised package over the original recovery tool input", () => {
    const original = {
      generation_id: "generation-1",
      content_digest: "content-original",
      destination_digest: "destination-original",
      package_digest: "package-original",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    };
    const snapshot = {
      result: {
        approved_package: {
          package_revision: 2,
          generation_id: "generation-1",
          wav_path: "audio/recap-r2.wav",
          synthetic_markdown_path: "audio/recap-r2.md",
          content_digest: "content-current",
          destination_digest: "destination-current",
          package_digest: "package-current",
          bridge_id: "bridge-1",
          root_id: "root-1",
          folder_binding_id: "binding-1"
        }
      }
    } as never;

    expect(authoritativeAudioRecoveryIdentity(snapshot, [
      { kind: "tool", toolName: "generate_audio", input: original }
    ] as never)).toEqual({
      generation_id: "generation-1",
      content_digest: "content-current",
      destination_digest: "destination-current",
      package_digest: "package-current",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1",
      package_revision: 2,
      wav_path: "audio/recap-r2.wav",
      synthetic_markdown_path: "audio/recap-r2.md"
    });
  });

  it("echoes the exact pending destination revision with the active routing authority", () => {
    expect(destinationApprovalFromSnapshot({
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "destination_approval_needed",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: {
        generation_id: "generation-1",
        content_digest: "content-digest",
        destination_revision: 2,
        wav_path: "audio/recap-2.wav",
        synthetic_markdown_path: "audio/recap-2.md",
        destination_digest: "destination-digest",
        package_digest: "package-digest"
      }
    }, {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    })).toEqual({
      generation_id: "generation-1",
      content_digest: "content-digest",
      destination_revision: 2,
      wav_path: "audio/recap-2.wav",
      synthetic_markdown_path: "audio/recap-2.md",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    });
  });

  it("revalidates an existing source and preflights every exact path before start", async () => {
    const draft = {
      draft_id: "draft-1",
      draft_digest: "draft-digest",
      source_kind: "existing",
      source_identity: "session-1",
      source_content: "Transcript",
      source_hash: "source-hash",
      wav_path: "audio/recap.wav",
      synthetic_markdown_path: "audio/recap.md",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    } as AudioComposerState["draft"];
    const calls: string[] = [];
    const authority = {
      readChroniclerTranscriptSource: async () => {
        calls.push("source");
        return {
          sessionId: "session-1",
          campaignId: "campaign-1",
          title: "Session",
          content: "Transcript",
          contentHash: "source-hash",
          folderBindingId: "binding-1"
        };
      },
      preflightAudioDestinations: async (paths: readonly string[], binding: string) => {
        calls.push(`preflight:${paths.join(",")}:${binding}`);
      }
    };

    await expect(authorizeAudioStart(draft!, authority)).resolves.toEqual(
      startAudioMessage(draft!)
    );
    expect(calls).toEqual([
      "source",
      "preflight:audio/recap.wav,audio/recap.md:binding-1"
    ]);
  });

  it("preflights the exact pending revision before destination-only approval", async () => {
    const preflightAudioDestinations = vi.fn(async () => undefined);
    const snapshot: NonNullable<AudioComposerState["snapshot"]> = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "destination_approval_needed",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: {
        generation_id: "generation-1",
        content_digest: "content-digest",
        destination_revision: 2,
        wav_path: "audio/recap-2.wav",
        synthetic_markdown_path: null,
        destination_digest: null,
        package_digest: null
      }
    };
    const routing = {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    };

    await expect(authorizeDestinationApproval(snapshot, routing, {
      preflightAudioDestinations
    })).resolves.toMatchObject({ wav_path: "audio/recap-2.wav" });
    expect(preflightAudioDestinations).toHaveBeenCalledWith(
      ["audio/recap-2.wav"],
      "binding-1"
    );
  });

  it("approves a Markdown-only revision after validating its unchanged owned WAV", async () => {
    const preflightAudioDestinations = vi.fn(async () => undefined);
    const verifyAudioArtifact = vi.fn(async () => undefined);
    const wavReceipt = {
      generation_id: "generation-1",
      artifact_role: "wav" as const,
      relative_path: "audio/recap.wav",
      content_hash: "wav-hash",
      content_size: 44,
      package_revision: 1,
      operation_id: "audio-write:generation-1:r1:wav",
      folder_binding_id: "binding-1"
    };
    const approvedPackage = {
      package_revision: 1,
      generation_id: "generation-1",
      source_kind: "synthetic" as const,
      source_identity: "topic-1",
      source_content: "Transcript",
      source_hash: "source-hash",
      recap_script: "Recap",
      voice: "Charon" as const,
      wav_path: "audio/recap.wav",
      synthetic_markdown_path: "audio/recap.md",
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1",
      content_digest: "content-1",
      destination_digest: "destination-1",
      package_digest: "package-1"
    };
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running" as const,
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "destination_approval_needed" as const,
        detail: ""
      },
      result: null,
      approved_package: approvedPackage,
      receipts: [wavReceipt],
      pending_destination_revision: {
        generation_id: "generation-1",
        content_digest: "content-1",
        destination_revision: 2,
        wav_path: "audio/recap.wav",
        synthetic_markdown_path: "audio/recap-r2.md",
        destination_digest: "destination-2",
        package_digest: "package-2"
      }
    };

    await expect(authorizeDestinationApproval(snapshot, {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    }, { preflightAudioDestinations, verifyAudioArtifact })).resolves.toMatchObject({
      synthetic_markdown_path: "audio/recap-r2.md"
    });
    expect(preflightAudioDestinations).toHaveBeenCalledWith(
      ["audio/recap-r2.md"],
      "binding-1"
    );
    expect(verifyAudioArtifact).toHaveBeenCalledWith(wavReceipt);

    await expect(authorizeDestinationApproval(snapshot, {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    }, { preflightAudioDestinations, verifyAudioArtifact })).resolves.toBeTruthy();
    expect(preflightAudioDestinations).toHaveBeenCalledTimes(2);
    expect(verifyAudioArtifact).toHaveBeenCalledTimes(2);

    verifyAudioArtifact.mockRejectedValueOnce(new Error("WAV content was tampered."));
    await expect(authorizeDestinationApproval(snapshot, {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    }, { preflightAudioDestinations, verifyAudioArtifact })).rejects.toThrow("tampered");
    await expect(authorizeDestinationApproval({ ...snapshot, receipts: [] }, {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    }, { preflightAudioDestinations, verifyAudioArtifact })).rejects.toThrow(
      "no authoritative receipt"
    );
  });

  it("revokes prior playback when the fixed child reports a new running generation", () => {
    const revoke = vi.fn();
    const current = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "completed",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "complete",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as AudioComposerState["snapshot"];
    const next = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running",
      status: {
        generation_id: "generation-2",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "generating_audio",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as AudioComposerState["snapshot"];

    expect(transitionAudioGeneration(current!, next!, "blob:old", revoke)).toEqual({
      snapshot: next,
      playbackUrl: null
    });
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it("invalidates a pending playback load when the generation changes before a URL exists", () => {
    const revoke = vi.fn();
    const current = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "completed",
      status: { generation_id: "generation-1", child_workflow_id: "chronicler-audio--agent-1", phase: "complete", detail: "" },
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as NonNullable<AudioComposerState["snapshot"]>;
    const next = {
      ...current,
      state: "running",
      status: { ...current.status, generation_id: "generation-2", phase: "generating_audio" }
    } as NonNullable<AudioComposerState["snapshot"]>;

    expect(transitionAudioGeneration(current, next, null, revoke)).toEqual({
      snapshot: next,
      playbackUrl: null
    });
    expect(revoke).toHaveBeenCalledOnce();
  });

  it("reopens review and clears a terminal generation when a new draft arrives", () => {
    const revoke = vi.fn();
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "completed",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "complete",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as AudioComposerState["snapshot"];

    expect(transitionPreparedDraft(
      "draft-old",
      { draft_digest: "draft-new" } as never,
      snapshot,
      "blob:old",
      revoke
    )).toEqual({
      observedDraftDigest: "draft-new",
      snapshot: null,
      playbackUrl: null
    });
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it("hides prior terminal output after a new start is accepted", () => {
    const revoke = vi.fn();
    const draft = { draft_digest: "draft-new" } as AudioDraft;

    expect(transitionAudioStart(draft, "blob:old", revoke)).toEqual({
      startedDraftDigest: "draft-new",
      snapshot: null,
      playbackUrl: null
    });
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it("ignores the old completed snapshot while the fixed child starts a new generation", () => {
    const stale = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "completed",
      status: {
        generation_id: "generation-old",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "complete",
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as NonNullable<AudioComposerState["snapshot"]>;

    expect(acceptPolledAudioSnapshot(stale, "generation-old")).toEqual({
      accepted: false,
      blockedGenerationId: "generation-old"
    });
  });

  it("retains the prior generation guard after a new draft clears the terminal snapshot", () => {
    expect(generationBlockedByStart(null, "generation-old")).toBe("generation-old");
  });

  it("keeps canceling visible until the authoritative terminal response arrives", async () => {
    let finishCancellation!: () => void;
    const cancel = vi.fn(() => new Promise<void>((resolve) => { finishCancellation = resolve; }));
    const status = {
      generation_id: "generation-1",
      child_workflow_id: "chronicler-audio--agent-1",
      phase: "generating_audio" as const,
      detail: ""
    };
    const running = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running",
      status,
      result: null,
      receipts: [],
      pending_destination_revision: null
    } as NonNullable<AudioComposerState["snapshot"]>;
    const terminal = {
      ...running,
      state: "canceled" as const,
      status: { ...status, phase: "canceled" as const },
      result: null
    };
    const changes = vi.fn();
    const request = runAudioCancellation(
      running,
      cancel,
      async () => terminal,
      changes
    );

    expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({
      status: expect.objectContaining({ phase: "canceling" })
    }));
    finishCancellation();
    await request;
    expect(changes).toHaveBeenLastCalledWith(terminal);
  });

  it("ignores non-canceling poll snapshots while cancellation awaits terminal authority", () => {
    const polled = {
      state: "running",
      status: { phase: "generating_audio" }
    } as NonNullable<AudioComposerState["snapshot"]>;

    expect(acceptCancellationStatus(polled, true)).toBe(false);
  });

  it("clears review, generation, and playback when the user changes source", () => {
    const revoke = vi.fn();

    expect(changeAudioSource("draft-1", "blob:old", revoke)).toEqual({
      invalidatedDraftDigest: "draft-1",
      startedDraftDigest: null,
      snapshot: null,
      playbackUrl: null
    });
    expect(revoke).toHaveBeenCalledTimes(1);
  });
});
