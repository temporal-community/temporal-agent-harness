// The probe agents' activity tool, apart from workflows.ts so the test's worker can register its
// body without importing the Strands SDK.

import { z } from "zod";
import { activityTool } from "@temporalio/agent-harness";

export const convertToCelsius = activityTool({
  name: "convert_to_celsius",
  description: "Convert a temperature from Fahrenheit to Celsius.",
  inputSchema: z.object({ fahrenheit: z.number() }),
  outputSchema: z.number(),
});

/** The MCP server name the test's StrandsPlugin registers. */
export const ECHO_MCP_SERVER = "echo";
