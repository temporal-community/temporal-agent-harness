// The OpenAI Agents SDK on a harness worker (Python's ai_sdks/openai_agents package): register
// `new OpenAIAgentsPlugin()` before `AgentHarnessPlugin`, and run agents with `runStreamed` from
// `@temporalio/agent-harness/ai-sdks/openai-agents-harness`. A `statelessMcpServer(name, ...)` in
// workflow code needs a `StatelessMCPServerProvider` of the same name in `mcpServerProviders`.

export { StatelessMCPServerProvider } from "@temporalio/openai-agents";
export { OpenAIAgentsPlugin, type OpenAIAgentsPluginOptions } from "./temporal-openai-agents.ts";
export {
  statelessMcpCallToolActivities,
  statelessMcpCallToolActivity,
  type StatelessMcpCallToolResult,
} from "./mcp.ts";
export {
  DEFAULT_STREAMING_BATCH_INTERVAL,
  INVOKE_MODEL_STREAM_ACTIVITY,
  createInvokeModelStreamActivity,
  toModelInvocationFailure,
  type InvokeModelStreamActivityInput,
  type SerializedStreamEvent,
} from "./invoke-model-activity.ts";
