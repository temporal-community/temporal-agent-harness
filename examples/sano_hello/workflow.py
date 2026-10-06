"""A hello-world OpenAI Agents SDK agent with activity tools.

The agent answers in plain text and can call two activity tools (``get_weather`` and
``get_local_time``). The UI reaches it over HTTP and standalone Nexus operations, not
through the FastAPI server. See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tools,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .tools import ALL_TOOLS


TASK_QUEUE = "sano-hello"
WORKFLOW_NAME = "SanoHelloAgent"
DEFAULT_MODEL = "gpt-5.1"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

You have two tools: `get_weather` returns the weather for a city, and `get_local_time`
returns the local time in a city. Call them when the user asks about weather or time
(don't guess), then answer in a sentence or two. For anything else, reply directly."""


@agent.defn(name=WORKFLOW_NAME)
class SanoHelloAgentWorkflow:
    """A two-tool conversational agent driven by the OpenAI Agents SDK."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: do not gate tool calls.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # OpenAI conversation state, kept across turns as the SDK's input-item list.
        self._conversation: list[TResponseInputItem] = []

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask about the weather or the time in a city."""
        sdk_agent = OpenAIAgent(
            name="SanoHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=as_openai_agent_tools(self._runner, ALL_TOOLS),
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]
        # context=self._runner routes the streamed model events to this turn's stream.
        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
