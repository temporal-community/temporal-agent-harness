import type { JSONValue, McpClient } from '@strands-agents/sdk';
import { activityInfo, Context as ActivityContext } from '@temporalio/activity';
import {
  callToolActivityName,
  listToolsActivityName,
  type CallToolInput,
  type McpToolInfo,
} from '@temporalio/agent-harness/ai-sdks/strands-agents';

/**
 * Builds the per-server `{server}-listTools` activity. Enumerates the server's
 * tools live over the plugin's connection on every call, so the agent sees the
 * server's current tools even if it is redeployed mid-workflow. Runs whenever a
 * workflow calls {@link TemporalMCPClient.listTools}.
 */
export function buildListToolsActivity(
  server: string,
  factory: () => McpClient,
  connections: McpConnections
): (input: { server: string }) => Promise<McpToolInfo[]> {
  const name = listToolsActivityName(server);
  const fn = async (_input: { server: string }): Promise<McpToolInfo[]> => {
    const record = connections.acquire(server, factory);
    try {
      const client = await record.client;
      refuseFactoryPrefix(server, client);
      const tools = await client.listTools();
      return tools.map((t) => ({
        name: t.name,
        description: t.description,
        inputSchema: t.toolSpec.inputSchema,
      }));
    } catch (err) {
      // The session may be broken (e.g. the server was redeployed); stop handing
      // it out so the next call reconnects to the current deployment. It is
      // closed once the calls still using it have finished.
      connections.retire(server, record);
      throw err;
    } finally {
      // Closes the connection if it was retired and this was the last in-flight
      // call; otherwise lets idle eviction resume.
      connections.release(server, record);
    }
  };
  Object.defineProperty(fn, 'name', { value: name });
  return fn;
}

// A prefix on the factory's McpClient renames the tools the agent sees, and the callTool activity
// then asks the server for those names. Refuse it, retryably, so a run waits for a fixed worker.
function refuseFactoryPrefix(server: string, client: McpClient): void {
  // Strands' McpClient keeps the option in a private field.
  const prefix = (client as unknown as { _prefix?: string })._prefix;
  if (prefix) {
    throw new Error(
      `The McpClient that the StrandsPlugin mcpClients factory returns for MCP server '${server}' sets ` +
        `prefix '${prefix}'. Set it on the workflow-side TemporalMCPClient instead: ` +
        `new TemporalMCPClient({ server: '${server}', prefix: '${prefix}' }).`
    );
  }
}

/**
 * Default for how long an *idle* MCP connection is kept open before it's
 * disconnected. The window only starts once no {@link callToolActivityName |
 * callTool} or {@link listToolsActivityName | listTools} activity is using the
 * connection, so a call that runs longer than this is never cut off. Override
 * per plugin via `StrandsPlugin({ mcpConnectionIdleTimeout })`.
 */
export const MCP_CONNECTION_IDLE_MS = 5 * 60 * 1000;

/** A worker-process MCP connection plus the state governing its idle eviction. */
interface ConnectionRecord {
  /** Resolves to the connected client, or rejects if the handshake failed. */
  client: Promise<McpClient>;
  /** Number of activities currently using this connection. */
  inflight: number;
  /**
   * Set after a failed call, or when the last Worker using the plugin stops
   * while a call still uses the connection. A retired record is no longer
   * handed out to new calls, and is only disconnected once `inflight` drops
   * back to 0, so one call can't close the session under its siblings.
   */
  retired: boolean;
  /** Armed only while `inflight` is 0. */
  idleTimer?: NodeJS.Timeout;
}

function clearIdleTimer(record: ConnectionRecord): void {
  if (record.idleTimer !== undefined) {
    clearTimeout(record.idleTimer);
    record.idleTimer = undefined;
  }
}

/** Disconnect a record that is no longer handed out. Best-effort. */
async function closeRecord(record: ConnectionRecord): Promise<void> {
  try {
    const client = await record.client;
    await client.disconnect();
  } catch {
    // Best-effort; the session may already be broken.
  }
}

/**
 * The worker-process MCP connections of one {@link StrandsPlugin}, keyed by
 * server name. Workers that share a plugin instance share its connections;
 * another plugin instance opens its own through its own factories, even for
 * the same server name. The plugin's `runContext` counts the Workers using
 * them, and the last one to stop closes them. A connection that a call still
 * uses is closed when that call finishes.
 */
export class McpConnections {
  private readonly records: Map<string, ConnectionRecord> = new Map();
  private workers = 0;
  // Harness: a field, not a constructor parameter property, under the repo's compiler settings.
  private readonly idleMs: number;

  /**
   * @param idleMs how long an idle connection stays open; defaults to
   * {@link MCP_CONNECTION_IDLE_MS}.
   */
  constructor(idleMs: number = MCP_CONNECTION_IDLE_MS) {
    this.idleMs = idleMs;
  }

