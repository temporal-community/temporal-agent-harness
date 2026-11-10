# @temporalio/agent-harness-worker

The worker side of the Temporal agent harness: `AgentHarnessPlugin` and the harness's activities,
the agent client, Code Mode's Monty activities, and the `agent-harness` command
(`init`, `schema`, `skills`, `doctor`; `agent-harness-schema` is the same `schema` command). Agents themselves are written against
[`@temporalio/agent-harness`](https://github.com/temporal-community/temporal-agent-harness/blob/main/typescript/packages/core/README.md), which runs in the workflow.

## Getting started

Follow the [TypeScript quickstart](https://github.com/temporal-community/temporal-agent-harness/tree/main/typescript/packages/core#readme)
for a complete npm install, agent, worker, registry, and first console session. It includes the
pinned Temporal and OpenAI dependencies and explains the startup processes.

The harness has one server, session manager, and console for both SDKs. They come from the
`temporal-agent-harness` Python package; `uvx` runs them without a Python project of your own.
Your agents run on your TypeScript worker.

## Worker configuration

The worker is an ordinary Temporal worker, so it can host other workflows and activities too.
`AgentHarnessPlugin` adds the harness's activities, payload offload, activity tool bodies
(`tools: [toolActivity(tool, impl), ...]`), and Code Mode when `@pydantic/monty` is installed.
Register it after your model integration's plugin, such as `OpenAIAgentsPlugin`,
`GoogleGenAIPlugin` or `StrandsPlugin`. The OpenAI plugin must be imported from this package's
`/ai-sdks/openai-agents` entry point to enable harness streaming. `StrandsPlugin` comes from
`/ai-sdks/strands-agents`; pass it `observerFactory: harnessObserverFactory` from
`/ai-sdks/strands-agents-harness` to stream onto the turn.

Keep the underlying Temporal SDK packages at the version required by the model integration.
The `@temporalio/agent-harness*` packages have their own, separate version. The quickstart
lists a complete pinned installation.

## Schema CLI and init data

An agent, in `agents.ts`:

```ts
import { z } from "zod";
import {
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  defineAgent,
  handler,
} from "@temporalio/agent-harness";

export const BriefedAgent = defineAgent({
  name: "BriefedAgent",
  approvalPolicyDefault: ToolApprovalPolicy.alwaysRequireHumanApproval(),
  initData: { schema: z.object({ topic: z.string() }).meta({ title: "Brief" }), required: true },
  handlers: {
    ask: handler({ description: "Ask about the topic.", input: TextMessage, output: TextReply }),
  },
  create: ({ data }) => ({ ask: async ({ text }) => ({ text: `${data.topic}: ${text}` }) }),
});
```

Add `closeAfterIdleDefault: "30 minutes"` to close an agent once no one has sent it a message
for that long, such as one behind a chat box whose session is not kept. An idle agent consumes no
compute while it waits, so leave this out for agents you want to keep. A caller overrides it with
`close_after_idle` (seconds, `0` for never) on the `AgentConfig`. Subagents never close on idle:
their parent decides when they end.

Its worker, in `worker.ts` (compile both files before running):

```ts
import { fileURLToPath } from "node:url";
import { loadClientConnectConfig } from "@temporalio/envconfig";
import { NativeConnection, Worker } from "@temporalio/worker";
import { AgentHarnessPlugin } from "@temporalio/agent-harness-worker";

const { connectionOptions, namespace } = loadClientConnectConfig();
const worker = await Worker.create({
  connection: await NativeConnection.connect(connectionOptions),
  namespace: namespace ?? "default",
  taskQueue: "briefed-agent-task-queue",
  workflowsPath: fileURLToPath(new URL("./agents.js", import.meta.url)),
  plugins: [new AgentHarnessPlugin()],
});
await worker.run();
```

The registry the server reads, `agents.toml`:

```toml
[[agents]]
key = "briefed"
workflow_type = "BriefedAgent"
task_queue = "briefed-agent-task-queue"
label = "Briefed"
description = "Answers questions about the topic it is started with."
schema = "BriefedAgent.schema.json"   # optional; see below
```

`schema` is the agent's schema document, relative to the registry. From it the server reads what
the agent takes to start, here a required `Brief`, so the console asks for it in a form before
starting a session; the server never imports your code. Write it, and again whenever the agent's
init data changes:

```bash
npx agent-harness schema ./dist/agents.js#BriefedAgent -o BriefedAgent.schema.json
```

It takes `<module>#<export>`: a module path relative to the current directory, TypeScript
(Node 22.18 or later runs it directly) or compiled JavaScript, and the name the agent is exported
as. Without `-o` it prints the document.

## Serving and connection configuration

Follow [Run the web UI](https://github.com/temporal-community/temporal-agent-harness/tree/main/typescript/packages/core#run-the-web-ui)
for the startup commands. The console and session manager use the registry and generated schema;
the server never imports your TypeScript code. Keep `workflow_type` equal to the agent's
`defineAgent({ name })`, and `task_queue` equal to the queue your worker polls.

`loadClientConnectConfig()` and the Python CLI resolve the Temporal connection from `TEMPORAL_*`
environment variables and profiles. With nothing configured, they use `localhost:7233` and
namespace `default`. For a project-local file, export `TEMPORAL_CONFIG_FILE` explicitly in every
service terminal; a `temporal.toml` in the current directory is not picked up automatically.
Use the same `TEMPORAL_PROFILE` and connection overrides for all services.

If you use `TASK_QUEUE_PREFIX`, set it consistently for the console, session manager, and worker,
and resolve the worker queue with `taskQueueName("briefed-agent-task-queue")`, exported by this
package. The registry keeps the unprefixed queue name; the server applies the prefix.

See the [registry and server documentation](https://github.com/temporal-community/temporal-agent-harness#run-the-web-ui--no-checkout-no-node)
and [quickstart troubleshooting](https://github.com/temporal-community/temporal-agent-harness/tree/main/typescript/packages/core#troubleshooting).
