"""Ask-the-user tool: human-in-the-loop clarification.

When a task is ambiguous or needs a decision only the user can make, a good
coding agent asks rather than guesses. In the durable mode this is a callback
tool: the workflow pauses and an attached client answers, so the wait costs no
worker time and survives a restart. The local runner supplies the same tool as
a terminal prompt instead (see ``local_runner``).

NB: no ``from __future__ import annotations``; the annotations build the
model-facing schema at runtime.
"""

from temporal_agent_harness.harness import agent


@agent.callback_tool_defn()
async def ask_user(question: str, choices: list[str] = []) -> str:
    """Ask the user a question and use their answer to continue. Use it when the task is
    ambiguous or needs a decision only the user can make: which of two files they mean, permission
    for a destructive or slow command, or a missing detail. Pass `choices` to offer a short list of
    options to pick from (leave it empty for a free-form answer). Prefer asking over guessing when
    it matters; ask again if the answer is unclear. Returns the user's answer as text."""
    ...  # no worker body: the attached client (or the local prompt) supplies the answer


ASK_TOOLS = [ask_user]
