"""Callback tools the wiki agent uses to manage a Markdown wiki on the USER'S machine.

Each is an ``@agent.callback_tool_defn`` — a tool with NO worker-side body. The agent (which
may run in a cloud worker with no access to the user's disk) pauses in-workflow and publishes a
``callback_requested`` event; a thin client attached on the user's laptop executes the operation
against a local wiki directory and returns the result via the ``provide_callback_result`` update
(see ``client.py``). The agent never touches a filesystem — it just calls these like any tool.

NB: no ``from __future__ import annotations`` here. The parameter + return annotations are read
directly to build the model-facing tool schemas (via the Gemini plugin's ``function_param``) and
the output-type validator, so they must be concrete types, not stringized.
"""

from temporal_agent_harness.harness import agent


@agent.callback_tool_defn()
async def ls(path: str) -> list[str]:
    """List the entries directly under a wiki directory, one per entry. Pass "." for the wiki
    root. Sub-directory entries end with a trailing "/". Use this to see what already exists in a
    folder before deciding where new content should go."""
    ...


@agent.callback_tool_defn()
async def tree(path: str) -> str:
    """Return an indented tree of the wiki subtree rooted at `path` (pass "." for the whole wiki),
    so you can understand how notes are currently organized before adding, editing, or moving
    anything. Directories are shown with a trailing "/"."""
    ...


@agent.callback_tool_defn()
async def read_file(path: str) -> str:
    """Read a UTF-8 Markdown file from the wiki and return its full contents. Always read a file
    before editing it, so you can append to or revise the existing text instead of clobbering it.
    `path` is relative to the wiki root, e.g. "projects/temporal.md"."""
    ...


@agent.callback_tool_defn()
async def write_file(path: str, content: str) -> str:
    """Create a new wiki file, or OVERWRITE an existing one, with `content` (UTF-8 Markdown),
    creating parent directories as needed. This replaces the whole file — to add to an existing
    note, read_file it first and write back the full, revised contents. Returns a short
    confirmation. `path` is relative to the wiki root, e.g. "recipes/pasta.md"."""
    ...


@agent.callback_tool_defn()
async def delete_file(path: str) -> str:
    """Delete a file from the wiki. Returns a short confirmation. Use sparingly — only when a note
    is truly obsolete or the user asks for it. `path` is relative to the wiki root."""
    ...


@agent.callback_tool_defn()
async def grep(pattern: str) -> list[str]:
    """Search every Markdown file in the wiki for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to find where a topic already lives before creating a new
    file for it (so related notes stay together)."""
    ...


# The full callback toolset, in a stable order — handed to the model as its tool menu and used to
# build the name -> tool dispatch map in the workflow.
WIKI_TOOLS = [ls, tree, read_file, write_file, delete_file, grep]
