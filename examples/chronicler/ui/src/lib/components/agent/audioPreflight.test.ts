import { describe, expect, it, vi } from "vitest";
import type { BridgeStatus } from "$lib/bridge/types";
import {
  AudioApprovalPreflightLifecycle,
  AudioDestinationApprovalLifecycle
} from "./audioPreflight";

function status(phase: BridgeStatus["phase"], binding: string | null): BridgeStatus {
  return {
    phase,
    directoryName: binding ? "campaign" : null,
    detail: "",
    completedCount: 0,
    pendingCount: 0,
    unsupportedOperations: [],
    lastConnectedAt: null,
    bridgeId: "browser-local",
    rootId: "campaign-root",
    handleBindingId: binding,
    canRebind: false
  };
}

describe("audio approval preflight lifecycle", () => {
  it("keeps approval disabled while the bridge is disconnected", async () => {
    const preflight = vi.fn(async () => undefined);
    const changes = vi.fn();
    const lifecycle = new AudioApprovalPreflightLifecycle(preflight, changes);

    await lifecycle.update(status("disconnected", null), {
      paths: ["audio/recap.wav"],
      folderBindingId: "binding-1"
    });

    expect(preflight).not.toHaveBeenCalled();
    expect(changes).toHaveBeenLastCalledWith({
      ready: false,
      detail: "Connect the active browser bridge to approve audio."
    });
  });

  it("keeps approval disabled when the reviewed package belongs to another binding", async () => {
    const preflight = vi.fn(async () => undefined);
    const changes = vi.fn();
    const lifecycle = new AudioApprovalPreflightLifecycle(preflight, changes);

    await lifecycle.update(status("connected", "binding-active"), {
      paths: ["audio/recap.wav"],
      folderBindingId: "binding-reviewed"
    });

    expect(preflight).not.toHaveBeenCalled();
    expect(changes).toHaveBeenLastCalledWith({
      ready: false,
      detail: "The reviewed package does not match the active folder binding."
    });
  });

  it("keeps approval disabled when proactive exact-path preflight finds a collision", async () => {
    const preflight = vi.fn(async () => {
      throw new Error("Audio destination already exists: audio/recap.wav");
    });
    const changes = vi.fn();
    const lifecycle = new AudioApprovalPreflightLifecycle(preflight, changes);

    await lifecycle.update(status("connected", "binding-1"), {
      paths: ["audio/recap.wav"],
      folderBindingId: "binding-1"
    });

    expect(preflight).toHaveBeenCalledWith(["audio/recap.wav"], "binding-1");
    expect(changes).toHaveBeenLastCalledWith({
      ready: false,
      detail: "Audio destination already exists: audio/recap.wav"
    });
  });
});

describe("destination approval lifecycle", () => {
  it("enables a running Markdown-only revision after validating its unchanged WAV", async () => {
    const authorize = vi.fn(async () => undefined);
    const changes = vi.fn();
    const lifecycle = new AudioDestinationApprovalLifecycle(authorize, changes);
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent-1",
      state: "running",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent-1",
        phase: "destination_approval_needed",
        detail: ""
      },
      result: null,
      approved_package: {
        package_revision: 1,
        generation_id: "generation-1",
        wav_path: "audio/recap.wav",
        synthetic_markdown_path: "audio/recap.md",
        folder_binding_id: "binding-1"
      },
      receipts: [{
        generation_id: "generation-1",
        artifact_role: "wav",
        relative_path: "audio/recap.wav",
        package_revision: 1,
        folder_binding_id: "binding-1"
      }],
      pending_destination_revision: {
        generation_id: "generation-1",
        destination_revision: 2,
        wav_path: "audio/recap.wav",
        synthetic_markdown_path: "audio/recap-r2.md"
      }
    } as never;
    const routing = {
      bridge_id: "bridge-1",
      root_id: "root-1",
      folder_binding_id: "binding-1"
    };

    await lifecycle.update(status("connected", "binding-1"), snapshot, routing);

    expect(authorize).toHaveBeenCalledWith(snapshot, routing);
    expect(changes).toHaveBeenLastCalledWith({
      ready: true,
      detail: "Exact changed destinations and unchanged artifacts are valid."
    });
  });
});
