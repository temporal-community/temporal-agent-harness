"""MCP-to-Nexus bridge and MCP SDK extensions."""

from nexus_mcp.tasks import MODERN_PROTOCOL_VERSION, TASKS_EXTENSION

from .bridge import IDEMPOTENCY_KEY_META_KEY, NexusMCPBridge
from .tasks import NexusTasksExtension

__all__ = [
    "IDEMPOTENCY_KEY_META_KEY",
    "MODERN_PROTOCOL_VERSION",
    "TASKS_EXTENSION",
    "NexusMCPBridge",
    "NexusTasksExtension",
]
