"""The agent surface shared by both backend/API-surface variants of this example.

Kept in its own module so the Interactions agent (``workflow.py``) and the
``generate_content`` agent (``workflow_generate_content.py``) present the *identical* tool and
persona to the model. That's what makes the two comparable: any difference you observe between
them is a property of the API surface, not of the agent.
"""

from __future__ import annotations

from temporal_agent_harness.harness import agent

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

You have one tool, `get_weather`, which returns the current weather for a city. When the user
asks about the weather somewhere, call it (don't guess), then tell them the answer in a sentence
or two. For anything else, just reply directly."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Canned lookup — a hello-world, not a real weather service. Being an inline `tool_defn`
    # (not an activity tool) keeps the worker free of tool activities, so the only thing it
    # registers is the Gemini plugin whose client is under test.
    return f"It's 72°F and sunny in {city}."
