"""Activity tools for the Sano Hello agent.

Each tool is an ``@agent.activity_tool_defn``: it runs as a Temporal activity, and the
harness owns its approval gate and its ``tool_start`` / ``tool_end`` events. The results
are canned, so the example needs no network access.

No ``from __future__ import annotations``: activity arguments and results cross the data
converter, and string annotations can break its type resolution.
"""

from datetime import timedelta

from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

_TOOL_CONFIG = ActivityConfig(start_to_close_timeout=timedelta(seconds=30))


@agent.activity_tool_defn(inherently_safe=True, activity_config=_TOOL_CONFIG)
async def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: A plain city name, e.g. "Paris".
    """
    return f"It's 72°F and sunny in {city}."


@agent.activity_tool_defn(inherently_safe=True, activity_config=_TOOL_CONFIG)
async def get_local_time(city: str) -> str:
    """Get the local time in a city.

    Args:
        city: A plain city name, e.g. "Tokyo".
    """
    return f"It is 9:41 AM in {city}."


# The workflow adapts these onto the SDK. The worker gives the same list to
# AgentHarnessPlugin(tools=...), which registers each activity body.
ALL_TOOLS = [get_weather, get_local_time]
