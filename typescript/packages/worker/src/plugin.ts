import { ApplicationFailure } from "@temporalio/activity";
import { SimplePlugin } from "@temporalio/plugin";
import type { ActivityInterceptorsFactory, WorkerOptions } from "@temporalio/worker";
import type { ExternalStorage } from "@temporalio/common";
import {
  CODE_RESUME_BATCH_ACTIVITY,
  CODE_START_BATCH_ACTIVITY,
  CODE_TYPE_CHECK_ACTIVITY,
} from "@temporalio/agent-harness/harness/code-mode";
import { DEFAULT_PAYLOAD_STORAGE, checkPayloadStorage } from "./utils/large-payload.ts";
import { stampAttempt } from "@temporalio/agent-harness/ai-sdks/integration-helpers/raw-events";
import { JEV_TOOL_APPROVAL_ACTIVITY } from "@temporalio/agent-harness/ai-sdks/jev/evaluator";
import { RUN_SUBAGENT_TURN_ACTIVITY } from "@temporalio/agent-harness/harness/agent-protocol";
import { jevToolApproval } from "./ai-sdks/jev/evaluator.ts";
import {
  collectFilesystemActivities,
  type FilesystemRegistration,
} from "./harness/code-mode/activity-fs.ts";
import { runSubagentTurn } from "./harness/subagent-activities.ts";

/** The harness's own activities, keyed by their wire names. */
export function harnessActivities(): Record<string, (...args: never[]) => Promise<unknown>> {
  return {
    [RUN_SUBAGENT_TURN_ACTIVITY]: runSubagentTurn,
    [JEV_TOOL_APPROVAL_ACTIVITY]: jevToolApproval,
  };
}

/**
 * Stamps the activity and attempt onto any raw-events topic an activity is given, so the
 * workflow can drop a retried model call's superseded attempt (see `raw-events.ts`).
 */
export const rawTopicAttemptInterceptor: ActivityInterceptorsFactory = (ctx) => ({
  inbound: {
    execute: (input, next) =>
      next({
        ...input,
        args: input.args.map((arg) => stampAttempt(arg, ctx.info.activityId, ctx.info.attempt)),
      }),
  },
});

export const CODE_MODE_MISSING_EXTRA_ERROR = "CodeModeExtraMissing";

// Code Mode is dispatched by activity name, and an unregistered name makes Temporal retry forever
// (a hung turn), so every worker registers these. A worker that runs Code Mode registers the real
// activities itself, which win.
const codeModeUnavailable = Object.fromEntries(
  [
    CODE_START_BATCH_ACTIVITY,
    CODE_RESUME_BATCH_ACTIVITY,
    CODE_TYPE_CHECK_ACTIVITY,
  ].map((name) => [
    name,
    async () => {
      throw ApplicationFailure.create({
        type: CODE_MODE_MISSING_EXTRA_ERROR,
        nonRetryable: true,
        message:
          `Code Mode (${name}) ran on a worker without its sandbox. Register codeModeActivities() from ` +
          "@temporalio/agent-harness-worker/harness/code-mode/monty-stepper on this worker and redeploy it.",
      });
    },
  ]),
);

/**
 * Registers the harness activities and interceptor on a worker (Python's AgentHarnessPlugin), and
 * offloads payloads over 1.5 MB to `largePayloadOffload` (default: Python's local-file store; pass
 * `s3PayloadStorage()` for multi-host deployments, or null to turn it off). Registration is
 * first-one-wins, as in Python: an activity or external storage the worker configures itself is
 * never displaced by the plugin's.
 */
export class AgentHarnessPlugin extends SimplePlugin {
  constructor({
    extraActivities = {},
    filesystems = [],
    largePayloadOffload = DEFAULT_PAYLOAD_STORAGE,
  }: {
    extraActivities?: object;
    /** Activity filesystems the worker's agents mount. Their `vfs.<name>.*` activities are registered here. */
    filesystems?: readonly FilesystemRegistration[];
    largePayloadOffload?: ExternalStorage | null;
  } = {}) {
    const filesystemActivities = collectFilesystemActivities(filesystems);
    super({
      name: "temporal-agent-harness",
      activities: (existing) => ({
        ...codeModeUnavailable,
        ...harnessActivities(),
        ...extraActivities,
        ...filesystemActivities,
        ...existing,
      }),
      workerInterceptors: {
        activity: [rawTopicAttemptInterceptor],
      },
      ...(largePayloadOffload
        ? {
            dataConverter: (existing) => ({
              ...existing,
              externalStorage: existing?.externalStorage ?? largePayloadOffload,
            }),
          }
        : {}),
    });
  }

  // A store that can't write would fail every task with a big payload, forever: refuse to start.
  override configureWorker(options: WorkerOptions): WorkerOptions {
    const configured = super.configureWorker(options);
    if (configured.dataConverter?.externalStorage)
      checkPayloadStorage(configured.dataConverter.externalStorage);
    return configured;
  }
}
