"""Narrow native tools and harness Code Mode share exactly one host dispatcher."""

import asyncio
from datetime import timedelta

from temporal_agent_harness.harness import agent

from .coding_models import (
    EditRequest,
    FileRequest,
    PatchRequest,
    SearchRequest,
    ToolCall,
    ToolResult,
)


def coding_tools(dispatch, *, writer: bool):
    @agent.tool_defn()
    async def list_files() -> ToolResult:
        """List visible repository files (up to 3,000)."""
        return await dispatch(ToolCall(kind="list"))

    @agent.tool_defn()
    async def read_file(request: FileRequest) -> ToolResult:
        """Read a text file and its exact hash; use that hash for edits."""
        return await dispatch(ToolCall(kind="read", path=request.path))

    @agent.tool_defn()
    async def search_code(request: SearchRequest) -> ToolResult:
        """Find literal text with file paths and line numbers (up to 80 hits)."""
        return await dispatch(ToolCall(kind="search", query=request.query))

    @agent.tool_defn()
    async def get_diff() -> ToolResult:
        """Read the current patch, including new files, against the starting commit."""
        return await dispatch(ToolCall(kind="diff"))

    @agent.tool_defn()
    async def get_workspace_revision() -> ToolResult:
        """Hash the current source snapshot; execution evidence is bound to this hash."""
        return await dispatch(ToolCall(kind="revision"))

    @agent.tool_defn()
    async def apply_patch(request: PatchRequest) -> ToolResult:
        """Preflight all file hashes, then atomically replace each file. Scope must be approved."""
        return await dispatch(ToolCall(kind="patch", changes=request.changes))

    @agent.tool_defn()
    async def edit_file(request: EditRequest) -> ToolResult:
        """Replace exactly one occurrence, guarded by the file's expected hash."""
        return await dispatch(ToolCall(kind="edit", edit=request))

    native = [list_files, read_file, search_code, get_diff, get_workspace_revision]
    if writer:
        native += [apply_patch, edit_file]
    sandbox = agent.code_mode_tool(
        native,
        name="change_code" if writer else "inspect_code",
        step_timeout=timedelta(seconds=15),
    )
    scripts = 0

    async def execute_code(script: str) -> str:
        """Execute one bounded repository script."""
        nonlocal scripts
        scripts += 1
        if scripts > 8:
            return "Code Mode limit reached: 8 scripts per role turn; use direct tools."
        if len(script) > 20000:
            return "Script exceeds 20,000 characters"
        try:
            return await asyncio.wait_for(sandbox(script), timeout=120)
        except TimeoutError:
            return "Code Mode stopped at its 120 second total limit; inspect receipts before continuing."

    # Preserve the package-generated signatures/documentation; the wrapper adds
    # total time and script-count limits to the package's per-step timeout.
    execute_code.__name__ = "change_code" if writer else "inspect_code"
    execute_code.__doc__ = sandbox.__doc__
    # tool_defn captures metadata at decoration time, so decorate under final name.
    execute_code = agent.tool_defn()(execute_code)
    return [*native, execute_code]
