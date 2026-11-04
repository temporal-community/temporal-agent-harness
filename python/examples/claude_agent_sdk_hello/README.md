# Hello-world Claude Agent SDK agent

A chat agent built on the Claude Agent SDK. Claude Code runs in a subprocess of the Worker, with its
own tools, and chats about the files in its working directory. It can hand a search to a Claude Code
subagent (`file-finder`) and call a custom tool (`current_time`). It asks before each Bash call.

The agent uses Temporal's
[Claude Agent SDK plugin](https://docs.temporal.io/develop/python/integrations/claude-agent-sdk)
as is. Claude Code keeps its own agent loop.

## What it shows

- Claude Code keeps its own tools and agent loop, and the harness still shows its work. You see each
  tool call in the web UI, and the subagent appears nested under the parent.
- You approve or deny each Bash call in the UI before it runs.
- Your own tools work too. `current_time` is a harness tool that Claude Code can call, and it
  follows the same approval policy.
- Claude doesn't forget. The conversation carries on across messages.
- A message survives a Worker or Claude Code crash. When either comes back, the turn starts again
  from your message, and a subagent carries on as the same subagent. An approval you were asked for is still the one to answer, and a call you already
  approved isn't asked again. Claude Code's tools may run again, though: a command cut off by the
  crash, or finished before it, can run a second time. A harness tool like `current_time` runs
  once when Claude repeats the same call.

## Layout

| File | Role |
|---|---|
| `workflow.py` | The agent. One `ask` handler sends each message to Claude Code. |
| `worker.py` | The Worker, using `ClaudeAgentSDKPlugin` and `AgentHarnessPlugin`. |
| `agents.toml`, `ClaudeAgentSDKHelloAgent.schema.json` | List this agent in the shared web UI. Generated from `BUILD.bazel` (`bazel run :agents_update`). |

There is no client of its own. Like the other examples, it runs on the shared example stack: the
session-manager worker, the API server, and the web UI.

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `ANTHROPIC_API_KEY`. The
claude-agent-sdk package bundles Claude Code, so there is nothing else to install. Then run these from this directory, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

Open http://localhost:8000, pick **Claude Agent SDK Hello**, and chat. For example, *"Find the
files that define tool approvals"* should go to the subagent, and *"How many Python files are
there? Use Bash."* waits for you to approve the command.

Claude Code works in the worker's directory, the repository root under `just worker`, and can read
anything the worker's user can. Beyond a demo, see the plugin's
[hardening notes](https://github.com/temporalio/ai-integrations/tree/main/python/claude_agent_sdk#hardening).

Without `just`, the equivalent commands (from the `python/` directory):

```sh
uv run --group examples temporal-agent-harness session-manager
uv run --group examples temporal-agent-harness serve examples/claude_agent_sdk_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.claude_agent_sdk_hello.worker
```
