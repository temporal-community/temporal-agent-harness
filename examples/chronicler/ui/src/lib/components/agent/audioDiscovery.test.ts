import { describe, expect, it, vi } from "vitest";
import type { BridgeStatus } from "$lib/bridge/types";
import { AudioSourceDiscoveryLifecycle } from "./audioDiscovery";

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

describe("audio source discovery lifecycle", () => {
  it("discovers sources when a delayed bridge connection becomes active", async () => {
    const ready = { status: "ready" as const, sessions: [] };
    const discover = vi.fn(async () => ready);
    const changes = vi.fn();
    const lifecycle = new AudioSourceDiscoveryLifecycle(discover, changes, vi.fn());

    await lifecycle.update(status("disconnected", null));
    await lifecycle.update(status("connected", "binding-1"));

    expect(discover).toHaveBeenCalledTimes(1);
    expect(changes).toHaveBeenLastCalledWith(ready);
  });

  it("clears stale discovery immediately when the active folder binding changes", async () => {
    let finishRebind!: (value: { status: "ready"; sessions: [] }) => void;
    const first = { status: "ready" as const, sessions: [] };
    const discover = vi.fn()
      .mockResolvedValueOnce(first)
      .mockImplementationOnce(() => new Promise((resolve) => { finishRebind = resolve; }));
    const changes = vi.fn();
    const lifecycle = new AudioSourceDiscoveryLifecycle(discover, changes, vi.fn());
    await lifecycle.update(status("connected", "binding-1"));

    const rebinding = lifecycle.update(status("connected", "binding-2"));

    expect(changes).toHaveBeenLastCalledWith(null);
    finishRebind({ status: "ready", sessions: [] });
    await rebinding;
  });

  it("retries discovery after a same-binding failure on the next connected status", async () => {
    const ready = { status: "ready" as const, sessions: [] };
    const discover = vi.fn()
      .mockRejectedValueOnce(new Error("temporary read failure"))
      .mockResolvedValueOnce(ready);
    const changes = vi.fn();
    const errors = vi.fn();
    const lifecycle = new AudioSourceDiscoveryLifecycle(discover, changes, errors);

    await lifecycle.update(status("connected", "binding-1"));
    await lifecycle.update(status("connected", "binding-1"));

    expect(discover).toHaveBeenCalledTimes(2);
    expect(errors).toHaveBeenCalledWith(expect.objectContaining({
      message: "temporary read failure"
    }));
    expect(changes).toHaveBeenLastCalledWith(ready);
  });

  it("ignores a stale discovery rejection after the active binding changes", async () => {
    let rejectOld!: (error: Error) => void;
    const ready = { status: "ready" as const, sessions: [] };
    const discover = vi.fn()
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }))
      .mockResolvedValueOnce(ready);
    const changes = vi.fn();
    const errors = vi.fn();
    const lifecycle = new AudioSourceDiscoveryLifecycle(discover, changes, errors);

    const oldDiscovery = lifecycle.update(status("connected", "binding-old"));
    await lifecycle.update(status("connected", "binding-new"));
    rejectOld(new Error("stale binding failure"));
    await oldDiscovery;

    expect(changes).toHaveBeenLastCalledWith(ready);
    expect(errors).not.toHaveBeenCalled();
  });
});
