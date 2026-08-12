import type { BridgeStatus } from "$lib/bridge/types";
import type { ChroniclerTranscriptDiscovery } from "$lib/bridge/source";

export class AudioSourceDiscoveryLifecycle {
  #bindingId: string | null = null;
  #generation = 0;

  constructor(
    private readonly discover: () => Promise<ChroniclerTranscriptDiscovery>,
    private readonly onChange: (discovery: ChroniclerTranscriptDiscovery | null) => void,
    private readonly onError: (error: unknown) => void
  ) {}

  async update(status: BridgeStatus): Promise<void> {
    if (status.phase !== "connected" || !status.handleBindingId) {
      this.#generation += 1;
      this.#bindingId = null;
      this.onChange(null);
      return;
    }
    if (this.#bindingId === status.handleBindingId) return;
    this.#bindingId = status.handleBindingId;
    const generation = ++this.#generation;
    this.onChange(null);
    try {
      const discovery = await this.discover();
      if (generation === this.#generation) this.onChange(discovery);
    } catch (error) {
      if (generation === this.#generation) {
        this.#bindingId = null;
        this.onError(error);
      }
    }
  }
}
