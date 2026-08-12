import { audioArtifactReceiptKey } from "./types";
import type {
  AudioArtifactReceiptRepository,
  AudioArtifactInspectionResult,
  CreateAudioArtifactOperation,
  CreateAudioArtifactResult,
  InspectAudioArtifactOperation,
  InspectAudioArtifactResult,
  ExecuteOperationResult,
  LocalOperationOutcome
} from "./types";

export const browserOperationAllowlist = new Set([
  "create_audio_artifact",
  "inspect_audio_artifact"
]);

export interface BrowserOperationExecutionContext {
  repository: AudioArtifactReceiptRepository;
  activeFolderBindingId: string;
}

class AudioArtifactCollisionError extends Error {
  constructor(relativePath: string) {
    super(`audio artifact collision at ${JSON.stringify(relativePath)}`);
    this.name = "AudioArtifactCollisionError";
  }
}

export function browserOperationError(error: unknown): string {
  if (error instanceof AudioArtifactCollisionError) {
    return `audio_artifact_collision: ${error.message}`;
  }
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return `Error: ${String(error)}`;
}

export function isFileSystemPermissionError(error: unknown): boolean {
  if (typeof error !== "object" || error === null || !("name" in error)) return false;
  return error.name === "NotAllowedError" || error.name === "SecurityError";
}

function requireString(input: Record<string, unknown>, key: string): string {
  const value = input[key];
  if (typeof value !== "string") throw new TypeError(`${key} must be a string`);
  return value;
}

function requireSafeInteger(
  input: Record<string, unknown>,
  key: string,
  minimum: number,
  description: string
): number {
  const value = input[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    throw new TypeError(`${key} must be a ${description} safe integer`);
  }
  return value;
}

function requireInteger(input: Record<string, unknown>, key: string): number {
  return requireSafeInteger(input, key, 0, "non-negative");
}

function requirePositiveInteger(input: Record<string, unknown>, key: string): number {
  return requireSafeInteger(input, key, 1, "positive");
}

function createAudioArtifactOperation(
  input: Record<string, unknown>
): CreateAudioArtifactOperation {
  const artifactRole = requireString(input, "artifact_role");
  if (artifactRole !== "wav" && artifactRole !== "synthetic_transcript") {
    throw new TypeError("artifact_role must be wav or synthetic_transcript");
  }
  return {
    operation_id: requireString(input, "operation_id"),
    generation_id: requireString(input, "generation_id"),
    artifact_role: artifactRole,
    relative_path: requireString(input, "relative_path"),
    content_base64: requireString(input, "content_base64"),
    expected_content_hash: requireString(input, "expected_content_hash"),
    expected_content_size: requireInteger(input, "expected_content_size"),
    folder_binding_id: requireString(input, "folder_binding_id"),
    package_revision: requirePositiveInteger(input, "package_revision")
  };
}

function inspectAudioArtifactOperation(
  input: Record<string, unknown>
): InspectAudioArtifactOperation {
  const artifactRole = requireString(input, "artifact_role");
  if (artifactRole !== "wav" && artifactRole !== "synthetic_transcript") {
    throw new TypeError("artifact_role must be wav or synthetic_transcript");
  }
  return {
    generation_id: requireString(input, "generation_id"),
    artifact_role: artifactRole,
    relative_path: requireString(input, "relative_path"),
    folder_binding_id: requireString(input, "folder_binding_id"),
    approved_package_revision: requirePositiveInteger(input, "approved_package_revision")
  };
}

function requireAudioContext(
  context: BrowserOperationExecutionContext | undefined
): BrowserOperationExecutionContext {
  if (!context) {
    throw new TypeError("audio artifact operations require browser execution context");
  }
  return context;
}

