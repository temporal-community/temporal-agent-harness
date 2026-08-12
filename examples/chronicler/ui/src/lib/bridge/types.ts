export type BridgePhase =
  | "unsupported"
  | "disconnected"
  | "permission-needed"
  | "standby"
  | "connected"
  | "error";

export interface BridgeStatus {
  phase: BridgePhase;
  directoryName: string | null;
  detail: string;
  completedCount: number;
  pendingCount: number;
  unsupportedOperations: string[];
  lastConnectedAt: number | null;
  bridgeId: string;
  rootId: string;
  handleBindingId: string | null;
  canRebind: boolean;
}

export interface LocalOperation {
  operation_id: string;
  bridge_id: string;
  root_id: string;
  kind: string;
  arguments: Record<string, unknown>;
  idempotency_key: string;
  output_schema: Record<string, unknown>;
}

export interface LocalOperationsResponse {
  workflow_id: string;
  bridge_id: string;
  root_id: string;
  operations: LocalOperation[];
}

export type LocalOperationOutcome =
  | { outcome: "success"; result: unknown; error?: never }
  | { outcome: "error"; result?: never; error: string };

export type LocalOperationResultRequest = LocalOperationOutcome & {
  bridge_id: string;
  root_id: string;
};

export interface LocalOperationResultAck {
  operation_id: string;
  accepted: boolean;
  settled?: boolean;
}

export interface StoredLocalOperationOutcome {
  key: string;
  workflowId: string;
  operationId: string;
  rootId: string;
  handleBindingId: string;
  idempotencyKey: string;
  operationFingerprint: string;
  operationKind?: string;
  operationFolderBindingId?: string | null;
  outcome: LocalOperationOutcome;
  createdAt: number;
}

export type AudioArtifactRole = "wav" | "synthetic_transcript";

export function audioArtifactReceiptKey(
  generationId: string,
  artifactRole: AudioArtifactRole
): string {
  return `${encodeURIComponent(generationId)}|${artifactRole}`;
}

export interface AudioArtifactReceipt {
  key: string;
  generationId: string;
  artifactRole: AudioArtifactRole;
  relativePath: string;
  contentHash: string;
  contentSize: number;
  folderBindingId: string;
  packageRevision: number;
  operationId: string;
}

export interface AudioArtifactReceiptRepository {
  loadAudioArtifactReceipt(
    generationId: string,
    artifactRole: AudioArtifactRole
  ): Promise<AudioArtifactReceipt | null>;
  saveAudioArtifactReceipt(receipt: AudioArtifactReceipt): Promise<void>;
  listAudioArtifactReceipts(): Promise<AudioArtifactReceipt[]>;
  removeAudioArtifactReceipt(
    generationId: string,
    artifactRole: AudioArtifactRole
  ): Promise<void>;
}

export interface CreateAudioArtifactOperation {
  operation_id: string;
  generation_id: string;
  artifact_role: AudioArtifactRole;
  relative_path: string;
  content_base64: string;
  expected_content_hash: string;
  expected_content_size: number;
  folder_binding_id: string;
  package_revision: number;
}

export interface CreateAudioArtifactResult {
  status: "created" | "reused";
  relative_path: string;
  observed_content_hash: string;
  content_size: number;
  receipt: AudioArtifactReceiptResult;
}

export interface InspectAudioArtifactOperation {
  generation_id: string;
  artifact_role: AudioArtifactRole;
  relative_path: string;
  folder_binding_id: string;
  approved_package_revision: number;
}

export type InspectAudioArtifactResult =
  | { status: "missing" }
  | {
      status: "owned";
      receipt: AudioArtifactReceiptResult;
      duration_s?: number;
    };

export interface AudioArtifactReceiptResult {
  generation_id: string;
  artifact_role: AudioArtifactRole;
  relative_path: string;
  content_hash: string;
  content_size: number;
  package_revision: number;
  operation_id: string;
  folder_binding_id: string;
}

export type AudioArtifactInspectionResult =
  | { status: "missing" }
  | { status: "owned"; observedContentHash: string; contentSize: number };

export interface DirectoryHandleRepository {
  getBridgeId(): Promise<string>;
  saveBinding(binding: { bridgeId: string; rootId: string }): Promise<void>;
  loadDirectory(): Promise<{
    handle: FileSystemDirectoryHandle;
    rootId: string;
    handleBindingId: string;
  } | null>;
  saveDirectory(handle: FileSystemDirectoryHandle): Promise<{
    rootId: string;
    handleBindingId: string;
  }>;
  countOutcomes(handleBindingId?: string): Promise<number>;
  listOutcomes(handleBindingId?: string): Promise<StoredLocalOperationOutcome[]>;
  loadOutcome(key: string): Promise<StoredLocalOperationOutcome | null>;
  saveOutcome(outcome: StoredLocalOperationOutcome): Promise<void>;
  removeOutcome(key: string): Promise<void>;
}

export interface BrowserBridgeApi {
  listOperations(
    workflowId: string,
    bridgeId: string,
    rootId: string,
    signal?: AbortSignal
  ): Promise<LocalOperation[]>;
  submitResult(
    workflowId: string,
    operationId: string,
    request: LocalOperationResultRequest,
    signal?: AbortSignal
  ): Promise<LocalOperationResultAck>;
}

export interface ExecuteOperationResult {
  supported: boolean;
  outcome?: LocalOperationOutcome;
}
