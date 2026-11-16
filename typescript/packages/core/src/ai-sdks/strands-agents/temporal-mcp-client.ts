import { McpClient, ToolValidationError } from '@strands-agents/sdk';
import type { JSONSchema, JSONValue } from '@strands-agents/sdk';
// Harness: Strands 1.24's own MCP peer; its McpClient takes this transport type.
import type { Transport } from '@modelcontextprotocol/client';
import * as workflow from '@temporalio/workflow';
import type { ActivityOptions } from '@temporalio/workflow';
// Harness: an ES module import; the cycle with temporal-mcp-tool is safe because the class is
// only used inside methods, after both modules have loaded.
import { TemporalMCPTool } from './temporal-mcp-tool.ts';

/** Tool descriptor returned by the per-server `{server}-listTools` activity. */
export interface McpToolInfo {
  name: string;
  description: string;
  inputSchema?: JSONSchema;
}

/** Activity input for the per-server `{server}-callTool` activity. */
export interface CallToolInput {
  toolName: string;
  args: JSONValue;
}

const STUB_TRANSPORT: Transport = {
  async start() {
    // No-op; TemporalMCPClient never connects from inside a workflow.
  },
  async send() {
    // No-op.
  },
  async close() {
    // No-op.
  },
};

/**
 * Options for {@link TemporalMCPClient}.
 *
 * The {@link ActivityOptions} apply to every per-tool activity invocation
 * the agent makes for this MCP server.
 *
 * `cacheTools` controls how often the agent re-lists this server's tools.
 * Strands lists an MCP client's tools once when the agent initializes and then
 * reuses that list for every turn, so the list is otherwise frozen for the
 * whole workflow.
 *
 * - `false` (default) — re-list the tools on each agent turn, so the agent
 *   picks up an MCP server that's restarted or redeployed mid-workflow. Costs
 *   one extra `{server}-listTools` activity per tool round.
 * - `true` — list once at the beginning of the workflow and reuse for all
 *   turns. Cheaper, but the tool list is fixed for the workflow's lifetime.
 *
 * `prefix` and `toolFilters` work as on Strands' `McpClient`: the agent sees
 * `{prefix}_{name}`, and string and `RegExp` filters match the server's tool
 * name. A callback filter receives the workflow-side {@link TemporalMCPTool}
 * and runs in the workflow, so it must be deterministic.
 */
export interface TemporalMCPClientOptions {
  server: string;
  activityOptions?: ActivityOptions;
  cacheTools?: boolean;
  /** Prefix for the agent's tool names, applied as `{prefix}_{name}`. */
  prefix?: string;
  /** Which of the server's tools the agent gets. */
  toolFilters?: McpToolFilters;
}

/**
 * Matches a tool, as in Strands' `McpToolMatcher`: a string matches the
 * server's tool name exactly, a `RegExp` matches it from the start, and a
 * callback receives the tool under the name the agent sees.
 */
export type McpToolMatcher = string | RegExp | ((tool: TemporalMCPTool) => boolean);

/** Which of a server's tools the agent gets, as in Strands' `McpToolFilters`. */
export interface McpToolFilters {
  /** When present, only tools that match at least one matcher. */
  allowed?: McpToolMatcher[];
  /** Tools that match at least one matcher are left out, even when also allowed. */
  rejected?: McpToolMatcher[];
}

/**
 * Minimal view of Strands' `ToolRegistry` used by {@link
 * TemporalMCPClient.refreshTools}. Typed structurally because `ToolRegistry`
 * isn't re-exported from `@strands-agents/sdk`'s public index.
 */
export interface ToolRegistryView {
  list(): { name: string }[];
  remove(name: string): void;
  addOrReplace(tools: never[]): void;
}

