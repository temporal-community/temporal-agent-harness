import { ApplicationFailure } from "@temporalio/activity";
import { SimplePlugin } from "@temporalio/plugin";
import type { WorkerOptions } from "@temporalio/worker";
import type { ExternalStorage } from "@temporalio/common";
import {
  CODE_RESUME_BATCH_ACTIVITY,
  CODE_START_BATCH_ACTIVITY,
  CODE_TYPE_CHECK_ACTIVITY,
  type CodeModeActivities,
} from "@temporalio/agent-harness/harness/code-mode";
import { DEFAULT_PAYLOAD_STORAGE, checkPayloadStorage } from "./utils/large-payload.ts";
import { JEV_TOOL_APPROVAL_ACTIVITY } from "@temporalio/agent-harness/ai-sdks/jev/evaluator";
import { RUN_SUBAGENT_TURN_ACTIVITY } from "@temporalio/agent-harness/harness/agent-protocol";
import { jevToolApproval } from "./ai-sdks/jev/evaluator.ts";
import type { ToolActivity } from "./harness/agent-workflow.ts";
import {
  collectFilesystemActivities,
  type FilesystemRegistration,
} from "./harness/code-mode/activity-fs.ts";
import { runSubagentTurn } from "./harness/subagent-activities.ts";

type Activities = Record<string, (...args: never[]) => Promise<unknown>>;

/** The harness's own activities, keyed by their wire names. */
export function harnessActivities(): Activities {
  return {
    [RUN_SUBAGENT_TURN_ACTIVITY]: runSubagentTurn,
    [JEV_TOOL_APPROVAL_ACTIVITY]: jevToolApproval,
  };
}

export const CODE_MODE_MISSING_EXTRA_ERROR = "CodeModeExtraMissing";

const CODE_MODE_ACTIVITIES = [
  CODE_START_BATCH_ACTIVITY,
  CODE_RESUME_BATCH_ACTIVITY,
  CODE_TYPE_CHECK_ACTIVITY,
] as const;

// Code Mode is dispatched by activity name, and an unregistered name makes Temporal retry forever
// (a hung turn), so a worker without @pydantic/monty registers these in its place.
export const codeModeUnavailable: Activities = Object.fromEntries(
  CODE_MODE_ACTIVITIES.map((name) => [
    name,
    async () => {
      throw ApplicationFailure.create({
        type: CODE_MODE_MISSING_EXTRA_ERROR,
        nonRetryable: true,
        message:
          `Code Mode (${name}) ran on a worker without its sandbox. Install @pydantic/monty on ` +
          "this worker and redeploy it.",
      });
    },
  ]),
);

const montyInstalled = (): boolean => {
  try {
    import.meta.resolve("@pydantic/monty");
    return true;
  } catch {
    return false;
  }
};

// Code Mode runs wherever @pydantic/monty is installed, as Python's does wherever its `code-mode`
// extra is. The binary loads on the first Code Mode call.
function codeModeActivities(): Activities {
  let stepper: Promise<CodeModeActivities> | undefined;
  const load = () =>
    (stepper ??= import("./harness/code-mode/monty-stepper.ts").then(({ codeModeActivities }) =>
      codeModeActivities(),
    ));
  return Object.fromEntries(
    CODE_MODE_ACTIVITIES.map((name) => [
      name,
      async (...args: unknown[]) =>
        ((await load())[name] as (...args: unknown[]) => Promise<unknown>)(...args),
    ]),
  );
}

function toolActivities(tools: readonly ToolActivity[]): Activities {
  const activities: Activities = {};
  for (const tool of tools) {
    if (tool?.kind !== "tool-activity")
      throw new TypeError(
        `AgentHarnessPlugin({ tools }) got ${String(tool)}, which is not a toolActivity(...)`,
      );
    if (tool.name in activities)
      throw new TypeError(`AgentHarnessPlugin({ tools }) has two tools named '${tool.name}'`);
    activities[tool.name] = tool.activity;
  }
  return activities;
}

/**
 * Registers what a worker needs to run harness agents (Python's AgentHarnessPlugin): the harness's
 * own activities, each tool's activity body, Code Mode (where @pydantic/monty is installed),
 * activity filesystems, and offload of
 * payloads over 1.5 MB to `largePayloadOffload` (default: Python's local-file store; pass
 * `s3PayloadStorage()` for multi-host deployments, or null to turn it off). Registration is
 * first-one-wins, as in Python: an activity or external storage the worker configures itself is
 * never displaced by the plugin's.
 */
export class AgentHarnessPlugin extends SimplePlugin {
  constructor({
    tools = [],
    filesystems = [],
    largePayloadOffload = DEFAULT_PAYLOAD_STORAGE,
  }: {
    /** The activity bodies of the agents' `activityTool` tools, each from `toolActivity(...)`. */
    tools?: readonly ToolActivity[];
    /** Activity filesystems the worker's agents mount. Their `vfs.<name>.*` activities are registered here. */
    filesystems?: readonly FilesystemRegistration[];
    largePayloadOffload?: ExternalStorage | null;
  } = {}) {
    // Resolved here, so a bad option fails at the developer's own call rather than in Worker.create.
    const pluginActivities = {
      ...(montyInstalled() ? codeModeActivities() : codeModeUnavailable),
      ...harnessActivities(),
      ...toolActivities(tools),
      ...collectFilesystemActivities(filesystems),
    };
    super({
      name: "temporal-agent-harness",
      activities: (existing) => ({ ...pluginActivities, ...existing }),
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
