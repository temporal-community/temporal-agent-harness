"""Reusable web API surface for Temporal Agent Kit."""

from typing import TYPE_CHECKING

from temporal_agent_kit.web.registry import (
    AgentDescriptor,
    AgentRegistry,
    AgentStart,
    load_agent_registry,
)
from temporal_agent_kit.web.sessions import Session, SessionPage

# ``create_agent_kit_app`` is exported lazily via ``__getattr__`` below so importing this package
# doesn't eagerly pull in FastAPI / the app module. Static type checkers can't see through a
# runtime ``__getattr__``, so re-import the real symbol under TYPE_CHECKING — the checker gets the
# true signature; at runtime this block is skipped and ``__getattr__`` still does the lazy loading.
if TYPE_CHECKING:
    from temporal_agent_kit.web.app import create_agent_kit_app


def __getattr__(name: str):
    if name == "create_agent_kit_app":
        from temporal_agent_kit.web.app import create_agent_kit_app

        return create_agent_kit_app
    raise AttributeError(name)


__all__ = [
    "AgentDescriptor",
    "AgentRegistry",
    "AgentStart",
    "Session",
    "SessionPage",
    "create_agent_kit_app",
    "load_agent_registry",
]
