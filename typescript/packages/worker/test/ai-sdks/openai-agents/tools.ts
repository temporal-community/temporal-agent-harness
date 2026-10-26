// The probe agents' activity tool, apart from workflows.ts so the test's worker can register its
// body without importing the Agents SDK.

import { z } from "zod";
import { activityTool } from "@temporalio/agent-harness";

export const getWeatherActivity = activityTool({
  name: "get_weather",
  description: "Return the current weather for a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
});
