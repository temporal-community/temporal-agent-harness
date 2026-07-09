"""Reusable web API/session-manager surface for Temporal Agent Harness."""

from typing import TYPE_CHECKING

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

# ``create_agent_harness_app`` / ``create_session_manager_worker`` are exported lazily via
# ``__getattr__`` below so importing this package doesn't eagerly pull in FastAPI / the app module.
# Static type checkers can't see through a runtime ``__getattr__`` (they'd infer the union of every
# name it can return, so a call fails to match either signature), so re-import the real symbols
# under TYPE_CHECKING — the checker gets the true signatures; at runtime this block is skipped and
# ``__getattr__`` still does the lazy loading.
if TYPE_CHECKING:
    from temporal_agent_harness.web.app import create_agent_harness_app
    from temporal_agent_harness.web.worker import create_session_manager_worker


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
