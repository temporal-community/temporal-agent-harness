"""A Harbor external agent that starts one fresh Temporal harness session."""

import asyncio
import json
from importlib.metadata import version
from typing import Any
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig, AgentEvent
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from harness_evals.atif import export_atif


class HarnessOptions(AgentOptions):
    address: str = "127.0.0.1:7233"
    namespace: str = "default"
    workflow_type: str
    task_queue: str
    handler: str = "ask"
    input_key: str = "text"
    init_data: dict[str, Any] | None = None
    session_timeout_sec: float = 120


class TemporalHarnessAgent(BaseAgent):
    capabilities = AgentCapabilities(atif=True)
    options_model = HarnessOptions

    @staticmethod
    def name() -> str:
        return "temporal-harness"

    def version(self) -> str:
        return version("temporal-agent-harness")

    def to_agent_info(self) -> AgentInfo:
        info = super().to_agent_info()
        info.name = f"temporal-harness:{self.options.workflow_type}"
        return info

    async def setup(self, environment: BaseEnvironment) -> None:
        if self.model_name is not None:
            raise ValueError(
                "Configure the model on the agent worker; this adapter cannot override it"
            )
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        options = self.options
        session_id = f"harness-eval-{uuid4().hex}"
        record: dict[str, Any] = {
            "schema_version": 1,
            "session_id": session_id,
            "workflow_type": options.workflow_type,
            "task_queue": options.task_queue,
            "harness_version": self.version(),
            "instruction": instruction,
            "events": [],
            "complete": False,
            "error": None,
            "output": None,
        }
        handle = None
        try:
            async with asyncio.timeout(options.session_timeout_sec):
                temporal = await Client.connect(
                    options.address,
                    namespace=options.namespace,
                    plugins=[AgentHarnessPlugin()],
                )
                args = [AgentConfig()]
                if options.init_data is not None:
                    args.append(options.init_data)
                handle = await temporal.start_workflow(
                    options.workflow_type,
                    args=args,
                    id=session_id,
                    task_queue=options.task_queue,
                )
                client = AgentClient(temporal, session_id)
                accepted = await client.submit_message(
                    options.handler,
                    {options.input_key: instruction},
                )
                record["message_id"] = accepted.message_id
                stream = await client.attach(on_item=lambda event, offset: event, from_offset=0)
                try:
                    async for envelope in stream:
                        if not isinstance(envelope, AgentEvent):
                            raise RuntimeError(f"Harness stream failed: {type(envelope).__name__}")
                        record["events"].append(envelope.model_dump(mode="json"))
                        event = envelope.event
                        if event.type == "message_handler_end":
                            record["output"] = event.output
                        elif event.type == "message_handler_error":
                            raise RuntimeError("Harness message handler failed; see run.json")
                        elif event.type in {"callback_requested", "tool_approval_requested"}:
                            raise RuntimeError(
                                "This prototype does not drive approvals or callbacks"
                            )
                        elif event.type.startswith("subagent_"):
                            raise RuntimeError("This prototype supports single-agent sessions only")
                        if event.type == "turn_end" and envelope.turn_id == accepted.turn_id:
                            record["complete"] = True
                            break
                finally:
                    await stream.aclose()
                if not record["complete"] or record["output"] is None:
                    raise RuntimeError("Harness stream ended without the completed reply")
        except BaseException as error:
            record["complete"] = False
            record["error"] = type(error).__name__
            raise
        finally:
            # Preserve partial evidence on errors/cancellation and clean up only our fresh session.
            try:
                self._save_recording(record, context)
            finally:
                if handle is not None:
                    cleanup = asyncio.create_task(self._close_session(handle))
                    await asyncio.shield(cleanup)

    def _save_recording(self, record: dict[str, Any], context: AgentContext) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n")
        trajectory = export_atif(record)
        (self.logs_dir / "trajectory.json").write_text(
            trajectory.model_dump_json(indent=2, exclude_none=True) + "\n"
        )
        metrics = trajectory.final_metrics
        context.n_input_tokens = metrics.total_prompt_tokens
        context.n_output_tokens = metrics.total_completion_tokens
        context.n_cache_tokens = metrics.total_cached_tokens

    async def _close_session(self, handle: Any) -> None:
        try:
            await handle.signal("close")
            await asyncio.wait_for(handle.result(), timeout=10)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return  # Session was already closed/terminated.
            await self._terminate_session(handle)
        except Exception:
            await self._terminate_session(handle)

    async def _terminate_session(self, handle: Any) -> None:
        # A worker that died cannot process close; terminate only our workflow.
        try:
            await handle.terminate("Harbor eval session cleanup")
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                self.logger.exception("Could not close eval session %s", handle.id)
