"""The same hello-world agent, on ``models.generate_content`` instead of the Interactions API.

This is the variant that **actually runs on GEAP**. Its sibling ``workflow.py`` is the same agent
on the Interactions API, which GEAP does not serve (see README.md, "The verdict") — so this module
is the concrete answer to "how hard is it to move off Interactions?", written so the two can be
diffed side by side. Same tool, same persona, same harness tool lifecycle; only the model surface
and the state handling differ.

What the port costs, visible in this file:

* **The response reduction gets SIMPLER.** Interactions streams a flat SSE sequence of
  ``StepStart`` / ``StepDelta`` / ``StepStop`` events over two near-identical discriminated
  unions, with function-call arguments arriving as JSON-string fragments to buffer per step index
  and reassemble. ``generate_content`` hands back finished ``Part``s that are either ``text`` or
  ``function_call`` — no buffering, no reassembly, no terminal-event bookkeeping. Roughly 70 lines
  of reduction collapse into the ``_split_parts`` helper below.
* **Conversation state gets HARDER, and this is the real cost.** Interactions keeps the transcript
  server-side and hands you one ``previous_interaction_id`` to chain. Here you own the whole
  history: every model turn and every function response must be appended to ``contents`` and
  resent. So the transcript now lives in workflow state (and therefore in workflow history), and
  the context window is yours to manage — see ``self._history``.
* **The tool loop is unchanged.** Deliberately: harness tools *require* ``run_tool`` (they read an
  ambient tool id and raise without it), so Gemini's automatic function calling cannot drive them
  and is switched off below. That is a feature here — approval policy and
  ``tool_start`` / ``tool_end`` / ``tool_error`` keep working exactly as before, and the loop you
  already have is the loop you keep.

Known gap, not a bug in this file: the harness's ``generate_content`` path publishes no
``model_interaction_*`` bracket and no ``tool_requested`` (its non-streamed activity publishes
nothing at all; the streamed one publishes only ``reply_delta``). So a turn here is *less*
observable than the same turn on Interactions. That is the same coupling fixed for the OpenAI
integration in issue #50, and it would want the same fix — publish at the model-call boundary —
before anything real ships on this path.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai import types
    from google.genai.client import AsyncClient

    from temporal_agent_harness.ai_sdks.google_genai_plugin import google_genai_client
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .tools import SYSTEM_INSTRUCTION, get_weather


TASK_QUEUE = "hello-gemini-enterprise"
DEFAULT_MODEL = "gemini-3.5-flash"

# Unlike the Interactions variant, this path needs the backend identity IN THE WORKFLOW: the SDK
# builds the request path workflow-side (`projects/<p>/locations/<l>/publishers/google/models/...`
# for GEAP vs `models/...` for the consumer API) and only the built path crosses into the activity.
# So a workflow that thinks it is on the consumer API would hand the activity's GEAP client a
# consumer-shaped path.
#
# AgentConfig deliberately carries only knobs universal to every agent, so this is not passed per
# session. Read once at import — fixed per worker process, not per turn — the same pattern
# react_agent uses for its streaming toggle. Consequence: it is NOT recorded in workflow history,
# so keep it stable for the lifetime of a session (flipping backends mid-session would change how
# subsequent requests are addressed).
USE_GEAP = any(
    os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}
    for name in ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI")
)
GEAP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
GEAP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"


@workflow.defn(name="HelloGeminiEnterpriseGenerateContentAgent")
@agent.defn
class HelloGeminiEnterpriseGenerateContentWorkflow:
    """The one-tool Gemini agent on ``models.generate_content`` — GEAP-capable."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._model: str = DEFAULT_MODEL
        # The whole conversation, client-side. This is what `previous_interaction_id` bought us on
        # the Interactions API — the cost of the port, in one attribute.
        self._history: list[types.Content] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        self._gemini = google_genai_client(
            # Unlike the Interactions variant, these matter here — see USE_GEAP above.
            vertexai=USE_GEAP,
            project=GEAP_PROJECT,
            location=GEAP_LOCATION,
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
                # Bounded, like the sibling: a backend/config error should answer fast, not hang.
                retry_policy=RetryPolicy(maximum_attempts=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask it anything; ask about the weather in a city and it
        calls its `get_weather` tool and tells you what it found."""
        return TextReply(text=await self._handle_chat_turn(self._gemini, message.text))

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one turn: call the model, dispatch tool calls, feed results back, repeat.

        Same shape as the Interactions loop — the difference is that ``contents`` accumulates the
        transcript here instead of a server-side interaction id, and the per-call reduction is a
        plain partition of finished parts.
        """
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration.from_callable_with_api_option(
                            callable=get_weather,
                            # The harness tool decorators expose a MODEL-FACING signature, so
                            # introspecting the decorated object yields exactly the schema the
                            # model should see (same trick `function_param` uses for Interactions).
                            api_option="VERTEX_AI" if USE_GEAP else "GEMINI_API",
                        )
                    ]
                )
            ],
            # Harness tools must go through `run_tool` (approval gate + lifecycle events), and they
            # raise if called without it. AFC would call them directly, so it is switched off and
            # this workflow drives the loop — exactly as the Interactions variant does.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        contents = [
            *self._history,
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)]),
        ]
        while True:
            response = await gemini.models.generate_content(
                model=self._model, contents=contents, config=config
            )
            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                # No candidate at all (e.g. a safety block). Commit what we have and say so,
                # rather than looping forever on an empty response.
                self._history = contents
                return "(the model returned no content)"

            contents.append(candidate.content)
            text, calls = _split_parts(candidate.content)

            if not calls:
                # Turn is done — persist the transcript for the next turn's chaining.
                self._history = contents
                return text

            results = await asyncio.gather(*(self._run_one_tool(c) for c in calls))
            contents.append(types.Content(role="user", parts=results))

    async def _run_one_tool(self, call: types.FunctionCall) -> types.Part:
        """Execute one tool call through ``run_tool`` and shape it as a function-response part.

        Identical in spirit to the Interactions variant: the harness keeps approval-policy
        evaluation and the tool's lifecycle events, and a failure comes back to the model as an
        error result rather than ending the turn.
        """
        name = call.name or ""
        # Vertex does not always populate a call id, but `run_tool` needs a stable one to
        # correlate the tool's events. workflow.uuid4() keeps that deterministic under replay.
        call_id = call.id or f"gemini:{name}:{workflow.uuid4()}"
        try:
            if name != get_weather.__name__:
                raise ValueError(f"unknown tool: {name!r}")
            result = await self._runner.run_tool(
                call_id, get_weather, **(call.args or {})
            )
            payload = {"result": _render_tool_result(result)}
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            payload = {"error": str(e)}
        return types.Part.from_function_response(name=name, response=payload)


def _split_parts(content: types.Content) -> tuple[str, list[types.FunctionCall]]:
    """Partition one model turn's parts into (reply text, requested function calls).

    This is the whole of the response reduction — compare
    ``workflow._execute_agent_interaction``, which has to fold an SSE event stream over two
    discriminated unions and reassemble streamed argument fragments to get here.
    """
    text_parts: list[str] = []
    calls: list[types.FunctionCall] = []
    for part in content.parts or []:
        if part.function_call is not None:
            calls.append(part.function_call)
        elif part.text:
            text_parts.append(part.text)
    return "".join(text_parts), calls


def _render_tool_result(result: object) -> str:
    """Render a tool's return value to text for the model."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)
