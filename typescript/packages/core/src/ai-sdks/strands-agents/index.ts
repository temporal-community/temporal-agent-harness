// Harness: package.json lists this module and load-polyfills.js under `sideEffects`, so webpack
// keeps both in a workflow bundle that imports only this module's re-exports.
// eslint-disable-next-line import/no-unassigned-import
import './load-polyfills.ts';

// Harness: StrandsPlugin and the model and MCP activities are the worker half of this package:
// `@temporalio/agent-harness-worker/ai-sdks/strands-agents`.
export { TemporalAgent, type TemporalAgentOptions } from './temporal-agent.ts';
export {
  TemporalModel,
  type TemporalModelOptions,
  GET_MODEL_CONFIG_ACTIVITY,
  INVOKE_STATEFUL_MODEL_ACTIVITY,
  INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY,
} from './temporal-model.ts';
export {
  TemporalMCPClient,
  type TemporalMCPClientOptions,
  type McpToolFilters,
  type McpToolMatcher,
  type McpToolInfo,
  type CallToolInput,
  listToolsActivityName,
  callToolActivityName,
} from './temporal-mcp-client.ts';
export { TemporalMCPTool } from './temporal-mcp-tool.ts';
export { TemporalActivityTool, type ActivityAsToolOptions } from './temporal-activity-tool.ts';
export {
  STRANDS_INTERRUPT_TYPE,
  STRANDS_MALFORMED_INTERRUPT_TYPE,
  // Harness: the worker half's failure converter.
  rewriteError,
} from './strands-errors.ts';
// Harness: the model activities' wire types, which the worker half implements.
export type {
  InvokeModelInput,
  InvokeModelStreamingInput,
  InvokeStatefulModelInput,
  InvokeStatefulModelStreamingInput,
  InvokeStatefulModelResult,
  GetModelConfigInput,
  ModelConfigInfo,
} from './model-activity.ts';
export * as workflow from './workflow.ts';
