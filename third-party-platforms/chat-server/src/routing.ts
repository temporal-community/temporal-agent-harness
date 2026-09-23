/**
 * Which agent a given bot talks to.
 *
 * One bot is one agent. Two Slack bots in a workspace can address two different harness
 * agents, so the mapping is keyed by the bot's own identity rather than being global. The
 * value is an `AgentDescriptor.key` — resolved against the live registry from
 * `GET /api/agents`, so nothing here duplicates what the session manager already knows about
 * workflow types and task queues.
 *
 * This is the one piece of genuine configuration in the whole server. Everything else about
 * an agent — its handlers, their schemas, whether they can join an open turn — is discovered
 * at runtime.
 */

export interface BotRoute {
  /** `AgentDescriptor.key` from `GET /api/agents`. */
  agentKey: string;
}

export type RoutingTable = Record<string, BotRoute>;

/**
 * Parse `BOT_AGENT_ROUTES`, e.g. `"T0123/B0456=support-agent,T0123/B0789=research-agent"`.
 * A bare `"support-agent"` sets the default for every bot, which is the single-bot case.
 */
export function parseRoutes(spec: string): { routes: RoutingTable; fallback?: BotRoute } {
  const routes: RoutingTable = {};
  let fallback: BotRoute | undefined;

  for (const entry of spec.split(',').map((s) => s.trim()).filter(Boolean)) {
    const eq = entry.indexOf('=');
    if (eq === -1) {
      fallback = { agentKey: entry };
      continue;
    }
    routes[entry.slice(0, eq).trim()] = { agentKey: entry.slice(eq + 1).trim() };
  }
  return { routes, fallback };
}

export function resolveAgentKey(
  table: { routes: RoutingTable; fallback?: BotRoute },
  botId: string | undefined,
): string | undefined {
  const route = (botId ? table.routes[botId] : undefined) ?? table.fallback;
  return route?.agentKey;
}
