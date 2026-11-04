"""A hello-world Claude Agent SDK agent on the harness.

Claude Code chats about the files in the Worker's directory and hands searches to its
``file-finder`` subagent. Each Bash call waits for your approval. See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

    from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import (
        HarnessClaudeSession,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


SYSTEM_PROMPT = """\
You are a friendly assistant for the files in your working directory. Answer in brief, natural
prose. To find files that match a description, delegate the search to the file-finder subagent."""

OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    agents={
        "file-finder": AgentDefinition(
            description="Finds the files in the working directory that match a description.",
            prompt="You find files. Reply with each matching path and one line on what it holds.",
            tools=["Glob", "Grep", "Read"],
        ),
    },
    tools=["Agent", "Bash", "Glob", "Grep", "Read"],
    # Leave this machine's Claude Code settings and CLAUDE.md files out of the agent.
    setting_sources=[],
)


@agent.tool_defn(inherently_safe=True)
async def current_time() -> str:
    """The current date and time, in UTC."""
    return workflow.now().isoformat()


@agent.defn(name="ClaudeAgentSDKHelloAgent")
class ClaudeAgentSDKHelloAgentWorkflow:
    """A Claude Code agent with a file-finding subagent, driven by the Claude Agent SDK."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy(
                auto_approve_inherently_safe=True,
                auto_approve_tools=frozenset({"Agent", "Glob", "Grep", "Read"}),
            ),
        )
        self._claude = HarnessClaudeSession(self._runner, OPTIONS, [current_time])

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with Claude Code about the files in its working directory."""
        result = await self._claude.run(message.text)
        return TextReply(text=result.result or "")
