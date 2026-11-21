"""A streaming Claude Code conversation in an Agent Kit Workflow."""

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
    )

    from temporal_agent_kit import agent
    from temporal_agent_kit.agent_protocol import (
        AgentConfig,
        ModelInteractionEnded,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_kit.agent_workflow import AgentWorkflowRunner
    from temporal_agent_kit.ai_sdks.claude_agent_sdk import (
        TemporalAgentKitOptions,
        TemporalTransport,
    )


TASK_QUEUE = "claude-agent-sdk-hello"


@agent.defn(name="ClaudeAgentSDKHello")
class ClaudeAgentSDKHelloWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )
        self._session_id: str | None = None
        self._reply_id: str | None = None  # The last reply of the last finished turn
        self._client = self._new_client()
        self._connected = False

    def _new_client(self) -> ClaudeSDKClient:
        """A client that starts a new session, or resumes this one after its last finished turn."""
        options = TemporalAgentKitOptions(
            runner=self._runner,
            setting_sources=[],
            resume=self._session_id,  # None starts a new session.
            resume_session_at=self._reply_id,
        )
        return ClaudeSDKClient(options=options, transport=TemporalTransport(options))

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask Claude a question or continue the conversation, repeating the turn if Claude Code's Worker is lost."""
        while True:
            try:
                if not self._connected:
                    await self._client.connect()
                    self._connected = True

                await self._client.query(message.text)

                reply_id = None  # The latest reply's in this turn
                async for response in self._client.receive_response():
                    if isinstance(response, SystemMessage) and response.subtype == "init":
                        self._session_id = response.data["session_id"]
                    elif isinstance(response, AssistantMessage) and response.parent_tool_use_id is None:
                        reply_id = response.uuid
                    elif isinstance(response, ResultMessage):
                        if response.is_error:
                            details = response.result or "\n".join(response.errors or [])
                            raise ApplicationError(
                                details or f"Claude Code ended with {response.subtype}.",
                                non_retryable=True,
                            )
                        self._reply_id = reply_id
                        return TextReply(text=response.result or "")

                raise ApplicationError("Claude Code ended without a result.", non_retryable=True)

            except ActivityError as error:
                # A cancelled turn stops instead of restarting. Agent Kit reports
                # the error as message_handler_error and keeps the agent running.
                if is_cancelled_exception(error):
                    raise

                # End the model call the lost Worker cut off, which never reported its end.
                self._runner.publish(ModelInteractionEnded())

                try:
                    await self._client.disconnect()
                except ActivityError:
                    pass # The Worker is gone, so Claude Code can't be closed anymore.
                
                self._connected = False

                # Repeat the turn after the last finished one, so a copy of this
                # message Claude Code saved before the Worker was lost is dropped.
                # With no finished turn, start a new session.
                if self._reply_id is None:
                    self._session_id = None

                self._client = self._new_client()

    @agent.teardown
    async def disconnect(self) -> None:
        await self._client.disconnect()
