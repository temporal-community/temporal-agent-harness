"""Compatibility wrapper for the example agent registry file."""

from __future__ import annotations

from pathlib import Path

from temporal_agent_harness.web.registry import load_agent_registry as _load_agent_registry
from temporal_agent_harness.web.session_manager import AgentDescriptor, AgentRegistry

DEFAULT_REGISTRY_PATH = Path(__file__).parent / "agents.toml"


def load_agent_registry(path: Path | str | None = None) -> AgentRegistry:
    registry_path = DEFAULT_REGISTRY_PATH if path is None else path
    return _load_agent_registry(registry_path)


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "AgentDescriptor",
    "AgentRegistry",
    "load_agent_registry",
]
