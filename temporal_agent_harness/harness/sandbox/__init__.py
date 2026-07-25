# ABOUTME: SDK-neutral sandboxes for harness agents — isolated execution for agents built on ANY
# AI SDK the harness wraps, because nothing here depends on one.
#
# Three pieces, in the order you meet them:
#
#   1. Implement :class:`SandboxBackend` for your sandbox provider (Docker, a Kubernetes pod, a VM
#      service, a hosted sandbox API). Optionally implement :class:`SupportsHydration` too, when
#      your backend can read your data store itself.
#   2. Register it on the worker: ``SandboxProvider("workspace", MyBackend())``, whose
#      ``.activities()`` you pass to your Worker. Every sandbox operation is then its own durable,
#      retried activity.
#   3. Give tools access to it: declare ``sandbox: Injected[SandboxHandle]`` on a
#      ``@agent.tool_defn`` tool, put ``SandboxRef("workspace")`` in that tool's injections, and
#      call :func:`attach_sandbox` once per run to say what the sandbox is for THIS run.
#
# Worker::
#
#     provider = SandboxProvider("workspace", MyBackend())
#     Worker(client, task_queue="agents", activities=[*provider.activities(), ...])
#
# Tool (module level, declared once)::
#
#     @agent.tool_defn()
#     async def grep_workspace(sandbox: Injected[SandboxHandle], pattern: str) -> str:
#         """Search the workspace for `pattern`."""
#         result = await sandbox.exec("grep", "-rn", pattern, ".")
#         return result.stdout or f"no matches for {pattern!r}"
#
#     TOOLS = [grep_workspace]
#     INJECTIONS = {"sandbox": SandboxRef("workspace")}
#
# Workflow (per run)::
#
#     attach_sandbox(runner, "workspace", MyOptions(tenant_id=cfg.tenant_id))
#
# Two properties worth knowing before you design around this:
#
#   * **Sandbox tools are inline tools.** A :class:`SandboxHandle` dispatches its own activities, so
#     it works in workflow code — which includes a ``@agent.tool_defn`` body, but not an
#     ``@agent.activity_tool_defn`` one. Durability comes from each sandbox operation being an
#     activity, not from wrapping the tool in one.
#   * **A sandbox is compute, not storage.** Durable execution gives you a durable log, not a
#     durable filesystem: if the sandbox dies, whatever the model wrote in it is gone unless you
#     persisted it. Treat the workspace as a cache — every durable fact should leave as a tool
#     result or via :meth:`SandboxHandle.persist`. :class:`SandboxReclaimed` exists so an agent can
#     tell "the call failed" from "the workspace is gone", and ``attach_sandbox(...,
#     on_reclaim="reacquire")`` transparently replaces a reclaimed sandbox when — and only when —
#     its workspace was entirely derived from the hydration source.

from temporal_agent_harness.harness.sandbox.handle import (
    DEFAULT_ACTIVITY_CONFIG,
    ReclaimRecovery,
    SandboxHandle,
)
from temporal_agent_harness.harness.sandbox.injection import (
    OnComplete,
    OnReclaim,
    SandboxRef,
    attach_sandbox,
    discard_sandbox,
)
from temporal_agent_harness.harness.sandbox.protocol import (
    ExecResult,
    FsEntry,
    SandboxBackend,
    SandboxError,
    SandboxOptions,
    SandboxReclaimed,
    SandboxState,
    SandboxUnavailable,
    SupportsHydration,
)
from temporal_agent_harness.harness.sandbox.provider import SandboxProvider

__all__ = [
    "DEFAULT_ACTIVITY_CONFIG",
    "ExecResult",
    "FsEntry",
    "OnComplete",
    "OnReclaim",
    "ReclaimRecovery",
    "SandboxBackend",
    "SandboxError",
    "SandboxHandle",
    "SandboxOptions",
    "SandboxProvider",
    "SandboxReclaimed",
    "SandboxRef",
    "SandboxState",
    "SandboxUnavailable",
    "SupportsHydration",
    "attach_sandbox",
    "discard_sandbox",
]
