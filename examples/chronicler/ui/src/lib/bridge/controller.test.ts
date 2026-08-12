import { describe, expect, it, vi } from "vitest";
import {
  assertDirectoryRebindSafe,
  BrowserBridgeController,
  canRebindDirectory,
  isMissingDiscoveryPlaceholder,
  processWorkflowOperations
} from "./controller";
import { WorkflowNotFoundError } from "./api";
import { operationFingerprint, outcomeKey } from "./engine";
import type {
  BrowserBridgeApi,
  DirectoryHandleRepository,
  LocalOperation,
  StoredLocalOperationOutcome
} from "./types";

const operation: LocalOperation = {
  operation_id: "job/1",
  bridge_id: "browser-local",
  root_id: "campaign-root",
  kind: "inspect_audio_artifact",
  arguments: {
    generation_id: "generation-1",
    artifact_role: "wav",
    relative_path: "audio/recap.wav",
    folder_binding_id: "binding-1",
    approved_package_revision: 1
  },
  idempotency_key: "job/1",
  output_schema: { type: "object" }
};

function repositoryWith(outcome: StoredLocalOperationOutcome) {
  const outcomes = new Map([[outcome.key, outcome]]);
  return {
    getBridgeId: vi.fn(async () => "browser-local"),
    saveBinding: vi.fn(async () => undefined),
    loadDirectory: vi.fn(async () => null),
    saveDirectory: vi.fn(async () => ({
      rootId: "campaign-root",
      handleBindingId: "binding-1"
    })),
    countOutcomes: vi.fn(async () => outcomes.size),
    listOutcomes: vi.fn(async () => [...outcomes.values()]),
    loadOutcome: vi.fn(async (key) => outcomes.get(key) ?? null),
    saveOutcome: vi.fn(async (value) => { outcomes.set(value.key, value); }),
    removeOutcome: vi.fn(async (key) => { outcomes.delete(key); })
  } satisfies DirectoryHandleRepository;
}

function storedOutcome(): StoredLocalOperationOutcome {
  return {
    key: outcomeKey("workflow/1", "campaign-root", "binding-1", operation),
    workflowId: "workflow/1",
    operationId: operation.operation_id,
    rootId: "campaign-root",
    handleBindingId: "binding-1",
    idempotencyKey: operation.idempotency_key,
    operationFingerprint: operationFingerprint(operation),
    outcome: { outcome: "success", result: "saved" },
    createdAt: 1
  };
}

