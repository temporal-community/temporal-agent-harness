import { describe, expect, it, vi } from "vitest";
import {
  drainStoredOutcomes,
  fulfillOperations,
  operationFingerprint,
  outcomeKey,
  reconcileSettledOutcomes
} from "./engine";
import type {
  BrowserBridgeApi,
  DirectoryHandleRepository,
  LocalOperation,
  StoredLocalOperationOutcome
} from "./types";

const operation: LocalOperation = {
  operation_id: "job/operation-1",
  bridge_id: "browser-1",
  root_id: "root-1",
  kind: "inspect_audio_artifact",
  arguments: {
    generation_id: "generation-1",
    artifact_role: "wav",
    relative_path: "audio/recap.wav",
    folder_binding_id: "binding-1",
    approved_package_revision: 1
  },
  idempotency_key: "job/operation-1",
  output_schema: { type: "object" }
};

function repositoryWith(outcome: StoredLocalOperationOutcome | null) {
  const values = new Map<string, StoredLocalOperationOutcome>();
  if (outcome) values.set(outcome.key, outcome);
  const repository: DirectoryHandleRepository = {
    getBridgeId: vi.fn(async () => "browser-1"),
    saveBinding: vi.fn(async () => undefined),
    loadDirectory: vi.fn(async () => null),
    saveDirectory: vi.fn(async () => ({ rootId: "root-1", handleBindingId: "binding-1" })),
    countOutcomes: vi.fn(async (handleBindingId?: string) =>
      [...values.values()].filter((value) =>
        !handleBindingId || value.handleBindingId === handleBindingId
      ).length
    ),
    listOutcomes: vi.fn(async (handleBindingId?: string) =>
      [...values.values()].filter((value) =>
        !handleBindingId || value.handleBindingId === handleBindingId
      )
    ),
    loadOutcome: vi.fn(async (key) => values.get(key) ?? null),
    saveOutcome: vi.fn(async (value) => { values.set(value.key, value); }),
    removeOutcome: vi.fn(async (key) => { values.delete(key); })
  };
  return repository;
}

