// Accepted message types for the Monty dynamic agent (Python's models.py).

import { z } from "zod";

export const RunScript = z.object({ script: z.string().meta({ title: "Script" }) }).meta({
  title: "RunScript",
  description:
    "A Python script to run in the sandbox for this turn.\n\n" +
    "The `run_script` operation's description documents the full sandbox contract — the async " +
    "script structure, the concurrency rules, and the exact host-function signatures and result " +
    "shapes the `script` may call. A type/syntax/runtime error in the script is reported as the " +
    "reply text (a bad script is normal input, not a failure).",
});