export function safePathSegments(path: string): string[] {
  const normalized = path.replaceAll("\\", "/");
  if (normalized.startsWith("/") || /^[a-zA-Z]:\//.test(normalized)) {
    throw new TypeError(`path ${JSON.stringify(path)} must be relative to the campaign directory`);
  }
  const segments = normalized.split("/").filter((segment) => segment !== "" && segment !== ".");
  if (segments.some((segment) => segment === "..")) {
    throw new TypeError(`path ${JSON.stringify(path)} escapes the campaign directory`);
  }
  return segments;
}

async function directoryAt(
  root: FileSystemDirectoryHandle,
  segments: string[],
  create = false
): Promise<FileSystemDirectoryHandle> {
  let directory = root;
  for (const segment of segments) {
    directory = await directory.getDirectoryHandle(segment, { create });
  }
  return directory;
}

async function fileAt(
  root: FileSystemDirectoryHandle,
  path: string,
  create = false
): Promise<FileSystemFileHandle> {
  const segments = safePathSegments(path);
  const name = segments.pop();
  if (!name) throw new TypeError("a file path is required");
  const parent = await directoryAt(root, segments, create);
  return parent.getFileHandle(name, { create });
}

function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "NotFoundError";
}

async function writeData(
  root: FileSystemDirectoryHandle,
  path: string,
  data: string | Uint8Array
): Promise<void> {
  const handle = await fileAt(root, path, true);
  const writable = await handle.createWritable();
  try {
    if (typeof data === "string") {
      await writable.write(data);
    } else {
      const buffer = new ArrayBuffer(data.byteLength);
      new Uint8Array(buffer).set(data);
      await writable.write(buffer);
    }
  } finally {
    await writable.close();
  }
}

async function exists(root: FileSystemDirectoryHandle, path: string): Promise<boolean> {
  try {
    await fileAt(root, path);
    return true;
  } catch (error) {
    if (isNotFound(error)) return false;
    throw error;
  }
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function sha256Hex(data: Uint8Array): Promise<string> {
  const buffer = new ArrayBuffer(data.byteLength);
  new Uint8Array(buffer).set(data);
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0")
  ).join("");
}

async function readBytes(
  root: FileSystemDirectoryHandle,
  path: string
): Promise<Uint8Array> {
  const file = await (await fileAt(root, path)).getFile();
  return new Uint8Array(await file.arrayBuffer());
}

function receiptMatchesOperation(
  receipt: Awaited<ReturnType<AudioArtifactReceiptRepository["loadAudioArtifactReceipt"]>>,
  operation: CreateAudioArtifactOperation
): boolean {
  return receipt !== null
    && receipt.key === audioArtifactReceiptKey(
      operation.generation_id,
      operation.artifact_role
    )
    && receipt.generationId === operation.generation_id
    && receipt.artifactRole === operation.artifact_role
    && receipt.relativePath === operation.relative_path
    && receipt.contentHash === operation.expected_content_hash
    && receipt.contentSize === operation.expected_content_size
    && receipt.folderBindingId === operation.folder_binding_id
    && receipt.packageRevision === operation.package_revision
    && receipt.operationId === operation.operation_id;
}

function workflowReceipt(
  operation: CreateAudioArtifactOperation,
  contentHash: string,
  contentSize: number
) {
  return {
    generation_id: operation.generation_id,
    artifact_role: operation.artifact_role,
    relative_path: operation.relative_path,
    content_hash: contentHash,
    content_size: contentSize,
    package_revision: operation.package_revision,
    operation_id: operation.operation_id,
    folder_binding_id: operation.folder_binding_id
  };
}

function workflowStoredReceipt(receipt: NonNullable<Awaited<
  ReturnType<AudioArtifactReceiptRepository["loadAudioArtifactReceipt"]>
>>) {
  return {
    generation_id: receipt.generationId,
    artifact_role: receipt.artifactRole,
    relative_path: receipt.relativePath,
    content_hash: receipt.contentHash,
    content_size: receipt.contentSize,
    package_revision: receipt.packageRevision,
    operation_id: receipt.operationId,
    folder_binding_id: receipt.folderBindingId
  };
}

