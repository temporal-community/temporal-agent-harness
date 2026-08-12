export interface AudioGenerationControlResult {
  childWorkflowId: string;
  state: "completed" | "canceled" | "failed" | "running";
  [key: string]: unknown;
}

export interface AudioGenerationControlTransport {
  cancelAudioGeneration(childWorkflowId: string, signal?: AbortSignal): Promise<void>;
  getAudioGenerationStatus(
    childWorkflowId: string,
    signal?: AbortSignal
  ): Promise<AudioGenerationControlResult>;
}

export interface AudioLeaderHooks {
  onActive?(): void;
  claimNext(signal?: AbortSignal): Promise<void>;
  drainOutbox(signal?: AbortSignal): Promise<void>;
  wait(signal: AbortSignal): Promise<void>;
}

export interface LockProvider {
  request(
    name: string,
    options: { ifAvailable: true },
    callback: (lock: object | null) => Promise<boolean>
  ): Promise<boolean>;
}

interface CancellationCommand {
  childWorkflowId: string;
  resolve(result: AudioGenerationControlResult): void;
  reject(error: unknown): void;
}

function isTerminalAudioState(
  result: AudioGenerationControlResult
): boolean {
  return result.state === "completed"
    || result.state === "canceled"
    || result.state === "failed";
}

export class AudioLeaderRuntime {
  #command: CancellationCommand | null = null;

  constructor(
    private readonly locks: LockProvider,
    private readonly hooks: AudioLeaderHooks,
    private readonly transport: AudioGenerationControlTransport,
    private readonly lockName = "temporal-agent-harness.browser-bridge.executor.v1"
  ) {}

  get cancellationPending(): boolean {
    return this.#command !== null;
  }

  cancelAudioGeneration(childWorkflowId: string): Promise<AudioGenerationControlResult> {
    if (this.#command) {
      return Promise.reject(new Error("An audio cancellation command is already pending."));
    }
    return new Promise((resolve, reject) => {
      this.#command = { childWorkflowId, resolve, reject };
    });
  }

  async runLeadershipAttempt(signal: AbortSignal): Promise<boolean> {
    let rejectAbort!: (error: DOMException) => void;
    let lockCallbackStarted = false;
    const aborted = new Promise<never>((_resolve, reject) => {
      rejectAbort = reject;
    });
    const onAbort = () => {
      const error = new DOMException("Browser bridge leadership stopped.", "AbortError");
      this.#rejectPending(error);
      if (!lockCallbackStarted) rejectAbort(error);
    };
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
    try {
      return await Promise.race([
        this.locks.request(this.lockName, { ifAvailable: true }, async (lock) => {
          lockCallbackStarted = true;
          if (!lock || signal.aborted) return false;
          this.hooks.onActive?.();
          while (!signal.aborted) {
            if (this.#command) {
              await this.#processCancellation(signal);
              continue;
            }
            await this.hooks.claimNext(signal);
            if (signal.aborted) break;
            if (this.#command) continue;
            await this.hooks.wait(signal);
          }
          return true;
        }),
        aborted
      ]);
    } catch (error) {
      if (signal.aborted && error instanceof DOMException && error.name === "AbortError") {
        return true;
      }
      throw error;
    } finally {
      signal.removeEventListener("abort", onAbort);
    }
  }

  #rejectPending(error: unknown): void {
    const command = this.#command;
    if (!command) return;
    this.#command = null;
    command.reject(error);
  }

  async #processCancellation(signal: AbortSignal): Promise<void> {
    const command = this.#command;
    if (!command) return;
    try {
      await this.hooks.drainOutbox(signal);
      await this.transport.cancelAudioGeneration(command.childWorkflowId, signal);
      while (true) {
        const result = await this.transport.getAudioGenerationStatus(
          command.childWorkflowId,
          signal
        );
        if (isTerminalAudioState(result)) {
          command.resolve(result);
          break;
        }
        await this.hooks.wait(signal);
      }
    } catch (error) {
      command.reject(error);
    } finally {
      if (this.#command === command) this.#command = null;
    }
  }
}
