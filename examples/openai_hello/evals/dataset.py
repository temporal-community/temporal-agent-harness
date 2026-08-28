"""Eval dataset for the OpenAI Hello agent.

Deliberately the same shape as ``examples/monty/evals/dataset.py``, against a completely
different AI SDK. That is the point: the dataset format, the runner, the scorers and the spans
are all at the harness layer, so switching from Gemini + Code Mode to the OpenAI Agents SDK
changes the ``workflow_type`` and nothing else.

The agent has exactly one tool — ``get_weather(city)``, a canned lookup returning
``"It's 72°F and sunny in {city}."`` — and a system prompt telling it to call the tool rather
than guess, and to answer directly for anything else. A tiny surface, but it still admits the
three failure modes worth catching: not using a tool it should, using one it shouldn't, and
reporting a number the tool never returned.
"""

from __future__ import annotations

from temporal_agent_harness.evals import TurnScript, TurnStep

from examples.openai_hello.workflow import TASK_QUEUE

WORKFLOW_TYPE = "OpenAIHelloAgent"
DATASET_NAME = "openai-hello"
DATASET_DESCRIPTION = (
    "Weather-assistant cases for the OpenAI Agents SDK example. Scored on tool use, not just "
    "on the wording of the reply."
)


def _case(*steps: TurnStep, expected: dict | None = None) -> TurnScript:
    return TurnScript(
        steps=list(steps),
        workflow_type=WORKFLOW_TYPE,
        task_queue=TASK_QUEUE,
        expected=expected,
    )


CASES: dict[str, TurnScript] = {
    "weather-uses-the-tool": _case(
        TurnStep.text("What's the weather in Tokyo?"),
        expected={"weather_cities": ["Osaka"]},
    ),
    "chitchat-uses-no-tool": _case(
        TurnStep.text("Hi! Who are you, in one sentence?"),
        # The system prompt says to answer directly for anything else. An agent that reaches
        # for its one tool regardless is the classic over-eager-tool-use failure, and it costs
        # a round trip on every unrelated message.
        expected={"weather_cities": []},
    ),
    "reasoning-question-uses-no-tool": _case(
        TurnStep.text("If it's 20°C outside, should I wear a coat?"),
        # Mentions temperature but asks for judgement, not a lookup — the near-miss that
        # trips a keyword-matching agent.
        expected={"weather_cities": []},
    ),
    "two-cities-one-turn": _case(
        TurnStep.text("Compare the weather in Paris and Berlin."),
        # Both must be looked up. Answering for one and inferring the other is the failure.
        expected={"weather_cities": ["Paris", "Berlin"]},
    ),
    # -- multi-turn: what a single-prompt dataset cannot express --------------------------
    "followup-carries-the-question": _case(
        TurnStep.text("What's the weather in Tokyo?"),
        TurnStep.text("What about Paris?"),
        # "What about Paris?" only means "the weather in Paris" if the agent kept the thread.
        expected={"weather_cities": ["Tokyo", "Paris"]},
    ),
    "followup-about-the-same-answer": _case(
        TurnStep.text("What's the weather in Tokyo?"),
        TurnStep.text("Is that warm enough for shorts?"),
        # A judgement about the answer already given — no second lookup needed. An agent that
        # re-calls the tool is burning a round trip to re-fetch what it already has.
        expected={"weather_cities": ["Tokyo"]},
    ),
    "unknown-capability": _case(
        TurnStep.text("Book me a flight to Tokyo."),
        # It has no booking tool. It should say so rather than pretend, and definitely not
        # call get_weather at it.
        expected={"weather_cities": []},
    ),
}


def cases() -> dict[str, TurnScript]:
    """The dataset, keyed by stable case id (Langfuse upserts on it, so ids must not drift)."""
    return dict(CASES)
