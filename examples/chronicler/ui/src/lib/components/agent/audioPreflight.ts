import type { BridgeStatus } from "$lib/bridge/types";
import type { ChroniclerAudioSnapshot } from "$lib/bridge/api";
import type { AudioRouting } from "./audioUi";

export interface AudioPreflightTarget {
  paths: readonly string[];
  folderBindingId: string;
}

export interface AudioPreflightState {
  ready: boolean;
  detail: string;
}

export class AudioApprovalPreflightLifecycle {
  #generation = 0;

  constructor(
    private readonly preflight: (
      paths: readonly string[],
      folderBindingId: string
    ) => Promise<void>,
    private readonly onChange: (state: AudioPreflightState) => void
  ) {}

  async update(status: BridgeStatus, target: AudioPreflightTarget): Promise<void> {
    const generation = ++this.#generation;
    if (status.phase !== "connected") {
      this.onChange({
        ready: false,
        detail: "Connect the active browser bridge to approve audio."
      });
      return;
    }
    if (status.handleBindingId !== target.folderBindingId) {
      this.onChange({
        ready: false,
        detail: "The reviewed package does not match the active folder binding."
      });
      return;
    }
    this.onChange({ ready: false, detail: "Checking exact audio destinations…" });
    try {
      await this.preflight(target.paths, target.folderBindingId);
      if (generation === this.#generation) {
        this.onChange({ ready: true, detail: "Exact audio destinations are available." });
      }
    } catch (error) {
      if (generation === this.#generation) {
        this.onChange({
          ready: false,
          detail: error instanceof Error ? error.message : String(error)
        });
      }
    }
  }
}

export class AudioDestinationApprovalLifecycle {
  #generation = 0;

  constructor(
    private readonly authorize: (
      snapshot: ChroniclerAudioSnapshot,
      routing: AudioRouting
    ) => Promise<unknown>,
    private readonly onChange: (state: AudioPreflightState) => void
  ) {}

  invalidate(): void {
    this.#generation += 1;
  }

  async update(
    status: BridgeStatus,
    snapshot: ChroniclerAudioSnapshot,
    routing: AudioRouting
  ): Promise<void> {
    const generation = ++this.#generation;
    if (status.phase !== "connected") {
      this.onChange({
        ready: false,
        detail: "Connect the active browser bridge to approve destinations."
      });
      return;
    }
    if (status.handleBindingId !== routing.folder_binding_id) {
      this.onChange({
        ready: false,
        detail: "The reviewed package does not match the active folder binding."
      });
      return;
    }
    this.onChange({ ready: false, detail: "Validating changed destinations and unchanged artifacts…" });
    try {
      await this.authorize(snapshot, routing);
      if (generation === this.#generation) {
        this.onChange({
          ready: true,
          detail: "Exact changed destinations and unchanged artifacts are valid."
        });
      }
    } catch (error) {
      if (generation === this.#generation) {
        this.onChange({
          ready: false,
          detail: error instanceof Error ? error.message : String(error)
        });
      }
    }
  }
}
