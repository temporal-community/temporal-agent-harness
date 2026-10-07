# Hello-world OpenAI Codex agent

The smallest interesting agent you can build on the harness with the **OpenAI Codex** agent loop:
it chats in plain text and can call one tool (`get_weather`). Unlike the other AI-SDK examples,
Codex is not a model-calling library — its loop runs in a `codex app-server` process — so the
adapter (`temporal_agent_harness.ai_sdks.codex_harness`) makes the *process* durable.

## What it demonstrates

- **Every tool call is a harness tool.** Codex's own built-in tools (shell, `apply_patch`, goals,
  …) are turned off for the thread; the tools the model sees are the agent's harness tools,
  offered to Codex as *dynamic tools*. Each call runs through `runner.run_tool(...)`, so the
  harness keeps approvals and `tool_requested` → `tool_start` → `tool_end`. Activity tools
  (`@agent.activity_tool_defn`) run as Activities; `agent.subagent_toolset(...)` tools start real
  child agent workflows, so subagents are durable too.
- **The conversation lives in the workflow.** Each turn is one or more *segments*. A segment is
  one Activity: it rebuilds a fresh `CODEX_HOME` from the rollout the workflow holds, resumes the
  thread, injects the real output of the previous tool call, and runs the turn. When the model
  calls a tool the segment ends and the workflow runs the tool, then starts the next segment. A
  Worker dying mid-turn just retries the segment from the last committed rollout — and a tool that
  already ran is not run again.
- **Live streaming.** The segment publishes `reply_delta` and a `model_interaction_started` /
  `…_ended` span (with token usage) to the turn stream.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `CodexHelloAgent` — the harness agent; one `ask` handler, one `get_weather` tool, driven by a `CodexSession`. |
| `worker.py` | Worker hosting the workflow; `CodexHarnessPlugin` registers the segment Activity and holds the Codex config/credentials, `AgentHarnessPlugin` (last) supplies the harness's converter and activities. |
| `agents.toml` | Registry entry that makes this agent selectable in the shared web UI. |

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set your Temporal connection profile.
Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. serves API + UI on http://localhost:8000
just worker            # 4. the agent worker — needs OPENAI_API_KEY
# or, with no credentials:
just worker-fake       # 4'. the same worker against a scripted fake model
```

With `just worker-fake` the "model" follows `CALL:<tool>|<json>` lines in your message, e.g. send:

```
What's the weather?
CALL:get_weather|{"city":"Paris"}
```

and you'll see the tool card (requested → start → end) and the reply streamed back.

## Caveats

This adapter is experimental. It uses Codex app-server APIs that are themselves experimental
(`dynamicTools`, `thread/inject_items`) and depends on the rollout file format, so the Codex
version is pinned (the `codex` extra). A tool call ends a segment, so each call restarts the
app-server (~0.1s). The shell, goals, `view_image` and Codex's own subagents are turned off per
thread. `apply_patch` has no per-thread switch (removing it takes a model-catalog override, which
this adapter does not do yet), so a real model's tool list may still include it — only the fake
model has been checked. Give the agent harness tools for anything it should do; failover of files a
tool writes to disk is the tool's business.