describe("fulfillOperations", () => {
  it("leaves audio operations for another folder binding unclaimed", async () => {
    const repository = Object.assign(repositoryWith(null), {
      loadAudioArtifactReceipt: vi.fn(async () => null),
      saveAudioArtifactReceipt: vi.fn(async () => undefined),
      listAudioArtifactReceipts: vi.fn(async () => []),
      removeAudioArtifactReceipt: vi.fn(async () => undefined)
    });
    const forOtherBinding: LocalOperation = {
      ...operation,
      operation_id: "job/binding-b",
      idempotency_key: "job/binding-b",
      arguments: { ...operation.arguments, folder_binding_id: "binding-B" }
    };
    const forActiveBinding: LocalOperation = {
      ...operation,
      operation_id: "job/binding-a",
      idempotency_key: "job/binding-a",
      arguments: { ...operation.arguments, folder_binding_id: "binding-A" }
    };
    const submitResult = vi.fn(async (_workflowId, operationId) => ({
      operation_id: operationId,
      accepted: true
    }));
    const getDirectoryHandle = vi.fn(async () => {
      throw new DOMException("Missing", "NotFoundError");
    });

    const summary = await fulfillOperations(
      { listOperations: vi.fn(async () => []), submitResult },
      repository,
      { getDirectoryHandle } as unknown as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-A",
      [forOtherBinding, forActiveBinding]
    );

    expect(summary).toEqual({ completed: 1, unsupported: [] });
    expect(getDirectoryHandle).toHaveBeenCalledTimes(1);
    expect(repository.loadOutcome).toHaveBeenCalledTimes(1);
    expect(submitResult).toHaveBeenCalledTimes(1);
    expect(submitResult).toHaveBeenCalledWith(
      "workflow-1",
      forActiveBinding.operation_id,
      expect.anything(),
      undefined
    );
  });

  it("does not resubmit a durable audio outcome through a different folder binding", async () => {
    const mismatched: LocalOperation = {
      ...operation,
      arguments: { ...operation.arguments, folder_binding_id: "binding-B" }
    };
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-A", mismatched),
      workflowId: "workflow-1",
      operationId: mismatched.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-A",
      idempotencyKey: mismatched.idempotency_key,
      operationFingerprint: operationFingerprint(mismatched),
      outcome: { outcome: "success", result: "created in another browser" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const submitResult = vi.fn(async () => ({
      operation_id: mismatched.operation_id,
      accepted: true
    }));

    await expect(fulfillOperations(
      { listOperations: vi.fn(async () => []), submitResult },
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-A",
      [mismatched]
    )).resolves.toEqual({ completed: 0, unsupported: [] });

    expect(submitResult).not.toHaveBeenCalled();
    expect(repository.removeOutcome).not.toHaveBeenCalled();
  });

  it("injects the active receipt context when fulfilling an inspect audio operation", async () => {
    const repository = Object.assign(repositoryWith(null), {
      loadAudioArtifactReceipt: vi.fn(async () => null),
      saveAudioArtifactReceipt: vi.fn(async () => undefined),
      listAudioArtifactReceipts: vi.fn(async () => []),
      removeAudioArtifactReceipt: vi.fn(async () => undefined)
    });
    const inspect: LocalOperation = {
      ...operation,
      kind: "inspect_audio_artifact",
      arguments: {
        generation_id: "generation-1",
        artifact_role: "wav",
        relative_path: "audio/recap.wav",
        folder_binding_id: "binding-1",
        approved_package_revision: 1
      }
    };
    const submitResult = vi.fn(async (_workflowId, operationId) => ({
      operation_id: operationId,
      accepted: true
    }));
    const root = {
      getDirectoryHandle: async () => { throw new DOMException("Missing", "NotFoundError"); }
    } as unknown as FileSystemDirectoryHandle;

    await fulfillOperations(
      { listOperations: vi.fn(async () => []), submitResult },
      repository,
      root,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [inspect]
    );

    expect(submitResult).toHaveBeenCalledWith(
      "workflow-1",
      inspect.operation_id,
      expect.objectContaining({
        outcome: "success",
        result: { status: "missing" }
      }),
      undefined
    );
  });

  it("resubmits a durable outcome without rerunning the filesystem operation", async () => {
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success", result: "wrote 11 characters" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const submitResult = vi.fn(async () => ({
      operation_id: operation.operation_id,
      accepted: true as const
    }));
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult
    };

    const summary = await fulfillOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [operation]
    );

    expect(summary).toEqual({ completed: 1, unsupported: [] });
    expect(repository.saveOutcome).not.toHaveBeenCalled();
    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
    expect(submitResult).toHaveBeenCalledWith(
      "workflow-1",
      operation.operation_id,
      {
        bridge_id: "browser-1",
        root_id: "root-1",
        outcome: "success",
        result: "wrote 11 characters"
      },
      undefined
    );
  });

  it("does not consume an unsupported operation", async () => {
    const repository = repositoryWith(null);
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn(async () => ({
        operation_id: operation.operation_id,
        accepted: true
      }))
    };
    const unsupported = {
      ...operation,
      kind: "upload_recording",
      arguments: { ...operation.arguments, folder_binding_id: "binding-other" }
    };

    const summary = await fulfillOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [unsupported]
    );

    expect(summary).toEqual({ completed: 0, unsupported: ["upload_recording"] });
    expect(api.submitResult).not.toHaveBeenCalled();
    expect(repository.saveOutcome).not.toHaveBeenCalled();
  });

  it("stops claiming operations from the current snapshot when cancellation becomes pending", async () => {
    const secondOperation = {
      ...operation,
      operation_id: "job/operation-2",
      idempotency_key: "job/operation-2"
    };
    const first = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success" as const, result: "first" },
      createdAt: 1
    };
    const second = {
      ...first,
      key: outcomeKey("workflow-1", "root-1", "binding-1", secondOperation),
      operationId: secondOperation.operation_id,
      idempotencyKey: secondOperation.idempotency_key,
      operationFingerprint: operationFingerprint(secondOperation),
      outcome: { outcome: "success" as const, result: "second" }
    };
    const repository = {
      ...repositoryWith(null),
      loadOutcome: vi.fn(async (key: string) =>
        key === first.key ? first : key === second.key ? second : null
      )
    };
    let cancellationPending = false;
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn(async (workflowId, operationId) => {
        cancellationPending = true;
        return { operation_id: operationId, accepted: true };
      })
    };

    await fulfillOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [operation, secondOperation],
      undefined,
      () => cancellationPending
    );

    expect(api.submitResult).toHaveBeenCalledTimes(1);
    expect(api.submitResult).toHaveBeenCalledWith(
      "workflow-1",
      operation.operation_id,
      expect.anything(),
      undefined
    );
  });

  it("retains a durable outcome when submission is rejected", async () => {
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success", result: "wrote 11 characters" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn(async () => {
        throw new Error("The pending operation belongs to a different bridge or root.");
      })
    };

    await expect(fulfillOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [operation]
    )).rejects.toThrow("different bridge or root");

    expect(repository.removeOutcome).not.toHaveBeenCalled();
    expect(repository.saveOutcome).not.toHaveBeenCalled();
  });

  it("rejects semantic reuse of an idempotency key within one folder binding", async () => {
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success", result: "old result" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const changed = {
      ...operation,
      arguments: {
        path: "site/index.html",
        content: "different content",
        folder_binding_id: "binding-1"
      }
    };
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn()
    };

    await expect(fulfillOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [changed]
    )).rejects.toThrow("reused for different operation semantics");
    expect(api.submitResult).not.toHaveBeenCalled();
  });

  it("clears an outbox result once the workflow no longer reports it pending", async () => {
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success", result: "already accepted" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);

    await expect(reconcileSettledOutcomes(
      repository,
      "workflow-1",
      "root-1",
      "binding-1",
      []
    )).resolves.toBe(1);
    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
  });

  it("clears a stale outbox result when the pending audio operation targets another binding", async () => {
    const mismatched: LocalOperation = {
      ...operation,
      arguments: { ...operation.arguments, folder_binding_id: "binding-B" }
    };
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-A", mismatched),
      workflowId: "workflow-1",
      operationId: mismatched.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-A",
      idempotencyKey: mismatched.idempotency_key,
      operationFingerprint: operationFingerprint(mismatched),
      outcome: { outcome: "success", result: "wrong binding" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);

    await expect(reconcileSettledOutcomes(
      repository,
      "workflow-1",
      "root-1",
      "binding-A",
      [mismatched]
    )).resolves.toBe(1);

    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
  });
});

