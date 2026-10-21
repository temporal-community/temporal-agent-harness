// Subagents as tools (Python's subagent_toolset.py): a model starts, messages, and stops instances
// of another agent through `start_<key>`, one `<key>_<handler>` per selected handler, and
// `stop_<key>`.

import { z } from "zod";
import { ToolApprovalPolicy, type AgentConfig } from "./agent-protocol/index.ts";
import { AgentWorkflowRunner, currentToolCall } from "./agent-workflow.ts";
import { tool, type AgentDefinition, type AgentWorkflow, type AnyHarnessTool } from "./agent.ts";

export const SubagentToolPolicy = {
  /** Every handler its author marked model_callable (the default). */
  allowModelCallable: () => ({
    allowedNames: null as string[] | null,
    includeNonModelCallable: false,
  }),
  allowOnly: (...names: string[]) => ({ allowedNames: names, includeNonModelCallable: true }),
  dangerouslyAllowAll: () => ({
    allowedNames: null as string[] | null,
    includeNonModelCallable: true,
  }),
};
export type SubagentToolPolicy = ReturnType<typeof SubagentToolPolicy.allowModelCallable>;

/**
 * Tools that let a model start, message, and stop instances of another agent as subagents:
 * `start_<key>`, one `<key>_<handler>` per selected handler, and `stop_<key>`.
 *
 * Nobody watches a child's approval gate, so a gated call inside it waits forever under the
 * child's own `approvalPolicyDefault`. `approvalPolicy: "inherit"` starts each child with the
 * parent's live policy and criteria (auto mode dropped if the child has no evaluator, which it
 * could not start with); a policy starts it with that one. Omitted: the child's default.
 */
export function subagentToolset(
  agent: AgentWorkflow,
  options: {
    key: string;
    taskQueue: string;
    workflowType?: string;
    tools?: SubagentToolPolicy;
    initData?: () => unknown;
    approvalPolicy?: "inherit" | ToolApprovalPolicy;
  },
): AnyHarnessTool[] {
  const def = agent.definition;
  const { key, taskQueue } = options;
  const workflowType = options.workflowType ?? def.name;
  if (options.initData !== undefined && def.initData === undefined) {
    throw new TypeError(`initData was given, but ${def.name} takes no init data.`);
  }
  if (options.initData === undefined && def.initData?.required) {
    throw new TypeError(
      `${def.name} requires init data, so subagentToolset needs an initData callback that supplies it.`,
    );
  }
  const policy = options.tools ?? SubagentToolPolicy.allowModelCallable();
  const selected = Object.entries(def.handlers).filter(
    ([name, h]) =>
      (policy.allowedNames === null || policy.allowedNames.includes(name)) &&
      (h.modelCallable || policy.includeNonModelCallable),
  );
  if (selected.length === 0) {
    throw new TypeError(
      `${def.name} exposes no handlers under the given SubagentToolPolicy, so it has no callable surface.`,
    );
  }
  const start = tool({
    name: `start_${key}`,
    description:
      `Start a new ${key} subagent and return its short handle. Call this first; then pass the returned handle as the ` +
      `\`subagent\` argument to the ${key}_* tools to drive that instance, and to stop_${key} to shut it down. You may ` +
      `start several instances to work on subtasks in parallel — each returns its own handle.`,
    inputSchema: z.object({}),
    outputSchema: z.string(),
    execute: async () => {
      const { runner } = currentToolCall(`start_${key}`);
      let data = options.initData?.();
      if (def.initData !== undefined && data !== undefined && data !== null)
        data = def.initData.schema.parse(data);
      return runner.startSubagent(key, workflowType, taskQueue, {
        config: childApprovalConfig(runner, def, options.approvalPolicy),
        data: data ?? undefined,
      });
    },
  });
  const sends = selected.map(([name, h]) =>
    tool({
      name: `${key}_${name}`,
      description:
        `${h.description}\n\nSends this to the ${key} subagent identified by \`subagent\` (the handle returned by ` +
        `start_${key}) and returns its reply. Send several messages to the same handle to have that instance expand on ` +
        `or clarify its earlier work.`,
      inputSchema: z.object({ subagent: z.string(), message: h.input }),
      outputSchema: h.output,
      execute: async ({ subagent, message }) => {
        const { runner } = currentToolCall(`${key}_${name}`);
        const payload = jsonClone(h.input.parse(message)) as Record<string, unknown>;
        return h.output.parse(await runner.runSubagentTurn(subagent, name, payload));
      },
    }),
  );
  const stop = tool({
    name: `stop_${key}`,
    description: `Stop the ${key} subagent identified by \`subagent\` (the handle returned by start_${key}). Use it when that instance's work is done.`,
    inputSchema: z.object({ subagent: z.string() }),
    outputSchema: z.string(),
    execute: async ({ subagent }) => {
      await currentToolCall(`stop_${key}`).runner.stopSubagent(subagent);
      return `stopped subagent '${subagent}'`;
    },
  });
  return [start, ...sends, stop] as AnyHarnessTool[];
}

function childApprovalConfig(
  runner: AgentWorkflowRunner,
  child: AgentDefinition,
  policy: "inherit" | ToolApprovalPolicy | undefined,
): Partial<AgentConfig> {
  if (policy === undefined) return {};
  if (policy !== "inherit") return { approval_policy: policy };
  const live = runner.approvalPolicy;
  return {
    approval_policy:
      child.autoModeEvaluator === undefined && live.auto_mode_enabled
        ? ToolApprovalPolicy.withAutoMode(live, false)
        : live,
    auto_approval_criteria: runner.autoApprovalCriteria,
  };
}

const jsonClone = (value: unknown): unknown => JSON.parse(JSON.stringify(value));
