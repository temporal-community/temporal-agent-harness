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
  * Worker-side (:mod:`.activities`): the two sandbox-stepping activities.
    ``AgentHarnessPlugin`` registers them on every worker (a worker can also register
    ``CODE_MODE_ACTIVITIES`` from that module by hand); nothing here imports that module, so
    the workflow-safe surface stays free of it. It imports no Monty itself — it checks that
    the ``code-mode`` extra is installed and then delegates — so it loads without the extra
    and a misconfigured worker fails a Code Mode call with an actionable, non-retryable error
    instead of leaving the activity names unregistered, which Temporal retries forever.
  * Worker-side engine (:mod:`.monty_stepper`): the stepping logic, and the only module that
    imports ``pydantic_monty``. Kept separate purely so it can import Monty normally and be
    type-checked against it; import it only after that extra check.
"""

from .stubs import CodeModeStubError
from .tool import code_mode_tool

__all__ = ["CodeModeStubError", "code_mode_tool"]
