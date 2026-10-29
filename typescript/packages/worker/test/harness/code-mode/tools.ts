// Harness tools the Code Mode tests render and run scripts against.

import { z } from "zod";
import { tool as harnessTool, type AnyHarnessTool } from "@temporalio/agent-harness";

/** A tool with no body: what Code Mode renders from it is all these tests read. */
export function stubTool<I extends z.ZodObject<z.ZodRawShape>>(
  name: string,
  input: I,
  output: z.ZodType | null = z.string(),
  description = `Do the ${name} thing.`,
): AnyHarnessTool {
  return harnessTool({
    name,
    description,
    inputSchema: input,
    ...(output ? { outputSchema: output } : {}),
    execute: async () => "" as never,
  }) as unknown as AnyHarnessTool;
}

export const beta = stubTool("beta", z.object({ x: z.number().int() }), z.number().int());
export const Box = z.object({ size: z.number().int() }).meta({ title: "Box" });
export const pack = stubTool("pack", z.object({ box: Box }), Box);
