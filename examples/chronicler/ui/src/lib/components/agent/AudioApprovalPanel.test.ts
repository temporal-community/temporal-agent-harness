import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import AudioApprovalPanel from "./AudioApprovalPanel.svelte";

describe("AudioApprovalPanel", () => {
  it("renders the complete synthetic review package before approval", () => {
    const { body } = render(AudioApprovalPanel, {
      props: {
        draft: {
          draft_id: "draft-1",
          draft_digest: "digest-1",
          source_kind: "synthetic",
          source_identity: "synthetic:draft-1",
          source_content: "# Synthetic Transcript\nThe party crossed the black bridge.",
          source_hash: "source-hash",
          recap_script: "Beyond the black bridge, the party found the bell.",
          voice: "Charon",
          wav_path: "audio/draft-1.wav",
          synthetic_markdown_path: "audio/draft-1.md",
          bridge_id: "bridge-1",
          root_id: "root-1",
          folder_binding_id: "binding-1"
        }
      }
    });

    expect(body).toContain("Review audio package");
    expect(body).toContain("# Synthetic Transcript");
    expect(body).toContain("Beyond the black bridge");
    expect(body).toContain("Charon");
    expect(body).toContain("audio/draft-1.wav");
    expect(body).toContain("audio/draft-1.md");
  });

  it("offers one explicit approval action without starting during render", () => {
    const onApprove = vi.fn();
    const { body } = render(AudioApprovalPanel, {
      props: {
        draft: {
          draft_id: "draft-1",
          draft_digest: "digest-1",
          source_kind: "existing",
          source_identity: "session-1",
          source_content: "The party crossed the bridge.",
          source_hash: "source-hash",
          recap_script: "The party crossed into danger.",
          voice: "Charon",
          wav_path: "audio/draft-1.wav",
          synthetic_markdown_path: null,
          bridge_id: "bridge-1",
          root_id: "root-1",
          folder_binding_id: "binding-1"
        },
        onApprove
      }
    });

    expect(body).toContain("Approve and generate");
    expect(body).not.toContain("Approve and remember");
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("collects a contextual change request against the reviewed draft", () => {
    const { body } = render(AudioApprovalPanel, {
      props: {
        draft: {
          draft_id: "draft-1",
          draft_digest: "digest-1",
          source_kind: "existing",
          source_identity: "session-1",
          source_content: "Transcript",
          source_hash: "source-hash",
          recap_script: "Recap",
          voice: "Charon",
          wav_path: "audio/draft-1.wav",
          synthetic_markdown_path: null,
          bridge_id: "bridge-1",
          root_id: "root-1",
          folder_binding_id: "binding-1"
        },
        onRequestChanges: () => undefined
      }
    });

    expect(body).toContain("What should change?");
    expect(body).toContain("Reprepare review");
  });
});
