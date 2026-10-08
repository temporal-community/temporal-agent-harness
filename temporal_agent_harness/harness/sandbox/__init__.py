"""Durable sandboxes for harness agents, on the OpenAI Agents SDK sandbox layer.

Needs the ``sandbox`` extra. Workflow modules import this package (and any ``agents.sandbox``
types) inside ``workflow.unsafe.imports_passed_through()``; core harness modules never import
it. See ``docs/design/agent-sandboxes.md``.
"""

from agents.sandbox.session.sandbox_session import SandboxSession

from temporal_agent_harness.harness.sandbox._client import TemporalSandboxClient
from temporal_agent_harness.harness.sandbox._provider import (
    SandboxClientProvider,
    sandbox_activities,
)
from temporal_agent_harness.harness.sandbox._session import TemporalSandboxSession
from temporal_agent_harness.harness.sandbox.capabilities import Filesystem, Shell
from temporal_agent_harness.harness.sandbox.config import IdlePolicy, SandboxConfig
from temporal_agent_harness.harness.sandbox_image import SandboxImage

__all__ = [
    "Filesystem",
    "IdlePolicy",
    "SandboxClientProvider",
    "SandboxConfig",
    "SandboxImage",
    "SandboxSession",
    "Shell",
    "TemporalSandboxClient",
    "TemporalSandboxSession",
    "sandbox_activities",
]