function authorityHarness(options: {
  immediateLockAttempts?: number;
  deferredLockAttemptNumbers?: number[];
  files?: Record<string, string>;
} = {}) {
  const files = options.files ?? {};
  const directory = (prefix = ""): FileSystemDirectoryHandle => ({
    kind: "directory",
    name: "campaign",
    getDirectoryHandle: async (name: string) => directory(`${prefix}${name}/`),
    getFileHandle: async (name: string, handleOptions?: { create?: boolean }) => {
      const path = `${prefix}${name}`;
      if (handleOptions?.create === false && !(path in files)) {
        throw new DOMException("File not found", "NotFoundError");
      }
      return {
        kind: "file",
        name,
        getFile: async () => new Blob([files[path]]) as File
      } as FileSystemFileHandle;
    },
    queryPermission: vi.fn(async () => "granted" as PermissionState),
    requestPermission: vi.fn(async () => "granted" as PermissionState),
    entries: async function* () {},
    values: async function* () {},
    isSameEntry: async () => false,
    removeEntry: async () => undefined,
    resolve: async () => null
  });
  const handle = directory();
  const repository = {
    ...repositoryWith(storedOutcome()),
    loadDirectory: vi.fn(async () => ({
      handle,
      rootId: "campaign-root",
      handleBindingId: "binding-1"
    })),
    listOutcomes: vi.fn(async () => []),
    countOutcomes: vi.fn(async () => 0)
  } satisfies DirectoryHandleRepository;
  const deferredLocks: Array<() => Promise<boolean>> = [];
  const immediateLockAttempts = options.immediateLockAttempts ?? Number.POSITIVE_INFINITY;
  const deferredLockAttemptNumbers = new Set(options.deferredLockAttemptNumbers ?? []);
  const locks = {
    request: vi.fn(async (
      _name: string,
      _options: { ifAvailable: true },
      callback: (lock: object | null) => Promise<boolean>
    ) => {
      if (
        locks.request.mock.calls.length <= immediateLockAttempts
        && !deferredLockAttemptNumbers.has(locks.request.mock.calls.length)
      ) return callback({});
      return new Promise<boolean>((resolve, reject) => {
        deferredLocks.push(async () => {
          try {
            const result = await callback({});
            resolve(result);
            return result;
          } catch (error) {
            reject(error);
            throw error;
          }
        });
      });
    })
  };
  vi.stubGlobal("window", {
    indexedDB: {},
    showDirectoryPicker: vi.fn(),
    setTimeout,
    clearTimeout
  });
  vi.stubGlobal("navigator", { locks });
  const transport = {
    cancelAudioGeneration: vi.fn(async () => undefined),
    getAudioGenerationStatus: vi.fn(async (childWorkflowId: string) => ({
      childWorkflowId,
      state: "canceled" as const
    }))
  };
  const controller = new BrowserBridgeController(
    vi.fn(),
    { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
    repository,
    { activeWorkflowIds: vi.fn(async () => []) } as never,
    transport
  );
  return {
    controller,
    deferredLocks,
    locks,
    cleanup: () => {
      controller.stop();
      vi.unstubAllGlobals();
    }
  };
}

describe("directory rebinding guard", () => {
  it("allows rebinding only after pending, running, and stored work drains", () => {
    expect(canRebindDirectory({
      pendingCount: 0,
      inFlightCount: 0,
      outboxCount: 0
    })).toBe(true);
  });

  it.each([
    { pendingCount: 1, inFlightCount: 0, outboxCount: 0 },
    { pendingCount: 0, inFlightCount: 1, outboxCount: 0 },
    { pendingCount: 0, inFlightCount: 0, outboxCount: 1 }
  ])("blocks unsafe rebinding for state %j", (state) => {
    expect(canRebindDirectory(state)).toBe(false);
    expect(() => assertDirectoryRebindSafe(state)).toThrow(
      "must settle before changing folders"
    );
  });
});

describe("predicted workflow discovery", () => {
  it("ignores an unstarted predicted child only when no durable outbox targets it", () => {
    const error = new WorkflowNotFoundError("audio-1", "not found");

    expect(isMissingDiscoveryPlaceholder(error, "audio-1", new Set())).toBe(true);
    expect(isMissingDiscoveryPlaceholder(error, "audio-1", new Set(["audio-1"]))).toBe(false);
    expect(isMissingDiscoveryPlaceholder(new Error("not found"), "audio-1", new Set())).toBe(false);
  });
});

describe("workflow outbox reconciliation", () => {
  it("retains the durable outbox when the workflow snapshot is temporarily unavailable", async () => {
    const stored = storedOutcome();
    const repository = repositoryWith(stored);
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => { throw new Error("Workflow was not found."); }),
      submitResult: vi.fn()
    };

    await expect(processWorkflowOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow/1",
      "browser-local",
      "campaign-root",
      "binding-1"
    )).rejects.toThrow("was not found");

    expect(repository.removeOutcome).not.toHaveBeenCalled();
    await expect(repository.loadOutcome(stored.key)).resolves.toEqual(stored);
  });

  it("clears the durable outbox after an authoritative settled acknowledgement", async () => {
    const stored = storedOutcome();
    const repository = repositoryWith(stored);
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => [operation]),
      submitResult: vi.fn(async () => ({
        operation_id: operation.operation_id,
        accepted: false,
        settled: true
      }))
    };

    await expect(processWorkflowOperations(
      api,
      repository,
      {} as FileSystemDirectoryHandle,
      "workflow/1",
      "browser-local",
      "campaign-root",
      "binding-1"
    )).resolves.toMatchObject({ completed: 1 });

    expect(repository.removeOutcome).toHaveBeenCalledWith(stored.key);
    await expect(repository.loadOutcome(stored.key)).resolves.toBeNull();
  });
});