describe("cancellation outbox drain", () => {
  it("discards a legacy audio outcome targeting a different folder binding without submitting it", async () => {
    const mismatched: LocalOperation = {
      ...operation,
      arguments: { ...operation.arguments, folder_binding_id: "binding-B" }
    };
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-A", mismatched),
      workflowId: "workflow-1",
      operationId: mismatched.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-A",
      idempotencyKey: mismatched.idempotency_key,
      operationFingerprint: operationFingerprint(mismatched),
      outcome: { outcome: "success", result: "created through the wrong browser" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const submitResult = vi.fn();

    await expect(drainStoredOutcomes(
      { listOperations: vi.fn(async () => []), submitResult },
      repository,
      "browser-1",
      "root-1",
      "binding-A"
    )).resolves.toBe(1);

    expect(submitResult).not.toHaveBeenCalled();
    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
  });

  it("resubmits and removes durable outcomes before cancellation continues", async () => {
    const stored: StoredLocalOperationOutcome = {
      key: outcomeKey("workflow-1", "root-1", "binding-1", operation),
      workflowId: "workflow-1",
      operationId: operation.operation_id,
      rootId: "root-1",
      handleBindingId: "binding-1",
      idempotencyKey: operation.idempotency_key,
      operationFingerprint: operationFingerprint(operation),
      outcome: { outcome: "success", result: "created" },
      createdAt: 1
    };
    const repository = repositoryWith(stored);
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn(async () => ({
        operation_id: operation.operation_id,
        accepted: true
      }))
    };

    await expect(drainStoredOutcomes(
      api,
      repository,
      "browser-1",
      "root-1",
      "binding-1"
    )).resolves.toBe(1);

    expect(api.submitResult).toHaveBeenCalledWith(
      "workflow-1",
      operation.operation_id,
      {
        bridge_id: "browser-1",
        root_id: "root-1",
        outcome: "success",
        result: "created"
      },
      undefined
    );
    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
  });
});

