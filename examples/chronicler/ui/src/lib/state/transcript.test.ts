import { describe, expect, it } from "vitest";
import { buildTranscript } from "$lib/state/transcript";
import { chroniclerPresentationAdapter } from "$chronicler/state/chroniclerPresentation";

describe("structured agent replies", () => {
  it("replaces streamed audio draft JSON with its review confirmation", () => {
    const items = buildTranscript([
      {
        event: "reply_delta",
        data: {
          type: "reply_delta",
          turn_number: 1,
          turn_id: "turn-1",
          timestamp: 1,
          text: JSON.stringify({
            source_content: "Hidden transcript content",
            recap_script: "A hidden spoken recap"
          })
        }
      },
      {
        event: "reply",
        data: {
          type: "reply",
          turn_number: 1,
          turn_id: "turn-1",
          timestamp: 2,
          output: {
            draft: {
              draft_id: "draft-1",
              source_content: "Hidden transcript content",
              recap_script: "A hidden spoken recap"
            }
          }
        }
      }
    ] as never, chroniclerPresentationAdapter);

    const item = items[0];
    expect(item).toMatchObject({
      kind: "agent",
      text: "Audio review package prepared.",
      output: { draft: { draft_id: "draft-1" } }
    });
    expect(item?.kind === "agent" && item.text).not.toContain("JSON");
    expect(item?.kind === "agent" && item.text).not.toContain("source_content");
    expect(item?.kind === "agent" && item.text).not.toContain("recap_script");
  });

  it("retains a typed audio draft reply for the specialized Chronicler surface", () => {
    const items = buildTranscript([{
      event: "reply",
      data: {
        type: "reply",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        output: {
          draft: {
            draft_id: "draft-1",
            draft_digest: "digest-1",
            source_kind: "existing"
          }
        }
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "agent",
      output: { draft: { draft_id: "draft-1" } }
    }]);
  });
});

describe("structured audio user messages", () => {
  it("renders a synthetic audio request as a concise topic prompt", () => {
    const items = buildTranscript([{
      event: "turn_started",
      data: {
        type: "turn_started",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        user_message: JSON.stringify({
          type: "prepare_audio",
          payload: {
            source: { source_kind: "synthetic", topic: "the crystal cavern" },
            bridge_id: "bridge-1",
            root_id: "root-1",
            folder_binding_id: "binding-1"
          }
        })
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "user",
      text: "Draft a spoken recap from topic: the crystal cavern"
    }]);
    expect(items[0]?.kind === "user" && items[0].text).not.toContain("{");
  });

  it("renders an existing transcript audio request using its source identity", () => {
    const items = buildTranscript([{
      event: "turn_started",
      data: {
        type: "turn_started",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        user_message: JSON.stringify({
          type: "prepare_audio",
          payload: {
            source: {
              source_kind: "existing",
              source_identity: "sessions/crystal-cavern.md",
              source_content: "Hidden transcript content",
              source_hash: "source-hash"
            },
            bridge_id: "bridge-1",
            root_id: "root-1",
            folder_binding_id: "binding-1"
          }
        })
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "user",
      text: "Create a spoken recap from transcript: sessions/crystal-cavern.md"
    }]);
  });

  it("renders an audio revision request as its change request", () => {
    const items = buildTranscript([{
      event: "turn_started",
      data: {
        type: "turn_started",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        user_message: JSON.stringify({
          type: "prepare_audio",
          payload: {
            change_request: "Make the opening more dramatic",
            base_draft_digest: "draft-digest",
            bridge_id: "bridge-1",
            root_id: "root-1",
            folder_binding_id: "binding-1"
          }
        })
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "user",
      text: "Revise the audio package: Make the opening more dramatic"
    }]);
  });

  it("renders an audio start request as an approval", () => {
    const items = buildTranscript([{
      event: "turn_started",
      data: {
        type: "turn_started",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        user_message: JSON.stringify({
          type: "start_audio",
          payload: {
            draft_id: "draft-1",
            draft_digest: "draft-digest",
            bridge_id: "bridge-1",
            root_id: "root-1",
            folder_binding_id: "binding-1"
          }
        })
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "user",
      text: "Approve and generate audio"
    }]);
  });

  it("renders an audio recovery request as a recovery action", () => {
    const items = buildTranscript([{
      event: "turn_started",
      data: {
        type: "turn_started",
        turn_number: 1,
        turn_id: "turn-1",
        timestamp: 1,
        user_message: JSON.stringify({
          type: "recover_audio",
          payload: {
            generation_id: "generation-1",
            content_digest: "content-digest",
            destination_digest: "destination-digest",
            bridge_id: "bridge-1",
            root_id: "root-1",
            folder_binding_id: "binding-1"
          }
        })
      }
    } as never], chroniclerPresentationAdapter);

    expect(items).toMatchObject([{
      kind: "user",
      text: "Recover audio generation"
    }]);
  });
});
