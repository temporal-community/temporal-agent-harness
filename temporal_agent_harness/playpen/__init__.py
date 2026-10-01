"""Playpen: an agent whose generated code can only act through calls a policy decides.

Code Mode is the sandbox and the proxy: a model-authored script runs in Monty, which has no
filesystem, network or imports, and its only way to affect anything is to call a host function,
which traps out of the sandbox into the workflow. Playpen adds the policy engine at that trap:
every host call is decided against a `Dogwood <https://github.com/dogwood-policy/dogwood>`_
policy — Cedar plus conditions over what the session has already done — before it runs. A call
the policy permits runs; one it forbids raises ``PermissionError`` in the script; one denied only
by a ``forbid`` annotated ``@on_deny("escalate")`` waits for a person to approve it.

The package splits along the workflow boundary, like the rest of the harness:

* Workflow-safe: :class:`DogwoodPolicy` (this module re-exports it) and :mod:`.models`.
* Worker-side: :class:`~.activity.DogwoodPolicyStore`, whose ``decide`` activity runs the
  ``dogwood`` CLI. Import it from :mod:`temporal_agent_harness.playpen.activity` on the worker.
* Offline: :mod:`.schema`, which generates a policy's Cedar action schema from the agent's
  host functions.
"""

from .models import DOGWOOD_DECIDE_ACTIVITY, DogwoodDecision, DogwoodEvent
from .policy import DogwoodPolicy

__all__ = ["DOGWOOD_DECIDE_ACTIVITY", "DogwoodDecision", "DogwoodEvent", "DogwoodPolicy"]