function receiptMatchesInspection(
  receipt: Awaited<ReturnType<AudioArtifactReceiptRepository["loadAudioArtifactReceipt"]>>,
  operation: InspectAudioArtifactOperation
): boolean {
  if (!receipt) return false;
  const expectedOperationId = [
    "audio-write",
    operation.generation_id,
    `r${receipt.packageRevision}`,
    operation.artifact_role
  ].join(":");
  return receipt !== null
    && receipt.key === audioArtifactReceiptKey(operation.generation_id, operation.artifact_role)
    && receipt.generationId === operation.generation_id
    && receipt.artifactRole === operation.artifact_role
    && receipt.relativePath === operation.relative_path
    && receipt.folderBindingId === operation.folder_binding_id
    && receipt.packageRevision <= operation.approved_package_revision
    && receipt.operationId === expectedOperationId;
}

function wavDurationSeconds(bytes: Uint8Array): number {
  if (
    bytes.byteLength < 44
    || new TextDecoder().decode(bytes.slice(0, 4)) !== "RIFF"
    || new TextDecoder().decode(bytes.slice(8, 12)) !== "WAVE"
  ) throw new Error("audio artifact is not a usable WAV");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let byteRate = 0;
  let dataSize = -1;
  for (let offset = 12; offset + 8 <= bytes.byteLength;) {
    const chunk = new TextDecoder().decode(bytes.slice(offset, offset + 4));
    const size = view.getUint32(offset + 4, true);
    const body = offset + 8;
    if (body + size > bytes.byteLength) break;
    if (chunk === "fmt " && size >= 16) {
      const format = view.getUint16(body, true);
      const channels = view.getUint16(body + 2, true);
      const sampleRate = view.getUint32(body + 4, true);
      byteRate = view.getUint32(body + 8, true);
      if (format !== 1 || channels === 0 || sampleRate === 0 || byteRate === 0) break;
    }
    if (chunk === "data") dataSize = size;
    offset = body + size + (size % 2);
  }
  if (byteRate <= 0 || dataSize < 0) throw new Error("audio artifact is not a usable WAV");
  return dataSize / byteRate;
}

export async function inspectAudioArtifact(
  root: FileSystemDirectoryHandle,
  repository: AudioArtifactReceiptRepository,
  operation: CreateAudioArtifactOperation,
  activeFolderBindingId: string
): Promise<AudioArtifactInspectionResult> {
  if (activeFolderBindingId !== operation.folder_binding_id) {
    throw new Error("audio artifact operation does not match the active folder binding");
  }
  if (!await exists(root, operation.relative_path)) return { status: "missing" };
  const receipt = await repository.loadAudioArtifactReceipt(
    operation.generation_id,
    operation.artifact_role
  );
  const existing = await readBytes(root, operation.relative_path);
  const existingHash = await sha256Hex(existing);
  if (
    !receiptMatchesOperation(receipt, operation)
    || existingHash !== operation.expected_content_hash
    || existing.byteLength !== operation.expected_content_size
  ) {
    throw new AudioArtifactCollisionError(operation.relative_path);
  }
  return {
    status: "owned",
    observedContentHash: existingHash,
    contentSize: existing.byteLength
  };
}

