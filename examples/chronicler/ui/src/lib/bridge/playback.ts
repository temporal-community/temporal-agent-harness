import type { ChroniclerAudioArtifactReceipt } from "./api";
import type { AudioArtifactReceiptRepository } from "./types";

function safePath(path: string): string[] {
  const parts = path.split("/");
  if (!parts.length || parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error("Audio artifact path must stay within the connected folder.");
  }
  return parts;
}

async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

function isWav(bytes: Uint8Array): boolean {
  return bytes.length >= 12
    && String.fromCharCode(...bytes.slice(0, 4)) === "RIFF"
    && String.fromCharCode(...bytes.slice(8, 12)) === "WAVE";
}

export async function verifyLocalAudioArtifact(
  root: FileSystemDirectoryHandle,
  repository: Pick<AudioArtifactReceiptRepository, "loadAudioArtifactReceipt">,
  receipt: ChroniclerAudioArtifactReceipt,
  activeFolderBindingId: string
): Promise<Blob> {
  const file = await verifyLocalAudioArtifactReceipt(
    root,
    repository,
    receipt,
    activeFolderBindingId
  );
  if (receipt.artifact_role !== "wav") throw new Error("Only WAV artifacts are playable.");
  if (!isWav(new Uint8Array(await file.arrayBuffer()))) {
    throw new Error("Audio artifact is not a usable WAV.");
  }
  return file;
}

export async function verifyLocalAudioArtifactReceipt(
  root: FileSystemDirectoryHandle,
  repository: Pick<AudioArtifactReceiptRepository, "loadAudioArtifactReceipt">,
  receipt: ChroniclerAudioArtifactReceipt,
  activeFolderBindingId: string
): Promise<Blob> {
  if (receipt.folder_binding_id !== activeFolderBindingId) {
    throw new Error("Audio artifact folder binding does not match the active folder.");
  }
  const stored = await repository.loadAudioArtifactReceipt(
    receipt.generation_id,
    receipt.artifact_role
  );
  if (
    !stored
    || stored.generationId !== receipt.generation_id
    || stored.relativePath !== receipt.relative_path
    || stored.contentHash !== receipt.content_hash
    || stored.contentSize !== receipt.content_size
    || stored.folderBindingId !== receipt.folder_binding_id
    || stored.packageRevision !== receipt.package_revision
    || stored.operationId !== receipt.operation_id
  ) {
    throw new Error("Audio artifact ownership receipt does not match the completed generation.");
  }

  const parts = safePath(receipt.relative_path);
  const filename = parts.pop();
  if (!filename) throw new Error("Audio artifact filename is required.");
  let directory = root;
  for (const part of parts) directory = await directory.getDirectoryHandle(part);
  const file = await (await directory.getFileHandle(filename)).getFile();
  if (file.size !== receipt.content_size || await sha256(file) !== receipt.content_hash) {
    throw new Error("Audio artifact content does not match its ownership receipt.");
  }
  return file;
}

interface ObjectUrlApi {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
}

interface PlaybackLoadSession {
  load(receipt: ChroniclerAudioArtifactReceipt): Promise<string | null>;
  dispose(): void;
}

export class AudioPlaybackLoadLifecycle {
  #request = 0;

  constructor(
    private readonly playback: PlaybackLoadSession,
    private readonly onUrl: (url: string) => void,
    private readonly onError: (error: string | null) => void
  ) {}

  async load(receipt: ChroniclerAudioArtifactReceipt): Promise<void> {
    const request = ++this.#request;
    try {
      const url = await this.playback.load(receipt);
      if (request !== this.#request || url === null) return;
      this.onUrl(url);
      this.onError(null);
    } catch (error) {
      if (request !== this.#request) return;
      this.onError(error instanceof Error ? error.message : String(error));
    }
  }

  invalidate(): void {
    this.#request += 1;
    this.playback.dispose();
  }
}

export class AudioPlaybackSession {
  #url: string | null = null;
  #generation = 0;

  constructor(
    private readonly verify: (receipt: ChroniclerAudioArtifactReceipt) => Promise<Blob>,
    private readonly urls: ObjectUrlApi = URL
  ) {}

  get url(): string | null {
    return this.#url;
  }

  async load(receipt: ChroniclerAudioArtifactReceipt): Promise<string | null> {
    const generation = ++this.#generation;
    let blob: Blob;
    try {
      blob = await this.verify(receipt);
    } catch (error) {
      if (generation !== this.#generation) return null;
      throw error;
    }
    const url = this.urls.createObjectURL(blob);
    if (generation !== this.#generation) {
      this.urls.revokeObjectURL(url);
      return null;
    }
    this.#revoke();
    this.#url = url;
    return this.#url;
  }

  dispose(): void {
    this.#generation += 1;
    this.#revoke();
  }

  #revoke(): void {
    if (!this.#url) return;
    this.urls.revokeObjectURL(this.#url);
    this.#url = null;
  }
}
