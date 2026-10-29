// The stateless MCP call activity the harness registers in place of `@temporalio/openai-agents`'
// (Python's openai_agents/_mcp.py keeps the server's isError flag the same way). The official one
// returns the result's content array, whose `isError` (agents-core attaches it to the array)
// does not survive serialization, so a failed MCP call reached the workflow as a success. This one
// runs the official call and returns `{content, isError}`; the workflow side of
// `statelessMcpServer` turns it back into agents-core's content-with-flag.

import type { CallToolResultContent } from "@openai/agents-core";
import { StatelessMCPServerProvider } from "@temporalio/openai-agents";

/** What the harness's stateless MCP call activity returns. */
export interface StatelessMcpCallToolResult {
  content: CallToolResultContent;
  isError: boolean;
}

/** The activity `statelessMcpServer(name)` calls a tool through (the official plugin's name). */
export const statelessMcpCallToolActivity = (serverName: string): string =>
  `${serverName}-stateless-call-tool`;

/**
 * The harness's call activity for each stateless provider, keyed by the official activity's name:
 * it connects, calls and closes the provider's server as the official one does, and keeps the
 * result's isError flag.
 */
export function statelessMcpCallToolActivities(
  providers: readonly unknown[],
): Record<string, (input: unknown) => Promise<StatelessMcpCallToolResult>> {
  const activities: Record<string, (input: unknown) => Promise<StatelessMcpCallToolResult>> = {};
  for (const provider of providers) {
    if (!(provider instanceof StatelessMCPServerProvider)) continue;
    const name = statelessMcpCallToolActivity(provider.name);
    const official = provider._getActivities()[name]!;
    activities[name] = async (input) => {
      const content = (await official(input)) as CallToolResultContent;
      return { content: [...content], isError: content.isError === true };
    };
  }
  return activities;
}
