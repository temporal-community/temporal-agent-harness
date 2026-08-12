import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import AudioGenerationCard from "./AudioGenerationCard.svelte";
import { audioRecoveryMessage } from "./audioUi";

describe("AudioGenerationCard", () => {
  it("renders the exact child workflow as a visibly nested phased operation", () => {
    const { body } = render(AudioGenerationCard, {
      props: {
        snapshot: {
          child_workflow_id: "chronicler-audio--agent/session-1",
          state: "running",
          status: {
            generation_id: "generation-1",
            child_workflow_id: "chronicler-audio--agent/session-1",
            phase: "saving_synthetic_transcript",
            detail: "Writing the approved synthetic transcript."
          },
          result: null,
          receipts: [],
          pending_destination_revision: null
        }
      }
    });

    expect(body).toContain("Generate audio");
    expect(body).toContain("Nested child workflow");
    expect(body).toContain("chronicler-audio--agent/session-1");
    expect(body).toContain("Generating audio");
    expect(body).toContain("Saving WAV");
    expect(body).toContain("Saving synthetic transcript");
    expect(body).toContain('aria-current="step"');
  });

  it("enables cancel only for the leader and guides standby users", () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running" as const,
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "generating_audio" as const,
        detail: ""
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    };
    const leader = render(AudioGenerationCard, {
      props: {
        snapshot,
        cancellation: { enabled: true, detail: "This active tab can cancel audio generation." },
        onCancel: () => undefined
      }
    }).body;
    const standby = render(AudioGenerationCard, {
      props: {
        snapshot,
        cancellation: {
          enabled: false,
          detail: "Switch to the active browser bridge tab to cancel audio generation."
        },
        onCancel: () => undefined
      }
    }).body;

    expect(leader).toContain("Cancel");
    expect(leader).not.toContain("disabled");
    expect(standby).toContain("Switch to the active browser bridge tab");
    expect(standby).toMatch(/<button[^>]*disabled[^>]*>/);
  });

  it("offers authoritative recovery without another package approval", () => {
    const onRecover = vi.fn();
    const status = {
      generation_id: "generation-1",
      child_workflow_id: "chronicler-audio--agent-1",
      phase: "failed" as const,
      detail: "The WAV needs recovery."
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
      state: "failed" as const,
      status,
      result: {
        generation_id: "generation-1",
        outcome: "failed" as const,
        status,
        duration_s: null,
        approved_package: null
      },
      approved_package: approvedPackage,
      receipts: [],
      pending_destination_revision: null
    };
    const { body } = render(AudioGenerationCard, {
      props: {
        snapshot,
        recoveryAvailable: true,
        onRecover
      }
    });
    const canceled = render(AudioGenerationCard, {
      props: {
        snapshot: {
          ...snapshot,
          state: "canceled",
          result: { ...snapshot.result, outcome: "canceled" }
        },
        recoveryAvailable: true,
        onRecover
      }
    }).body;
    const completed = render(AudioGenerationCard, {
      props: {
        snapshot: {
          ...snapshot,
          state: "completed",
          result: { ...snapshot.result, outcome: "completed" }
        },
        recoveryAvailable: true,
        onRecover
      }
    }).body;

    expect(body).toContain("Recover approved package");
    expect(body).not.toContain("Approve package");
    expect(audioRecoveryMessage(snapshot, [])).toEqual({
      type: "recover_audio",
      payload: {
        generation_id: "generation-1",
        content_digest: "content-1",
        destination_digest: "destination-1",
        package_digest: "package-1",
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "binding-1"
      }
    });
    expect(canceled).not.toContain("Recover approved package");
    expect(completed).not.toContain("Recover approved package");
    expect(onRecover).not.toHaveBeenCalled();
  });

  it("requests destination-only approval for a late collision", () => {
    const onApproveDestination = vi.fn();
    const { body } = render(AudioGenerationCard, {
      props: {
        snapshot: {
          child_workflow_id: "chronicler-audio--agent-1",
          state: "running",
          status: {
            generation_id: "generation-1",
            child_workflow_id: "chronicler-audio--agent-1",
            phase: "destination_approval_needed",
            detail: "The approved WAV path became occupied."
          },
          result: null,
          receipts: [],
          pending_destination_revision: null
        },
        destinationApproval: {
          generation_id: "generation-1",
          content_digest: "content-digest",
          destination_revision: 2,
          wav_path: "audio/recap-2.wav",
          synthetic_markdown_path: null,
          bridge_id: "bridge-1",
          root_id: "root-1",
          folder_binding_id: "binding-1"
        },
        onApproveDestination
      }
    });

    expect(body).toContain("Approve new destinations");
    expect(body).toContain("audio/recap-2.wav");
    expect(body).not.toContain("Exact narration");
    expect(onApproveDestination).not.toHaveBeenCalled();
  });

  it("shows the measured duration from a completed generation result", () => {
    const status = {
      generation_id: "generation-1",
      child_workflow_id: "chronicler-audio--agent-1",
      phase: "complete" as const,
      detail: ""
    };
    const { body } = render(AudioGenerationCard, {
      props: {
        snapshot: {
          child_workflow_id: "chronicler-audio--agent-1",
          state: "completed",
          status,
          result: {
            generation_id: "generation-1",
            outcome: "completed",
            status,
            duration_s: 92.4,
            approved_package: null
          },
          receipts: [],
          pending_destination_revision: null
        }
      }
    });

    expect(body).toContain("Duration");
    expect(body).toContain("1:32");
  });

  it("disables duplicate cancellation while authoritative cancellation is pending", () => {
    const { body } = render(AudioGenerationCard, {
      props: {
        snapshot: {
          child_workflow_id: "chronicler-audio--agent-1",
          state: "running",
          status: {
            generation_id: "generation-1",
            child_workflow_id: "chronicler-audio--agent-1",
            phase: "canceling",
            detail: "Waiting for authoritative cancellation…"
          },
          result: null,
          receipts: [],
          pending_destination_revision: null
        },
        cancellation: { enabled: true, detail: "This active tab can cancel." },
        onCancel: () => undefined
      }
    });

    expect(body).toContain("Canceling");
    expect(body).toMatch(/<button[^>]*disabled[^>]*>/);
  });
});
