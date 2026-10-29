# ABOUTME: Public surface of the agent harness. Re-exports the workflow-side runtime
# (AgentWorkflowRunner) so agent authors import flat names from ``harness`` without reaching
# into private modules. Declare accepted messages as ``@agent.accepts`` handler methods;
# construct a runner via ``AgentWorkflowRunner(config, stream=..., approval_policy_default=...)``
# in ``@agent.init``; the ``run`` that ``@agent.defn`` generates drives its turn loop.

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_workflow import (
    AgentToolContext,
    AgentWorkflowRunner,
)

__all__ = [
    "agent",
    "AgentToolContext",
    "AgentWorkflowRunner",
]
