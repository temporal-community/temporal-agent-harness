import { describe, expect, it, vi } from "vitest";
import {
  browserOperationError,
  executeBrowserOperation,
  executeCreateOnlyAudioArtifact,
  inspectAudioArtifact,
  safePathSegments
} from "./executor";
import { audioArtifactReceiptKey } from "./types";
import type {
  AudioArtifactReceipt,
  AudioArtifactReceiptRepository
} from "./types";

function arrayBufferOf(bytes: Uint8Array): ArrayBuffer {
  const buffer = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(buffer).set(bytes);
  return buffer;
}

function wavBytes(durationSeconds = 1): Uint8Array {
  const sampleRate = 8_000;
  const channels = 1;
  const bitsPerSample = 16;
  const dataSize = sampleRate * durationSeconds * channels * (bitsPerSample / 8);
  const bytes = new Uint8Array(44 + dataSize);
  const view = new DataView(bytes.buffer);
  bytes.set(new TextEncoder().encode("RIFF"), 0);
  view.setUint32(4, 36 + dataSize, true);
  bytes.set(new TextEncoder().encode("WAVEfmt "), 8);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channels * (bitsPerSample / 8), true);
  view.setUint16(32, channels * (bitsPerSample / 8), true);
  view.setUint16(34, bitsPerSample, true);
  bytes.set(new TextEncoder().encode("data"), 36);
  view.setUint32(40, dataSize, true);
  return bytes;
}

function fakeDirectory(
  initial: Record<string, Uint8Array> = {},
  transformWrite: (bytes: Uint8Array) => Uint8Array = (bytes) => bytes
) {
  const files = new Map(Object.entries(initial));
  const directory = (prefix = ""): FileSystemDirectoryHandle => ({
    kind: "directory",
    name: prefix.split("/").filter(Boolean).at(-1) ?? "root",
    getDirectoryHandle: async (name: string, options?: { create?: boolean }) => {
      const path = `${prefix}${name}/`;
      const exists = [...files].some(([key]) => key.startsWith(path));
      if (!exists && !options?.create) throw new DOMException("Missing", "NotFoundError");
      return directory(path);
    },
    getFileHandle: async (name: string, options?: { create?: boolean }) => {
      const path = `${prefix}${name}`;
      if (!files.has(path) && !options?.create) {
        throw new DOMException("Missing", "NotFoundError");
      }
      return {
        kind: "file",
        name,
        getFile: async () => new Blob([
          arrayBufferOf(files.get(path) ?? new Uint8Array())
        ]) as File,
        createWritable: async () => {
          let pending = new Uint8Array();
          return {
            write: async (value: BufferSource | Blob | string) => {
              if (typeof value === "string") pending = new TextEncoder().encode(value);
              else if (value instanceof Blob) pending = new Uint8Array(await value.arrayBuffer());
              else {
                const view = ArrayBuffer.isView(value)
                  ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
                  : new Uint8Array(value);
                pending = new Uint8Array(view);
              }
            },
            close: async () => { files.set(path, transformWrite(pending)); }
          } as FileSystemWritableFileStream;
        }
      } as FileSystemFileHandle;
    },
    entries: async function* () {},
    values: async function* () {},
    isSameEntry: async () => false,
    queryPermission: async () => "granted",
    requestPermission: async () => "granted",
    removeEntry: async () => undefined,
    resolve: async () => null
  });
  return { root: directory(), files };
}

function receiptRepository(): AudioArtifactReceiptRepository {
  return {
    loadAudioArtifactReceipt: vi.fn(async () => null),
    saveAudioArtifactReceipt: vi.fn(async () => undefined),
    listAudioArtifactReceipts: vi.fn(async () => []),
    removeAudioArtifactReceipt: vi.fn(async () => undefined)
  };
}

