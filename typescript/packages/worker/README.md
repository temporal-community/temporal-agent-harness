# @temporalio/agent-harness-worker

The worker side of the Temporal agent harness: `runAgentWorker`, `AgentHarnessPlugin` and the
harness's activities, the agent client, Code Mode's Monty activities, and the
`agent-harness-schema` command. Agents themselves are written against
[`@temporalio/agent-harness`](../core/README.md), which runs in the workflow.

## Serving TypeScript agents

The harness has one server, session manager and console, for agents written with either SDK.
They come from the `temporal-agent-harness` Python package, which `uvx` runs without a Python
project of your own. Your agents run on your TypeScript worker.

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

Its worker, in `worker.ts`:

```ts
import { runAgentWorker } from "@temporalio/agent-harness-worker";

await runAgentWorker({
  agents: new URL("./agents.ts", import.meta.url),
  taskQueue: "briefed-agent-task-queue",
});
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
npx agent-harness-schema ./agents.ts#BriefedAgent -o BriefedAgent.schema.json
```

It takes `<module>#<export>`: a module path relative to the current directory, TypeScript
(Node 22.18 or later runs it directly) or compiled JavaScript, and the name the agent is exported
as. Without `-o` it prints the document.

Then, one command per terminal:

```bash
# 1. A local Temporal dev server (needs the `temporal` CLI).
temporal server start-dev

# 2. The session-manager worker, which starts your agents as child workflows.
uvx --from 'temporal-agent-harness[ui]' temporal-agent-harness session-manager

# 3. The console and JSON API on http://localhost:8000.
uvx --from 'temporal-agent-harness[ui]' temporal-agent-harness serve ./agents.toml

# 4. Your worker.
node worker.ts
```

`runAgentWorker` and both `temporal-agent-harness` commands resolve their Temporal connection from
the same `TEMPORAL_*` environment variables and `temporal.toml` profiles, so they land on the same
namespace; with nothing configured they use the dev server's `localhost:7233`. The registry format
and the server are described in the
[Python package's README](https://github.com/temporal-community/temporal-agent-harness#readme).
