// The worker half of the vendored Strands Agents integration: the plugin that registers the model
// and MCP activities. The workflow half is `@temporalio/agent-harness/ai-sdks/strands-agents`.
export { StrandsPlugin, type StrandsPluginOptions } from './plugin.ts';