async function audioOperation(
  bytes: Uint8Array,
  overrides: Partial<Parameters<typeof executeCreateOnlyAudioArtifact>[2]> = {}
) {
  const expectedHash = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", arrayBufferOf(bytes))),
    (value) => value.toString(16).padStart(2, "0")
  ).join("");
  return {
    operation_id: "audio-write:generation-7:r1:wav",
    generation_id: "generation-7",
    artifact_role: "wav" as const,
    relative_path: "audio/recap.wav",
    content_base64: btoa(String.fromCharCode(...bytes)),
    expected_content_hash: expectedHash,
    expected_content_size: bytes.byteLength,
    folder_binding_id: "binding-a",
    package_revision: 1,
    ...overrides
  };
}

function storedReceipt(
  operation: Awaited<ReturnType<typeof audioOperation>>,
  overrides: Partial<AudioArtifactReceipt> = {}
): AudioArtifactReceipt {
  return {
    key: audioArtifactReceiptKey(operation.generation_id, operation.artifact_role),
    generationId: operation.generation_id,
    artifactRole: operation.artifact_role,
    relativePath: operation.relative_path,
    contentHash: operation.expected_content_hash,
    contentSize: operation.expected_content_size,
    folderBindingId: operation.folder_binding_id,
    packageRevision: operation.package_revision,
    operationId: operation.operation_id,
    ...overrides
  };
}

describe("safePathSegments", () => {
  it("normalizes relative campaign paths", () => {
    expect(safePathSegments("./site/pages/home.html")).toEqual([
      "site",
      "pages",
      "home.html"
    ]);
  });

  it.each(["../secret", "site/../../secret", "/etc/passwd", "C:/secret"])(
    "rejects paths outside the campaign root: %s",
    (path) => expect(() => safePathSegments(path)).toThrow()
  );
});

