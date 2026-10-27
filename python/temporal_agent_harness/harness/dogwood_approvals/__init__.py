"""Auto mode for tool approvals: let a Dogwood tool policy, served over Nexus, decide.

``dogwood_evaluator(endpoint=...)`` returns an ``auto_mode_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner` that asks the
``VerifyToolCalls`` operation of the temporal-untrusted-workers Dogwood verifier about each
gated call, after recording with ``RecordInputEvents`` the messages and tool results the run
received since it last asked, so a policy can judge a call by the run's history (see
:mod:`.evaluator`). Which tools may run, and with which arguments, lives in the policy files, so
the evaluator is configured per agent, never rewritten.

A tool whose arguments a policy can't read directly gets a :data:`PolicyCallBuilder`, which
turns them into facts and extra checks. :func:`.schema.dogwood_schema` writes the action schema
for an agent's tools, checks and messages, so the policy and what the evaluator sends can't
drift apart.

Workflow-safe throughout, and no worker-side activity of its own: the operations are called
from the workflow.
"""

from temporal_agent_harness.harness.dogwood_approvals.evaluator import (
    DEFAULT_RECORD_OPERATION,
    DEFAULT_SERVICE,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_OPERATION,
    InputEvent,
    InputEventKind,
    PolicyCall,
    PolicyCallBuilder,
    PolicyCalls,
    RecordInputEventsRequest,
    VerifyCaller,
    VerifyToolCall,
    VerifyToolCallsRequest,
    decide,
    dogwood_evaluator,
    input_events,
)
from temporal_agent_harness.harness.dogwood_approvals.schema import dogwood_schema
from temporal_agent_harness.harness.dogwood_approvals.toolpolicy_pb2 import (
    Decision,
    RecordInputEventsResponse,
    VerifyToolCallsResponse,
)

__all__ = [
    "DEFAULT_RECORD_OPERATION",
    "DEFAULT_SERVICE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_VERIFY_OPERATION",
    "Decision",
    "InputEvent",
    "InputEventKind",
    "PolicyCall",
    "PolicyCallBuilder",
    "PolicyCalls",
    "RecordInputEventsRequest",
    "RecordInputEventsResponse",
    "VerifyCaller",
    "VerifyToolCall",
    "VerifyToolCallsRequest",
    "VerifyToolCallsResponse",
    "decide",
    "dogwood_evaluator",
    "dogwood_schema",
    "input_events",
]
