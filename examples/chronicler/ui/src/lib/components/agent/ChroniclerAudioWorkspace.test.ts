import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import ChroniclerAudioWorkspace from "./ChroniclerAudioWorkspace.svelte";

describe("ChroniclerAudioWorkspace", () => {
  it("offers another recap after a completed generation", () => {
    const { body } = render(ChroniclerAudioWorkspace, {
      props: {
        snapshot: {
          child_workflow_id: "chronicler-audio--session-1",
          state: "completed",
          status: {
            generation_id: "generation-1",
            child_workflow_id: "chronicler-audio--session-1",
            phase: "complete",
            detail: "Audio generation completed."
          },
          result: null,
          receipts: [],
          pending_destination_revision: null
        },
        onChangeSource: () => undefined
      }
    });

    expect(body).toContain("Create another recap");
    expect(body).not.toContain("Change source");
  });

  it("offers both an eligible transcript and a topic-based synthetic source", () => {
    const { body } = render(ChroniclerAudioWorkspace, {
      props: {
        discovery: {
          status: "ready",
          sessions: [{
            status: "selectable",
            source: {
              sessionId: "session-1",
              campaignId: "campaign-1",
              title: "The Black Bell",
              content: "The party crossed the bridge.",
              contentHash: "source-hash",
              folderBindingId: "binding-1"
            }
          }]
        }
      }
    });

    expect(body).toContain("Create spoken recap");
    expect(body).toContain("The Black Bell");
    expect(body).toContain("Use transcript");
    expect(body).toContain("Draft from a topic");
  });
});
