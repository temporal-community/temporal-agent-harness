"""The two worker-side activities that step the Monty sandbox, surfacing host calls to the workflow.

Thin Temporal wrappers. Each checks that the optional ``code-mode`` extra is installed, then
delegates to :mod:`.monty_stepper`, which holds the actual stepping logic — see that module for
the suspend-at-external-call protocol and the async/concurrent batch driver.

Why the stepping lives in an activity (not the workflow):
  * Monty's *async* API (``acreate``/``run_async``) cannot run in a Temporal workflow at
    all — its Rust↔asyncio bridge needs ``loop.call_soon_threadsafe``, which the
    deterministic workflow loop forbids (it raises NotImplementedError). The synchronous
    ``start``/``resume`` API sidesteps that entirely — no event loop, no threads. (The
    async *concurrency* the script sees does NOT use Monty's async API; it's the
    defer-future / FutureSnapshot protocol over that synchronous stepping API.)
  * Even so, executing arbitrary (sandboxed) user code is not something to do inside a
    workflow. So the workflow stays the pure orchestrator: it calls ``code_start_batch``
    once, then alternates running each awaited batch of host calls (durable activities,
    concurrently) with ``code_resume_batch`` until the script completes.

This module is WORKER-SIDE — never import it from workflow code or from ``harness.agent``.
The workflow-side driver dispatches these activities by NAME (``CODE_START_BATCH_ACTIVITY`` /
``CODE_RESUME_BATCH_ACTIVITY`` in :mod:`.batch_models`), so it never imports this module.

Why the extra is checked HERE, per call, rather than at import: this module deliberately does
not import ``pydantic_monty``, so it loads with or without the ``code-mode`` extra and a worker
can always register these two activities. That matters because the driver dispatches by NAME —
with nothing registered under these names Temporal answers with
``ApplicationError(type="NotFoundError")``, which is RETRYABLE, so a worker missing the extra
would retry "is not registered on this worker" forever and the turn would just hang. Checking
inside the activity turns that into one immediate, non-retryable failure naming the extra to
install. The check cannot live in the workflow instead: ``agent.code_mode_tool(...)`` runs in
the workflow sandbox, where the installed-package question is both unanswerable (imports are
restricted) and the wrong question — the answer depends on the worker process, not on workflow
history, so branching on it would be nondeterministic across a replay. So a deploy that
accidentally drops the extra costs some failed tool calls, never a broken workflow.

The snapshot crosses the activity boundary as bytes (``FutureSnapshot.dump()`` /
``load_snapshot``). Those bytes can be large and end up in workflow history, which is why
every client/worker must connect with the large-payload offload data converter.

No ``from __future__ import annotations`` — the models crossing Temporal's pydantic converter
require concrete annotations (see :mod:`.batch_models`).
"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .batch_models import (
    CODE_RESUME_BATCH_ACTIVITY,
    CODE_START_BATCH_ACTIVITY,
    CodeBatchStep,
    ResumeBatchInput,
)

# The error type an operator (or an agent's error handling) can match on to tell a worker
# missing the `code-mode` extra apart from a script that genuinely failed.
CODE_MODE_MISSING_EXTRA_ERROR = "CodeModeExtraMissing"


def _require_code_mode_extra() -> None:
    """Fail the call with an actionable error unless the ``code-mode`` extra is installed.

    Called at the top of each activity, before importing :mod:`.monty_stepper` (which imports
    Monty at module scope and would otherwise raise a bare ImportError). Probing
    ``pydantic_monty`` itself, rather than wrapping the stepper import in ``except
    ImportError``, keeps the diagnosis honest: only Monty's actual absence produces this
    error, while an ImportError from a bug inside the stepper still surfaces as itself.

    Cheap enough to pay on every call — after the first one it is a ``sys.modules`` lookup.

    Non-retryable on purpose: the call fails once and the agent sees a failed tool call rather
    than a turn that stalls indefinitely. It does NOT self-heal — a call that failed during a
    bad deploy stays failed; calls made after the extra is restored succeed.
    """
    try:
        import pydantic_monty  # noqa: F401  (presence probe only)
    except ImportError as e:
        raise ApplicationError(
            "Code Mode ran on a worker without the harness's `code-mode` extra, which "
            "provides pydantic-monty — the sandbox Code Mode scripts run in. Install "
            "temporal-agent-harness[code-mode] on this worker and redeploy it.",
            type=CODE_MODE_MISSING_EXTRA_ERROR,
            non_retryable=True,
        ) from e


@activity.defn(name=CODE_START_BATCH_ACTIVITY)
async def code_start_batch(
    script: str, type_check_stubs: str | None = None
) -> CodeBatchStep:
    """Compile + start ``script``, running to the first awaited batch of host calls or done.

    See :func:`.monty_stepper.start_batch`."""
    _require_code_mode_extra()
    from .monty_stepper import start_batch

    return start_batch(script, type_check_stubs)


@activity.defn(name=CODE_RESUME_BATCH_ACTIVITY)
async def code_resume_batch(input: ResumeBatchInput) -> CodeBatchStep:
    """Resume a FutureSnapshot with the workflow-computed results, then run to the next batch.

    See :func:`.monty_stepper.resume_batch`."""
    _require_code_mode_extra()
    from .monty_stepper import resume_batch

    return resume_batch(input)


# The two sandbox-stepping activities every Code Mode worker registers, regardless of which
# tools its Code Mode tools expose (they are tool-agnostic — the tools are dispatched through the
# runner as their own activities). ``AgentHarnessPlugin`` registers these unconditionally — the
# extra is checked per call by _require_code_mode_extra(), not at registration — alongside the
# durable bodies of the host tools passed to its ``tools=``, so a Code Mode worker normally
# names neither::
#
#     client = await Client.connect(..., plugins=[AgentHarnessPlugin(tools=my_tools)])
#     Worker(client, task_queue=..., workflows=[MyAgent])
#
# Use this list directly only when assembling a worker's activity list by hand::
#
#     Worker(..., activities=[*CODE_MODE_ACTIVITIES, agent.tool_activity(my_tool), ...])
CODE_MODE_ACTIVITIES = [code_start_batch, code_resume_batch]
