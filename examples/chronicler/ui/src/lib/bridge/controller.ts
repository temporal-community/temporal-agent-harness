import {
  type ChroniclerAudioArtifactReceipt,
  HttpBrowserBridgeApi,
  HttpChroniclerAudioApi,
  WorkflowDiscovery,
  WorkflowNotFoundError
} from "./api";
import { drainStoredOutcomes, fulfillOperations, reconcileSettledOutcomes } from "./engine";
import { IndexedDbBridgeRepository } from "./persistence";
import { isFileSystemPermissionError } from "./executor";
import { verifyLocalAudioArtifact, verifyLocalAudioArtifactReceipt } from "./playback";
import {
  AudioLeaderRuntime,
  type AudioGenerationControlResult,
  type AudioGenerationControlTransport,
  type LockProvider
} from "./leaderRuntime";
import {
  ChroniclerSourceService,
  type ChroniclerSourceContext,
  type ChroniclerTranscriptDiscovery,
  type ChroniclerTranscriptSource
} from "./source";
import type {
  BridgeStatus,
  AudioArtifactReceiptRepository,
  BrowserBridgeApi,
  DirectoryHandleRepository,
  LocalOperation
} from "./types";

const lockName = "temporal-agent-harness.browser-bridge.executor.v1";
const pollIntervalMs = 1_000;