export async function executeCreateOnlyAudioArtifact(
  root: FileSystemDirectoryHandle,
  repository: AudioArtifactReceiptRepository,
  operation: CreateAudioArtifactOperation,
  activeFolderBindingId: string
): Promise<CreateAudioArtifactResult> {
  const data = decodeBase64(operation.content_base64);
  const observedHash = await sha256Hex(data);
  if (data.byteLength !== operation.expected_content_size) {
    throw new Error("audio artifact content size does not match the approved operation");
  }
  if (observedHash !== operation.expected_content_hash) {
    throw new Error("audio artifact content hash does not match the approved operation");
  }
  const inspection = await inspectAudioArtifact(
    root,
    repository,
    operation,
    activeFolderBindingId
  );
  if (inspection.status === "owned") {
    return {
      status: "reused",
      relative_path: operation.relative_path,
      observed_content_hash: inspection.observedContentHash,
      content_size: inspection.contentSize,
      receipt: workflowReceipt(
        operation,
        inspection.observedContentHash,
        inspection.contentSize
      )
    };
  }

  await writeData(root, operation.relative_path, data);
  const written = await readBytes(root, operation.relative_path);
  const writtenHash = await sha256Hex(written);
  if (
    writtenHash !== operation.expected_content_hash
    || written.byteLength !== operation.expected_content_size
  ) {
    throw new Error("audio artifact observed file hash does not match the approved write");
  }
  await repository.saveAudioArtifactReceipt({
    key: audioArtifactReceiptKey(operation.generation_id, operation.artifact_role),
    generationId: operation.generation_id,
    artifactRole: operation.artifact_role,
    relativePath: operation.relative_path,
    contentHash: writtenHash,
    contentSize: written.byteLength,
    folderBindingId: operation.folder_binding_id,
    packageRevision: operation.package_revision,
    operationId: operation.operation_id
  });
  return {
    status: "created",
    relative_path: operation.relative_path,
    observed_content_hash: writtenHash,
    content_size: written.byteLength,
    receipt: workflowReceipt(operation, writtenHash, written.byteLength)
  };
}

async function executeInspectAudioArtifact(
  root: FileSystemDirectoryHandle,
  repository: AudioArtifactReceiptRepository,
  operation: InspectAudioArtifactOperation,
  activeFolderBindingId: string
): Promise<InspectAudioArtifactResult> {
  if (activeFolderBindingId !== operation.folder_binding_id) {
    throw new Error("audio artifact operation does not match the active folder binding");
  }
  if (!await exists(root, operation.relative_path)) return { status: "missing" };
  const receipt = await repository.loadAudioArtifactReceipt(
    operation.generation_id,
    operation.artifact_role
  );
  const bytes = await readBytes(root, operation.relative_path);
  const contentHash = await sha256Hex(bytes);
  if (
    !receipt
    || !receiptMatchesInspection(receipt, operation)
    || receipt.contentHash !== contentHash
    || receipt.contentSize !== bytes.byteLength
  ) {
    throw new AudioArtifactCollisionError(operation.relative_path);
  }
  return {
    status: "owned",
    receipt: workflowStoredReceipt(receipt),
    ...(operation.artifact_role === "wav" ? { duration_s: wavDurationSeconds(bytes) } : {})
  };
}

export async function executeBrowserOperation(
  root: FileSystemDirectoryHandle,
  operation: string,
  input: Record<string, unknown>,
  context?: BrowserOperationExecutionContext
): Promise<ExecuteOperationResult> {
  if (!browserOperationAllowlist.has(operation)) return { supported: false };
  try {
    let result: unknown;
    if (operation === "create_audio_artifact") {
      const audioContext = requireAudioContext(context);
      result = await executeCreateOnlyAudioArtifact(
        root,
        audioContext.repository,
        createAudioArtifactOperation(input),
        audioContext.activeFolderBindingId
      );
    } else if (operation === "inspect_audio_artifact") {
      const audioContext = requireAudioContext(context);
      result = await executeInspectAudioArtifact(
        root,
        audioContext.repository,
        inspectAudioArtifactOperation(input),
        audioContext.activeFolderBindingId
      );
    } else {
      return { supported: false };
    }
    const outcome: LocalOperationOutcome = { outcome: "success", result };
    return { supported: true, outcome };
  } catch (error) {
    if (isFileSystemPermissionError(error)) throw error;
    return {
      supported: true,
      outcome: { outcome: "error", error: browserOperationError(error) }
    };
  }
}