describe("executeBrowserOperation", () => {
  it("does not classify lookalike free-text errors as audio artifact collisions", () => {
    expect(browserOperationError(
      new Error("an unrelated collision occurred while reading metadata")
    )).toBe("Error: an unrelated collision occurred while reading metadata");
  });

  it("dispatches an allowlisted inspect operation without artifact content bytes", async () => {
    const repository = receiptRepository();

    await expect(executeBrowserOperation(
      fakeDirectory().root,
      "inspect_audio_artifact",
      {
        generation_id: "generation-7",
        artifact_role: "wav",
        relative_path: "audio/recap.wav",
        folder_binding_id: "binding-1",
        approved_package_revision: 1
      },
      { repository, activeFolderBindingId: "binding-1" }
    )).resolves.toEqual({
      supported: true,
      outcome: { outcome: "success", result: { status: "missing" } }
    });
  });

  it("returns the durable receipt and measured duration for an owned WAV", async () => {
    const bytes = wavBytes();
    const write = await audioOperation(bytes, {
      operation_id: "audio-write:generation-7:r1:wav"
    });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(storedReceipt(write));

    await expect(executeBrowserOperation(
      fakeDirectory({ "audio/recap.wav": bytes }).root,
      "inspect_audio_artifact",
      {
        generation_id: write.generation_id,
        artifact_role: write.artifact_role,
        relative_path: write.relative_path,
        folder_binding_id: write.folder_binding_id,
        approved_package_revision: 2
      },
      { repository, activeFolderBindingId: write.folder_binding_id }
    )).resolves.toEqual({
      supported: true,
      outcome: {
        outcome: "success",
        result: {
          status: "owned",
          receipt: {
            generation_id: write.generation_id,
            artifact_role: write.artifact_role,
            relative_path: write.relative_path,
            content_hash: write.expected_content_hash,
            content_size: write.expected_content_size,
            package_revision: write.package_revision,
            operation_id: write.operation_id,
            folder_binding_id: write.folder_binding_id
          },
          duration_s: 1
        }
      }
    });
  });

  it("rejects an inspected artifact whose active file was tampered after receipt", async () => {
    const approved = wavBytes();
    const write = await audioOperation(approved);
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(storedReceipt(write));
    const tampered = new Uint8Array(approved);
    tampered[tampered.length - 1] = 1;

    const result = await executeBrowserOperation(
      fakeDirectory({ "audio/recap.wav": tampered }).root,
      "inspect_audio_artifact",
      {
        generation_id: write.generation_id,
        artifact_role: write.artifact_role,
        relative_path: write.relative_path,
        folder_binding_id: write.folder_binding_id,
        approved_package_revision: write.package_revision
      },
      { repository, activeFolderBindingId: write.folder_binding_id }
    );

    if (!result.supported || !result.outcome || result.outcome.outcome !== "error") {
      throw new Error("expected error");
    }
    expect(result.outcome.error.startsWith("audio_artifact_collision:")).toBe(true);
  });

  it("rejects inspection when the durable receipt belongs to another write operation", async () => {
    const bytes = wavBytes();
    const write = await audioOperation(bytes);
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(write, { operationId: "write-other" })
    );

    const result = await executeBrowserOperation(
      fakeDirectory({ "audio/recap.wav": bytes }).root,
      "inspect_audio_artifact",
      {
        generation_id: write.generation_id,
        artifact_role: write.artifact_role,
        relative_path: write.relative_path,
        folder_binding_id: write.folder_binding_id,
        approved_package_revision: write.package_revision
      },
      { repository, activeFolderBindingId: write.folder_binding_id }
    );

    if (!result.supported || !result.outcome || result.outcome.outcome !== "error") {
      throw new Error("expected error");
    }
    expect(result.outcome.error.startsWith("audio_artifact_collision:")).toBe(true);
  });

  it("rejects inspection when the approved current path differs from the receipt path", async () => {
    const bytes = wavBytes();
    const write = await audioOperation(bytes);
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(write, { relativePath: "audio/original.wav" })
    );

    const result = await executeBrowserOperation(
      fakeDirectory({ "audio/recap.wav": bytes }).root,
      "inspect_audio_artifact",
      {
        generation_id: write.generation_id,
        artifact_role: write.artifact_role,
        relative_path: write.relative_path,
        folder_binding_id: write.folder_binding_id,
        approved_package_revision: 2
      },
      { repository, activeFolderBindingId: write.folder_binding_id }
    );

    if (!result.supported || !result.outcome || result.outcome.outcome !== "error") {
      throw new Error("expected error");
    }
    expect(result.outcome.error.startsWith("audio_artifact_collision:")).toBe(true);
  });

  it("dispatches an approved create-only audio artifact and returns its receipt", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory();
    const repository = receiptRepository();

    const result = await executeBrowserOperation(
      root,
      "create_audio_artifact",
      operation,
      { repository, activeFolderBindingId: operation.folder_binding_id }
    );

    expect(result).toEqual({
      supported: true,
      outcome: {
        outcome: "success",
        result: expect.objectContaining({
          status: "created",
          receipt: expect.objectContaining({
            generation_id: operation.generation_id,
            artifact_role: operation.artifact_role,
            operation_id: operation.operation_id
          })
        })
      }
    });
  });

  it("settles malformed create-only audio arguments as a supported error", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { expected_content_size: _, ...malformed } = operation;

    await expect(executeBrowserOperation(
      fakeDirectory().root,
      "create_audio_artifact",
      malformed,
      {
        repository: receiptRepository(),
        activeFolderBindingId: operation.folder_binding_id
      }
    )).resolves.toEqual({
      supported: true,
      outcome: {
        outcome: "error",
        error: "TypeError: expected_content_size must be a non-negative safe integer"
      }
    });
  });

  it("maps create-only collisions to the stable exact error-code prefix", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);

    const result = await executeBrowserOperation(
      fakeDirectory({ "audio/recap.wav": bytes }).root,
      "create_audio_artifact",
      operation,
      {
        repository: receiptRepository(),
        activeFolderBindingId: operation.folder_binding_id
      }
    );

    expect(result).toMatchObject({
      supported: true,
      outcome: { outcome: "error" }
    });
    if (!result.supported || !result.outcome || result.outcome.outcome !== "error") {
      throw new Error("expected error");
    }
    expect(result.outcome.error.startsWith("audio_artifact_collision:")).toBe(true);
  });

  it("rejects non-positive package revisions before writing an audio artifact", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);

    for (const packageRevision of [0, -1]) {
      const { root, files } = fakeDirectory();
      const repository = receiptRepository();

      const result = await executeBrowserOperation(
        root,
        "create_audio_artifact",
        { ...operation, package_revision: packageRevision },
        {
          repository,
          activeFolderBindingId: operation.folder_binding_id
        }
      );

      expect(result).toEqual({
        supported: true,
        outcome: {
          outcome: "error",
          error: "TypeError: package_revision must be a positive safe integer"
        }
      });
      expect(files.size).toBe(0);
      expect(repository.saveAudioArtifactReceipt).not.toHaveBeenCalled();
    }
  });

  it("settles an audio operation for a different active binding as an error", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);

    await expect(executeBrowserOperation(
      fakeDirectory().root,
      "create_audio_artifact",
      operation,
      {
        repository: receiptRepository(),
        activeFolderBindingId: "binding-other"
      }
    )).resolves.toEqual({
      supported: true,
      outcome: {
        outcome: "error",
        error: "Error: audio artifact operation does not match the active folder binding"
      }
    });
  });

  it("leaves non-allowlisted operations pending for another executor", async () => {
    const result = await executeBrowserOperation(
      {} as FileSystemDirectoryHandle,
      "upload_recording",
      { session_id: "session-1" }
    );
    expect(result).toEqual({ supported: false });
  });

  it.each(["save_recording", "delete_file", "grep"])(
    "does not execute unsafe browser operation %s",
    async (operation) => {
      await expect(executeBrowserOperation(
        {} as FileSystemDirectoryHandle,
        operation,
        {}
      )).resolves.toEqual({ supported: false });
    }
  );

  it("rethrows filesystem permission failures so the operation remains pending", async () => {
    const root = {
      getFileHandle: async () => {
        throw new DOMException("Permission denied", "NotAllowedError");
      }
    } as unknown as FileSystemDirectoryHandle;

    await expect(executeBrowserOperation(root, "inspect_audio_artifact", {
      generation_id: "generation-1",
      artifact_role: "wav",
      relative_path: "secret.wav",
      folder_binding_id: "binding-1",
      approved_package_revision: 1
    }, { repository: receiptRepository(), activeFolderBindingId: "binding-1" }))
      .rejects.toMatchObject({ name: "NotAllowedError" });
  });
});