describe("shared audio cancellation runtime", () => {
  it("rejects destination preflight while the bridge is disconnected", async () => {
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
      repositoryWith(storedOutcome()),
      { activeWorkflowIds: vi.fn(async () => []) } as never
    );

    await expect(controller.preflightAudioDestinations(
      ["audio/recap.wav"],
      "binding-1"
    )).rejects.toThrow("active browser bridge tab");
  });

  it("rejects destination preflight for a different folder binding", async () => {
    const harness = authorityHarness();
    await harness.controller.initialize();

    await expect(harness.controller.preflightAudioDestinations(
      ["audio/recap.wav"],
      "binding-other"
    )).rejects.toThrow("folder binding");
    harness.cleanup();
  });

  it("rejects destination preflight when an exact proposed path already exists", async () => {
    const harness = authorityHarness({ files: { "audio/recap.wav": "existing" } });
    await harness.controller.initialize();

    await expect(harness.controller.preflightAudioDestinations(
      ["audio/recap.wav"],
      "binding-1"
    )).rejects.toThrow("already exists");
    harness.cleanup();
  });

  it("rejects cancellation immediately after stop without orphaning a command", async () => {
    const harness = authorityHarness();
    await harness.controller.initialize();

    harness.controller.stop();

    expect(harness.controller.status.phase).not.toBe("connected");
    await expect(harness.controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).rejects.toThrow("active browser bridge tab");
    harness.cleanup();
  });

  it("rejects cancellation while reconnect is waiting to reacquire leadership", async () => {
    const harness = authorityHarness({ immediateLockAttempts: 1 });
    await harness.controller.initialize();

    await harness.controller.reconnect();

    expect(harness.controller.status.phase).not.toBe("connected");
    await expect(harness.controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).rejects.toThrow("active browser bridge tab");
    expect(harness.deferredLocks).toHaveLength(1);
    harness.cleanup();
  });

  it("does not advertise authority after restart until the replacement lock begins", async () => {
    const harness = authorityHarness({ immediateLockAttempts: 1 });
    await harness.controller.initialize();
    harness.controller.stop();

    await harness.controller.initialize();

    expect(harness.controller.status.phase).not.toBe("connected");
    await expect(harness.controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).rejects.toThrow("active browser bridge tab");
    await expect(harness.controller.readChroniclerTranscriptSource("session-1"))
      .rejects.toThrow("active browser bridge tab");
    expect(harness.deferredLocks).toHaveLength(1);
    harness.cleanup();
  });

  it("ignores a stale lock callback released after leadership was stopped", async () => {
    const harness = authorityHarness({ immediateLockAttempts: 0 });
    await harness.controller.initialize();
    expect(harness.deferredLocks).toHaveLength(1);

    harness.controller.stop();
    harness.deferredLocks[0]();
    await Promise.resolve();
    await Promise.resolve();

    expect(harness.controller.status.phase).not.toBe("connected");
    await expect(harness.controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).rejects.toThrow("active browser bridge tab");
    await expect(harness.controller.readChroniclerTranscriptSource("session-1"))
      .rejects.toThrow("active browser bridge tab");
    harness.cleanup();
  });

  it("does not let a stale leadership loop clear replacement authority", async () => {
    const harness = authorityHarness({
      deferredLockAttemptNumbers: [1],
      files: {
        "sessions.json": JSON.stringify({
          sessions: [{
            session_id: "session-1",
            campaign_id: "campaign-1",
            title: "The Black Bell",
            recorded_at: "2026-08-01",
            number: 1,
            audio_file: "session-1.wav"
          }]
        }),
        "transcripts/session-1.json": JSON.stringify({
          session_id: "session-1",
          model: "gemini-2.5-flash",
          duration_s: 90,
          full_text: "The party crossed the silent bridge.",
          segments: [{
            speaker: "GM",
            start_s: 0,
            end_s: 3,
            text: "The party crossed the silent bridge."
          }]
        })
      }
    });
    await harness.controller.initialize();
    expect(harness.deferredLocks).toHaveLength(1);

    harness.controller.stop();
    await harness.controller.initialize();
    expect(harness.controller.status.phase).toBe("connected");

    await harness.deferredLocks[0]();
    await Promise.resolve();

    expect(harness.controller.status.phase).toBe("connected");
    await expect(harness.controller.readChroniclerTranscriptSource("session-1"))
      .resolves.toMatchObject({ folderBindingId: "binding-1" });
    harness.cleanup();
  });

  it("drains an in-flight claim before reconnecting leadership", async () => {
    let settleClaim!: () => void;
    const handle = {
      name: "campaign",
      queryPermission: vi.fn(async () => "granted" as PermissionState),
      requestPermission: vi.fn(async () => "granted" as PermissionState)
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-1"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    const listOperations = vi.fn(() => {
      if (listOperations.mock.calls.length === 1) {
        return new Promise<LocalOperation[]>((resolve) => {
          settleClaim = () => resolve([]);
        });
      }
      return Promise.resolve([]);
    });
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => callback({}))
    };
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", { locks });
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations, submitResult: vi.fn() },
      repository,
      { activeWorkflowIds: vi.fn(async () => ["workflow/1"]) } as never,
      {
        cancelAudioGeneration: vi.fn(async () => undefined),
        getAudioGenerationStatus: vi.fn()
      }
    );
    await controller.initialize();
    await Promise.resolve();
    const cancellationRejection = controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    ).catch((error) => error);
    let reconnectSettled = false;
    const reconnect = controller.reconnect().then(() => {
      reconnectSettled = true;
    });
    await Promise.resolve();
    await Promise.resolve();

    await expect(cancellationRejection).resolves.toMatchObject({ name: "AbortError" });
    expect(reconnectSettled).toBe(false);
    expect(locks.request).toHaveBeenCalledTimes(1);
    expect(listOperations).toHaveBeenCalledTimes(1);

    settleClaim();
    await reconnect;
    await Promise.resolve();

    expect(locks.request).toHaveBeenCalledTimes(2);
    expect(listOperations).toHaveBeenCalledTimes(2);
    controller.stop();
    vi.unstubAllGlobals();
  });

  it("enables cancellation and source access once the replacement lock is held", async () => {
    const harness = authorityHarness({
      immediateLockAttempts: 1,
      files: {
        "sessions.json": JSON.stringify({
          sessions: [{
            session_id: "session-1",
            campaign_id: "campaign-1",
            title: "The Black Bell",
            recorded_at: "2026-08-01",
            number: 1,
            audio_file: "session-1.wav"
          }]
        }),
        "transcripts/session-1.json": JSON.stringify({
          session_id: "session-1",
          model: "gemini-2.5-flash",
          duration_s: 90,
          full_text: "The party crossed the silent bridge.",
          segments: [{
            speaker: "GM",
            start_s: 0,
            end_s: 3,
            text: "The party crossed the silent bridge."
          }]
        })
      }
    });
    await harness.controller.initialize();
    harness.controller.stop();
    await harness.controller.initialize();

    harness.deferredLocks[0]();
    await Promise.resolve();
    await Promise.resolve();

    expect(harness.controller.status.phase).toBe("connected");
    await expect(harness.controller.readChroniclerTranscriptSource("session-1"))
      .resolves.toMatchObject({ folderBindingId: "binding-1" });
    await expect(harness.controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).resolves.toMatchObject({ state: "canceled" });
    harness.cleanup();
  });

  it("queues cancellation through the controller's active held-lock loop", async () => {
    const handle = {
      name: "campaign",
      queryPermission: vi.fn(async () => "granted")
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-1"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    const api: BrowserBridgeApi = {
      listOperations: vi.fn(async () => []),
      submitResult: vi.fn()
    };
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => callback({}))
    };
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", { locks });
    const discovery = {
      activeWorkflowIds: vi.fn(async () => [])
    };
    let controller!: BrowserBridgeController;
    const transport = {
      cancelAudioGeneration: vi.fn(async () => undefined),
      getAudioGenerationStatus: vi.fn(async (childWorkflowId: string) => {
        return { childWorkflowId, state: "canceled" as const };
      })
    };
    controller = new BrowserBridgeController(
      vi.fn(),
      api,
      repository,
      discovery as never,
      transport
    );

    await controller.initialize();
    await expect(controller.cancelAudioGeneration(
      "chronicler-audio--chat-1"
    )).resolves.toEqual({
      childWorkflowId: "chronicler-audio--chat-1",
      state: "canceled"
    });

    expect(locks.request).toHaveBeenCalledTimes(1);
    expect(transport.cancelAudioGeneration).toHaveBeenCalledTimes(1);
    controller.stop();
    vi.unstubAllGlobals();
  });

  it("uses the HTTP audio transport by default", async () => {
    const handle = {
      name: "campaign",
      queryPermission: vi.fn(async () => "granted" as PermissionState)
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-1"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => callback({}))
    };
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent/session-1",
      state: "canceled",
      status: {
        generation_id: "generation-1",
        child_workflow_id: "chronicler-audio--agent/session-1",
        phase: "canceled",
        detail: "Cancellation completed."
      },
      result: null,
      receipts: []
    };
    const fetch = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit
    ) => new Response(JSON.stringify(snapshot), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", { locks });
    vi.stubGlobal("fetch", fetch);
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
      repository,
      { activeWorkflowIds: vi.fn(async () => []) } as never
    );

    await controller.initialize();
    await expect(controller.cancelAudioGeneration(
      "chronicler-audio--agent/session-1"
    )).resolves.toMatchObject({ state: "canceled" });

    expect(fetch.mock.calls.map(([input]) => String(input))).toEqual([
      "api/chronicler/audio/cancel",
      expect.stringContaining("api/chronicler/audio/status?")
    ]);
    controller.stop();
    vi.unstubAllGlobals();
  });

  it("waits for stopped leadership to exit before reconnecting", async () => {
    const handle = {
      name: "campaign",
      queryPermission: vi.fn(async () => "granted"),
      requestPermission: vi.fn(async () => "granted")
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-1"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    let activeLocks = 0;
    let maxActiveLocks = 0;
    const locks = {
      request: vi.fn(async (
        _name: string,
        _options: { ifAvailable: true },
        callback: (lock: object | null) => Promise<boolean>
      ) => {
        activeLocks += 1;
        maxActiveLocks = Math.max(maxActiveLocks, activeLocks);
        try {
          return await callback({});
        } finally {
          activeLocks -= 1;
        }
      })
    };
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", { locks });
    let settleDiscovery!: () => void;
    const activeWorkflowIds = vi.fn(() => {
      if (activeWorkflowIds.mock.calls.length === 1) {
        return new Promise<string[]>((resolve) => {
          settleDiscovery = () => resolve([]);
        });
      }
      return Promise.resolve([]);
    });
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
      repository,
      {
        activeWorkflowIds
      } as never
    );

    await controller.initialize();
    controller.stop();
    let reconnectSettled = false;
    const reconnect = controller.reconnect().then(() => {
      reconnectSettled = true;
    });
    await Promise.resolve();

    expect(reconnectSettled).toBe(false);
    expect(locks.request).toHaveBeenCalledTimes(1);
    settleDiscovery();
    await reconnect;
    await Promise.resolve();

    expect(locks.request).toHaveBeenCalledTimes(2);
    expect(maxActiveLocks).toBe(1);
    controller.stop();
    vi.unstubAllGlobals();
  });
});

