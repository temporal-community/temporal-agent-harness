"""A Gemini Interactions conversation that the workflow holds and sends whole on every call.

Why not chain with ``previous_interaction_id``: the API rebuilds a chained conversation from
the stored interactions, and a STREAMED interaction stores only its own steps (a non-streamed
one stores the whole conversation so far). The rebuild reads the previous interaction and the
one before it, no further, so once two streamed interactions follow each other the rebuilt
conversation starts partway through — at a function result whose call is missing — and the
API rejects the next function result with ``400 "Please ensure that function response turn
comes immediately after a function call turn"``. A tool-calling loop that streams every model
call hits that on its third tool result.

So the conversation lives here instead, in workflow state, and goes out as the ``input`` of
every ``interactions.create``. That also makes the workflow, not the API's storage, the record
of what the model was told: a replay, a retry or a restarted worker sends exactly the same
conversation.

Typical tool-calling turn::

    conversation.add_user_text(text)
    while True:
        stream = await gemini.interactions.create(
            model=MODEL, input=conversation.steps, tools=tools, stream=True
        )
        reply = await conversation.read_reply(stream)
        if not reply.function_calls:
            return reply.text
        conversation.add_function_results(await run_calls(reply.function_calls))

The cost is that every model call carries the whole conversation in its activity input, so a
long session grows its workflow history accordingly (the large-payload offload stores big
inputs out of band).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from typing import Any

from google.genai._interactions.types import (
    ErrorEvent,
    FunctionCallStep,
    InteractionCompletedEvent,
    InteractionSSEEvent,
    StepDelta,
    StepStart,
)
from google.genai._interactions.types.function_result_step_param import (
    FunctionResultStepParam,
)
from google.genai._interactions.types.step_delta import (
    DeltaArgumentsDelta,
    DeltaText,
    DeltaThoughtSignature,
    DeltaThoughtSummary,
)
from temporalio.exceptions import ApplicationError

# The model steps a conversation can carry back to the API. Built-in tools (search, code
# execution, ...) stream steps of their own that this does not rebuild.
_SUPPORTED_STEPS = ("thought", "function_call", "model_output")


@dataclass
class InteractionReply:
    """One model reply: its text, the function calls it made, and the interaction's id."""

    text: str
    function_calls: list[FunctionCallStep]
    interaction_id: str


class InteractionConversation:
    """The steps of one conversation with a Gemini model, oldest first.

    Pass :attr:`steps` as ``input=`` to every ``interactions.create``, without
    ``previous_interaction_id`` (see the module docstring for why).
    """

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []

    @property
    def steps(self) -> list[dict[str, Any]]:
        """The whole conversation, to send as ``input=``."""
        return list(self._steps)

    def add_user_text(self, text: str) -> None:
        self._steps.append({"type": "user_input", "content": [{"type": "text", "text": text}]})

    def add_function_results(self, results: Iterable[FunctionResultStepParam]) -> None:
        """The results of the function calls in the last reply, in any order."""
        self._steps.extend(dict(r) for r in results)

    async def read_reply(self, stream: AsyncIterable[InteractionSSEEvent]) -> InteractionReply:
        """Read one streamed ``interactions.create`` to its end and add the model's steps.

        The steps are added only once the stream completes, so a failed call leaves the
        conversation as it was. Raises :class:`ApplicationError` on an error event, on a
        stream that ends without completing, and — non-retryably — on a step this
        conversation cannot carry back to the model.
        """
        steps: dict[int, dict[str, Any]] = {}
        arguments: dict[int, str] = {}
        text: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=error):
                    message = (error.message if error else None) or "stream error"
                    code = (error.code if error else None) or "stream_error"
                    raise ApplicationError(message, type=code)
                case StepStart(index=index, step=step):
                    if step.type not in _SUPPORTED_STEPS:
                        raise ApplicationError(
                            f"the model produced a {step.type!r} step, which an "
                            f"InteractionConversation cannot carry back to it",
                            type="UnsupportedInteractionStep",
                            non_retryable=True,
                        )
                    steps[index] = step.model_dump(mode="json", exclude_none=True)
                case StepDelta(index=index, delta=DeltaText(text=chunk)):
                    text[index] = text.get(index, "") + (chunk or "")
                case StepDelta(index=index, delta=DeltaArgumentsDelta(arguments=chunk)):
                    arguments[index] = arguments.get(index, "") + (chunk or "")
                case StepDelta(index=index, delta=DeltaThoughtSignature(signature=signature)):
                    if signature:
                        steps[index]["signature"] = signature
                case StepDelta(index=index, delta=DeltaThoughtSummary(content=content)):
                    if content is not None:
                        steps[index].setdefault("summary", []).append(
                            content.model_dump(mode="json", exclude_none=True)
                        )
                case StepDelta(delta=delta):
                    raise ApplicationError(
                        f"the model streamed a {delta.type!r} delta, which an "
                        f"InteractionConversation cannot carry back to it",
                        type="UnsupportedInteractionStep",
                        non_retryable=True,
                    )
                case InteractionCompletedEvent(interaction=interaction):
                    interaction_id = interaction.id
        if interaction_id is None:
            raise ApplicationError(
                "stream ended without interaction.completed event", type="stream_error"
            )

        model_steps: list[dict[str, Any]] = []
        function_calls: list[FunctionCallStep] = []
        for index in sorted(steps):
            step = steps[index]
            if step["type"] == "function_call" and index in arguments:
                step["arguments"] = json.loads(arguments[index])
            if step["type"] == "model_output" and index in text:
                step["content"] = [{"type": "text", "text": text[index]}]
            if step["type"] == "function_call":
                function_calls.append(FunctionCallStep.model_validate(step))
            model_steps.append(step)
        self._steps.extend(model_steps)
        return InteractionReply(
            text="".join(text[i] for i in sorted(text)),
            function_calls=function_calls,
            interaction_id=interaction_id,
        )
