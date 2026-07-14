"""MCP and logging models."""

from typing import Any, Literal

from pydantic import Field

from .base import OpenCodeBaseModel


MCPConnectionStatus = Literal["connected", "disconnected", "error"]
LogLevel = Literal["debug", "info", "warn", "error"]


class LogRequest(OpenCodeBaseModel):
    """Log entry request."""

    service: str
    level: LogLevel
    message: str
    extra: dict[str, Any] | None = None


class MCPStatus(OpenCodeBaseModel):
    """MCP server status."""

    name: str
    status: MCPConnectionStatus
    tools: list[str] = Field(default_factory=list)
    error: str | None = None


class AddMcpServerRequest(OpenCodeBaseModel):
    """Request to add an MCP server dynamically.

    For stdio servers, provide ``command`` (and optionally ``args`` / ``env``).
    For HTTP/SSE servers, provide ``url``.
    """

    name: str | None = None
    """Name for the server (used as client_id)."""

    command: str | None = None
    """Command to run (for stdio servers)."""

    args: list[str] | None = None
    """Arguments for the command."""

    url: str | None = None
    """URL for HTTP/SSE servers."""

    env: dict[str, str] | None = None
    """Environment variables for the server."""


class McpAuthorizationResponse(OpenCodeBaseModel):
    """Response from starting MCP OAuth flow."""

    authorization_url: str
    """URL to open in browser for authorization."""


class McpResource(OpenCodeBaseModel):
    """MCP resource info matching OpenCode SDK McpResource type."""

    name: str
    """Name of the resource."""

    uri: str
    """URI identifying the resource location."""

    description: str | None = None
    """Optional description of the resource."""

    mime_type: str | None = None
    """MIME type of the resource content."""

    client: str
    """Name of the MCP client/server providing this resource."""
