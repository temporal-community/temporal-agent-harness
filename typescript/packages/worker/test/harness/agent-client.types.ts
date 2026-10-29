// Compile-time checks on AgentClient typed from an agent definition: it knows the agent's handler
// names and payloads, and an untyped client takes any. `tsc` fails the build if any stop holding.

import type { AgentClient, MessagePayload, MessageType } from "../../src/harness/agent-client.ts";
import type { CodeModeProbeAgent } from "../fixtures/workflows.ts";

type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Extends<A, B> = [A] extends [B] ? true : false;
const holds = <T extends true>(): T => true as T;

holds<Equals<MessageType<typeof CodeModeProbeAgent>, "run">>();
holds<Extends<MessagePayload<typeof CodeModeProbeAgent, "run">, { text: string }>>();
holds<Equals<Parameters<AgentClient<typeof CodeModeProbeAgent>["submitMessage"]>[0], "run">>();
holds<Equals<MessageType<unknown>, string>>();
// @ts-expect-error -- not a handler of this agent
export type NotAHandler = MessagePayload<typeof CodeModeProbeAgent, "nope">;
