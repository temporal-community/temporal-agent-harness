"""Callback tools the builder agent edits the flow script with.

The script lives in the user's browser, not in the workflow: each of these is an
``@agent.callback_tool_defn`` with no worker-side body, so a call pauses the turn until the page
(``ui/App.svelte``) reads or changes its editor and returns the result. That is also what lets the
page draw each edit as it lands.

NB: no ``from __future__ import annotations`` — the annotations build the model-facing schemas and
the result validators.
"""

from temporal_agent_harness.harness import agent


@agent.callback_tool_defn(inherently_safe=True)
async def read_code() -> str:
    """Read the flow script in the user's editor. Each line comes back prefixed with its 1-based
    line number and a bar ("  12 | ..."); the prefix is not part of the code. An empty editor
    reads as "(empty)". Read before editing so edits match the code as it is now."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def write_code(code: str) -> str:
    """Replace the whole script with `code`. Use it for a first draft or a rewrite; for a change
    to part of an existing script, use edit_code."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def edit_code(old_text: str, new_text: str) -> str:
    """Replace `old_text`, which must appear exactly once in the script, with `new_text`. Copy
    `old_text` from read_code without the line-number prefixes, and include enough surrounding
    lines to make it unique. Pass an empty `new_text` to delete. Several small edits read better
    to the user than one large one."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def export_code() -> str:
    """The script exactly as it is in the editor, with no line numbers. Not given to the model:
    ``check_code`` calls it to get the script it checks."""
    ...


# What the model edits with. `export_code` is left out: the model checks the script with
# `check_code`, which reads it through `export_code` itself.
EDITOR_TOOLS = [read_code, write_code, edit_code]