  /** Runs one Worker; once no Worker uses these connections, closes them. */
  async use(next: () => Promise<void>): Promise<void> {
    this.workers += 1;
    try {
      await next();
    } finally {
      this.workers -= 1;
      if (this.workers === 0) {
        await Promise.all([...this.records.keys()].map((server) => this.evict(server)));
      }
    }
  }

  /**
   * Stop handing out `server`'s connection, and disconnect it now or when the
   * last call still using it finishes.
   */
  async evict(server: string): Promise<void> {
    const record = this.records.get(server);
    if (record === undefined) return;
    // Delete before awaiting: a call arriving during `disconnect` must open a
    // fresh connection rather than join the one being torn down.
    this.records.delete(server);
    clearIdleTimer(record);
    if (record.inflight > 0) {
      record.retired = true;
      return;
    }
    await closeRecord(record);
  }

  /**
   * Return the cached connection for `server`, opening one lazily if needed,
   * and mark a call in flight so idle eviction can't disconnect underneath it.
   * Concurrent first-callers dedupe onto a single connect handshake by awaiting
   * the same record. Every caller must {@link release} once its call completes.
   */
  acquire(server: string, factory: () => McpClient): ConnectionRecord {
    let record = this.records.get(server);
    if (record === undefined) {
      const entry: ConnectionRecord = {
        inflight: 0,
        retired: false,
        client: (async () => {
          const client = factory();
          await client.connect();
          return client;
        })(),
      };
      // If the connect handshake fails, drop the rejected promise so the next
      // call retries instead of caching the failure forever.
      entry.client.catch(() => {
        if (this.records.get(server) === entry) this.records.delete(server);
      });
      this.records.set(server, entry);
      record = entry;
    }
    record.inflight += 1;
    clearIdleTimer(record);
    return record;
  }

  /**
   * Flag `record` as suspect after a failed call. It is unpublished at once so
   * new calls open a fresh connection, but not disconnected until the calls
   * still using it have released it (see {@link release}).
   */
  retire(server: string, record: ConnectionRecord): void {
    // Already evicted or replaced; whoever did that is closing it.
    if (this.records.get(server) !== record) return;
    record.retired = true;
    this.records.delete(server);
    clearIdleTimer(record);
  }

  /**
   * Mark a call done. Once no calls remain in flight, disconnect a retired
   * connection or arm idle eviction for a healthy one.
   */
  release(server: string, record: ConnectionRecord): void {
    record.inflight -= 1;
    if (record.retired) {
      if (record.inflight === 0) void closeRecord(record);
      return;
    }
    // Only the record still cached under this server arms a timer; a record
    // already evicted must not schedule one, or it could later evict a
    // different, healthy connection for the same server.
    if (record.inflight > 0 || this.records.get(server) !== record) return;
    const timer = setTimeout(() => {
      // `acquire` clears this timer synchronously, so a pending timer implies
      // the connection is still idle; re-checked to keep that invariant local.
      if (record.inflight === 0) void this.evict(server);
    }, this.idleMs);
    // Don't let an idle MCP connection keep the worker process alive.
    timer.unref?.();
    record.idleTimer = timer;
  }
}

/**
 * Builds the per-server `{server}-callTool` activity. Reuses the plugin's MCP
 * session, opened lazily through the user-supplied factory — successive calls
 * share one connection rather than reconnecting per call. Idle connections are
 * disconnected after the plugin's idle timeout.
 */
export function buildCallToolActivity(
  server: string,
  factory: () => McpClient,
  connections: McpConnections
): (input: CallToolInput) => Promise<JSONValue> {
  const name = callToolActivityName(server);
  const fn = async (input: CallToolInput): Promise<JSONValue> => {
    ActivityContext.current().log.debug(`Calling MCP tool ${input.toolName} on server ${server}`, {
      activityId: activityInfo().activityId,
    });
    const record = connections.acquire(server, factory);
    // Dispatch by name without re-listing tools on the connection: the agent
    // already has the schema from `listTools`, and `McpClient.callTool` only
    // reads `tool.name` to build the request. A minimal `{ name }` matches the
    // by-name dispatch the Python SDK does via `session.call_tool`.
    const tool = { name: input.toolName } as unknown as Parameters<McpClient['callTool']>[0];
    try {
      const client = await record.client;
      return await client.callTool(tool, input.args);
    } catch (err) {
      // The session may be broken; stop handing it out so the next call
      // reconnects. It is closed once the calls still using it have finished.
      connections.retire(server, record);
      throw err;
    } finally {
      // Closes the connection if it was retired and this was the last in-flight
      // call; otherwise lets idle eviction resume.
      connections.release(server, record);
    }
  };
  Object.defineProperty(fn, 'name', { value: name });
  return fn;
}
