"""Accepted message types for the Monty dynamic agent.

NB: no ``from __future__ import annotations`` here — these cross Temporal's pydantic
converter, and stringized annotations leave the discriminated-union machinery "not fully
defined". Keep annotations concrete.
"""

from pydantic import BaseModel


class RunScript(BaseModel):
    """A Python script to run in the sandbox for this turn.

    The ``run_script`` operation's description documents the full sandbox contract — the async
    script structure, the concurrency rules, and the exact host-function signatures and result
    shapes the ``script`` may call. A type/syntax/runtime error in the script is reported as the
    reply text (a bad script is normal input, not a failure)."""

    script: str
