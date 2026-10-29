// Compile-time checks on the committed protocol types: `tsc` fails the build if any of these
// stop holding.

import type { AgentEvent, AgentEventType, AgentStreamItem, CallbackRequested } from "../src/protocol.ts";

type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
const holds = <T extends true>(): T => true as T;

holds<Equals<AgentEvent["event"], AgentStreamItem>>();
holds<Equals<Extract<AgentStreamItem, { type: "callback_requested" }>, CallbackRequested>>();
holds<Equals<Extract<AgentEventType, "reply_delta" | "state_patch">, "reply_delta" | "state_patch">>();

// The envelope names the message that caused an event, or null for turn brackets.
holds<Equals<AgentEvent["message_id"], string | null>>();
