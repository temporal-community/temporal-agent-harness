import { describe, expect, it, vi } from "vitest";
import { startAudioLiveEffects } from "./audioLive";

describe("Chronicler audio live effects", () => {
  it("mounts and subscribes without requesting status before an audio child may exist", () => {
    const dependencies = {
      mountBridge: vi.fn(() => vi.fn()),
      subscribeBridge: vi.fn(() => vi.fn()),
      refreshStatus: vi.fn(async () => undefined),
      startPolling: vi.fn(() => 1),
      stopPolling: vi.fn()
    };

    const cleanup = startAudioLiveEffects(true, false, dependencies);
    cleanup();

    expect(dependencies.mountBridge).toHaveBeenCalledOnce();
    expect(dependencies.subscribeBridge).toHaveBeenCalledOnce();
    expect(dependencies.refreshStatus).not.toHaveBeenCalled();
    expect(dependencies.startPolling).not.toHaveBeenCalled();
  });

  it("refreshes immediately and starts polling once an audio child may exist", () => {
    const dependencies = {
      mountBridge: vi.fn(() => vi.fn()),
      subscribeBridge: vi.fn(() => vi.fn()),
      refreshStatus: vi.fn(async () => undefined),
      startPolling: vi.fn(() => 1),
      stopPolling: vi.fn()
    };

    const cleanup = startAudioLiveEffects(true, true, dependencies);
    cleanup();

    expect(dependencies.mountBridge).toHaveBeenCalledOnce();
    expect(dependencies.subscribeBridge).toHaveBeenCalledOnce();
    expect(dependencies.refreshStatus).toHaveBeenCalledOnce();
    expect(dependencies.startPolling).toHaveBeenCalledOnce();
    expect(dependencies.stopPolling).toHaveBeenCalledWith(1);
  });

  it("starts no bridge, status request, discovery trigger, or poll in historical replay", () => {
    const dependencies = {
      mountBridge: vi.fn(() => vi.fn()),
      subscribeBridge: vi.fn(() => vi.fn()),
      refreshStatus: vi.fn(async () => undefined),
      startPolling: vi.fn(() => 1),
      stopPolling: vi.fn()
    };

    const cleanup = startAudioLiveEffects(false, false, dependencies);
    cleanup();

    expect(dependencies.mountBridge).not.toHaveBeenCalled();
    expect(dependencies.subscribeBridge).not.toHaveBeenCalled();
    expect(dependencies.refreshStatus).not.toHaveBeenCalled();
    expect(dependencies.startPolling).not.toHaveBeenCalled();
  });
});
