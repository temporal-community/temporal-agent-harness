---
name: temporal-harness-python
description: Build, run, test, and integrate Python agents using the Temporal Agent Harness SDK and shared harness API. Use for agent authoring, tools, approvals, state, session clients, or startup troubleshooting in applications using this SDK.
---

# Temporal Agent Harness for Python

Build against the installed package's public API. Read its version before choosing imports and
integration dependencies. These resources ship with the SDK; no repository checkout is required.
Preserve the application's chosen model provider and framework. The bundled OpenAI starter is an
example, not a requirement to replace an existing integration.

## Choose the task

- New agent or worker: read [authoring](references/authoring.md), then the bundled starter,
  which is installed next to this file and is the complete worked example for this release:
  [assets/starter/my_agent/workflow.py.tmpl](assets/starter/my_agent/workflow.py.tmpl) (agent, handlers, tool) and
  [assets/starter/my_agent/worker.py.tmpl](assets/starter/my_agent/worker.py.tmpl) (worker and plugins). Adapt it rather
  than re-deriving the wiring. The package's `init` command materializes it as a new project.
- Connect an application, stream replies, handle approvals, or generate client types: read
  [the shared API guide](references/api.md).
- State, Code Mode, callback tools, or subagents: read [advanced features](references/advanced.md).
- Validate behavior or troubleshoot startup: read [verification](references/verification.md).

Inside the harness repository checkout itself, use the `temporal-harness-repo` skill in its
`.agents/skills/` instead: there, Bazel and `just` replace these commands and registries are generated.

Load only the references relevant to the task. Use schemas and public types to discover the
agent's interface before searching implementation files. The project may also have its own
instructions; these resources describe SDK usage, not permission to change deployment settings.

## Runtime boundaries

An agent runs as a Temporal workflow on your Python worker. The console, HTTP API, and
session-manager worker currently come from the Python package. All services must connect to the
same Temporal namespace; worker queues must match `agents.toml`. Provider API keys belong on the
agent worker. Model integration plugins precede `AgentHarnessPlugin`.

Keep external I/O out of workflow code. Register activity tool bodies on the worker, regenerate
schemas after changing declarations, and verify a complete session using installed packages.
Verify with the starter's `ping` handler first: it needs no model call or provider key.
Do not import from repository-only `examples` or tests in an application.
