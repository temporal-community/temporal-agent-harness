/**
 * The browser surface AgentRunController reaches for, plus the fixtures more than
 * one test builds the same way.
 *
 * There is no module loader here any more. Vitest runs the tests through Vite, so
 * a test imports `agentRun.svelte.ts` the way the app does and the runes compile on
 * the way in; the SSR server this file used to boot per check, and the private
 * optimizer cache that server needed so a thrown assertion could not corrupt a
 * running `pnpm dev`, both went with it.
 */
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
 * Varied frame fixture — turn boundaries every 50 frames so markRuntimeNode stays
 * realistic.
 *
 * Started as a copy of the one in projectionCost.test.mjs and has since drifted
 * from it in two ways, so the two are not interchangeable: this one numbers
 * `resume_offset` from 1 rather than from 0, and it carries a `replay` flag its
 * callers switch. Consolidating them means reconciling those first.
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

