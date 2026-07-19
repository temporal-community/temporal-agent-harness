"""Location and weather tools for the ReAct agent, as harness activity tools.

Four durable, activity-backed tools (``@agent.activity_tool_defn``) that call real public APIs
over httpx. Because they do network I/O they run as Temporal activities, never inline in the
workflow. Each is adapted onto the OpenAI Agents SDK in ``workflow.py`` via
``as_openai_agent_tools``, so the harness still owns the approval gate and each tool's
``tool_start`` / ``tool_end`` / ``tool_error`` events; the worker registers each activity body
with ``agent.tool_activity(...)`` (see ``worker.py``, which uses ``ALL_ACTIVITIES``).

The tool schema the model sees (name, description, parameters) is derived from each function's
signature and docstring, so the first docstring line becomes the tool description and the
``Args`` document the parameters.

No ``from __future__ import annotations`` — activity args/returns cross Temporal's data
converter, and stringized annotations can trip its type resolution (mirrors the Monty example's
activities module). The types here are plain ``str`` / ``float``, so concrete annotations are fine.
"""

from datetime import timedelta
from urllib.parse import quote

import httpx
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

# Per-tool execution ceiling for these quick HTTP calls (mirrors demo2's 30s).
_TOOL_TIMEOUT = timedelta(seconds=30)
# Per-request HTTP timeout, comfortably under the activity's start-to-close.
_HTTP_TIMEOUT = 5.0


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=_TOOL_TIMEOUT),
)
async def get_ip_address() -> str:
    """Get the public IP address of the machine running the agent."""
    async with httpx.AsyncClient() as client:
        response = await client.get("https://icanhazip.com", timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.text.strip()


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=_TOOL_TIMEOUT),
)
async def get_location_info(ipaddress: str) -> str:
    """Get the location for an IP address: city, region, country, latitude, and longitude.

    Args:
        ipaddress: An IP address, e.g. "8.8.8.8".
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://ip-api.com/json/{ipaddress}", timeout=_HTTP_TIMEOUT
        )
        response.raise_for_status()
        return response.text


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=_TOOL_TIMEOUT),
)
async def get_coordinates(city: str) -> str:
    """Get the latitude and longitude for a city name.

    Args:
        city: The city to look up, e.g. "Barcelona".
    """
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(city)}&count=1"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.text


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=_TOOL_TIMEOUT),
)
async def get_weather(latitude: float, longitude: float) -> str:
    """Get current weather at a latitude/longitude: temperature (Fahrenheit), weather code, wind speed.

    Args:
        latitude: Latitude of the location.
        longitude: Longitude of the location.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,weather_code,wind_speed_10m"
        "&temperature_unit=fahrenheit"
    )
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.text


# The in-workflow tool dispatchers (what workflow.py adapts onto the SDK via
# as_openai_agent_tools) and the matching @activity.defn bodies the worker registers.
# tool_activity() returns the durable activity body for each dispatcher.
ALL_TOOLS = [get_ip_address, get_location_info, get_coordinates, get_weather]
ALL_ACTIVITIES = [agent.tool_activity(t) for t in ALL_TOOLS]
