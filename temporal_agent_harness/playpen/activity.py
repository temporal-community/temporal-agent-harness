"""Worker-side: decide a Playpen host call against a Dogwood policy, with the ``dogwood`` CLI.

:class:`DogwoodPolicyStore` is the policy store — a directory with one sub-directory per policy,
each holding ``policy.dw`` (the rules) and ``schema.cedarschema`` (the actions they are written
against) — and its :meth:`~DogwoodPolicyStore.decide` activity is the policy engine. Each
decision replays the session's whole trace through Dogwood's reference interpreter
(``dogwood replay``) and reads the verdict for the last event, the call being decided.

Replaying the whole trace keeps the decision a pure function of its input: the trace lives in
workflow state, so a retried activity, a restarted worker, or a different worker reaches the
same verdict, and no engine state on this worker's disk can drift from workflow history. The
cost is a replay per decision, which is fine at the length of one agent session.

The policy files are read on every decision, so an edit takes effect on the next call. Each
decision records the SHA-256 of the policy and schema text it was made under.

This module is WORKER-SIDE — never import it from workflow code. The workflow side dispatches
the activity by name (``DOGWOOD_DECIDE_ACTIVITY``).

NB: no ``from __future__ import annotations`` — the activity's annotations are read concretely
to type its argument and result across the pydantic converter.
"""

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import (
    DOGWOOD_DECIDE_ACTIVITY,
    DogwoodDecideInput,
    DogwoodDecision,
    DogwoodRule,
)
from .policy_file import PolicyRule, parse_rules
from .trace import render_trace

logger = logging.getLogger(__name__)

POLICY_FILE = "policy.dw"
SCHEMA_FILE = "schema.cedarschema"

# The event kinds of a Playpen trace (see :data:`~.models.EventKind`). Dogwood's default schema,
# plus ``admitted``: a call the gate let through. Policies that ask "has this already been
# done?" ask about ``admitted`` rather than ``request`` — a ``formerly`` over ``request`` matches
# the very call being decided — or ``response``, which a concurrent call that was let through
# but has not finished yet would not have produced.
EVENT_SCHEMA = """\
decision event <A>::request {
    ...inputs(A),
    pin callerPrincipal: principalType(A) = principal,
    callerResource: resourceType(A),
    requestId: String,
}

event <A>::admitted {
    ...inputs(A),
    pin callerPrincipal: principalType(A) = principal,
    callerResource: resourceType(A),
    requestId: String,
}

event <A>::response {
    ...inputs(A),
    ...outputs(A),
    pin callerPrincipal: principalType(A) = principal,
    callerResource: resourceType(A),
    requestId: String,
}
"""

_NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_POLICY_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
DEFAULT_CLI_TIMEOUT = timedelta(seconds=20)