const initialStatus: BridgeStatus = {
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

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function safeRelativePathParts(relativePath: string): string[] {
  if (relativePath.includes("\\") || relativePath.startsWith("/")) {
    throw new Error(`Unsafe audio destination: ${relativePath}`);
  }
  const parts = relativePath.split("/");
  if (parts.length === 0 || parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error(`Unsafe audio destination: ${relativePath}`);
  }
  return parts;
}

async function destinationExists(
  root: FileSystemDirectoryHandle,
  relativePath: string
): Promise<boolean> {
  const parts = safeRelativePathParts(relativePath);
  const filename = parts.pop()!;
  let directory = root;
  try {
    for (const part of parts) {
      directory = await directory.getDirectoryHandle(part, { create: false });
    }
    await directory.getFileHandle(filename, { create: false });
    return true;
  } catch (error) {
    if (error instanceof DOMException && error.name === "NotFoundError") return false;
    throw error;
  }
}

export interface RebindState {
  pendingCount: number;
  inFlightCount: number;
  outboxCount: number;
}

export function canRebindDirectory(state: RebindState): boolean {
  return state.pendingCount === 0 && state.inFlightCount === 0 && state.outboxCount === 0;
}

export function assertDirectoryRebindSafe(state: RebindState): void {
  if (canRebindDirectory(state)) return;
  throw new Error("Pending, running, or stored local operations must settle before changing folders.");
}

export interface WorkflowProcessingResult {
  operations: LocalOperation[];
  completed: number;
  unsupported: string[];
}

export function isMissingDiscoveryPlaceholder(
  error: unknown,
  workflowId: string,
  outboxWorkflowIds: ReadonlySet<string>
): boolean {
  return error instanceof WorkflowNotFoundError && !outboxWorkflowIds.has(workflowId);
}

export async function processWorkflowOperations(
  api: BrowserBridgeApi,
  repository: DirectoryHandleRepository,
  directory: FileSystemDirectoryHandle,
  workflowId: string,
  bridgeId: string,
  rootId: string,
  handleBindingId: string,
  signal?: AbortSignal,
  shouldStopClaiming?: () => boolean
): Promise<WorkflowProcessingResult> {
  // Reconciliation is intentionally downstream of an authoritative snapshot. If polling
  // fails (including a transient workflow 404), no durable outbox entry is discarded.
  const operations = await api.listOperations(workflowId, bridgeId, rootId, signal);
  const summary = await fulfillOperations(
    api,
    repository,
    directory,
    workflowId,
    bridgeId,
    rootId,
    handleBindingId,
    operations,
    signal,
    shouldStopClaiming
  );
  await reconcileSettledOutcomes(
    repository,
    workflowId,
    rootId,
    handleBindingId,
    operations
  );
  return { operations, ...summary };
}

export class BrowserBridgeController {
  readonly #api: BrowserBridgeApi;
  readonly #repository: DirectoryHandleRepository;
  readonly #discovery: WorkflowDiscovery;
  readonly #onChange: (status: BridgeStatus) => void;
  #status = { ...initialStatus };
  #directory: FileSystemDirectoryHandle | null = null;
  #bridgeId = "";
  #rootId = "";
  #handleBindingId = "";
  #abort: AbortController | null = null;
  #loopPromise: Promise<void> | null = null;
  #leaderRuntime: AudioLeaderRuntime | null = null;
  #hasLeadership = false;
  #leadershipGeneration = 0;
  readonly #sources: ChroniclerSourceService;
  #inFlight = 0;

  constructor(
    onChange: (status: BridgeStatus) => void,
    api: BrowserBridgeApi = new HttpBrowserBridgeApi(),
    repository: DirectoryHandleRepository = new IndexedDbBridgeRepository(),
    discovery: WorkflowDiscovery = new WorkflowDiscovery(),
    private readonly audioTransport: AudioGenerationControlTransport = new HttpChroniclerAudioApi()
  ) {
    this.#onChange = onChange;
    this.#api = api;
    this.#repository = repository;
    this.#discovery = discovery;
    this.#sources = new ChroniclerSourceService(() => this.#sourceContext());
  }

  get status(): BridgeStatus {
    return this.#status;
  }

  #update(patch: Partial<BridgeStatus>): void {
    this.#status = { ...this.#status, ...patch };
    this.#onChange(this.#status);
  }

  async initialize(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return;
    if (!("showDirectoryPicker" in window) || !("indexedDB" in window) || !navigator.locks) {
      this.#update({
        phase: "unsupported",
        detail: "The browser bridge requires Chromium with File System Access and Web Locks."
      });
      return;
    }
    this.#bridgeId = await this.#repository.getBridgeId();
    if (signal?.aborted) return;
    const stored = await this.#repository.loadDirectory();
    if (signal?.aborted) return;
    if (!stored) {
      this.#update({ ...initialStatus, bridgeId: this.#bridgeId });
      return;
    }
    this.#directory = stored.handle;
    this.#rootId = stored.rootId;
    this.#handleBindingId = stored.handleBindingId;
    this.#update({
      directoryName: stored.handle.name,
      bridgeId: this.#bridgeId,
      rootId: this.#rootId,
      handleBindingId: this.#handleBindingId,
      canRebind: false
    });
    const permission = await stored.handle.queryPermission({ mode: "readwrite" });
    if (signal?.aborted) return;
    if (permission !== "granted") {
      this.#update({
        phase: "permission-needed",
        detail: "Reconnect the campaign directory to resume local operations."
      });
      return;
    }
    this.#start();
  }

  async chooseDirectory(): Promise<void> {
    if (this.#directory && !this.#status.canRebind) {
      throw new Error("Wait for pending local operations to settle before changing folders.");
    }
    const previousDirectory = this.#directory;
    if (previousDirectory) await this.#stopAndDrain();
    this.#update({ canRebind: false, detail: "Checking for pending local operations…" });
    try {
      const changed = await navigator.locks.request(lockName, { ifAvailable: true }, async (lock) => {
        if (!lock) return false;
        assertDirectoryRebindSafe({
          outboxCount: await this.#repository.countOutcomes(),
          pendingCount: previousDirectory ? await this.#pendingOperationCount() : 0,
          inFlightCount: this.#inFlight
        });
        const handle = await window.showDirectoryPicker({
          id: "temporal-chronicler-campaign",
          mode: "readwrite"
        });
        if (previousDirectory && await this.#pendingOperationCount() > 0) {
          throw new Error("A local operation arrived while choosing the folder; try again after it settles.");
        }
        const binding = await this.#repository.saveDirectory(handle);
        this.#directory = handle;
        this.#rootId = binding.rootId;
        this.#handleBindingId = binding.handleBindingId;
        this.#bridgeId ||= await this.#repository.getBridgeId();
        this.#update({
          directoryName: handle.name,
          bridgeId: this.#bridgeId,
          rootId: this.#rootId,
          handleBindingId: this.#handleBindingId,
          completedCount: 0,
          pendingCount: 0,
          unsupportedOperations: []
        });
        return true;
      });
      if (!changed) throw new Error("Another tab is using the browser bridge.");
    } finally {
      if (this.#directory) this.#start();
    }
  }

  async reconnect(): Promise<void> {
    if (!this.#directory) return this.chooseDirectory();
    await this.#stopAndDrain();
    const permission = await this.#directory.requestPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      this.#update({
        phase: "permission-needed",
        detail: "Read/write permission was not granted."
      });
      return;
    }
    this.#start();
  }

  stop(): void {
    this.#abort?.abort();
    this.#abort = null;
    this.#leadershipGeneration += 1;
    this.#hasLeadership = false;
    this.#leaderRuntime = null;
    if (this.#status.phase === "connected") {
      this.#update({
        phase: "standby",
        detail: "Waiting to acquire active browser bridge leadership.",
        canRebind: false
      });
    }
  }

  cancelAudioGeneration(childWorkflowId: string): Promise<AudioGenerationControlResult> {
    if (!this.#hasLeadership || !this.#leaderRuntime) {
      return Promise.reject(new Error(
        "Switch to the active browser bridge tab to cancel audio generation."
      ));
    }
    return this.#leaderRuntime.cancelAudioGeneration(childWorkflowId);
  }

  async preflightAudioDestinations(
    relativePaths: readonly string[],
    folderBindingId: string
  ): Promise<void> {
    if (!this.#hasLeadership || !this.#directory || !this.#handleBindingId) {
      throw new Error("Audio destination approval is available only in the active browser bridge tab.");
    }
    if (folderBindingId !== this.#handleBindingId) {
      throw new Error("Audio destinations do not match the active folder binding.");
    }
    const permission = await this.#directory.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      throw new Error("Campaign directory read/write permission is required for audio approval.");
    }
    for (const relativePath of relativePaths) {
      if (await destinationExists(this.#directory, relativePath)) {
        throw new Error(`Audio destination already exists: ${relativePath}`);
      }
    }
  }

  discoverChroniclerTranscriptSources(): Promise<ChroniclerTranscriptDiscovery> {
    return this.#sources.discover();
  }

  readChroniclerTranscriptSource(sessionId: string): Promise<ChroniclerTranscriptSource> {
    return this.#sources.read(sessionId);
  }

  isChroniclerTranscriptSourceCurrent(
    source: ChroniclerTranscriptSource
  ): Promise<boolean> {
    return this.#sources.isCurrent(source);
  }

  async verifyAudioPlayback(receipt: ChroniclerAudioArtifactReceipt): Promise<Blob> {
    if (!this.#hasLeadership || !this.#directory || !this.#handleBindingId) {
      throw new Error("Audio playback is available only in the active browser bridge tab.");
    }
    const permission = await this.#directory.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      throw new Error("Campaign directory read/write permission is required for audio playback.");
    }
    return verifyLocalAudioArtifact(
      this.#directory,
      this.#repository as DirectoryHandleRepository & AudioArtifactReceiptRepository,
      receipt,
      this.#handleBindingId
    );
  }

  async verifyAudioArtifact(receipt: ChroniclerAudioArtifactReceipt): Promise<void> {
    if (!this.#hasLeadership || !this.#directory || !this.#handleBindingId) {
      throw new Error("Audio verification is available only in the active browser bridge tab.");
    }
    const permission = await this.#directory.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      throw new Error("Campaign directory read/write permission is required for audio verification.");
    }
    if (receipt.artifact_role === "wav") {
      await verifyLocalAudioArtifact(
        this.#directory,
        this.#repository as DirectoryHandleRepository & AudioArtifactReceiptRepository,
        receipt,
        this.#handleBindingId
      );
      return;
    }
    await verifyLocalAudioArtifactReceipt(
      this.#directory,
      this.#repository as DirectoryHandleRepository & AudioArtifactReceiptRepository,
      receipt,
      this.#handleBindingId
    );
  }

  async #sourceContext(): Promise<ChroniclerSourceContext> {
    if (!this.#hasLeadership || !this.#directory || !this.#handleBindingId) {
      throw new Error("Chronicler sources are available only in the active browser bridge tab.");
    }
    const permission = await this.#directory.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      throw new Error("Campaign directory read/write permission is required.");
    }
    return {
      root: this.#directory,
      folderBindingId: this.#handleBindingId
    };
  }

  async #stopAndDrain(): Promise<void> {
    const loop = this.#loopPromise;
    this.stop();
    if (loop) await loop;
    this.#loopPromise = null;
  }

  #start(): void {
    this.stop();
    if (!this.#directory) return;
    this.#abort = new AbortController();
    const signal = this.#abort.signal;
    const generation = ++this.#leadershipGeneration;
    let leaderRuntime!: AudioLeaderRuntime;
    const isCurrent = () => this.#isCurrentLeadership(signal, generation, leaderRuntime);
    leaderRuntime = new AudioLeaderRuntime(
      navigator.locks as unknown as LockProvider,
      {
        onActive: () => {
          if (!isCurrent()) return;
          this.#hasLeadership = true;
          this.#update({
            phase: "connected",
            detail: "This tab is fulfilling local operations.",
            lastConnectedAt: Date.now(),
            canRebind: false
          });
        },
        claimNext: (claimSignal) => this.#pollOnce(claimSignal, isCurrent),
        drainOutbox: (drainSignal) => drainStoredOutcomes(
          this.#api,
          this.#repository,
          this.#bridgeId,
          this.#rootId,
          this.#handleBindingId,
          drainSignal
        ).then(() => undefined),
        wait: (waitSignal) => sleep(pollIntervalMs, waitSignal)
      },
      this.audioTransport,
      lockName
    );
    this.#leaderRuntime = leaderRuntime;
    this.#loopPromise = this.#leadershipLoop(signal, generation, leaderRuntime).finally(() => {
      if (this.#abort?.signal === signal) this.#loopPromise = null;
    });
  }

  async #pendingOperationCount(signal?: AbortSignal): Promise<number> {
    const workflowIds = await this.#discovery.activeWorkflowIds(signal);
    let count = 0;
    for (const workflowId of workflowIds) {
      try {
        count += (await this.#api.listOperations(
          workflowId,
          this.#bridgeId,
          this.#rootId,
          signal
        )).length;
      } catch (error) {
        // Discovery predicts both high-level child IDs before either tool is invoked.
        if (!(error instanceof WorkflowNotFoundError)) throw error;
      }
    }
    return count;
  }

  async #leadershipLoop(
    signal: AbortSignal,
    generation: number,
    leaderRuntime: AudioLeaderRuntime
  ): Promise<void> {
    while (!signal.aborted) {
      try {
        const led = await leaderRuntime.runLeadershipAttempt(signal);
        if (!this.#isCurrentLeadership(signal, generation, leaderRuntime)) return;
        this.#hasLeadership = false;
        if (!led) {
          this.#update({
            phase: "standby",
            detail: "Another tab is active; switch to it to cancel audio generation.",
            canRebind: false
          });
          await sleep(2_000, signal);
        }
      } catch (error) {
        if (!this.#isCurrentLeadership(signal, generation, leaderRuntime)) return;
        this.#hasLeadership = false;
        this.#update({ phase: "error", detail: message(error) });
        await sleep(2_000, signal);
      }
    }
  }

  #isCurrentLeadership(
    signal: AbortSignal,
    generation: number,
    leaderRuntime: AudioLeaderRuntime
  ): boolean {
    return !signal.aborted
      && generation === this.#leadershipGeneration
      && this.#leaderRuntime === leaderRuntime;
  }

  async #pollOnce(
    signal?: AbortSignal,
    isCurrent: () => boolean = () => true
  ): Promise<void> {
    if (!signal || signal.aborted || !this.#directory || !isCurrent()) return;
      try {
        const outbox = await this.#repository.listOutcomes(this.#handleBindingId);
        if (!isCurrent()) return;
        const discoveredWorkflowIds = await this.#discovery.activeWorkflowIds(signal);
        if (!isCurrent()) return;
        const outboxWorkflowIds = new Set(outbox.map((outcome) => outcome.workflowId));
        const workflowIds = [...new Set([
          ...discoveredWorkflowIds,
          ...outbox.map((outcome) => outcome.workflowId)
        ])];
        let pendingCount = 0;
        let completed = 0;
        const unsupported = new Set<string>();
        for (const workflowId of workflowIds) {
          if (this.#leaderRuntime?.cancellationPending) break;
          this.#inFlight += 1;
          this.#update({ canRebind: false });
          let result;
          try {
            result = await processWorkflowOperations(
              this.#api,
              this.#repository,
              this.#directory,
              workflowId,
              this.#bridgeId,
              this.#rootId,
              this.#handleBindingId,
              signal,
              () => this.#leaderRuntime?.cancellationPending === true
            );
          } catch (error) {
            // A predicted child does not exist until its high-level tool runs. Missing workflows
            // with durable outbox entries remain errors so their outcomes are never discarded.
            if (isMissingDiscoveryPlaceholder(error, workflowId, outboxWorkflowIds)) {
              continue;
            }
            throw error;
          } finally {
            this.#inFlight -= 1;
          }
          if (!isCurrent()) return;
          pendingCount += result.operations.length;
          completed += result.completed;
          for (const operation of result.unsupported) unsupported.add(operation);
        }
        const outboxCount = await this.#repository.countOutcomes();
        if (!isCurrent()) return;
        this.#update({
          phase: "connected",
          detail: pendingCount ? `Processing ${pendingCount} local operation(s).` : "Ready for local operations.",
          pendingCount,
          completedCount: this.#status.completedCount + completed,
          unsupportedOperations: [...unsupported].sort(),
          lastConnectedAt: Date.now(),
          canRebind: canRebindDirectory({
            pendingCount,
            inFlightCount: this.#inFlight,
            outboxCount
          })
        });
      } catch (error) {
        if (!isCurrent()) return;
        if (isFileSystemPermissionError(error)) {
          this.#update({
            phase: "permission-needed",
            detail: "Campaign directory permission was lost; reconnect to continue.",
            canRebind: false
          });
          this.#abort?.abort();
          return;
        }
        this.#update({ phase: "error", detail: message(error), canRebind: false });
      }
  }
}