describe("controller-owned Chronicler sources", () => {
  it("rejects source read and discovery immediately after stop", async () => {
    const harness = authorityHarness();
    await harness.controller.initialize();

    harness.controller.stop();

    await expect(harness.controller.readChroniclerTranscriptSource("session-1"))
      .rejects.toThrow("active browser bridge tab");
    await expect(harness.controller.discoverChroniclerTranscriptSources())
      .rejects.toThrow("active browser bridge tab");
    harness.cleanup();
  });

  it("reads a transcript through the controller's private active handle and binding", async () => {
    const files: Record<string, string> = {
      "sessions.json": JSON.stringify({
        sessions: [{
          session_id: "session-1",
          campaign_id: "campaign-1",
          title: "The Black Bell",
          recorded_at: "2026-08-01",
          number: 1,
          audio_file: "session-1.wav"
        }]
      }),
      "transcripts/session-1.json": JSON.stringify({
        session_id: "session-1",
        model: "gemini-2.5-flash",
        duration_s: 90,
        full_text: "The party crossed the silent bridge.",
        segments: [{
          speaker: "GM",
          start_s: 0,
          end_s: 3,
          text: "The party crossed the silent bridge."
        }]
      })
    };
    const directory = (prefix = ""): FileSystemDirectoryHandle => ({
      kind: "directory",
      name: "campaign",
      getDirectoryHandle: async (name: string) => directory(`${prefix}${name}/`),
      getFileHandle: async (name: string) => ({
        kind: "file",
        name,
        getFile: async () => new Blob([files[`${prefix}${name}`]]) as File
      }) as FileSystemFileHandle,
      queryPermission: vi.fn(async () => "granted" as PermissionState),
      requestPermission: vi.fn(async () => "granted" as PermissionState),
      entries: async function* () {},
      values: async function* () {},
      isSameEntry: async () => false,
      removeEntry: async () => undefined,
      resolve: async () => null
    });
    const handle = directory();
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-private"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", {
      locks: {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      }
    });
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
      repository,
      { activeWorkflowIds: vi.fn(async () => []) } as never
    );

    await controller.initialize();
    await expect(controller.readChroniclerTranscriptSource("session-1"))
      .resolves.toMatchObject({
        sessionId: "session-1",
        folderBindingId: "binding-private"
      });

    controller.stop();
    vi.unstubAllGlobals();
  });

  it("rejects source discovery after current-handle permission is lost", async () => {
    const queryPermission = vi.fn()
      .mockResolvedValueOnce("granted")
      .mockResolvedValue("denied");
    const handle = {
      name: "campaign",
      queryPermission
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      ...repositoryWith(storedOutcome()),
      loadDirectory: vi.fn(async () => ({
        handle,
        rootId: "campaign-root",
        handleBindingId: "binding-private"
      })),
      listOutcomes: vi.fn(async () => []),
      countOutcomes: vi.fn(async () => 0)
    } satisfies DirectoryHandleRepository;
    vi.stubGlobal("window", {
      indexedDB: {},
      showDirectoryPicker: vi.fn(),
      setTimeout,
      clearTimeout
    });
    vi.stubGlobal("navigator", {
      locks: {
        request: vi.fn(async (_name, _options, callback) => callback({}))
      }
    });
    const controller = new BrowserBridgeController(
      vi.fn(),
      { listOperations: vi.fn(async () => []), submitResult: vi.fn() },
      repository,
      { activeWorkflowIds: vi.fn(async () => []) } as never
    );

    await controller.initialize();
    await expect(controller.discoverChroniclerTranscriptSources())
      .rejects.toThrow("read/write permission");

    controller.stop();
    vi.unstubAllGlobals();
  });
});
