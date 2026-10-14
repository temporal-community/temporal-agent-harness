# OpenAI agent, LLM calls over Nexus (prototype)

The sibling of [`openai_hello`](../openai_hello): the same harness agent (chat +
one `get_weather` tool, driven through the shared web UI), but its **LLM calls
travel over Temporal Nexus** to a standalone **model router** instead of hitting
the provider from a model activity.

Streaming is intentionally **not** implemented — this uses `Runner.run`.

## The pieces

```
 examples/openai_nexus/            the agent (this dir)
   workflow.py   OpenAINexusAgent — harness agent, Runner.run
   worker.py     wires the plugin's workflow_model_provider = nexus_model_provider
   nexus_transport.py   the transport swap (below)

 nexus/model_router/               the "LLM API over Nexus" (standalone)
   models.py / service.py   ChatCompletionRequest -> ChatCompletion (its own wire types)
   handler.py / workflow.py  async, workflow-backed operation (model calls exceed sync's ~10s)
   activities.py            the OpenAI call; the seam for LiteLLM/multi-provider
   worker.py                `just router` — creates the endpoint, calls OpenAI
```

## How it works (and why a custom model provider isn't enough)

The OpenAI Agents SDK supports custom model providers, but in the Temporal
integration the model call runs inside a **Temporal activity** — and
`workflow.create_nexus_client(...)` only works in **workflow** context. So a
provider dropped into the activity path can't reach Nexus.

Instead the plugin grew a small, Nexus-agnostic seam:
`ModelActivityParameters.workflow_model_provider` — `(model_name) -> Model`,
resolved and called by the model stub **in the workflow**, with the live
`tools`/`handoffs`/`model_settings` (no serialization, no reconstruction).

`nexus_transport.py` provides that model: an `OpenAIChatCompletionsModel` whose
OpenAI client is a stand-in whose `chat.completions.create` goes **over Nexus**.
The SDK does all the Responses↔ChatCompletions translation; the only thing swapped
is the transport. So:

```
Runner.run (workflow)
  └─ model stub → workflow_model_provider(name)
       └─ OpenAIChatCompletionsModel(client = NexusChatClient)
            └─ chat.completions.create(...)                 # LiteLLM/OpenAI-shaped request
                 └─ create_nexus_client(ModelRouterService) → chat_completion op
                      └─ ModelRouterWorkflow → invoke_chat_completion activity → OpenAI
```

The `chat_completion` operation is asynchronous (workflow-backed), not sync,
because model calls exceed the ~10s a Nexus sync operation allows — so the router
starts a `ModelRouterWorkflow` that runs the call as a retryable activity, and the
caller's `execute_operation` durably waits for it.

The wire format is the OpenAI Chat Completions shape (what LiteLLM/OpenRouter
standardize on); `nexus/model_router` owns those wire types. LiteLLM's real value
(multi-provider fan-out) belongs **in the router's activity**, server-side, where it
can run unrestricted — today it just calls OpenAI.

Because `get_weather` is adapted with `as_openai_agent_tool`, a weather question
is two model calls over Nexus: request the tool → run it in the workflow →
second call for the final answer. Both go to the router.

## Run it (shared web UI stack)

`OPENAI_API_KEY` is needed by the **router** (it calls OpenAI), not by this agent
worker. Each in its own terminal, from `examples/openai_nexus`:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just router            # 2. the model router (LLM API over Nexus); creates the endpoint
just session-manager   # 3. packaged session-manager worker
just server            # 4. FastAPI API + UI on :8000
just worker            # 5. this agent worker
```

Open http://localhost:8000, pick **"OpenAI over Nexus"**, and ask
`What's the weather in Paris?`. The reply arrives when the turn completes
(non-streaming); the tool call shows up via `tool_start` / `tool_end`.

The turn's UI-visible event sequence is
`turn_started → tool_start → tool_end → reply → turn_end`.

## Notes & limitations (it's a prototype)

- **No streaming.** Only `Runner.run`; no live token / `model_interaction_*`
  events (those come from the activity streaming observer, which this path skips).
- The agent tool is `async` on purpose — a sync `@function_tool` would be invoked
  in a thread executor, which the workflow sandbox forbids.
- The SDK's `OpenAIChatCompletionsModel.get_response` runs in the workflow so the
  Nexus call can originate there. Its converters are pure/deterministic and the
  only workflow command is the Nexus op, so the run is replay-safe.
- The router forwards to OpenAI with retries disabled; making it multi-provider is
  an `activities.py` change (drop in `litellm.acompletion`), no client changes.
