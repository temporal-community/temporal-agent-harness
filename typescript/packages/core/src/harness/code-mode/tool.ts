// Code Mode: expose a set of harness tools to a model as ONE tool that runs a model-written Python
// script in Monty, the Python harness's sandbox, where the tools are async host functions.
// Independent host calls awaited together run concurrently as durable tool calls, and every host
// call goes through its own tool's approval gate and lifecycle. The script runs to completion in
// a `CodeModeDriver` (driver.ts). What the model is told is contract/code_mode/'s text
// (texts.ts).

import { proxyActivities } from "@temporalio/workflow";
import { z } from "zod";
import { tool as harnessTool, type AnyHarnessTool, type HarnessTool } from "../agent.ts";
import { currentToolCall } from "../agent-workflow.ts";
import {
  CodeModeDriver,
  DEFAULT_STEP_TIMEOUT,
  STEP_RETRY,
  type CodeModeActivities,
} from "./driver.ts";
import { renderHostInterface, renderTypeCheckStubs, type TypeCheckStubs } from "./stubs.ts";
import { CONTRACT, FILESYSTEM_STUB, NO_FILESYSTEM, WITH_FILESYSTEM } from "./texts.ts";
import { validateMounts, type Mount } from "./vfs.ts";

/** The model-facing listing of the mounts. */
export function renderFilesystem(mounts: readonly Mount[]): string {
  if (!mounts.length) return "";
  const lines = mounts.map(
    (m) =>
      `- \`${m.path}\` (${m.readOnly ? "read-only" : "writable"})${m.description ? `: ${m.description}` : ""}`,
  );
  return `\nFilesystem:\n\n${lines.join("\n")}\n`;
}

export interface CodeModeOptions {
  name: string;
  /** Applies to the run-code tool only; each host call re-enters the gate under its own tool. */
  inherentlySafe?: boolean;
  autoApprovalCriteria?: string;
  /** Values for the tools' injected parameters, supplied to every host call. */
  injections?: Record<string, unknown>;
  /** Bounds one sandbox step's activity (compile-to-first-batch or resume-to-next-batch). */
  stepTimeout?: string | number;
  /**
   * Extra contract text appended for the model (Python's OKF guide). Empty leaves the
   * description unchanged.
   */
  guide?: string;
  /**
   * The sandbox's virtual filesystem, as mounts at distinct, non-nested paths. Each
   * file operation a script makes is carried out as fs_* tool calls through the runner, so it is
   * subject to the approval policy and publishes tool lifecycle events. Build the mounts' backends
   * in `create`, so each workflow has its own.
   */
  mounts?: readonly Mount[];
}

export type CodeModeTool = HarnessTool<{ script: string }, string> & {
  /** The type-check stubs scripts are checked against. */
  readonly codeModeStubs: TypeCheckStubs;
  readonly stepTimeout: string | number;
};

function validateTools(tools: readonly AnyHarnessTool[]): Map<string, AnyHarnessTool> {
  const byName = new Map<string, AnyHarnessTool>();
  for (const tool of tools) {
    if (byName.has(tool.name))
      throw new TypeError(`duplicate host-function name '${tool.name}' in this codeModeTool.`);
    byName.set(tool.name, tool);
  }
  return byName;
}

export function codeModeTool(
  tools: readonly AnyHarnessTool[],
  options: CodeModeOptions,
): CodeModeTool {
  const mounts = validateMounts(options.mounts ?? []);
  if (tools.length === 0 && mounts.length === 0)
    throw new TypeError("codeModeTool requires at least one tool or mount.");
  const toolsByName = validateTools(tools);
  const stepTimeout = options.stepTimeout ?? DEFAULT_STEP_TIMEOUT;
  // Generated once at construction: an unrenderable type throws now, not at script time.
  const rendered = renderTypeCheckStubs(tools);
  const stubs: TypeCheckStubs = mounts.length
    ? { ...rendered, source: `${rendered.source}\n${FILESYSTEM_STUB}\n` }
    : rendered;
  const fill: Record<string, string> = {
    sandbox: mounts.length ? WITH_FILESYSTEM : NO_FILESYSTEM,
    interface: tools.length ? renderHostInterface(tools) : "(none)",
    filesystem: renderFilesystem(mounts),
    guide: options.guide ?? "",
  };
  // One pass, so a placeholder inside a filled-in value is left as it is.
  const description = CONTRACT.replace(
    /\{(sandbox|interface|filesystem|guide)\}/g,
    (_, key: string) => fill[key]!,
  );
  const injections = options.injections ?? {};

  const tool = harnessTool({
    name: options.name,
    description,
    inputSchema: z.object({ script: z.string() }),
    outputSchema: z.string(),
    inherentlySafe: options.inherentlySafe ?? true,
    ...(options.autoApprovalCriteria ? { autoApprovalCriteria: options.autoApprovalCriteria } : {}),
    execute: async ({ script }) => {
      const { runner } = currentToolCall(`codeModeTool '${options.name}'`);
      const driver = new CodeModeDriver(runner, toolsByName, {
        injections,
        stubs,
        mounts,
        stepTimeout,
      });
      return driver.runScript(script);
    },
  });
  // Read back by codeModeTypeCheck, so a script can be checked against these exact stubs.
  return Object.assign(tool, { codeModeStubs: stubs, stepTimeout });
}

/** Type-check a `script` against a Code Mode tool's host functions without running it. */
export async function codeModeTypeCheck(
  codeTool: CodeModeTool,
  script: string,
): Promise<string | null> {
  const { code_type_check } = proxyActivities<CodeModeActivities>({
    startToCloseTimeout: codeTool.stepTimeout,
    retry: STEP_RETRY,
  });
  return (await code_type_check(script, codeTool.codeModeStubs)) || null;
}
