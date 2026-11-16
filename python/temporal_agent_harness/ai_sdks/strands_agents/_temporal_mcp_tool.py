from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, cast

from mcp.types import (
    URL_ELICITATION_REQUIRED,
    BlobResourceContents,
    CallToolResult,
    ElicitationRequiredErrorData,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
    Tool,
)
from strands.tools.mcp import MCPAgentTool
from strands.tools.mcp.mcp_client import MIME_TO_FORMAT
from strands.tools.mcp.mcp_types import MCPToolResult
from strands.types._events import ToolResultEvent
from strands.types.tools import ToolGenerator, ToolResultContent, ToolUse

from temporalio.exceptions import ActivityError, ApplicationError

from temporal_agent_harness.ai_sdks.strands_agents._temporal_activity_tool import _activity_error_event

if TYPE_CHECKING:
    from temporalio.mcp import TemporalMCPClient as DurableMCPClient

# Blob MIME types that Strands' MCPClient hands to the model as text.
_TEXT_MIME_TYPES = (
    "application/json",
    "application/xml",
    "application/javascript",
    "application/yaml",
    "application/x-yaml",
)


class TemporalMCPTool(MCPAgentTool):
    """Workflow-side stub for a single MCP tool; calls it through an activity.

    It is a Strands ``MCPAgentTool`` whose ``mcp_client`` is the durable
    ``temporalio.mcp.TemporalMCPClient``, so its name, spec and filters work as
    in Strands.
    """

    def __init__(
        self, client: DurableMCPClient, tool: Tool, prefix: str | None = None
    ) -> None:
        """Bind this tool to its server's durable client and listed MCP tool."""
        super().__init__(
            tool,
            cast(Any, client),
            name_override=f"{prefix}_{tool.name}" if prefix else None,
        )
        self._client = client

    @property
    def tool_type(self) -> str:
        """Tool kind identifier used by Strands."""
        return "temporal_mcp"

    async def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        """Execute the tool through the server's call-tool activity."""
        tool_use_id = tool_use["toolUseId"]
        arguments = tool_use["input"]
        if not isinstance(arguments, dict):
            # The MCP client would reject it only after taking the server's
            # shared connection, and then close that connection.
            yield ToolResultEvent(
                _error_result(tool_use_id, "tool input must be a JSON object")
            )
            return
        try:
            result = await self._client.call_tool(self.mcp_tool.name, arguments)
        except ActivityError as error:
            yield _elicitation_event(tool_use_id, error) or _activity_error_event(
                tool_use_id, error
            )
            return
        try:
            tool_result = _tool_result(tool_use_id, result)
        except Exception as error:
            # Strands' MCPClient reports a result it cannot map as a failed call.
            tool_result = _error_result(tool_use_id, str(error))
        yield ToolResultEvent(tool_result)


def _error_result(tool_use_id: str, message: str) -> MCPToolResult:
    return MCPToolResult(
        status="error",
        toolUseId=tool_use_id,
        content=[{"text": f"Tool execution failed: {message}"}],
    )


def _elicitation_event(
    tool_use_id: str, error: ActivityError
) -> ToolResultEvent | None:
    """Report a URL elicitation error with its data, as Strands' ``MCPClient`` does."""
    cause = error.__cause__
    if not (
        isinstance(cause, ApplicationError)
        and cause.type == "MCPProtocolError"
        and len(cause.details) > 1
        and cause.details[0] == URL_ELICITATION_REQUIRED
    ):
        return None
    try:
        data = ElicitationRequiredErrorData.model_validate(cause.details[1])
        elicitations = json.dumps(
            [e.model_dump(exclude_none=True) for e in data.elicitations]
        )
    except Exception:
        return None
    return ToolResultEvent(
        MCPToolResult(
            status="error",
            toolUseId=tool_use_id,
            content=[
                {
                    "text": f"MCP Elicitation required: [{cause.message}] "
                    f"with data {elicitations}"
                }
            ],
        ),
        exception=cause,
    )


def _tool_result(tool_use_id: str, result: CallToolResult) -> MCPToolResult:
    """Map an MCP tool result the way Strands' ``MCPClient`` does."""
    tool_result = MCPToolResult(
        status="error" if result.is_error else "success",
        toolUseId=tool_use_id,
        content=[
            mapped
            for content in result.content
            if (mapped := _tool_result_content(content)) is not None
        ],
    )
    if result.structured_content is not None:
        tool_result["structuredContent"] = result.structured_content
    if result.meta:
        tool_result["metadata"] = result.meta
    tool_result["isError"] = result.is_error
    return tool_result


def _tool_result_content(content: Any) -> ToolResultContent | None:
    """Map one MCP content block as Strands' ``MCPClient`` does.

    Like Strands, it drops the blocks it does not support and raises for an
    image whose type is not supported or whose data does not decode.
    """
    if isinstance(content, TextContent):
        return {"text": content.text}
    if isinstance(content, ImageContent):
        return {
            "image": {
                "format": MIME_TO_FORMAT[content.mime_type],
                "source": {"bytes": base64.b64decode(content.data)},
            }
        }
    if isinstance(content, EmbeddedResource):
        resource = content.resource
        if isinstance(resource, TextResourceContents):
            return {"text": resource.text}
        if isinstance(resource, BlobResourceContents):
            try:
                data = base64.b64decode(resource.blob)
            except ValueError:
                return None
            mime_type = resource.mime_type or ""
            if (
                mime_type.startswith("text/")
                or mime_type in _TEXT_MIME_TYPES
                or mime_type.endswith(("+json", "+xml"))
            ):
                return {"text": data.decode(errors="replace")}
            if mime_type in MIME_TO_FORMAT:
                return {
                    "image": {
                        "format": MIME_TO_FORMAT[mime_type],
                        "source": {"bytes": data},
                    }
                }
    return None
