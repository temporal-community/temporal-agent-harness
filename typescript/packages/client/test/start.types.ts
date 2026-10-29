// Compile-time checks that startSession asks for exactly the init data an agent declares:
// required, optional, or none. `tsc` fails the build if any of these stop holding. The agent
// types and definitions have the shape harness-codegen emits.

import { startSession, type SessionStarter } from "../src/start.ts";

interface Trip {
  traveler: string;
}
interface Nothing {
  handlers: Record<never, never>;
  states: Record<never, never>;
}

interface RequiredAgent extends Nothing {
  initData: { data: Trip; required: true };
}
interface OptionalAgent extends Nothing {
  initData: { data: Trip; required: false };
}
interface NoDataAgent extends Nothing {
  initData: null;
}
/** Written by hand before init data existed: it says nothing about it. */
type LegacyAgent = Nothing;

const RequiredAgent: { readonly workflowType: "RequiredAgent"; readonly schema?: RequiredAgent } = {
  workflowType: "RequiredAgent"
};
const OptionalAgent: { readonly workflowType: "OptionalAgent"; readonly schema?: OptionalAgent } = {
  workflowType: "OptionalAgent"
};
const NoDataAgent: { readonly workflowType: "NoDataAgent"; readonly schema?: NoDataAgent } = {
  workflowType: "NoDataAgent"
};
const LegacyAgent: { readonly workflowType: "LegacyAgent"; readonly schema?: LegacyAgent } = {
  workflowType: "LegacyAgent"
};

declare const starter: SessionStarter;
const trip: Trip = { traveler: "Ada" };

export async function checks(): Promise<void> {
  await startSession(starter, RequiredAgent, { data: trip });
  // @ts-expect-error required data must be sent
  await startSession(starter, RequiredAgent);
  // @ts-expect-error required data must be sent
  await startSession(starter, RequiredAgent, { sessionId: "s" });
  // @ts-expect-error the data must be the agent's model
  await startSession(starter, RequiredAgent, { data: { name: "Ada" } });

  await startSession(starter, OptionalAgent);
  await startSession(starter, OptionalAgent, { data: trip });
  await startSession(starter, OptionalAgent, { data: undefined });
  await startSession(starter, OptionalAgent, { sessionId: "s" });

  await startSession(starter, NoDataAgent);
  // @ts-expect-error the agent takes no init data
  await startSession(starter, NoDataAgent, { data: trip });

  await startSession(starter, LegacyAgent, { sessionId: "s" });

  // An agent known only at runtime: data is open JSON, and optional.
  await startSession(starter, { workflowType: "Whatever" });
  await startSession(starter, { workflowType: "Whatever" }, { data: { any: "thing" } });
}
