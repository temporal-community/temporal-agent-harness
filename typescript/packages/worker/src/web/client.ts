// The connection rule Python's server and session manager use, for TypeScript workers and clients:
// Temporal's standard client config (TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE / TEMPORAL_* env),
// falling back to the `temporal server start-dev` defaults when nothing configures them.

import { Client, Connection, type ClientPlugin } from "@temporalio/client";
import { loadClientConnectConfig } from "@temporalio/envconfig";
import { NativeConnection } from "@temporalio/worker";
import { DEFAULT_PAYLOAD_STORAGE } from "../utils/large-payload.ts";

export const DEFAULT_TEMPORAL_ADDRESS = "localhost:7233";
export const DEFAULT_NAMESPACE = "default";

export function resolveConnectConfig(): {
  address: string;
  namespace: string;
  connectionOptions: Record<string, unknown>;
} {
  const { connectionOptions, namespace } = loadClientConnectConfig();
  const address = connectionOptions.address ?? DEFAULT_TEMPORAL_ADDRESS;
  return {
    address,
    namespace: namespace ?? DEFAULT_NAMESPACE,
    connectionOptions: { ...connectionOptions, address },
  };
}

// "No server running" is the commonest first-run failure; say which address was tried and how to
// change it, keeping the original as `detail:`. Any other error passes through untouched.
export async function withConnectHelp<T>(address: string, connect: () => Promise<T>): Promise<T> {
  try {
    return await connect();
  } catch (e) {
    if (
      !(e instanceof Error) ||
      (e.name !== "TransportError" && !e.message.includes("Failed to connect"))
    )
      throw e;
    throw new Error(
      `cannot reach a Temporal server at ${address}.\n` +
        "  Start one with `temporal server start-dev`, or point this command at a\n" +
        "  different cluster with --temporal-address HOST:PORT, TEMPORAL_ADDRESS, or a\n" +
        "  temporal.toml profile (see --help for the full resolution order).\n" +
        `  detail: ${e.message}`,
      { cause: e },
    );
  }
}

/** `plugins` are client plugins, e.g. the Temporal OpenTelemetry plugin for client spans. */
export async function connectClient({
  plugins,
}: { plugins?: ClientPlugin[] } = {}): Promise<Client> {
  const { address, namespace, connectionOptions } = resolveConnectConfig();
  const connection = await withConnectHelp(address, () => Connection.connect(connectionOptions));
  return new Client({
    connection,
    namespace,
    dataConverter: { externalStorage: DEFAULT_PAYLOAD_STORAGE },
    ...(plugins?.length ? { plugins } : {}),
  });
}

export async function connectWorker(): Promise<{
  connection: NativeConnection;
  namespace: string;
  address: string;
}> {
  const { address, namespace, connectionOptions } = resolveConnectConfig();
  return {
    connection: await withConnectHelp(address, () => NativeConnection.connect(connectionOptions)),
    namespace,
    address,
  };
}
