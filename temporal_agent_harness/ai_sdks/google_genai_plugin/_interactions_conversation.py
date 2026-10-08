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

from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass

from google.genai._interactions.types import (
    ErrorEvent,
    FunctionCallStep,
    FunctionCallStepParam,
    InteractionCompletedEvent,
    InteractionSSEEvent,
    ModelOutputStep,
    ModelOutputStepParam,
    StepDelta,
    StepParam,
    StepStart,
    TextContent,
    TextContentParam,
    ThoughtStep,
    ThoughtStepParam,
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
from pydantic import TypeAdapter
from temporalio.exceptions import ApplicationError

# A function call's arguments, parsed from the JSON fragments streamed for it.
_ARGUMENTS: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


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
        self._steps: list[StepParam] = []

    @property
    def steps(self) -> list[StepParam]:
        """The whole conversation, to send as ``input=``."""
        return list(self._steps)

    def add_user_text(self, text: str) -> None:
        self._steps.append({"type": "user_input", "content": [{"type": "text", "text": text}]})

    def add_function_results(self, results: Iterable[FunctionResultStepParam]) -> None:
        """The results of the function calls in the last reply, in any order."""
        self._steps.extend(r.copy() for r in results)

    async def read_reply(self, stream: AsyncIterable[InteractionSSEEvent]) -> InteractionReply:
        """Read one streamed ``interactions.create`` to its end and add the model's steps.

        The steps are added only once the stream completes, so a failed call leaves the
        conversation as it was. Raises :class:`ApplicationError` on an error event, on a
        stream that ends without completing, and — non-retryably — on a step this
        conversation cannot carry back to the model.
        """
        thoughts: dict[int, ThoughtStepParam] = {}
        calls: dict[int, FunctionCallStep] = {}
        arguments: dict[int, str] = {}
        outputs: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=error):
                    message = (error.message if error else None) or "stream error"
                    code = (error.code if error else None) or "stream_error"
                    raise ApplicationError(message, type=code)
                case StepStart(index=index, step=ThoughtStep(signature=signature, summary=summary)):
                    thought: ThoughtStepParam = {"type": "thought"}
                    if signature:
                        thought["signature"] = signature
                    if summary:
                        thought["summary"] = [_text_param(c) for c in summary]
                    thoughts[index] = thought
                case StepStart(index=index, step=FunctionCallStep() as call):
                    calls[index] = call
                case StepStart(index=index, step=ModelOutputStep(content=content)):
                    outputs[index] = "".join(_text_param(c)["text"] for c in content or ())
                case StepStart(step=step):
                    raise _unsupported(f"produced a {step.type!r} step")
                case StepDelta(index=index, delta=DeltaText(text=chunk)) if index in outputs:
                    outputs[index] += chunk or ""
                case StepDelta(index=index, delta=DeltaArgumentsDelta(arguments=chunk)) if (
                    index in calls
                ):
                    arguments[index] = arguments.get(index, "") + (chunk or "")
                case StepDelta(index=index, delta=DeltaThoughtSignature(signature=signature)) if (
                    index in thoughts
                ):
                    if signature:
                        thoughts[index]["signature"] = signature
                case StepDelta(index=index, delta=DeltaThoughtSummary(content=content)) if (
                    index in thoughts
                ):
                    if content is not None:
                        thoughts[index]["summary"] = [
                            *thoughts[index].get("summary", ()),
                            _text_param(content),
                        ]
                case StepDelta(delta=delta):
                    raise _unsupported(f"streamed a {delta.type!r} delta")
                case InteractionCompletedEvent(interaction=interaction):
                    interaction_id = interaction.id
        if interaction_id is None:
            raise ApplicationError(
                "stream ended without interaction.completed event", type="stream_error"
            )

        model_steps: list[StepParam] = []
        function_calls: list[FunctionCallStep] = []
        for index in sorted(thoughts.keys() | calls.keys() | outputs.keys()):
            if index in thoughts:
                model_steps.append(thoughts[index])
            elif index in calls:
                call = calls[index]
                if index in arguments:
                    call = FunctionCallStep(
                        type="function_call",
                        id=call.id,
                        name=call.name,
                        arguments=_ARGUMENTS.validate_json(arguments[index]),
                        signature=call.signature,
                    )
                function_calls.append(call)
                call_step: FunctionCallStepParam = {
                    "type": "function_call",
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                if call.signature:
                    call_step["signature"] = call.signature
                model_steps.append(call_step)
            else:
                output: ModelOutputStepParam = {"type": "model_output"}
                if outputs[index]:
                    output["content"] = [{"type": "text", "text": outputs[index]}]
                model_steps.append(output)
        self._steps.extend(model_steps)
        return InteractionReply(
            text="".join(outputs[i] for i in sorted(outputs)),
            function_calls=function_calls,
            interaction_id=interaction_id,
        )


def _text_param(content: object) -> TextContentParam:
    """A text block the conversation can send back; anything else is refused."""
    if not isinstance(content, TextContent) or content.annotations:
        raise _unsupported(f"produced {type(content).__name__} content")
    return {"type": "text", "text": content.text}


def _unsupported(what: str) -> ApplicationError:
    return ApplicationError(
        f"the model {what}, which an InteractionConversation cannot carry back to it",
        type="UnsupportedInteractionStep",
        non_retryable=True,
    )
