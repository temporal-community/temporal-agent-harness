// `ask_user`: the conversational agents' callback tool for asking the user something mid-turn
// (Python's ask_user.py).
//
// A callback tool has no worker-side body. When the model calls it, the turn pauses in the
// workflow and publishes callback_requested; the console shows the model's question with an
// answer box, and the answer comes back through the provide_callback_result update as the tool's
// result. So the model can ask whatever it needs, whenever it needs it, and carry on in the same
// turn instead of ending it to ask in a text reply.
//
// Each call asks one question. To ask several, the model calls it once per question in the same
// reply; the conversational loop runs a reply's calls together, so the console shows them all at
// once, each with its own answer box.

import { z } from "zod";
import { tool } from "@temporalio/agent-harness";

export const askUser = tool({
  name: "ask_user",
  description: `Ask the user ONE question and wait for their answer, without ending your turn. Use it
when you need something only the user can tell you (a traveler's name, a date, which flight
they want) to keep going. \`question\` must ask exactly one thing: never combine questions
("where from, where to, and when?" is three calls). To ask several things, make one call per
question in the same reply; they are all shown to the user at once. Pass \`options\` when the
answer is one of a few choices. The answer is the user's own words.`,
  inputSchema: z.object({
    question: z.string(),
    options: z.array(z.string()).nullable().default(null),
  }),
  outputSchema: z.string(),
});
