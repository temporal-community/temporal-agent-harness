# Model router (LLM API over Nexus)

A **prototype** model router exposed as a Temporal Nexus service. Callers send a
chat-completion request over Nexus; the router calls a model provider and returns
the response. Today it forwards every request to OpenAI; the design point is that
this is the single place a real router would select a backend from the requested
model and fan out to many providers (e.g. via LiteLLM).

Standalone: nothing here depends on the OpenAI Agents plugin. Its wire format is
the OpenAI **Chat Completions** shape — the de-facto standard LiteLLM and
OpenRouter also use — so any chat-completions-shaped caller can use it.

## Contract

```python
@nexusrpc.service
class ModelRouterService:
    chat_completion: nexusrpc.Operation[ChatCompletionRequest, ChatCompletion]
```

- `ChatCompletionRequest` (`models.py`) — the router's own request: `model`,
  `messages`, and a `params` dict carrying the rest (`tools`, `tool_choice`,
  `temperature`, …) in OpenAI/LiteLLM shape.
- `ChatCompletion` — reused verbatim from the OpenAI SDK (that's exactly what a
  chat-completions call returns, so there's nothing to translate).

## Files

| File | Role |
|---|---|
| `models.py` | `ChatCompletionRequest` — the router's own request model. |
| `service.py` | `ModelRouterService` contract + `NEXUS_ENDPOINT` / `TASK_QUEUE`. Light, import-safe in workflow context. |
| `handler.py` | `ModelRouterServiceHandler` — the async, workflow-backed operation; starts `ModelRouterWorkflow` per call. |
| `workflow.py` | `ModelRouterWorkflow` — backs the operation so it isn't time-capped; runs the model call as an activity. |
| `activities.py` | `ModelRouterActivities.invoke_chat_completion` — the actual provider call. The seam for LiteLLM / multi-provider routing. |
| `worker.py` | Standalone worker; serves the handler + workflow + activity and creates the Nexus endpoint. |

## Run

```sh
# from the repo root (needs a Temporal server + OPENAI_API_KEY):
uv run --group examples python -m nexus.model_router.worker
# or, from this dir:
just router
```

It creates the endpoint on startup (idempotent). Equivalent CLI:

```sh
temporal operator nexus endpoint create \
  --name model-router-endpoint \
  --target-namespace default \
  --target-task-queue model-router
```

## Who calls it

[`examples/openai_nexus`](../../examples/openai_nexus) — an OpenAI Agents SDK
agent whose model calls are routed here over Nexus. That example imports only the
light contract (`ModelRouterService` / `ChatCompletionRequest` / `NEXUS_ENDPOINT`)
to build its Nexus client.

## Limitations (prototype)

- `chat_completion` is an asynchronous, **workflow-backed** operation (a Nexus
  *sync* operation caps at ~10s, which model calls exceed): each call starts a
  `ModelRouterWorkflow` that runs the provider call as a retryable activity. Still
  non-streaming (one request → one response).
- Always routes to OpenAI; `handler.py` is where provider selection / LiteLLM
  would go.
- Uses `temporalio.contrib.pydantic.pydantic_data_converter`, compatible with the
  OpenAI Agents plugin's converter on the caller side.
