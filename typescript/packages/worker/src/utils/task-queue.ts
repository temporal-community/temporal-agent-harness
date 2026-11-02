// One environment variable that moves a whole harness deployment onto its own task queues.
//
// Each agent worker polls its own queue, agents.toml names those queues for the server, and the
// session manager has a fixed workflow id and queue. Setting TASK_QUEUE_PREFIX to the same value
// for every process changes all of them together: workers poll `<prefix>-<queue>`, the server
// routes sessions there, and the session manager becomes `<prefix>-session-manager`. Unset, every
// name is unchanged. The Python SDK's `temporal_agent_harness.utils.task_queue` is the same rule.

export const TASK_QUEUE_PREFIX_ENV = "TASK_QUEUE_PREFIX";

/** `base` under this process's TASK_QUEUE_PREFIX, or `base` itself when it is unset. */
export function taskQueueName(base: string): string {
  const prefix = process.env[TASK_QUEUE_PREFIX_ENV];
  return prefix ? `${prefix}-${base}` : base;
}
