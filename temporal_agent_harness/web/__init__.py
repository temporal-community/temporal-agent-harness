"""Reusable web API/session-manager surface for Temporal Agent Harness."""

from temporal_agent_harness.web.registry import load_agent_registry
from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_ID,
    SESSION_MANAGER_TASK_QUEUE,
    AgentDescriptor,
    AgentRegistry,
    CreateSessionRequest,
    Session,
    SessionManagerWorkflow,
)


def __getattr__(name: str):
    if name == "create_agent_harness_app":
        from temporal_agent_harness.web.app import create_agent_harness_app

        return create_agent_harness_app
    if name == "create_session_manager_worker":
        from temporal_agent_harness.web.worker import create_session_manager_worker

        return create_session_manager_worker
    raise AttributeError(name)


__all__ = [
    "SESSION_MANAGER_ID",
    "SESSION_MANAGER_TASK_QUEUE",
    "AgentDescriptor",
    "AgentRegistry",
    "CreateSessionRequest",
    "Session",
    "SessionManagerWorkflow",
    "create_agent_harness_app",
    "create_session_manager_worker",
    "load_agent_registry",
]
