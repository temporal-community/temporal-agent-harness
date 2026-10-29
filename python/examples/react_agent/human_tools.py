# ABOUTME: Human-in-the-loop callback tool(s) for the ReAct agent.
# ask_user parks in-workflow until an external client returns the user's typed answer.

# NOTE: no `from __future__ import annotations` — the return type is read concretely to build
# the tool's output schema (the client's result is validated against it), mirroring
# tool_activities.py. A stringized annotation would break that resolution.

from temporal_agent_harness.harness import agent


@agent.callback_tool_defn(inherently_safe=True)
async def ask_user(question: str) -> str:
    """Ask the user a clarifying question and use their typed answer to continue.

    Use this when a request is ambiguous or needs information only the user can provide —
    e.g. which city they mean, or permission to use their current location.

    Args:
        question: The question to put to the user, in plain language.
    """
    # Never runs on the worker: this is a callback tool. When the model calls it, the harness
    # publishes a `callback_requested` event and parks the turn on a durable wait until an
    # attached client (examples/react_agent/client.py) returns the user's answer as the result.
    ...


# ask_user is a callback tool (no activity body), so — unlike tool_activities.ALL_TOOLS — it is
# NOT registered with the worker. It is adapted onto the SDK in workflow.py alongside ALL_TOOLS.
HUMAN_TOOLS = [ask_user]
