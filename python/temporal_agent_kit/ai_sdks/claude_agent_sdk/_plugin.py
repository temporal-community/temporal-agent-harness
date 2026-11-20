"""Worker plugin that lets Workflows use the Claude Agent SDK and runs the Activities that reach Claude Code."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.metadata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from temporalio.plugin import SimplePlugin
from temporalio.worker import Worker, WorkerConfig, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from temporal_agent_kit.ai_sdks.claude_agent_sdk._activities import ConnectionActivities
from temporal_agent_kit.ai_sdks.claude_agent_sdk._subprocess import SubprocessConnector
from temporal_agent_kit.ai_sdks.claude_agent_sdk._types import Connector


class ClaudeAgentSDKPlugin(SimplePlugin):
    """Use the Claude Agent SDK in Workflows.

    In the Workflow, pass the Claude Agent SDK a :class:`TemporalTransport` as
    its ``transport``. This plugin runs the transport's Activities, which reach
    Claude Code through ``connector``.

    Each plugin instance owns one connection and its private task queue. Use a
    separate plugin instance for each main Worker.
    """

    def __init__(
        self,
        *,
        connector: Connector | None = None,
        setup_activities: Sequence[Callable[..., Any]] = (),
    ) -> None:
        """Create the plugin.

        Args:
            connector: Starts Claude Code and reaches it. Defaults to
                :class:`SubprocessConnector`, which runs it in a subprocess of
                the Worker, as the Claude Agent SDK does.
            setup_activities: Activities callable on the private task queue, such
                as downloading files before Claude Code starts.
        """
        self._setup_activities = setup_activities
        self._connection_activities = ConnectionActivities(
            connector if connector is not None else SubprocessConnector(),
            f"claude-agent-sdk-{uuid.uuid4()}",
        )

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if runner is None:
                raise ValueError("No WorkflowRunner provided to ClaudeAgentSDKPlugin.")

            if isinstance(runner, SandboxedWorkflowRunner):
                # Import anyio's asyncio backend here, outside the sandbox.
                # Inside, anyio imports it lazily with import_module, which
                # skips the passthrough below and fails on its threading imports.
                importlib.import_module("anyio._backends._asyncio")

                return dataclasses.replace(
                    runner,
                    # Pass these through, so the Workflow shares one copy of
                    # them with the Worker. anyio, the Claude Agent SDK's async
                    # runtime, uses threads, and sniffio keeps the running
                    # async library in a thread-local.
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "claude_agent_sdk", "anyio", "sniffio"
                    ),
                )
            return runner

        super().__init__(
            name="ClaudeAgentSDKPlugin",
            workflow_runner=workflow_runner,
        )

    def version(self) -> str:
        """Return this package's version, which worker heartbeats report."""
        try:
            return importlib.metadata.version("temporal-agent-kit")
        except importlib.metadata.PackageNotFoundError:
            return ""

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """Register the task queue reservation Activity on the main Worker."""
        if config.get("task_queue") != self._connection_activities.host_specific_task_queue:
            config["activities"] = [
                *config.get("activities", []),
                self._connection_activities.get_available_task_queue,
            ]

        return super().configure_worker(config)

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        """Run the connector Worker alongside the main Worker."""
        if worker.task_queue == self._connection_activities.host_specific_task_queue:
            await next(worker)
            return

        try:
            async with Worker(
                worker.client,
                task_queue=self._connection_activities.host_specific_task_queue,
                activities=[
                    self._connection_activities.connect,
                    self._connection_activities.write,
                    self._connection_activities.read,
                    self._connection_activities.end_input,
                    self._connection_activities.close,
                    *self._setup_activities,
                ],
            ):
                await super().run_worker(worker, next)
        finally:
            await asyncio.shield(self._connection_activities.close())
