"""Temporal Agent Kit (experimental): durable agents on Temporal, with the AI SDKs you already use.

Agent authors import the core API from here: ``agent`` (the ``@agent.defn`` / ``@agent.accepts``
/ tool decorators) and the workflow-side runtime (``AgentWorkflowRunner``). Further modules:

  * :mod:`temporal_agent_kit.state`, :mod:`temporal_agent_kit.agent_protocol`,
    :mod:`temporal_agent_kit.code_mode` — observable state, the event protocol and Code Mode.
  * :mod:`temporal_agent_kit.ai_sdks` — integrations that make AI SDK calls durable Temporal
    activities.
  * :mod:`temporal_agent_kit.plugin` — ``AgentKitPlugin``, the one Temporal plugin that wires a
    client and worker for all of the above: it supplies the data converter and registers the
    kit's activities, so a worker declares only its workflows.
"""

from temporal_agent_kit import agent
from temporal_agent_kit.agent_workflow import (
    AgentToolContext,
    AgentWorkflowRunner,
)

__all__ = [
    "agent",
    "AgentToolContext",
    "AgentWorkflowRunner",
]
