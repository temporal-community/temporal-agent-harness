import { BrowserBridgeController } from "./controller";
import type { ChroniclerAudioArtifactReceipt } from "./api";
import type { AudioGenerationControlResult } from "./leaderRuntime";
import type { BridgeStatus } from "./types";
import type {
  ChroniclerTranscriptDiscovery,
  ChroniclerTranscriptSource
} from "./source";

export interface BrowserBridgeControllerHandle {
  readonly status: BridgeStatus;
  initialize(signal?: AbortSignal): Promise<void>;
  chooseDirectory(): Promise<void>;
  reconnect(): Promise<void>;
  cancelAudioGeneration(childWorkflowId: string): Promise<AudioGenerationControlResult>;
  discoverChroniclerTranscriptSources(): Promise<ChroniclerTranscriptDiscovery>;
  readChroniclerTranscriptSource(sessionId: string): Promise<ChroniclerTranscriptSource>;
  isChroniclerTranscriptSourceCurrent(source: ChroniclerTranscriptSource): Promise<boolean>;
  verifyAudioPlayback(receipt: ChroniclerAudioArtifactReceipt): Promise<Blob>;
  verifyAudioArtifact(receipt: ChroniclerAudioArtifactReceipt): Promise<void>;
  preflightAudioDestinations(
    relativePaths: readonly string[],
    folderBindingId: string
  ): Promise<void>;
  stop(): void;
}

type ControllerFactory = (
  onChange: (status: BridgeStatus) => void
) => BrowserBridgeControllerHandle;

export function audioCancellationAvailability(status: BridgeStatus): {
  enabled: boolean;
  detail: string;
} {
  if (status.phase === "connected") {
    return {
      enabled: true,
      detail: "This active tab can cancel audio generation."
    };
  }
  if (status.phase === "standby") {
    return {
      enabled: false,
      detail: "Switch to the active browser bridge tab to cancel audio generation."
    };
  }
  return {
    enabled: false,
    detail: "Connect the browser bridge before canceling audio generation."
  };
}

export class SharedBrowserBridgeRuntime {
  readonly controller: BrowserBridgeControllerHandle;
  #mounts = 0;
  #generation = 0;
  #initialization: AbortController | null = null;
  #status: BridgeStatus;
  #listeners = new Set<(status: BridgeStatus) => void>();

  constructor(
    factory: ControllerFactory = (onChange) => new BrowserBridgeController(onChange)
  ) {
    this.controller = factory((status) => this.#publish(status));
    this.#status = this.controller.status;
  }

  get status(): BridgeStatus {
    return this.#status;
  }

  subscribe(listener: (status: BridgeStatus) => void): () => void {
    this.#listeners.add(listener);
    listener(this.#status);
    return () => this.#listeners.delete(listener);
  }

  cancelAudioGeneration(childWorkflowId: string): Promise<AudioGenerationControlResult> {
    return this.controller.cancelAudioGeneration(childWorkflowId);
  }

  discoverChroniclerTranscriptSources(): Promise<ChroniclerTranscriptDiscovery> {
    return this.controller.discoverChroniclerTranscriptSources();
  }

  readChroniclerTranscriptSource(sessionId: string): Promise<ChroniclerTranscriptSource> {
    return this.controller.readChroniclerTranscriptSource(sessionId);
  }

  isChroniclerTranscriptSourceCurrent(source: ChroniclerTranscriptSource): Promise<boolean> {
    return this.controller.isChroniclerTranscriptSourceCurrent(source);
  }

  verifyAudioPlayback(receipt: ChroniclerAudioArtifactReceipt): Promise<Blob> {
    return this.controller.verifyAudioPlayback(receipt);
  }

  verifyAudioArtifact(receipt: ChroniclerAudioArtifactReceipt): Promise<void> {
    return this.controller.verifyAudioArtifact(receipt);
  }

  preflightAudioDestinations(
    relativePaths: readonly string[],
    folderBindingId: string
  ): Promise<void> {
    return this.controller.preflightAudioDestinations(relativePaths, folderBindingId);
  }

  mount(): () => void {
    this.#mounts += 1;
    if (this.#mounts === 1) {
      const generation = ++this.#generation;
      const initialization = new AbortController();
      this.#initialization = initialization;
      void this.controller.initialize(initialization.signal)
        .catch((error: unknown) => {
          if (
            generation !== this.#generation
            || initialization.signal.aborted
            || this.#mounts === 0
          ) return;
          this.#publish({
            ...this.#status,
            phase: "error",
            detail: error instanceof Error ? error.message : String(error)
          });
        })
        .finally(() => {
          if (this.#initialization === initialization) this.#initialization = null;
        });
    }
    let mounted = true;
    return () => {
      if (!mounted) return;
      mounted = false;
      this.#mounts -= 1;
      if (this.#mounts === 0) {
        this.#generation += 1;
        this.#initialization?.abort();
        this.#initialization = null;
        this.controller.stop();
      }
    };
  }

  #publish(status: BridgeStatus): void {
    this.#status = status;
    for (const listener of this.#listeners) listener(status);
  }
}

export const browserBridgeRuntime = new SharedBrowserBridgeRuntime();
