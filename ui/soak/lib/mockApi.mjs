/**
 * Thin AgentApi stub: sessions existence/full, attach iterable, status.
 * Call counts pin the lite-poll vs enrich contract without a Temporal cluster.
 */

const abortError = () => Object.assign(new Error("aborted"), { name: "AbortError" });

/** A stream the soak drives frame by frame (and can end or drop). */
export function controllableStream({ ignoreAbort = false } = {}) {
  const queue = [];
  let wake = null;
  let ended = false;
  let failure = null;
  const ping = () => {
    wake?.();
    wake = null;
  };
  return {
    push(...frames) {
      queue.push(...frames);
      ping();
    },
    end() {
      ended = true;
      ping();
    },
    drop(error = new Error("Failed to fetch")) {
      failure = error;
      ping();
    },
    async *iterate(signal) {
      while (true) {
        if (signal?.aborted && !ignoreAbort) throw abortError();
        if (queue.length) {
          yield queue.shift();
          continue;
        }
        if (failure) {
          const error = failure;
          failure = null;
          throw error;
        }
        if (ended) return;
        await new Promise((resolve) => {
          wake = resolve;
          signal?.addEventListener("abort", resolve, { once: true });
        });
      }
    }
  };
}

/** Empty attach body — caught-up RUNNING shape. */
export async function* silentStream() {
  return;
}

/**
 * @param {object} opts
 * @param {() => object[]} [opts.sessions]
 * @param {() => string} [opts.revision]
 * @param {(id: string, call: number) => AsyncIterable} [opts.streamFor]
 * @param {(id: string) => string} [opts.statusFor]
 */
export function createMockApi({
  sessions = () => [],
  revision = () => "rev-0",
  streamFor = () => silentStream(),
  statusFor = () => "RUNNING"
} = {}) {
  const counts = {
    listSessions: 0,
    listSessionsExistence: 0,
    attach: 0,
    workflowStatus: 0
  };
  const attachCalls = [];

  const api = {
    async listAgents() {
      return { agents: [] };
    },
    async listSessions() {
      counts.listSessions += 1;
      return sessions().map((s) => ({ ...s }));
    },
    async listSessionsExistence() {
      counts.listSessionsExistence += 1;
      const rows = sessions().map((s) => ({ ...s }));
      return { revision: revision(), sessions: rows };
    },
    async getSession(sessionId) {
      const found = sessions().find((s) => s.workflow_id === sessionId);
      if (!found) throw new Error(`No session ${sessionId}`);
      return { ...found };
    },
    async createSession() {
      throw new Error("createSession not stubbed");
    },
    async agentInterface() {
      return [];
    },
    async operatorInterface() {
      return [];
    },
    async workflowStatus(workflowId) {
      counts.workflowStatus += 1;
      const status = statusFor(workflowId);
      return {
        workflow_id: workflowId,
        execution_status: status,
        closed: status !== "RUNNING"
      };
    },
    attach(sessionId, fromOffset, signal) {
      counts.attach += 1;
      attachCalls.push({ sessionId, fromOffset, at: Date.now() });
      const iter = streamFor(sessionId, attachCalls.length);
      if (iter && typeof iter[Symbol.asyncIterator] === "function") {
        return (async function* () {
          for await (const frame of iter) {
            if (signal?.aborted) throw abortError();
            yield frame;
          }
        })();
      }
      if (typeof iter?.iterate === "function") return iter.iterate(signal);
      return silentStream();
    },
    async submitMessage() {
      return { ok: true };
    }
  };

  return { api, counts, attachCalls };
}