/**
 * Workflow-side handle to an MCP server registered on the worker.
 *
 * The transport factory and tool discovery live worker-side via
 * `StrandsPlugin({ mcpClients: { server: () => new McpClient(...) } })`.
 * This handle carries the server name (which selects the registered
 * factory), the tool `prefix` and `toolFilters`, and the per-call activity
 * options.
 *
 * Extends {@link McpClient} so it can be passed directly in an
 * {@link AgentConfig.tools | Agent.tools} array (Strands' `flattenTools`
 * dispatches on `instanceof McpClient`). `connect()` and `disconnect()`
 * are no-ops; `listTools()` returns lightweight {@link TemporalMCPTool}
 * wrappers whose `stream()` dispatches to the per-server `{server}-callTool`
 * activity registered by {@link StrandsPlugin}.
 */
export class TemporalMCPClient extends McpClient {
  private readonly server: string;
  private readonly activityOptions: ActivityOptions;
  private readonly prefix: string | undefined;
  private readonly toolFilters: McpToolFilters | undefined;
  /** Whether the agent lists this server's tools once or re-lists each turn. */
  readonly cacheTools: boolean;
  // The names this client's tools hold in each agent's registry: the names of the tools Strands
  // added there, then the names of each refresh. While the server lists a name, the name is this
  // client's, whatever tool workflow code puts under it.
  private readonly held = new WeakMap<ToolRegistryView, Set<string>>();
  // The tools this client listed, to find its names in a registry it has no record for.
  private readonly listed = new WeakSet<object>();

  constructor(options: TemporalMCPClientOptions) {
    super({ transport: STUB_TRANSPORT });
    this.server = options.server;
    this.activityOptions = options.activityOptions ?? {};
    this.cacheTools = options.cacheTools ?? false;
    this.prefix = options.prefix;
    this.toolFilters = options.toolFilters;
  }

  override async connect(): Promise<void> {
    // No-op; all I/O happens inside the per-server callTool activity.
  }

  override async disconnect(): Promise<void> {
    // No-op.
  }

  // The base `McpClient.listTools` returns `Promise<McpTool[]>`, but `McpTool`
  // isn't re-exported from `@strands-agents/sdk`'s public index. The cast to
  // `never[]` keeps the override compatible; the agent only uses
  // `name`/`toolSpec`/`stream` on each tool, all of which
  // {@link TemporalMCPTool} provides.
  override async listTools(): Promise<never[]> {
    const activities = workflow.proxyActivities<{
      [key: string]: (args: { server: string }) => Promise<McpToolInfo[]>;
    }>({
      startToCloseTimeout: '10 minutes',
      ...this.activityOptions,
    });
    const infos = await activities[listToolsActivityName(this.server)]!({ server: this.server });
    const tools = infos
      .map((info) => new TemporalMCPTool(this.server, info, this.activityOptions, this.prefix))
      .filter((tool) => included(tool, this.toolFilters));
    for (const tool of tools) this.listed.add(tool);
    return tools as never[];
  }

  /**
   * Re-list this server's tools and reconcile them into the agent's tool
   * registry, dropping tools that no longer exist and adding new ones, so a
   * mid-workflow MCP-server restart is reflected in the next call.
   * {@link TemporalAgent} instead re-lists each of its `cacheTools: false`
   * clients in its own hook after each tool round, and checks and applies
   * their listings together, so a name can move between its servers.
   *
   * A listed name that another tool of the agent has, that differs from one
   * only by `-` or `_`, that the listing has twice, or that Strands rejects
   * throws Strands' `ToolValidationError` and leaves the registry as it was.
   */
  async refreshTools(registry: ToolRegistryView): Promise<void> {
    const tools = (await this.listTools()) as unknown as TemporalMCPTool[];
    TemporalMCPClient.applyListings(registry, [{ client: this, tools }]);
  }

