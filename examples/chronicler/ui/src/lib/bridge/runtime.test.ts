import { describe, expect, it, vi } from "vitest";
import {
  audioCancellationAvailability,
  SharedBrowserBridgeRuntime
} from "./runtime";
import type { BridgeStatus } from "./types";

const disconnectedStatus: BridgeStatus = {
  phase: "disconnected",
  directoryName: null,
  detail: "Choose a campaign directory to connect this browser.",
  completedCount: 0,
  pendingCount: 0,
  unsupportedOperations: [],
  lastConnectedAt: null,
  bridgeId: "browser-local",
  rootId: "campaign-root",
  handleBindingId: null,
  canRebind: true
};

describe("shared browser bridge runtime", () => {
  it("shares one controller lifecycle across multiple UI consumers", async () => {
    const controller = {
      status: disconnectedStatus,
      initialize: vi.fn(async () => undefined),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(async (childWorkflowId: string) => ({
        childWorkflowId,
        state: "canceled" as const
      })),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const factory = vi.fn(() => controller);
    const runtime = new SharedBrowserBridgeRuntime(factory);

    const releaseControl = runtime.mount();
    const releasePicker = runtime.mount();
    await Promise.resolve();

    expect(factory).toHaveBeenCalledTimes(1);
    expect(runtime.controller).toBe(controller);
    expect(controller.initialize).toHaveBeenCalledTimes(1);
    releaseControl();
    expect(controller.stop).not.toHaveBeenCalled();
    releasePicker();
    expect(controller.stop).toHaveBeenCalledTimes(1);
  });

  it("queues cancellation through the shared runtime owner", async () => {
    const controller = {
      status: { ...disconnectedStatus, phase: "connected" as const },
      initialize: vi.fn(async () => undefined),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(async (childWorkflowId: string) => ({
        childWorkflowId,
        state: "canceled" as const
      })),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    await expect(runtime.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).resolves.toEqual({
      childWorkflowId: "chronicler-audio--chat-1",
      state: "canceled"
    });
    expect(controller.cancelAudioGeneration).toHaveBeenCalledTimes(1);
  });

  it("exposes source access without accepting a raw handle or binding", async () => {
    const source = {
      sessionId: "session-1",
      campaignId: "campaign-1",
      title: "The Black Bell",
      content: "Transcript",
      contentHash: "hash-1",
      folderBindingId: "binding-private"
    };
    const controller = {
      status: { ...disconnectedStatus, phase: "connected" as const },
      initialize: vi.fn(async () => undefined),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(),
      discoverChroniclerTranscriptSources: vi.fn(async () => ({
        status: "ready" as const,
        sessions: [{ status: "selectable" as const, source }]
      })),
      readChroniclerTranscriptSource: vi.fn(async () => source),
      isChroniclerTranscriptSourceCurrent: vi.fn(async () => false),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    await expect(runtime.readChroniclerTranscriptSource("session-1"))
      .resolves.toBe(source);
    await expect(runtime.discoverChroniclerTranscriptSources())
      .resolves.toMatchObject({ status: "ready" });
    await expect(runtime.isChroniclerTranscriptSourceCurrent(source))
      .resolves.toBe(false);

    expect(controller.readChroniclerTranscriptSource).toHaveBeenCalledWith("session-1");
    expect(controller.discoverChroniclerTranscriptSources).toHaveBeenCalledWith();
    expect(controller.isChroniclerTranscriptSourceCurrent).toHaveBeenCalledWith(source);
  });

  it("verifies playback through the shared active controller", async () => {
    const receipt = { generation_id: "generation-1", artifact_role: "wav" as const } as never;
    const blob = new Blob(["RIFF....WAVE"], { type: "audio/wav" });
    const controller = {
      status: { ...disconnectedStatus, phase: "connected" as const },
      initialize: vi.fn(async () => undefined),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(async () => blob),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    await expect(runtime.verifyAudioPlayback(receipt)).resolves.toBe(blob);
    expect(controller.verifyAudioPlayback).toHaveBeenCalledWith(receipt);
  });

  it("preflights exact audio destinations through the shared active controller", async () => {
    const controller = {
      status: { ...disconnectedStatus, phase: "connected" as const },
      initialize: vi.fn(async () => undefined),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(async () => undefined),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    await runtime.preflightAudioDestinations(
      ["audio/recap.wav", "audio/recap.md"],
      "binding-1"
    );

    expect(controller.preflightAudioDestinations).toHaveBeenCalledWith(
      ["audio/recap.wav", "audio/recap.md"],
      "binding-1"
    );
  });

  it("enables cancellation only for the active tab and directs standby users", () => {
    expect(audioCancellationAvailability({
      ...disconnectedStatus,
      phase: "connected"
    })).toEqual({
      enabled: true,
      detail: "This active tab can cancel audio generation."
    });
    expect(audioCancellationAvailability({
      ...disconnectedStatus,
      phase: "standby"
    })).toEqual({
      enabled: false,
      detail: "Switch to the active browser bridge tab to cancel audio generation."
    });
  });

  it("prevents delayed initialization from starting after the final unmount", async () => {
    let finishInitialize!: () => void;
    let pollStarts = 0;
    const controller = {
      status: disconnectedStatus,
      initialize: vi.fn(async (signal?: AbortSignal) => {
        await new Promise<void>((resolve) => { finishInitialize = resolve; });
        if (!signal?.aborted) pollStarts += 1;
      }),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    const release = runtime.mount();
    release();
    finishInitialize();
    await Promise.resolve();
    await Promise.resolve();

    expect(pollStarts).toBe(0);
    expect(controller.stop).toHaveBeenCalledTimes(1);
  });

  it("starts a fresh initialization generation after a full stop and remount", async () => {
    const initializationSignals: AbortSignal[] = [];
    const controller = {
      status: disconnectedStatus,
      initialize: vi.fn(async (signal?: AbortSignal) => {
        if (signal) initializationSignals.push(signal);
      }),
      chooseDirectory: vi.fn(async () => undefined),
      reconnect: vi.fn(async () => undefined),
      cancelAudioGeneration: vi.fn(),
      discoverChroniclerTranscriptSources: vi.fn(),
      readChroniclerTranscriptSource: vi.fn(),
      isChroniclerTranscriptSourceCurrent: vi.fn(),
      verifyAudioPlayback: vi.fn(),
      verifyAudioArtifact: vi.fn(),
      preflightAudioDestinations: vi.fn(),
      stop: vi.fn()
    };
    const runtime = new SharedBrowserBridgeRuntime(() => controller);

    const releaseFirst = runtime.mount();
    releaseFirst();
    const releaseSecond = runtime.mount();
    await Promise.resolve();

    expect(controller.initialize).toHaveBeenCalledTimes(2);
    expect(initializationSignals).toHaveLength(2);
    expect(initializationSignals[0]).not.toBe(initializationSignals[1]);
    expect(initializationSignals[0].aborted).toBe(true);
    expect(initializationSignals[1].aborted).toBe(false);
    releaseSecond();
  });
});
