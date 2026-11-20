"""The default connector: Claude Code in a subprocess of the Worker, run by the Claude Agent SDK's own transport."""

from __future__ import annotations

from collections.abc import Callable

from claude_agent_sdk import ClaudeAgentOptions, Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from temporal_agent_kit.ai_sdks.claude_agent_sdk._types import ConnectActivityInput


class SubprocessConnector:
    """Run Claude Code in a subprocess of the Worker, as the Claude Agent SDK's ``SubprocessCLITransport`` does."""

    def __init__(self, *, stderr: Callable[[str], None] | None = None) -> None:
        """Optionally log Claude Code's stderr output on the Worker."""
        self._stderr = stderr # https://code.claude.com/docs/en/agent-sdk/troubleshooting#processerror-command-failed-with-exit-code

    async def connect(self, request: ConnectActivityInput) -> Transport:
        """Start Claude Code."""
        options = ClaudeAgentOptions(
            cli_path=request.cli_path,
            cwd=request.cwd,
            env=request.env,
            user=request.user,
            max_buffer_size=request.max_buffer_size,
            stderr=self._stderr,
        )
        transport = SubprocessCLITransport(prompt="", options=options)

        # Run the command line the Workflow built, in place of one built from these options.
        cli_path = transport._cli_path
        assert cli_path is not None
        transport._build_command = lambda: [cli_path, *request.args]

        await transport.connect()
        return transport
