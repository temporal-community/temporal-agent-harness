"""Compatibility re-exports for the packaged harness session manager."""

from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_ID,
    SESSION_MANAGER_TASK_QUEUE,
    AgentDescriptor,
    AgentRegistry,
    CreateSessionRequest,
    Session,
    SessionManagerWorkflow,
)

__all__ = [
    "SESSION_MANAGER_ID",
    "SESSION_MANAGER_TASK_QUEUE",
    "AgentDescriptor",
    "AgentRegistry",
    "CreateSessionRequest",
    "Session",
    "SessionManagerWorkflow",
]
