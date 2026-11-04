# TypeScript authoring

Use `@temporalio/agent-harness` in workflow modules and `@temporalio/agent-harness-worker` on the
Node worker. `npx agent-harness init ./my-agent` materializes a runnable OpenAI starter. The same
starter is installed with this skill under `assets/starter/`; read `agents.ts.tmpl` and
`worker.ts.tmpl` there for the complete wiring. Its exact Temporal SDK versions match the model
plugin; keep those versions aligned. Harness package versions are separate from the underlying
Temporal SDK versions. Compile ESM before starting.

A minimal agent, with a safe inline tool and a model-free handler:

```ts
import { z } from "zod";
import { TextMessage, TextReply, ToolApprovalPolicy, defineAgent, handler, tool } from "@temporalio/agent-harness";

export const getWeather = tool({
  name: "get_weather",
  description: "Return sample weather for a city.",
  inputSchema: z.object({ city: z.string() }),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async ({ city }) => `It's 72°F and sunny in ${city}.`,
});

export const HelloAgent = defineAgent({
  name: "HelloAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowInherentlySafe(),
  handlers: {
    ping: handler({ description: "Echo without a model call.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner }) => ({
    async ping({ text }) {
      return { text };
    },
  }),
});
```

The starter's `ask` handler adds the model turn: it passes `getWeather` through
`asOpenAIAgentTool(runner, ...)` and awaits `runStreamed(runner, sdkAgent, input, ...)`.

Public authoring imports come from `@temporalio/agent-harness`: `defineAgent`, `handler`, `tool`,
`state`, `TextMessage`, `TextReply`, and `ToolApprovalPolicy`. Use Zod schemas for inputs and
outputs. `defineAgent({ name, handlers, create })` returns the workflow export; `create` receives
its runner, init data, and declared state references. Keep Node imports and external I/O in the
worker or activities, not in the workflow module.

Use `ToolApprovalPolicy.allowInherentlySafe()` for tools explicitly marked `inherentlySafe`.
For OpenAI, import `asOpenAIAgentTool` and `runStreamed` from
`@temporalio/agent-harness/ai-sdks/openai-agents-harness`. Consume the stream and await completion.
Keep conversation history inside the agent instance. The worker's `OpenAIAgentsPlugin` comes from
`@temporalio/agent-harness-worker/ai-sdks/openai-agents`, followed by `AgentHarnessPlugin`.
Activity tool implementations are registered using `toolActivity` on the harness plugin.

Point `workflowsPath` at the compiled agent module. Generate its schema with
`agent-harness schema ./dist/agents.js#Export -o Agent.schema.json` (the `agent-harness-schema`
binary is the same command). The registry's `workflow_type`
must match `defineAgent`'s `name`, and its queue must match the worker. The schema CLI does not
start Temporal. The shared Python server reads the schema without importing TypeScript code.

The shared services come from the Python package: the starter's `npm run services` runs
`temporal-agent-harness dev agents.toml` through `uvx`, beside Temporal and the Node worker.
`npx agent-harness doctor --schema Agent.schema.json` checks the installed package, schema, API,
and agent worker availability. For direct Temporal connection and local TOML validation, use the
shared Python CLI's `doctor --registry agents.toml`. Both support `--json` and never reset services.
