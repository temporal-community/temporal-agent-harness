"""Auto mode for tool approvals: let a Cedar tool policy, served over Nexus, decide.

``cedar_evaluator(endpoint=...)`` returns an ``auto_mode_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner` that asks the
``VerifyToolCalls`` operation of the temporal-untrusted-workers tool verifiers about each gated
call (see :mod:`.evaluator`). Which tools may run, and with which arguments, lives in the policy
files, so the evaluator is configured per agent, never rewritten.

Code Mode scripts are judged by the facts
:func:`~temporal_agent_harness.harness.code_mode.analyze_script` reads off them, plus an optional
classifier's answers (``jev_script_classifier()`` is the builtin).
:func:`.schema.cedar_schema` writes the Cedar schema for an agent's tools, so the policy and the
parameters the evaluator sends can't drift apart.

Workflow-safe throughout, and no worker-side activity: the operation is called from the
workflow.
"""

from .evaluator import (
    DEFAULT_OPERATION,
    DEFAULT_SERVICE,
    DEFAULT_TIMEOUT,
    VerifyCaller,
    VerifyToolCall,
    VerifyToolCallsRequest,
    cedar_evaluator,
    decide,
    script_parameter,
)
from .toolpolicy_pb2 import VerifyToolCallsResponse

__all__ = [
    "DEFAULT_OPERATION",
    "DEFAULT_SERVICE",
    "DEFAULT_TIMEOUT",
    "VerifyCaller",
    "VerifyToolCall",
    "VerifyToolCallsRequest",
    "VerifyToolCallsResponse",
    "cedar_evaluator",
    "decide",
    "script_parameter",
]
