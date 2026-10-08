# Parent and subagent messaging

One OpenAI Agents example combines the existing `agent.subagent_toolset()` with
`agent.send_message_tool()` and `agent.AgentMessageInbox`.

The parent starts a persistent researcher, sends instructions using its returned
handle, and invokes its typed `ask` operation. The child consumes those instructions
and sends a message back to `parent`. The parent then sends a follow-up and invokes
another turn on that same child before stopping it. Both agents run actual SDK
loops; model responses are local fixtures and need no credentials.

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

The runner verifies a single child with two turns, two messages each way, and
graceful child completion. It prints a direct Temporal UI URL. Browse
http://localhost:8233/namespaces/shared-tools-demo/workflows to inspect parent and
child histories, subagent turn activities, and Signals. The parent's `result` and
both agents' `received_messages` queries expose the exchange.

For interactive chat, run the session manager and UI in separate terminals:

```sh
TEMPORAL_NAMESPACE=shared-tools-demo .venv/bin/python -m temporal_agent_harness.web.cli session-manager
TEMPORAL_NAMESPACE=shared-tools-demo .venv/bin/python -m temporal_agent_harness.web.cli serve examples/shared_workflow_tools/agents.toml --host 127.0.0.1 --port 8001
```

Open http://localhost:8001 and ask "Why is the sky blue?". The chat's **Agent
communication** panel shows the parent, its subagent, Active/Stopped status, and
an exchange timeline with sender, recipient, timestamp, and full message text.
Signal deliveries are distinguished from delegated turn requests and replies.
Delivery means the Signal arrived, not that the recipient has read it. Start a
new run to see delivery events; older runs retain their existing tool logs.

The fixed local model
chooses the same sequence for every prompt. Signals queue messages without
automatically starting an agent turn; here the child consumes each message during
its next typed turn. The shared tools also support Pydantic AI and Gemini adapters,
covered in the unit tests.

The parent stays running for further turns. Signal `close` to finish it, and stop
the worker, session manager, and UI processes when finished.
