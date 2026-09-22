# OpenAI automatic model router

This agent resolves `model="auto"` before constructing an OpenAI agent.

For every user turn, the worker records a routing decision before the model call:

- `simple` → `gpt-5.6-luna`
- `moderate` → `gpt-5.6-terra`
- `complex` → `gpt-5.6-sol`
- `frontier` → `gpt-6-astra`

The OpenAI agent receives the resulting concrete model ID, which is then reused for every model
invocation in that SDK run, including tool-loop follow-ups and handoffs.

## Run it

From the repository root, copy `.env.example` to `.env.local` and set both `OPENAI_API_KEY` and
`TYPESAFE_API_KEY`. Then run these in separate terminals:

```sh
just temporal
just session-manager
just server
just worker-openai-auto-router
```

Open <http://localhost:8000>, choose **OpenAI Auto Router**, and send a request. The agent uses
the usual streamed OpenAI path, while the model ID is selected by a recorded activity.

To run only this example from its directory, see its `justfile`.