class DogwoodPolicyStore:
    """The policies Playpen agents are decided against, and the CLI that decides them.

    Register :meth:`decide` on the worker that hosts Playpen agents::

        store = DogwoodPolicyStore(Path("policies"), dogwood_cli="/path/to/dogwood")
        Worker(..., activities=[store.decide])
    """

    def __init__(
        self,
        root: Path,
        *,
        dogwood_cli: str,
        cli_timeout: timedelta = DEFAULT_CLI_TIMEOUT,
    ) -> None:
        if not root.is_dir():
            raise ValueError(f"policy store {root} is not a directory")
        if not Path(dogwood_cli).is_file():
            raise ValueError(f"dogwood CLI {dogwood_cli} does not exist")
        self._root = root
        self._cli = dogwood_cli
        self._cli_timeout = cli_timeout

    @activity.defn(name=DOGWOOD_DECIDE_ACTIVITY)
    async def decide(self, input: DogwoodDecideInput) -> DogwoodDecision:
        """Decide ``input.request`` against the named policy, after ``input.history``."""
        policy_dir = self._policy_dir(input.policy)
        policy_text = (policy_dir / POLICY_FILE).read_text()
        schema_text = (policy_dir / SCHEMA_FILE).read_text()
        rules = parse_rules(policy_text)
        namespace = _namespace(schema_text)
        events = [*input.history, input.request]
        decision_events = sum(1 for e in events if e.kind == "request")

        with tempfile.TemporaryDirectory(prefix="dogwood-") as tmp:
            trace_path = Path(tmp) / "trace.log"
            events_path = Path(tmp) / "events.dwschema"
            trace_path.write_text(render_trace(namespace, input.principal, events))
            events_path.write_text(EVENT_SCHEMA)
            report = await self._replay(
                policy_dir / POLICY_FILE, policy_dir / SCHEMA_FILE, events_path, trace_path
            )

        verdicts = report["verdicts"]
        if len(verdicts) != decision_events:
            raise ApplicationError(
                f"dogwood returned {len(verdicts)} verdicts for {decision_events} decision "
                "events; cannot tell which one is this call's",
                type="DogwoodVerdictMismatch",
                non_retryable=True,
            )
        verdict = verdicts[-1]
        if verdict["errors"]:
            # Dogwood degrades rather than aborting on an evaluation error, so a verdict that
            # carries errors may rest on a rule that could not be evaluated. Fail the decision;
            # the harness sends a call whose evaluator failed to a person.
            raise ApplicationError(
                f"dogwood could not fully evaluate the policy: {verdict['errors']}",
                type="DogwoodEvaluationError",
                non_retryable=True,
            )
        determining = [rules[i] for i in verdict["determining_rules"]]
        decision = _decision(
            input.request.action,
            allowed=verdict["verdict"] == "allow",
            determining=determining,
            rules=rules,
            policy_sha256=hashlib.sha256((policy_text + schema_text).encode()).hexdigest(),
        )
        logger.info(
            "dogwood: %s %s -> %s (%s)",
            input.request.action,
            input.request.request_id,
            decision.verdict,
            decision.reason,
        )
        return decision

    def _policy_dir(self, name: str) -> Path:
        if not _POLICY_NAME.match(name):
            raise ApplicationError(
                f"policy name {name!r} must be letters, digits, '-' or '_'",
                type="DogwoodPolicyName",
                non_retryable=True,
            )
        policy_dir = self._root / name
        for required in (POLICY_FILE, SCHEMA_FILE):
            if not (policy_dir / required).is_file():
                raise ApplicationError(
                    f"policy {name!r} has no {required} in {policy_dir}",
                    type="DogwoodPolicyMissing",
                    non_retryable=True,
                )
        return policy_dir

    async def _replay(
        self, policy: Path, schema: Path, event_schema: Path, trace: Path
    ) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self._cli,
            "replay",
            str(policy),
            "--policy-schema",
            str(schema),
            "--event-schema",
            str(event_schema),
            "--trace",
            str(trace),
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), self._cli_timeout.total_seconds()
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            # Exit 2 is Dogwood rejecting the policy, schema or trace — retrying cannot fix it.
            raise ApplicationError(
                f"dogwood replay exited {proc.returncode}: "
                f"{(stderr or stdout).decode(errors='replace').strip()}",
                type="DogwoodRejected",
                non_retryable=proc.returncode == 2,
            )
        return json.loads(stdout)


def _decision(
    action: str,
    *,
    allowed: bool,
    determining: list[PolicyRule],
    rules: list[PolicyRule],
    policy_sha256: str,
) -> DogwoodDecision:
    refs = [DogwoodRule(index=r.index, annotations=r.annotations) for r in determining]
    if allowed:
        return DogwoodDecision(
            verdict="allow",
            reason="Permitted by " + ", ".join(r.id for r in determining),
            rules=refs,
            policy_sha256=policy_sha256,
        )
    if not determining:
        # An implicit deny: no forbid fired, and no permit covered the call.
        permits = [r for r in rules if r.effect == "permit" and r.covers(action)]
        reason = f"No rule in the policy permits this {action} call."
        explained = [r.annotations["reason"] for r in permits if "reason" in r.annotations]
        if explained:
            reason += f" {action} is permitted only when: " + " ".join(explained)
        return DogwoodDecision(
            verdict="deny", reason=reason, rules=refs, policy_sha256=policy_sha256
        )
    reason = " ".join(
        f"{r.id}: {r.annotations['reason']}" if "reason" in r.annotations else r.id
        for r in determining
    )
    # A hard forbid outranks an escalation: a person is asked only when every rule that
    # denied the call says a person may decide it.
    escalate = all(r.escalates_on_deny for r in determining)
    return DogwoodDecision(
        verdict="escalate" if escalate else "deny",
        reason=reason,
        rules=refs,
        policy_sha256=policy_sha256,
    )


def _namespace(schema_text: str) -> str:
    namespaces = _NAMESPACE.findall(schema_text)
    if len(namespaces) != 1:
        raise ApplicationError(
            f"the policy schema must declare exactly one namespace, found {namespaces}",
            type="DogwoodSchema",
            non_retryable=True,
        )
    return namespaces[0]
