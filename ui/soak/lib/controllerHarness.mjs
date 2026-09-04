/**
 * memoryStorage + window stubs + Vite SSR boot for AgentRunController.
 * Patterns from ui/scripts/check-frame-arrival.mjs / check-caught-up-attach.mjs.
 */
import { createCheckServer } from "../../scripts/checkServer.mjs";

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function waitFor(label, predicate, timeoutMs = 4_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(25);
  }
  throw new Error(`timed out after ${timeoutMs}ms waiting for ${label}`);
}

export function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
    key: (i) => [...map.keys()][i] ?? null,
    get length() {
      return map.size;
    }
  };
}

/**
 * Install browser globals the controller reaches for.
 * @param {{ controllableRaf?: boolean }} [opts]
 * @returns {{ storage: { local: ReturnType<typeof memoryStorage>, session: ReturnType<typeof memoryStorage> }, freshStorage: () => void, rafQueue: Function[], flushRaf: () => void }}
 */
export function installBrowserSurface({ controllableRaf = false } = {}) {
  const storage = { local: memoryStorage(), session: memoryStorage() };
  const freshStorage = () => {
    storage.local = memoryStorage();
    storage.session = memoryStorage();
  };
  const listeners = new Map();
  const rafQueue = [];

  globalThis.window = {
    get localStorage() {
      return storage.local;
    },
    get sessionStorage() {
      return storage.session;
    },
    setTimeout: (...args) => setTimeout(...args),
    clearTimeout: (...args) => clearTimeout(...args),
    setInterval: (...args) => setInterval(...args),
    clearInterval: (...args) => clearInterval(...args),
    addEventListener: (type, fn) => {
      listeners.set(type, [...(listeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type, fn) => {
      listeners.set(
        type,
        (listeners.get(type) ?? []).filter((item) => item !== fn)
      );
    },
    history: { replaceState() {} },
    location: { href: "http://localhost/", pathname: "/", search: "" }
  };
  globalThis.localStorage = new Proxy({}, { get: (_, p) => storage.local[p] });
  globalThis.sessionStorage = new Proxy(
    {},
    { get: (_, p) => storage.session[p] }
  );

  if (controllableRaf) {
    globalThis.requestAnimationFrame = (fn) => {
      rafQueue.push(fn);
      return rafQueue.length;
    };
    globalThis.cancelAnimationFrame = (id) => {
      /* queue is drained wholesale; id is unused */
      void id;
    };
  } else {
    globalThis.requestAnimationFrame = (fn) =>
      setTimeout(() => fn(Date.now()), 0);
    globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
  }

  const flushRaf = () => {
    const batch = rafQueue.splice(0);
    for (const fn of batch) fn(performance.now());
  };

  return { storage, freshStorage, rafQueue, flushRaf, listeners };
}

/** Session row shaped like the wire. */
export function session(id, over = {}) {
  return {
    workflow_id: id,
    created_at: 1,
    label: id,
    agent_workflow_type: "IncidentTriageWorkflow",
    is_message_queuing_enabled: false,
    run_id: `${id}-run`,
    execution_status: "RUNNING",
    closed: false,
    ...over
  };
}

/**
 * Varied frame fixture from check-projection-cost.mjs — turn boundaries every
 * 50 frames so markRuntimeNode stays realistic.
 * @param {number} i
 * @param {{ replay?: boolean }} [opts]
 */
export function variedFrame(i, { replay = true } = {}) {
  const KINDS = [
    "reply_delta",
    "tool_start",
    "tool_end",
    "model_interaction_started",
    "model_interaction_ended"
  ];
  const kind =
    i % 50 === 0
      ? "turn_started"
      : i % 25 === 6
        ? "subagent_message_sent"
        : KINDS[i % KINDS.length];
  const isChild = i % 5 === 2;
  return {
    event: kind,
    data: {
      type: kind,
      agent_id: isChild ? "kid" : "root",
      turn_id: `t${(i / 50) | 0}`,
      turn_number: 1 + ((i / 50) | 0),
      timestamp: i * 0.01,
      resume_offset: i + 1,
      event_offset: i,
      delta: `token ${i}`,
      user_message: `ask ${i}`,
      tool_id: `tool-${(i / 5) | 0}`,
      tool_name: "search",
      subagent_id: "kid",
      subagent_turn: 1 + ((i / 25) | 0),
      workflow_id: "wf-kid",
      model: "gemini-3.5-flash",
      replay
    }
  };
}

/** Force the expensive $derived projections the UI would read on a paint. */
export function touchProjections(controller) {
  void controller.replayTimeline;
  void controller.fullReplayLog;
  void controller.stepTimeline;
  void controller.chatTranscript;
  void controller.graph;
  void controller.usage;
}

/**
 * Boot Vite SSR + AgentRunController (and optional agentRunStorage).
 * Caller must have installed the browser surface first.
 * @param {string} callerUrl import.meta.url of the soak file
 */
export async function loadControllerModules(callerUrl) {
  const vite = await createCheckServer(callerUrl);
  const { AgentRunController } = await vite.ssrLoadModule(
    "/src/lib/state/agentRun.svelte.ts"
  );
  const storageMod = await vite.ssrLoadModule(
    "/src/lib/state/agentRunStorage.ts"
  );
  return {
    vite,
    AgentRunController,
    writeCachedFrames: storageMod.writeCachedFrames,
    readCachedFrames: storageMod.readCachedFrames,
    framePublishChunkSize: (
      await vite.ssrLoadModule("/src/lib/state/hydration.ts")
    ).framePublishChunkSize
  };
}
