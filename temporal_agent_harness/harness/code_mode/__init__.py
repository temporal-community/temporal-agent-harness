"""Code Mode: expose a set of harness tools to a model as a single run-a-script tool.

``code_mode_tool(tools, name=...)`` returns one tool that runs a model-authored Python script in
a sandbox whose only capabilities are those tools (as async host functions). The model writes
code — with loops, conditionals, and ``asyncio.gather`` concurrency — to orchestrate many tool
calls in one turn; every host call goes through the runner, keeping its approval policy and tool
lifecycle events.

The package splits cleanly along the Temporal workflow boundary, so importing it never requires
the sandbox engine (the optional ``code-mode`` extra):

  * Workflow-safe (this ``__init__`` and the ``batch_models`` / ``stubs`` / ``driver`` / ``tool``
    modules): safe to import anywhere, including inside a workflow. ``code_mode_tool`` and
    ``CodeModeStubError`` are the public surface, re-exported here.
  * Worker-side (:mod:`.activities`): imports ``pydantic_monty`` and defines the sandbox-stepping
    activities. A worker registers them via ``from ...code_mode.activities import
    CODE_MODE_ACTIVITIES``; nothing here imports that module, so the workflow-safe surface stays
    free of the engine dependency.
"""

from .stubs import CodeModeStubError
from .tool import code_mode_tool

__all__ = ["CodeModeStubError", "code_mode_tool"]