describe("audio operation fulfillment", () => {
  it("executes audio writes with the active binding and durable receipt repository", async () => {
    const files = new Map<string, Uint8Array>();
    const fileBuffer = (bytes: Uint8Array): ArrayBuffer => {
      const buffer = new ArrayBuffer(bytes.byteLength);
      new Uint8Array(buffer).set(bytes);
      return buffer;
    };
    const directory = (prefix = ""): FileSystemDirectoryHandle => ({
      kind: "directory",
      name: "campaign",
      getDirectoryHandle: async (name: string) => directory(`${prefix}${name}/`),
      getFileHandle: async (name: string, options?: { create?: boolean }) => {
        const path = `${prefix}${name}`;
        if (!files.has(path) && !options?.create) {
          throw new DOMException("Missing", "NotFoundError");
        }
        return {
          kind: "file",
          name,
          getFile: async () => new Blob([
            fileBuffer(files.get(path) ?? new Uint8Array())
          ]) as File,
          createWritable: async () => ({
            write: async (value: BufferSource) => {
              const view = ArrayBuffer.isView(value)
                ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
                : new Uint8Array(value);
              files.set(path, new Uint8Array(view));
            },
            close: async () => undefined
          }) as FileSystemWritableFileStream
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
    const repository = {
      ...repositoryWith(null),
      loadAudioArtifactReceipt: vi.fn(async () => null),
      saveAudioArtifactReceipt: vi.fn(async () => undefined),
      listAudioArtifactReceipts: vi.fn(async () => []),
      removeAudioArtifactReceipt: vi.fn(async () => undefined)
    };
    const audioOperation: LocalOperation = {
      ...operation,
      operation_id: "audio-write-1",
      idempotency_key: "audio-write-1",
      kind: "create_audio_artifact",
      arguments: {
        operation_id: "audio-write-1",
        generation_id: "generation-1",
        artifact_role: "wav",
        relative_path: "audio/recap.wav",
        content_base64: "",
        expected_content_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        expected_content_size: 0,
        folder_binding_id: "binding-1",
        package_revision: 1
      }
    };
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn(async () => ({
        operation_id: audioOperation.operation_id,
        accepted: true
      }))
    };

    await expect(fulfillOperations(
      api,
      repository,
      directory(),
      "workflow-1",
      "browser-1",
      "root-1",
      "binding-1",
      [audioOperation]
    )).resolves.toEqual({ completed: 1, unsupported: [] });

    expect(repository.saveAudioArtifactReceipt).toHaveBeenCalledWith(
      expect.objectContaining({ folderBindingId: "binding-1" })
    );
    expect(api.submitResult).toHaveBeenCalledWith(
      "workflow-1",
      "audio-write-1",
      expect.objectContaining({
        outcome: "success",
        result: expect.objectContaining({ status: "created" })
      }),
      undefined
    );
  });
});
