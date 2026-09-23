// The type parameter a session is generic over: an agent's handlers and observable state,
// as `harness-codegen` generates them. Untyped when there is no generated type to give.

export interface AgentSchema {
  handlers: Record<string, { input: unknown; output: unknown }>;
  states: Record<string, unknown>;
}

/** Any agent, discovered at runtime rather than generated: payloads and state are open JSON. */
export interface UntypedAgent extends AgentSchema {
  handlers: Record<string, { input: Record<string, unknown>; output: Record<string, unknown> }>;
  states: Record<string, unknown>;
}

export type HandlerName<A extends AgentSchema> = keyof A["handlers"] & string;
export type HandlerInput<A extends AgentSchema, H extends HandlerName<A>> = A["handlers"][H]["input"];
export type HandlerOutput<A extends AgentSchema, H extends HandlerName<A>> = A["handlers"][H]["output"];
export type StateId<A extends AgentSchema> = keyof A["states"] & string;
