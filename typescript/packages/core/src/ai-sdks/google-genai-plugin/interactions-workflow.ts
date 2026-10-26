// Workflow-side helpers for the Interactions API (Python's _interactions_workflow.py). The workflow
// drives the tool-calling loop itself, running each call with `runner.runTool`.

import type { Interactions } from "@google/genai";
import type { AnyHarnessTool } from "../../harness/agent-workflow/types.ts";

/**
 * A harness tool as an Interactions-API function declaration, for the `tools` of
 * `interactions.create`. The tool's name is the name the model calls it by, and its model-facing
 * parameters (injected ones excluded) are the declared JSON schema.
 */
export function functionParam(tool: AnyHarnessTool): Interactions.Function {
  return {
    type: "function",
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
  };
}
