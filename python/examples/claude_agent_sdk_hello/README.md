# Claude Agent SDK Hello

A multi-turn chat with Claude Code that streams Claude's replies and thinking to the
Agent Kit console. If the Worker running Claude Code stops mid-reply, the turn
starts over on another Worker.

## Run locally

Set `ANTHROPIC_API_KEY` in the repo-root `.env.local` or authenticate Claude Code
on the Worker host. From the repo root, start Temporal and then run the example
in another terminal.

```sh
just temporal
just dev claude_agent_sdk_hello
```

Open http://localhost:8000, choose **Claude Agent SDK Hello**, and send a message.
