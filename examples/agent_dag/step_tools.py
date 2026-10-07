"""The tools a step agent can be given, reused from the other examples.

The travel tools are the Monty example's simulated flight and hotel activities, and
``get_weather`` is the OpenAI hello example's canned lookup. None of them reaches the real world.
"""

from typing import get_args

from examples.monty.activities import ALL_TOOLS as TRAVEL_TOOLS
from examples.openai_hello.workflow import get_weather

from .models import StepToolName

STEP_TOOLS = {tool.__name__: tool for tool in [*TRAVEL_TOOLS, get_weather]}

if set(STEP_TOOLS) != set(get_args(StepToolName)):
    raise TypeError(
        f"StepToolName {sorted(get_args(StepToolName))} does not match the step tools "
        f"{sorted(STEP_TOOLS)}"
    )


def catalog() -> str:
    """One line per tool, for the builder's prompt: its name and what it does."""
    lines = []
    for name, tool in STEP_TOOLS.items():
        summary = (tool.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        lines.append(f"- {name}: {summary}")
    return "\n".join(lines)
