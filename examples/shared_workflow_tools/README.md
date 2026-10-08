# Shared workflow tools

One OpenAI Agents example demonstrates the SDK independent
`agent.child_workflow_as_tool()`, `agent.send_message_tool()`, and
`agent.AgentMessageInbox`. The model uses local fixtures, so no provider
credentials are needed. The SDK loop, harness approvals and tool events,
Temporal model activities, child workflows, and Signals execute for real.

The fixed scenario is "Why is the sky blue?". The coordinator calls a research
child, receives its progress Signal, and sends the result to a persistent
mailbox. Agents and mailboxes remain running for inspection and further turns.

From the repository root, with the example dependencies installed:

```sh
temporal server start-dev
temporal operator namespace create --namespace shared-tools-demo
.venv/bin/python -m examples.shared_workflow_tools.worker
```

In another terminal:

```sh
.venv/bin/python -m examples.shared_workflow_tools.run
```

The runner verifies the result, mailbox delivery, and child completion, then
prints a direct Temporal UI history URL. Browse
http://localhost:8233/namespaces/shared-tools-demo/workflows to see the research
child, incoming progress Signal, outgoing mailbox Signal, and model activities.
The coordinator's `result` query returns its reply; the mailbox's `messages`
query returns delivered envelopes.

For interactive chat, run the runner once to create the mailbox, then start the
session manager and UI in separate terminals:

```sh
TEMPORAL_NAMESPACE=shared-tools-demo .venv/bin/python -m temporal_agent_harness.web.cli session-manager
TEMPORAL_NAMESPACE=shared-tools-demo .venv/bin/python -m temporal_agent_harness.web.cli serve examples/shared_workflow_tools/agents.toml --host 127.0.0.1 --port 8001
```

Open http://localhost:8001 and ask "Why is the sky blue?". The local model always
chooses the same tool sequence regardless of the prompt. This demonstrates tool
integration, rather than provider behavior or model selection quality.

The shared APIs also support the existing Pydantic AI and Gemini adapters;
adapter coverage lives in `tests/harness/test_workflow_tools.py`.

Signal `close` to the coordinator or mailbox to finish a session. Stop the
worker, session manager, and UI processes when finished.
