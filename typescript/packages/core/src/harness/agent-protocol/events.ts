// The agent's event-stream vocabulary (Python's agent_protocol/events.py). The event types are not
// redeclared here: they are the generated `Protocol` namespace of `@temporalio/agent-harness-client`,
// itself generated from contract/protocol.schema.json, so a payload this backend publishes is
// type-checked against exactly what the client and UI parse.

import type { Protocol } from "@temporalio/agent-harness-client";

export type AgentEvent = Protocol.AgentEvent;
export type AgentStreamItem = Protocol.AgentStreamItem;
export type AgentEventType = Protocol.AgentEventType;
export type TokenUsage = Protocol.TokenUsage;
export type StreamEventOf<T extends AgentEventType> = Extract<AgentStreamItem, { type: T }>;

// The pubsub topic the agent publishes its turn events on.
export const TURN_EVENTS_TOPIC = "turn_events";
