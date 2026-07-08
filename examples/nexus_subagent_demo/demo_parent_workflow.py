"""Model-free demo parent that drives the Echo subagent through the Nexus-brokered subagent
path (agent-registry discovery -> sendAgentMessage/pollMessages -> executeOperatorCommand),
end to end, with no LLM in the loop.

Mirrors ``tests/examples/monty/_subagent_e2e_parent.py``: a handler calls the runner's
subagent methods directly rather than through the generated (model-facing) toolset, since
there's no model here to call tools. The difference from that test fixture is the
TRANSPORT — this drives the subagent dynamically via ``discover_registry_agents``/
``start_subagent_from_registry`` (Nexus + agent registry) instead of a statically-wired,
same-cluster child workflow.
"""

from __future__ import annotations

import json
import os
from typing import Any

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from subagents.registry import start_subagent_from_registry

    from .dynamic_toolset import discover_and_build_tools, tool_declaration

# Read once, at workflow-construction time — same operational discipline as the harness's own
# NEXUS_SUBAGENT env read (subagent_toolset.py): a deployment-wide toggle, not meant to change
# mid-flight for a given running workflow.
REGISTRY_ENDPOINT = os.environ.get("AGENT_REGISTRY_NEXUS_ENDPOINT", "agent-registry-endpoint")

DEMO_PARENT_TASK_QUEUE = "subagent-demo-parent"

# The registry key the Echo worker self-registers under (see echo_worker.py).
ECHO_AGENT_KEY = "echo"


class EchoViaSubagent(BaseModel):
    """Drive one echo subagent turn through the Nexus-brokered path."""

    text: str = Field(description="Text to send to the echo subagent.")


class ListDynamicTools(BaseModel):
    """List every tool the registry's current directory would synthesize right now (see
    dynamic_toolset.py's as_tool()/discover_and_build_tools() prototype)."""


class DynamicToolDeclaration(BaseModel):
    """One synthesized tool's model-facing declaration, as built by dynamic_toolset.py's
    tool_declaration()."""

    name: str
    description: str
    parameters: dict[str, Any]


class ListDynamicToolsReply(BaseModel):
    """Every tool discover_and_build_tools() would synthesize from the registry's current
    directory, model-facing-declaration only — no subagent was started to produce this."""

    tools: list[DynamicToolDeclaration]


class DynamicCall(BaseModel):
    """Call one dynamically-synthesized tool directly — simulating what a model-driven tool
    loop would do after seeing list_dynamic_tools' declarations."""

    tool_name: str = Field(description="One of the names from list_dynamic_tools' output.")
    arguments: dict[str, Any] = Field(default_factory=dict)


@workflow.defn(name="SubagentDemoParent")
@agent.defn
class SubagentDemoParentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # agent_key -> handle, shared across every discover_and_build_tools()/as_tool() call
        # this parent makes, so a dynamically-synthesized tool reuses one running instance per
        # agent_key across turns instead of leaking a fresh one on every call.
        self._dynamic_handle_cache: dict[str, str] = {}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def echo_via_subagent(self, msg: EchoViaSubagent) -> TextReply:
        """Discover the registered "echo" subagent via the Nexus agent registry, start an
        instance of it, send it `text`, stop it, and reply with what it echoed back."""
        handle = await start_subagent_from_registry(
            self._runner, ECHO_AGENT_KEY, REGISTRY_ENDPOINT
        )
        try:
            result = await self._runner.run_subagent_turn(handle, "echo", {"text": msg.text})
        finally:
            await self._runner.stop_subagent(handle)
        return TextReply(text=result.get("text", ""))

    @agent.accepts
    async def list_dynamic_tools(self, msg: ListDynamicTools) -> ListDynamicToolsReply:
        """Force a fresh discover_subagents call and synthesize a real per-handler tool for
        each discovered handler via as_tool(), then report each tool's model-facing
        declaration (name, description, parameters) exactly as it would be shown to a model.
        No subagent is actually invoked."""
        tools = await discover_and_build_tools(
            self._runner, REGISTRY_ENDPOINT, self._dynamic_handle_cache
        )
        return ListDynamicToolsReply(
            tools=[
                DynamicToolDeclaration(
                    name=decl["name"],
                    description=decl["description"],
                    parameters=decl["parameters"],
                )
                for decl in (tool_declaration(fn) for fn in tools)
            ]
        )

    @agent.accepts
    async def dynamic_call(self, msg: DynamicCall) -> TextReply:
        """Force a fresh discover_subagents call, synthesize tools via as_tool(), find the
        one named `tool_name`, and call it with `arguments` — simulating what a model-driven
        tool loop would do after seeing list_dynamic_tools' declarations."""
        tools = await discover_and_build_tools(
            self._runner, REGISTRY_ENDPOINT, self._dynamic_handle_cache
        )
        by_name = {fn.__name__: fn for fn in tools}
        fn = by_name.get(msg.tool_name)
        if fn is None:
            raise ApplicationError(
                f"Unknown dynamic tool {msg.tool_name!r}. Currently available: "
                f"{sorted(by_name)}.",
                {"tool_name": msg.tool_name, "known": sorted(by_name)},
                type="UnknownDynamicTool",
                non_retryable=True,
            )
        result = await self._runner.run_tool(f"dynamic-{msg.tool_name}", fn, **msg.arguments)
        return TextReply(text=json.dumps(result))
