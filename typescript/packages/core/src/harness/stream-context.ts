// Identifies the in-flight turn for code that publishes to the agent's stream from outside the
// workflow, e.g. an activity (Python's stream_context.py).

import { z } from "zod";

export const TurnStreamContextSchema = z.object({
  turn_id: z.string(),
  turn_number: z.number().int(),
  message_id: z.string().nullable().default(null),
  agent_id: z.string(),
});
export type TurnStreamContext = z.output<typeof TurnStreamContextSchema>;
