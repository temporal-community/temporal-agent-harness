import { describe, expect, it } from "vitest";
import { chroniclerPresentationAdapter } from "./chroniclerPresentation";

describe("agent message presentation", () => {
  it("uses the same human-readable audio preview before and after serialization", () => {
    const message = {
      type: "prepare_audio",
      payload: {
        source: { source_kind: "synthetic", topic: "the crystal cavern" },
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "binding-1"
      }
    } as const;

    expect(chroniclerPresentationAdapter.messageText(message)).toBe(
      "Draft a spoken recap from topic: the crystal cavern"
    );
    expect(chroniclerPresentationAdapter.messageText(JSON.stringify(message))).toBe(
      "Draft a spoken recap from topic: the crystal cavern"
    );
  });
});