describe("create-only audio artifacts", () => {
  it("creates a missing artifact and stores its ownership receipt", async () => {
    const { root, files } = fakeDirectory();
    const repository = receiptRepository();
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);

    const result = await executeCreateOnlyAudioArtifact(
      root,
      repository,
      operation,
      operation.folder_binding_id
    );

    expect(result.status).toBe("created");
    expect(files.get("audio/recap.wav")).toEqual(bytes);
    expect(repository.saveAudioArtifactReceipt).toHaveBeenCalledWith(
      expect.objectContaining({
        generationId: "generation-7",
        artifactRole: "wav",
        relativePath: "audio/recap.wav",
        contentHash: operation.expected_content_hash,
        operationId: "audio-write:generation-7:r1:wav"
      })
    );
  });

  it("rejects a same-hash pre-existing file without an ownership receipt", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const { root } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    const operation = await audioOperation(bytes);

    await expect(executeCreateOnlyAudioArtifact(
      root,
      repository,
      operation,
      operation.folder_binding_id
    )).rejects.toThrow("collision");
    expect(repository.saveAudioArtifactReceipt).not.toHaveBeenCalled();
  });

  it("reuses a pre-existing file only when its ownership receipt matches", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root, files } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation)
    );

    await expect(
      executeCreateOnlyAudioArtifact(
        root,
        repository,
        operation,
        operation.folder_binding_id
      )
    ).resolves.toMatchObject({ status: "reused" });
    expect(files.get("audio/recap.wav")).toEqual(bytes);
    expect(repository.saveAudioArtifactReceipt).not.toHaveBeenCalled();
  });

  it("rejects a receipt owned by a different generation", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation, { generationId: "generation-other" })
    );

    await expect(
      executeCreateOnlyAudioArtifact(
        root,
        repository,
        operation,
        operation.folder_binding_id
      )
    ).rejects.toThrow("collision");
  });

  it("rejects an owned path whose observed file hash has changed", async () => {
    const approvedBytes = new TextEncoder().encode("RIFF-approved");
    const operation = await audioOperation(approvedBytes);
    const { root } = fakeDirectory({
      "audio/recap.wav": new TextEncoder().encode("RIFF-tampered")
    });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation)
    );

    await expect(
      executeCreateOnlyAudioArtifact(
        root,
        repository,
        operation,
        operation.folder_binding_id
      )
    ).rejects.toThrow("collision");
  });

  it("reports a missing revised destination so recovery writes only that artifact", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes, {
      operation_id: "write-wav-r2",
      relative_path: "audio/recap-r2.wav",
      package_revision: 2
    });
    const { root } = fakeDirectory();
    const repository = receiptRepository();

    await expect(
      inspectAudioArtifact(root, repository, operation, operation.folder_binding_id)
    ).resolves.toEqual({ status: "missing" });
    expect(repository.loadAudioArtifactReceipt).not.toHaveBeenCalled();
  });

  it("reports an owned active-handle artifact so recovery skips its write", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation)
    );

    await expect(
      inspectAudioArtifact(root, repository, operation, operation.folder_binding_id)
    ).resolves.toEqual({
      status: "owned",
      observedContentHash: operation.expected_content_hash,
      contentSize: operation.expected_content_size
    });
  });

  it("rejects inspection through a different active folder binding", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes, {
      folder_binding_id: "binding-b"
    });
    const { root } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation, { folderBindingId: "binding-a" })
    );

    await expect(
      inspectAudioArtifact(root, repository, operation, operation.folder_binding_id)
    ).rejects.toThrow("collision");
  });

  it("rejects create content that does not match the approved hash", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const { root, files } = fakeDirectory();
    const repository = receiptRepository();
    const operation = await audioOperation(bytes, {
      expected_content_hash: "0".repeat(64)
    });

    await expect(
      executeCreateOnlyAudioArtifact(
        root,
        repository,
        operation,
        operation.folder_binding_id
      )
    ).rejects.toThrow("content hash");
    expect(files.size).toBe(0);
  });

  it("rejects an approved artifact when the active handle binding differs", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory({ "audio/recap.wav": bytes });
    const repository = receiptRepository();
    vi.mocked(repository.loadAudioArtifactReceipt).mockResolvedValue(
      storedReceipt(operation)
    );

    await expect(
      inspectAudioArtifact(root, repository, operation, "binding-b")
    ).rejects.toThrow("active folder binding");
  });

  it("returns the workflow-facing ownership receipt for a created artifact", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory();

    const result = await executeCreateOnlyAudioArtifact(
      root,
      receiptRepository(),
      operation,
      operation.folder_binding_id
    );

    expect(result.receipt).toEqual({
      generation_id: operation.generation_id,
      artifact_role: operation.artifact_role,
      relative_path: operation.relative_path,
      content_hash: operation.expected_content_hash,
      content_size: operation.expected_content_size,
      package_revision: operation.package_revision,
      operation_id: operation.operation_id,
      folder_binding_id: operation.folder_binding_id
    });
  });

  it("verifies the observed file hash after the browser write", async () => {
    const bytes = new TextEncoder().encode("RIFF-audio");
    const operation = await audioOperation(bytes);
    const { root } = fakeDirectory({}, () =>
      new TextEncoder().encode("RIFF-corrupt")
    );
    const repository = receiptRepository();

    await expect(executeCreateOnlyAudioArtifact(
      root,
      repository,
      operation,
      operation.folder_binding_id
    )).rejects.toThrow("observed file hash");
    expect(repository.saveAudioArtifactReceipt).not.toHaveBeenCalled();
  });
});