  /**
   * One `AfterToolsEvent` hook per client in `clients`, the `cacheTools: false` clients of one agent
   * in `tools` order, to register in that order. Strands runs them last to first. Each lists its
   * server as {@link refreshTools} does, so the `listTools` activities keep the order and timing
   * they had with one `refreshTools()` hook per client, unless a re-listing is refused: then every
   * server has listed before the refusal. The last client's hook starts the tool round, and the
   * first client's checks the round's listings together and applies them. So a name can move from
   * one server to another between tool rounds, and a name that two of the new listings share is
   * refused.
   *
   * @internal
   */
  static refreshHooks(clients: TemporalMCPClient[]): ((event: RefreshEvent) => Promise<void>)[] {
    const round: Listing[] = [];
    return clients.map(
      (client, i) => (event: RefreshEvent) =>
        client.listInRound(event.agent.toolRegistry, round, i === clients.length - 1, i === 0)
    );
  }

  // List this server into `round`, which `start` empties first, and apply the round at its `end`.
  private async listInRound(registry: ToolRegistryView, round: Listing[], start: boolean, end: boolean): Promise<void> {
    if (start) round.length = 0;
    const tools = (await this.listTools()) as unknown as TemporalMCPTool[];
    round.unshift({ client: this, tools });
    if (end) TemporalMCPClient.applyListings(registry, round);
  }

  /**
   * Make each of `clients` hold the name of each tool it listed that Strands adds to `registry`,
   * the agent's registry, when Strands adds it. Strands adds a listing tool by tool and stops at a
   * refused one, and a listing of an initialization that failed can add its tools after the agent
   * initialized again, so only the tools that are added count, whenever they are.
   *
   * @internal
   */
  static holdAdded(clients: TemporalMCPClient[], registry: ToolRegistryView & { add(tool: never): void }): void {
    const add = registry.add.bind(registry);
    registry.add = (tool: never) => {
      try {
        add(tool);
      } finally {
        const present = new Set<object>(registry.list());
        for (const added of [tool].flat() as { name: string }[]) {
          const client = clients.find((c) => c.listed.has(added));
          if (client === undefined || !present.has(added)) continue;
          const names = client.held.get(registry) ?? new Set();
          client.held.set(registry, names.add(added.name));
        }
      }
    };
  }

  // The names this client's tools hold in `registry`.
  private heldIn(registry: ToolRegistryView): Set<string> {
    return this.held.get(registry) ?? new Set(registry.list().flatMap((t) => (this.listed.has(t) ? [t.name] : [])));
  }

  // Check `listings` against each other, the agent's other tools and Strands' name rules, then apply
  // them: drop the names each client held and no longer lists, and add or replace the listed tools.
  // A refusal throws Strands' `ToolValidationError` and changes nothing.
  // Harness: renamed from `apply`, which TypeScript 7 with noImplicitOverride reads as overriding
  // the class's static `Function.prototype.apply`.
  private static applyListings(registry: ToolRegistryView, listings: Listing[]): void {
    const held = new Map(listings.map(({ client }) => [client, client.heldIn(registry)]));
    const freed = new Set([...held.values()].flatMap((names) => [...names]));
    const taken = new Map<string, { name: string }>();
    for (const tool of registry.list()) {
      if (!freed.has(tool.name)) taken.set(normalized(tool.name), tool);
    }
    const seen = new Map<string, TemporalMCPTool>();
    for (const { client, tools } of listings) {
      const own = new Map<string, TemporalMCPTool>();
      for (const tool of tools) {
        const { name } = tool;
        const rejected = rejection(name);
        if (rejected !== undefined) {
          throw new ToolValidationError(
            `${rejected}. MCP server ${listedAs(tool)} lists '${name}'. Drop it with the toolFilters of the ` +
              `server's TemporalMCPClient.`
          );
        }
        const first = own.get(normalized(name));
        if (first !== undefined) {
          const both = first.name === name ? 'it twice' : 'both names';
          throw duplicateToolName(name, first.name, `MCP server '${client.server}' lists ${both}.`);
        }
        own.set(normalized(name), tool);
        const earlier = seen.get(normalized(name));
        if (earlier !== undefined) throw duplicateToolName(name, earlier.name, clash(earlier, tool));
        const other = taken.get(normalized(name));
        if (other !== undefined) {
          const source =
            other instanceof TemporalMCPTool && !client.listed.has(other)
              ? clash(other, tool)
              : `MCP server ${listedAs(tool)} lists it, and another tool of the agent has it. Set a prefix (or a ` +
                `different one) on the server's TemporalMCPClient, or drop '${tool.serverToolName}' with its toolFilters.`;
          throw duplicateToolName(name, other.name, source);
        }
      }
      for (const [key, tool] of own) seen.set(key, tool);
    }
    for (const { client, tools } of listings) {
      const names = new Set(tools.map((t) => t.name));
      for (const name of held.get(client)!) {
        if (!names.has(name)) registry.remove(name);
      }
    }
    for (const { client, tools } of listings) {
      registry.addOrReplace(tools as never[]);
      client.held.set(registry, new Set(tools.map((t) => t.name)));
    }
  }
}

