// The workflow bundles the recorded histories ran on, keyed by their directory under histories/.

import { fileURLToPath } from "node:url";
import type { ReplayWorkerOptions } from "@temporalio/worker";
import { AgentHarnessPlugin } from "../../../src/index.ts";
import { DEFAULT_PAYLOAD_STORAGE } from "../../../src/utils/large-payload.ts";
import { FIXTURES } from "../../support.ts";

/** The compiled scripted demo module, bundled at run time. */
export const SCRIPTED_DEMO = fileURLToPath(new URL("./scripted-demo.js", import.meta.url));

export type Bundle = "scripted-demo" | "fixtures";

/** How each bundle's worker ran: the scripted demo with AgentHarnessPlugin, the fixtures as test/support.ts runs them. */
export const BUNDLES: Record<Bundle, ReplayWorkerOptions> = {
  "scripted-demo": { workflowsPath: SCRIPTED_DEMO, plugins: [new AgentHarnessPlugin()] },
  fixtures: {
    workflowsPath: FIXTURES,
    dataConverter: { externalStorage: DEFAULT_PAYLOAD_STORAGE },
  },
};
