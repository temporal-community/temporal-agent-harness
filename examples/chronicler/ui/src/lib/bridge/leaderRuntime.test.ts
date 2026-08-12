import { describe, expect, it, vi } from "vitest";
import { AudioLeaderRuntime } from "./leaderRuntime";

describe("audio cancellation in the bridge leader", () => {
  it("processes a queued cancellation without releasing and reacquiring the leader lock", async () => {
    const events: string[] = [];
    const abort = new AbortController();
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => {
        events.push("lock-enter");
        const result = await callback({});
        events.push("lock-exit");
        return result;
      })
    };
    const runtime = new AudioLeaderRuntime(
      locks,
      {
        claimNext: vi.fn(async () => { abort.abort(); }),
        drainOutbox: vi.fn(async () => { events.push("drain"); }),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => { events.push("cancel"); }),
        getAudioGenerationStatus: vi.fn(async (childWorkflowId) => {
          events.push("status");
          return { childWorkflowId, state: "canceled" as const };
        })
      }
    );

    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    await runtime.runLeadershipAttempt(abort.signal);

    await expect(cancellation).resolves.toEqual({
      childWorkflowId: "chronicler-audio--chat-1",
      state: "canceled"
    });
    expect(locks.request).toHaveBeenCalledTimes(1);
    expect(events).toEqual([
      "lock-enter",
      "drain",
      "cancel",
      "status",
      "lock-exit"
    ]);
  });

  it("does not claim another operation after cancellation is queued", async () => {
    const abort = new AbortController();
    let settleClaim!: () => void;
    const events: string[] = [];
    const claimNext = vi.fn(() => {
      events.push(`claim-${claimNext.mock.calls.length}`);
      if (claimNext.mock.calls.length > 1) {
        abort.abort();
        return Promise.resolve();
      }
      return new Promise<void>((resolve) => {
        settleClaim = resolve;
      });
    });
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => callback({}))
    };
    const runtime = new AudioLeaderRuntime(
      locks,
      {
        claimNext,
        drainOutbox: vi.fn(async () => undefined),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus: vi.fn(async (childWorkflowId) => {
          events.push("terminal-status");
          return { childWorkflowId, state: "canceled" as const };
        })
      }
    );

    const leadership = runtime.runLeadershipAttempt(abort.signal);
    await Promise.resolve();
    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    settleClaim();

    await cancellation;
    await leadership;
    expect(events).toEqual(["claim-1", "terminal-status", "claim-2"]);
  });

  it("returns completed when completion wins the cancellation race", async () => {
    const abort = new AbortController();
    const runtime = new AudioLeaderRuntime(
      {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      },
      {
        claimNext: vi.fn(async () => { abort.abort(); }),
        drainOutbox: vi.fn(async () => undefined),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus: vi.fn(async (childWorkflowId) => {
          return { childWorkflowId, state: "completed" as const };
        })
      }
    );

    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    await runtime.runLeadershipAttempt(abort.signal);

    await expect(cancellation).resolves.toEqual({
      childWorkflowId: "chronicler-audio--chat-1",
      state: "completed"
    });
  });

  it("keeps polling inside the held lock when cancellation status is still running", async () => {
    const abort = new AbortController();
    const wait = vi.fn(async () => undefined);
    const statuses = ["running", "canceled"] as const;
    const getAudioGenerationStatus = vi.fn(async (childWorkflowId: string) => {
      const state = statuses[getAudioGenerationStatus.mock.calls.length - 1];
      return { childWorkflowId, state };
    });
    const runtime = new AudioLeaderRuntime(
      {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      },
      {
        claimNext: vi.fn(async () => { abort.abort(); }),
        drainOutbox: vi.fn(async () => undefined),
        wait
      },
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus
      }
    );

    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    await runtime.runLeadershipAttempt(abort.signal);

    await expect(cancellation).resolves.toMatchObject({ state: "canceled" });
    expect(getAudioGenerationStatus).toHaveBeenCalledTimes(2);
    expect(wait).toHaveBeenCalledTimes(1);
  });

  it("returns completed when running transitions to completion during cancellation", async () => {
    const abort = new AbortController();
    const statuses = ["running", "completed"] as const;
    const getAudioGenerationStatus = vi.fn(async (childWorkflowId: string) => {
      const state = statuses[getAudioGenerationStatus.mock.calls.length - 1];
      return { childWorkflowId, state };
    });
    const runtime = new AudioLeaderRuntime(
      {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      },
      {
        claimNext: vi.fn(async () => { abort.abort(); }),
        drainOutbox: vi.fn(async () => undefined),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus
      }
    );

    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    await runtime.runLeadershipAttempt(abort.signal);

    await expect(cancellation).resolves.toMatchObject({ state: "completed" });
    expect(getAudioGenerationStatus).toHaveBeenCalledTimes(2);
  });

  it("resumes polling after authoritative cancellation settles", async () => {
    const events: string[] = [];
    const abort = new AbortController();
    const runtime = new AudioLeaderRuntime(
      {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      },
      {
        claimNext: vi.fn(async () => {
          events.push("claim");
          abort.abort();
        }),
        drainOutbox: vi.fn(async () => { events.push("drain"); }),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => { events.push("cancel"); }),
        getAudioGenerationStatus: vi.fn(async (childWorkflowId) => {
          events.push("status");
          return { childWorkflowId, state: "canceled" as const };
        })
      }
    );

    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    await runtime.runLeadershipAttempt(abort.signal);
    await cancellation;

    expect(events).toEqual(["drain", "cancel", "status", "claim"]);
  });

  it("rejects and clears a queued cancellation when stopped during an unresolved claim", async () => {
    const abort = new AbortController();
    let settleClaim!: () => void;
    const runtime = new AudioLeaderRuntime(
      {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      },
      {
        claimNext: vi.fn(() => new Promise<void>((resolve) => {
          settleClaim = resolve;
        })),
        drainOutbox: vi.fn(async () => undefined),
        wait: vi.fn(async () => undefined)
      },
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus: vi.fn()
      }
    );

    const leadership = runtime.runLeadershipAttempt(abort.signal);
    await Promise.resolve();
    const cancellation = runtime.cancelAudioGeneration("chronicler-audio--chat-1");
    const cancellationRejection = cancellation.catch((error) => error);
    abort.abort();
    await Promise.resolve();
    await Promise.resolve();

    expect(runtime.cancellationPending).toBe(false);
    await expect(cancellationRejection).resolves.toMatchObject({ name: "AbortError" });
    let leadershipSettled = false;
    void leadership.then(() => { leadershipSettled = true; });
    await Promise.resolve();
    expect(leadershipSettled).toBe(false);

    settleClaim();
    await expect(leadership).resolves.toBe(true);
  });
});