/** The part of Strands' `AfterToolsEvent` that {@link TemporalMCPClient.refreshHooks} reads. */
interface RefreshEvent {
  agent: { toolRegistry: ToolRegistryView };
}

// One client's new listing.
interface Listing {
  client: TemporalMCPClient;
  tools: TemporalMCPTool[];
}

// Strands' McpClient filter rule: whether `filters` let the agent have `tool`.
function included(tool: TemporalMCPTool, filters: McpToolFilters | undefined): boolean {
  if (!filters) return true;
  if (filters.allowed !== undefined && !filters.allowed.some((m) => matches(tool, m))) return false;
  return !(filters.rejected !== undefined && filters.rejected.some((m) => matches(tool, m)));
}

function matches(tool: TemporalMCPTool, matcher: McpToolMatcher): boolean {
  if (typeof matcher === 'function') return matcher(tool);
  if (typeof matcher === 'string') return matcher === tool.serverToolName;
  // The sticky flag anchors the match at the start of the name, as Strands does.
  const anchored = new RegExp(matcher.source, matcher.flags.includes('y') ? matcher.flags : `${matcher.flags}y`);
  return anchored.test(tool.serverToolName);
}

// Strands' words when its ToolRegistry rejects a tool name, or undefined when it accepts it.
function rejection(name: string): string | undefined {
  if (name.length < 1 || name.length > 64) return 'Tool name must be between 1 and 64 characters';
  if (!/^[a-zA-Z0-9_-]+$/.test(name)) return 'Tool name must contain only alphanumeric characters, hyphens, and underscores';
  return undefined;
}

// Strands' ToolRegistry refuses a name that differs from a registered one only by '-' or '_'.
function normalized(name: string): string {
  return name.replaceAll('-', '_');
}

// The server that lists `tool`, with the server's own name for it when a prefix renames it.
function listedAs(tool: TemporalMCPTool): string {
  return tool.serverToolName === tool.name ? `'${tool.server}'` : `'${tool.server}' (as '${tool.serverToolName}')`;
}

// Where two clashing MCP tools of two clients come from: `held` is the agent's, and `tool` the new one.
function clash(held: TemporalMCPTool, tool: TemporalMCPTool): string {
  return held.server === tool.server
    ? `The agent has two TemporalMCPClient handles for MCP server '${tool.server}'. Give one a different ` +
        `prefix, narrow one's toolFilters, or pass only one.`
    : `MCP servers ${listedAs(held)} and ${listedAs(tool)} both list it. Set a prefix (or a different one) on ` +
        `one server's TemporalMCPClient, or drop the tool from one with its toolFilters.`;
}

// Strands' words when its ToolRegistry refuses a name, then where the two tools come from.
function duplicateToolName(name: string, other: string, source: string): ToolValidationError {
  const refused =
    other === name ? `Tool with name '${name}' already registered.` : `Tool name '${name}' already exists as '${other}'.`;
  return new ToolValidationError(`${refused} ${source}`);
}

export function listToolsActivityName(server: string): string {
  return `${server}-listTools`;
}

export function callToolActivityName(server: string): string {
  return `${server}-callTool`;
}
