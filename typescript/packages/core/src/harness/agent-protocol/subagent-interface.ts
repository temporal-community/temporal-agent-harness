// The subagent-turn activity contract between a parent agent's workflow and the harness activity
// that drives one turn on a child (Python's agent_protocol/subagent_interface.py).

import { z } from "zod";
import { messagePayload } from "./agent-interface.ts";
import { TurnStreamContextSchema } from "../stream-context.ts";

export const RUN_SUBAGENT_TURN_ACTIVITY = "run_subagent_turn";
export const DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT = "20 seconds";
export const DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT = "1 hour";

export const RunSubagentTurnInputSchema = z.object({
  child_workflow_id: z.string(),
  type: z.string(),
  payload: messagePayload,
  from_offset: z.number().int().default(0),
  handle: z.string(),
  agent_key: z.string(),
  parent_stream_context: TurnStreamContextSchema,
});
export type RunSubagentTurnInput = z.output<typeof RunSubagentTurnInputSchema>;

export interface SubagentTurnResult {
  output: Record<string, unknown>;
  turn_id: string;
  turn_number: number;
  consumed_offset: number;
}
