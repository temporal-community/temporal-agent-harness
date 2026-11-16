# Nexus travel demo

A manual check of the AgentService Nexus adapter: a Temporal workflow drives the Monty
conversational travel agent (`MontyChatAgent`) using only the Nexus contract. It doesn't use a
harness client, and it doesn't import the agent.

`caller.py`'s workflow runs two turns:

1. Asks for SFO → Tokyo flights, then immediately sends `set_approval_policy`
   (`dangerously_skip_all`) mid-turn, so no tool call waits on a human. When the agent first
   requests a tool, it sends a steering message ("Only nonstop flights, please.") and a
   `set_model` call, both mid-turn.
2. Asks to book the cheapest nonstop for Ada Lovelace, steering mid-turn again.

`just caller-with-approvals` skips the policy switch, so the caller approves every gated tool
call itself (`approveToolCall`). Either way, it approves anything gated and answers the agent's
`ask_user` questions (`provideCallbackResult`). At the end it calls `queryAgentStatus`,
`queryAgentInterface` and `closeSession`.

Each `pollMessages` attaches a completion callback to the agent's workflow, and the server
caps the callbacks one workflow accumulates (`system.maxCallbacksPerWorkflow`, 32 by default).
Past that, the agent's workflow stops accepting updates. The caller waits 5 seconds between
polls so each one collects everything published meanwhile, and logs how many polls the session
has used after each turn.

## Run

Set `GEMINI_API_KEY` in the repo-root `.env.local`. Then, from this directory, each in its own
terminal:

```bash
just temporal        # 1. local dev server
just endpoint        # 2. once, after the dev server is up
just agent-worker    # 3. Monty travel agent
just adapter-worker  # 4. AgentService Nexus handler
just caller          # 5. the caller workflow
```

The caller prints each step as it happens, then the whole transcript:

- `[sendAgentMessage ask] ... -> turn 1, opened, ...` starts a turn. A mid-turn steering
  message reports `joined`.
- `[steering_enqueued]` and then `[steering_claimed]` mean the steered text reached the model
  with the next tool result. `[steering_dropped]` means the turn ended before another tool
  call.
- `[mid-turn reply]` is the output of a message that joined the open turn, such as
  `set_model`'s.
- `[approveToolCall]`, `[provideCallbackResult]` and `[agent]` (the turn's reply) follow.

To watch the session in the harness console, also run `just session-manager` and
`just console`, each in its own terminal, before `just caller`. Then open
<http://localhost:8000>. Once the caller sends its first message, the session appears in the
session list as a discovered Monty (Conversational) session, `nexus-travel-<id>`. Open it to
follow the turns live. The caller closes the session at the end, which takes it off the list.

In the Temporal UI (<http://localhost:8233>), the caller is `nexus-travel-caller-<id>`, and its
history shows each Nexus operation. The agent it drove is `nexus-travel-<id>`, with the same
`<id>`.
