import { executeBrowserOperation } from "./executor";
import type {
  AudioArtifactReceiptRepository,
  BrowserBridgeApi,
  DirectoryHandleRepository,
  LocalOperation,
  StoredLocalOperationOutcome
} from "./types";

export interface FulfillmentSummary {
  completed: number;
  unsupported: string[];
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${stableJson(record[key])}`
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function operationFingerprint(operation: LocalOperation): string {
  return stableJson({
    kind: operation.kind,
    arguments: operation.arguments,
    output_schema: operation.output_schema
  });
}

export function outcomeKey(
  workflowId: string,
  rootId: string,
  handleBindingId: string,
  operation: LocalOperation
): string {
  return [
    workflowId,
    rootId,
    handleBindingId,
    operation.idempotency_key || operation.operation_id
  ].map(encodeURIComponent).join("|");
}

async function submitStoredOutcome(
  api: BrowserBridgeApi,
  stored: StoredLocalOperationOutcome,
  bridgeId: string,
  rootId: string,
  signal?: AbortSignal
): Promise<boolean> {
  const acknowledgement = await api.submitResult(
    stored.workflowId,
    stored.operationId,
    { bridge_id: bridgeId, root_id: rootId, ...stored.outcome },
    signal
  );
  return acknowledgement.accepted || acknowledgement.settled === true;
}

function audioReceiptRepository(
  repository: DirectoryHandleRepository
): AudioArtifactReceiptRepository {
  const candidate = repository as Partial<AudioArtifactReceiptRepository>;
  if (
    typeof candidate.loadAudioArtifactReceipt !== "function"
    || typeof candidate.saveAudioArtifactReceipt !== "function"
    || typeof candidate.listAudioArtifactReceipts !== "function"
    || typeof candidate.removeAudioArtifactReceipt !== "function"
  ) {
    throw new TypeError("Audio operations require the durable receipt repository.");
  }
  return candidate as AudioArtifactReceiptRepository;
}

function operationTargetsFolderBinding(
  operation: LocalOperation,
  handleBindingId: string
): boolean {
  if (
    operation.kind !== "create_audio_artifact"
    && operation.kind !== "inspect_audio_artifact"
  ) return true;
  return operation.arguments.folder_binding_id === handleBindingId;
}

function storedOutcomeTargetsFolderBinding(
  stored: StoredLocalOperationOutcome,
  handleBindingId: string
): boolean {
  let kind = stored.operationKind;
  let targetBindingId = stored.operationFolderBindingId;
  if (kind === undefined || targetBindingId === undefined) {
    try {
      const fingerprint = JSON.parse(stored.operationFingerprint) as {
        kind?: unknown;
        arguments?: { folder_binding_id?: unknown };
      };
      if (kind === undefined && typeof fingerprint.kind === "string") {
        kind = fingerprint.kind;
      }
      const fingerprintBindingId = fingerprint.arguments?.folder_binding_id;
      if (targetBindingId === undefined && typeof fingerprintBindingId === "string") {
        targetBindingId = fingerprintBindingId;
      }
    } catch {
      return true;
    }
  }
  if (kind !== "create_audio_artifact" && kind !== "inspect_audio_artifact") return true;
  return targetBindingId === handleBindingId;
}

export async function fulfillOperations(
  api: BrowserBridgeApi,
  repository: DirectoryHandleRepository,
  root: FileSystemDirectoryHandle,
  workflowId: string,
  bridgeId: string,
  rootId: string,
  handleBindingId: string,
  operations: LocalOperation[],
  signal?: AbortSignal,
  shouldStopClaiming: () => boolean = () => false
): Promise<FulfillmentSummary> {
  let completed = 0;
  const unsupported = new Set<string>();
  for (const operation of operations) {
    if (signal?.aborted || shouldStopClaiming()) break;
    if (operation.bridge_id !== bridgeId || operation.root_id !== rootId) continue;
    if (!operationTargetsFolderBinding(operation, handleBindingId)) continue;
    const key = outcomeKey(workflowId, rootId, handleBindingId, operation);
    const fingerprint = operationFingerprint(operation);
    let stored = await repository.loadOutcome(key);
    if (stored && stored.operationFingerprint !== fingerprint) {
      throw new Error(
        `Local operation idempotency key ${JSON.stringify(operation.idempotency_key)} ` +
        "was reused for different operation semantics."
      );
    }
    if (!stored) {
      const executed = await executeBrowserOperation(
        root,
        operation.kind,
        operation.arguments,
        operation.kind === "create_audio_artifact" || operation.kind === "inspect_audio_artifact"
          ? {
              repository: audioReceiptRepository(repository),
              activeFolderBindingId: handleBindingId
            }
          : undefined
      );
      if (!executed.supported || !executed.outcome) {
        unsupported.add(operation.kind);
        continue;
      }
      stored = {
        key,
        workflowId,
        operationId: operation.operation_id,
        rootId,
        handleBindingId,
        idempotencyKey: operation.idempotency_key,
        operationFingerprint: fingerprint,
        operationKind: operation.kind,
        operationFolderBindingId:
          typeof operation.arguments.folder_binding_id === "string"
            ? operation.arguments.folder_binding_id
            : null,
        outcome: executed.outcome,
        createdAt: Date.now()
      } satisfies StoredLocalOperationOutcome;
      await repository.saveOutcome(stored);
    }
    if (await submitStoredOutcome(api, stored, bridgeId, rootId, signal)) {
      await repository.removeOutcome(key);
      completed += 1;
    }
  }
  return { completed, unsupported: [...unsupported].sort() };
}

export async function reconcileSettledOutcomes(
  repository: DirectoryHandleRepository,
  workflowId: string,
  rootId: string,
  handleBindingId: string,
  operations: LocalOperation[]
): Promise<number> {
  const pendingKeys = new Set(
    operations
      .filter((operation) => operationTargetsFolderBinding(operation, handleBindingId))
      .map((operation) => outcomeKey(workflowId, rootId, handleBindingId, operation))
  );
  let removed = 0;
  for (const stored of await repository.listOutcomes(handleBindingId)) {
    if (stored.workflowId !== workflowId || stored.rootId !== rootId) continue;
    if (pendingKeys.has(stored.key)) continue;
    await repository.removeOutcome(stored.key);
    removed += 1;
  }
  return removed;
}

export async function drainStoredOutcomes(
  api: BrowserBridgeApi,
  repository: DirectoryHandleRepository,
  bridgeId: string,
  rootId: string,
  handleBindingId: string,
  signal?: AbortSignal
): Promise<number> {
  let removed = 0;
  for (const stored of await repository.listOutcomes(handleBindingId)) {
    if (!storedOutcomeTargetsFolderBinding(stored, handleBindingId)) {
      await repository.removeOutcome(stored.key);
      removed += 1;
      continue;
    }
    if (!await submitStoredOutcome(api, stored, bridgeId, rootId, signal)) {
      throw new Error("Durable local-operation outcome did not settle before cancellation.");
    }
    await repository.removeOutcome(stored.key);
    removed += 1;
  }
  return removed;
}
