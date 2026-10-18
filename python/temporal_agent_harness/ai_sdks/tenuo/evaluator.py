"""``tenuo_evaluator`` — an auto mode evaluator that checks each gated call against a Tenuo
warrant.

A Tenuo warrant is a signed capability token for ONE task: which tools the agent may call,
with constraints on their arguments, until it expires. It is issued outside the agent and
handed to the session in its init data, so the authority travels with the task rather than
living in the agent's code. The warrant can also require approval for some calls (an approval
gate on a tool or on an argument's value) and names who may give it: approvers' public keys and
how many must sign.

The evaluator answers the gate from the warrant:

  * the warrant doesn't allow the call (tool not granted, an argument out of bounds, an
    argument the warrant doesn't name, the warrant expired, revoked, or not signed by a trusted
    root) → ``DENY``, with Tenuo's reason as the error the model sees;
  * the warrant allows the call outright → ``APPROVE``;
  * the warrant allows it only with approval → the evaluator gets approvals through the worker's
    Tenuo ``approval_handler`` (see :mod:`.approvals`), verifies them against the approvers the
    warrant names, then ``APPROVE``. A refusal, an invalid signature, or no decision before the
    warrant expires → ``DENY``.

For a warrant-gated call it doesn't return ``ESCALATE``: an approval through the harness's
``tool_approval`` update isn't signed by anyone the warrant names, so it can't satisfy the
warrant's gate. (Until the harness can stop accepting ``tool_approval`` for a call an
evaluator is deciding, a ``tool_approval`` sent while the evaluator waits still settles the
gate first. Don't give that update to anyone the warrant doesn't name as an approver.)

CONFIRM. A warrant is a ceiling, so a person can safely approve anything under it. ``confirm``
names calls the warrant allows that should still wait for a person in the harness's approval
UI: the evaluator returns ``ESCALATE`` for them instead of ``APPROVE``, with no keys involved.
Pass tool names (``confirm=["refund"]``, every refund) or a function of the tool and its
arguments (``confirm=lambda tool, args: tool == "refund" and args["amount_cents"] > 5000``,
refunds over $50). Tool names are the warrant's, after ``tool_mappings``.

``confirm`` can only make the evaluator ask more often. It is consulted only for calls the
warrant has already allowed, so whatever it returns, a call the warrant refuses is denied and a
call the warrant gates still needs its approvers' signatures. That also makes it a convenience,
not a boundary: a mistake in it means asking more or less often, inside the warrant. If the
function raises, the call is put to a person. It runs inside the workflow, so like any workflow
code it must be deterministic: decide from the tool and arguments only, never the clock, the
environment or the network. To record who approved, write the same rule into the warrant as an
approval gate that names approvers instead.

Approving with "remember" allow-lists the tool in the harness, and the harness then sends later
calls of it straight through without asking the evaluator, so don't remember approvals of tools
``confirm`` covers.

CONFIGURATION is a ``tenuo.temporal.TenuoPluginConfig``, the one Tenuo's Temporal plugin uses:
its trusted roots, key resolver, revocation list, clearance requirements, ``tool_mappings`` and
``approval_handler`` apply here the same way.

OBSERVE MODE works as in Tenuo's other integrations, for trying a warrant on real traffic before
enforcing it: every call is checked in full, but a call the warrant would refuse is approved
with ``would_deny`` in the details, and gated calls don't wait for approval. Turn it on with
Tenuo's global mode (``tenuo.configure(mode="audit")`` in tenuo 0.3.2, ``mode="observe"`` from
0.3.3, which keeps the old names as aliases) or the config's ``dry_run=True``, which scopes it to
this config. ``confirm`` still applies, so a call it covers, including one the warrant would
refuse, is put to a person rather than run unasked.

The rulebook is the warrant, not :class:`AutoApprovalCriteria`. The harness still only reaches
an evaluator for calls that match a criteria set, so wire :func:`warrant_criteria` as the
agent's criteria to put every gated call in front of the warrant.

DETERMINISM. The evaluator runs in the workflow, so it reads time from ``workflow.now()``, and
its only I/O is the approval activity. Tenuo's full check (chain signatures back to a trusted
root, expiry, revocation, capabilities, constraints, proof of possession and approval
signatures) runs ``as_of`` that time, so a replay takes the same path however much later it
happens. The proof of possession is signed in-workflow with the holder key; Ed25519 signatures
are deterministic, so that is replay-safe too.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from pydantic_core import to_jsonable_python
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    # Native extensions. Re-importing them inside the workflow sandbox would load a second
    # copy alongside the worker's, so they are passed through.
    from tenuo import Authorizer, SigningKey, Warrant
    from tenuo.config import is_enforce_mode
    from tenuo.temporal import TenuoPluginConfig
    from tenuo_core import SignedApproval

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalDecision,
    AutoApprovalVerdict,
)
from temporal_agent_harness.harness.agent_workflow import AUTO_MODE_EVALUATOR_ATTR

from temporal_agent_harness.ai_sdks.tenuo.approvals import REQUEST_APPROVAL_ACTIVITY, ApprovalInput, ApprovalOutcome

#: The name :func:`warrant_criteria` registers its catch-all set under.
WARRANT_CRITERIA_SET = "tenuo_warrant"

# Retries of the approval activity while a handler reports "still waiting". The total is
# bounded by the warrant's expiry (schedule_to_close), not by an attempt count.
_APPROVAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(seconds=30),
)


def warrant_criteria() -> AutoApprovalCriteria:
    """Criteria that send every gated call to the evaluator.

    The harness reaches an evaluator only for a call that matched a criteria set, and sends
    the rest to a human. With a warrant as the rulebook there are no per-tool rules to write,
    so this is one catch-all set that names where the rules really are.
    """
    return AutoApprovalCriteria(
        sets={
            WARRANT_CRITERIA_SET: AutoApprovalCriteriaSet(
                effect="Checked against the session's Tenuo warrant.",
            )
        },
        default=WARRANT_CRITERIA_SET,
    )


def build_authorizer(config: TenuoPluginConfig) -> Authorizer:
    """An Authorizer with the same validation policy Tenuo's Temporal plugin applies: the
    config's trusted roots, clearance requirements and revocation list."""
    authorizer = Authorizer(trusted_roots=list(config.trusted_roots or []))
    for tool, clearance in (config.clearance_requirements or {}).items():
        authorizer.require_clearance(tool, clearance)
    if config.revocation_list is not None:
        authorizer.set_revocation_list(config.revocation_list)
    return authorizer


def decide(
    chain: Sequence[Warrant],
    tool: str,
    arguments: Mapping[str, Any],
    *,
    authorizer: Authorizer,
    signing_key: SigningKey,
    now: int,
    approvals: Sequence[SignedApproval] | None = None,
) -> AutoApprovalDecision:
    """The warrant's answer for one call, checked ``as_of`` the unix time ``now``.

    Pure apart from ``now``, which is the caller's to supply, so every path can be tested
    without a workflow. ``arguments`` must already be JSON-shaped.

    For a call the warrant gates, pass the signed ``approvals`` collected for it. Without them,
    the answer is ``ESCALATE``, meaning "needs the warrant's approvers", which
    :func:`tenuo_evaluator` turns into an approval request rather than returning.
    """
    leaf = chain[-1]
    details: dict[str, Any] = {"warrant_id": leaf.id, "tool": tool, "checked_at": now}

    # The warrant itself first: signed back to a trusted root, not expired, not revoked. A
    # warrant that fails here allows nothing, so even a gated call is denied, never sent for
    # approval.
    try:
        authorizer.verify_chain(list(chain), as_of=now)
    except Exception as e:
        return _deny(
            f"tenuo: this session's warrant is not valid ({e})", details, code=type(e).__name__
        )

    # Then approval gates. This also checks the capability and its constraints, so a call the
    # warrant would refuse is denied here rather than sent for approval.
    try:
        requirement = leaf.approval_requirement(tool, dict(arguments))
    except Exception as e:  # a malformed gate is a warrant Tenuo won't honour
        return _deny(f"tenuo: this session's warrant can't be applied: {e}", details,
                     code="invalid-approval-gate")
    details["status"] = requirement.status
    if requirement.is_denied():
        return _deny(
            "tenuo: this session's warrant does not allow this call "
            f"({_violation(requirement.reason, arguments)})",
            details,
            code=requirement.code,
        )
    if requirement.requires_approval() and approvals is None:
        if requirement.argument:
            details["argument"] = requirement.argument
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.ESCALATE,
            reason="tenuo: this session's warrant allows this call only with its approvers' "
            "signed approval" + (f" ({requirement.argument})" if requirement.argument else ""),
            details=details,
        )

    # Allowed, or gated with approvals in hand: the full authorization, as of `now`.
    pop = leaf.sign(signing_key, tool, dict(arguments), now)
    try:
        authorizer.check_chain(
            list(chain),
            tool,
            dict(arguments),
            signature=bytes(pop),
            approvals=list(approvals) if approvals else None,
            as_of=now,
        )
    except Exception as e:
        return _deny(
            f"tenuo: this session's warrant does not allow this call ({e})",
            details,
            code=type(e).__name__,
        )
    if approvals:
        details["approvals"] = len(approvals)
    return AutoApprovalDecision(
        verdict=AutoApprovalVerdict.APPROVE,
        reason=f"tenuo: allowed by warrant {leaf.id}"
        + (" with its approvers' signed approval" if approvals else ""),
        details=details,
    )


def _violation(reason: str, arguments: Mapping[str, Any]) -> str:
    """Tenuo's reason, with the offending value added when it names one of the arguments.

    The reason is what the model reads. "order_id: value does not match constraint" leads it to
    tell the customer to check the order ID's format; naming the value says what was refused.
    """
    name = reason.split(":", 1)[0]
    if name in arguments:
        return f"{name} {arguments[name]!r} is outside what it allows"
    return reason


def _deny(reason: str, details: dict[str, Any], *, code: str | None) -> AutoApprovalDecision:
    if code:
        details["code"] = code
    return AutoApprovalDecision(verdict=AutoApprovalVerdict.DENY, reason=reason, details=details)


def observe_mode(config: TenuoPluginConfig) -> bool:
    """Whether to observe rather than enforce: Tenuo's global mode, as every Tenuo integration
    reads it, or the Temporal config's ``dry_run``."""
    return bool(config.dry_run) or not is_enforce_mode()


def _observed(decision: AutoApprovalDecision) -> AutoApprovalDecision:
    """Observe mode: record what enforcement would have done, and let the call through."""
    if decision.verdict is AutoApprovalVerdict.APPROVE:
        return decision
    details = {**decision.details, "mode": "observe"}
    if decision.verdict is AutoApprovalVerdict.DENY:
        details["would_deny"] = decision.reason
    else:
        details["would_require_approval"] = decision.reason
    return AutoApprovalDecision(
        verdict=AutoApprovalVerdict.APPROVE,
        reason=f"tenuo (observe mode): allowed; enforcing would not have: {decision.reason}",
        details=details,
    )


async def _get_approvals(
    leaf: Warrant, tool: str, arguments: dict[str, Any], *, deadline: datetime
) -> ApprovalOutcome:
    """Ask the worker's approval handler, retrying while it reports "still waiting", until the
    warrant expires."""
    remaining = deadline - workflow.now()
    if remaining <= timedelta(0):
        return ApprovalOutcome(denied="the warrant expired before approval was requested")
    return await workflow.execute_activity(
        REQUEST_APPROVAL_ACTIVITY,
        ApprovalInput(warrant=leaf.to_base64(), tool=tool, arguments=arguments),
        result_type=ApprovalOutcome,
        # A handler may wait on a person for as long as the warrant lasts.
        schedule_to_close_timeout=remaining,
        start_to_close_timeout=remaining,
        retry_policy=_APPROVAL_RETRY,
        summary=f"tenuo approval: {tool}",
    )


def tenuo_evaluator(
    *,
    warrant: Warrant | str | Sequence[Warrant | str],
    config: TenuoPluginConfig,
    key_id: str = "",
    confirm: Iterable[str] | Callable[[str, Mapping[str, Any]], bool] = (),
):
    """Build an evaluator to pass as ``auto_mode_evaluator=``.

    Build it in the agent's ``@agent.init``, from the session's init data, so each session is
    judged by its own warrant. If the warrant gates any calls, register
    :class:`~.approvals.TenuoApprovalActivities` on the agent's worker with the same ``config``.

    Args:
        warrant: The session's warrant: a :class:`~tenuo.Warrant`, its base64 form, or a
            delegation chain ordered root first (the last one is the agent's).
        config: The worker's ``TenuoPluginConfig``. Its key resolver must resolve ``key_id`` to
            the holder key the warrant was issued to (a config built with ``signing_key=``
            ignores ``key_id``). This runs in the workflow, so, as with Tenuo's Temporal plugin,
            preload keys an environment-based resolver reads before starting the worker.
        key_id: Which holder key to resolve, for configs with a ``key_resolver``.
        confirm: Calls the warrant allows that should still wait for a person in the
            harness's approval UI: tool names (the warrant's), or a deterministic function of
            the tool name and its arguments. See CONFIRM above.

    Returns:
        An async callable of the :data:`AutoModeEvaluator` shape. It returns ``APPROVE`` or
        ``DENY``, and ``ESCALATE`` only for allowed calls ``confirm`` covers.
    """
    if isinstance(warrant, (Warrant, str)):
        warrant = [warrant]
    chain = [Warrant.from_base64(w) if isinstance(w, str) else w for w in warrant]
    if not chain:
        raise ValueError("tenuo_evaluator needs a warrant")
    signing_key = config.key_resolver.resolve_sync(key_id)
    # Checked here, in @agent.init, so a key that can't sign for this warrant fails next to
    # the wiring rather than as a denial on the first gated call.
    if chain[-1].authorized_holder != signing_key.public_key:
        raise ValueError(
            f"the config's signing key is not the holder of warrant {chain[-1].id}; "
            "configure the key the warrant was issued to"
        )
    authorizer = build_authorizer(config)
    observe = observe_mode(config)
    if callable(confirm):
        asks = confirm
    else:
        names = frozenset(confirm)

        def asks(tool: str, _arguments: Mapping[str, Any]) -> bool:
            return tool in names

    def confirmed(
        decision: AutoApprovalDecision, tool: str, arguments: dict[str, Any]
    ) -> AutoApprovalDecision:
        """An approval the warrant gave on its own, handed to a person if ``confirm`` asks."""
        if decision.verdict is not AutoApprovalVerdict.APPROVE:
            return decision
        try:
            ask = asks(tool, MappingProxyType(copy.deepcopy(arguments)))
        except Exception:
            ask = True  # a confirm that fails asks, rather than letting the call through
        if not ask:
            return decision
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.ESCALATE,
            reason=f"{decision.reason}; asking a person to confirm",
            details={**decision.details, "confirm": True},
        )

    async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        tool = config.tool_mappings.get(ctx.tool_name, ctx.tool_name)
        arguments = to_jsonable_python(ctx.tool_input)

        def check(approvals: Sequence[SignedApproval] | None = None) -> AutoApprovalDecision:
            return decide(
                chain,
                tool,
                arguments,
                authorizer=authorizer,
                signing_key=signing_key,
                now=int(workflow.now().timestamp()),
                approvals=approvals,
            )

        decision = check()
        if observe:
            return confirmed(_observed(decision), tool, arguments)
        if decision.verdict is not AutoApprovalVerdict.ESCALATE:
            return confirmed(decision, tool, arguments)

        # The warrant gates this call: get its approvers' signatures, then check again.
        # Any failure here is a denial. Raising would make the harness escalate the call to
        # its approval UI, which is exactly what a warrant's approvers replace.
        try:
            outcome = await _get_approvals(
                chain[-1], tool, arguments,
                deadline=datetime.fromisoformat(chain[-1].expires_at()),
            )
        except Exception as e:
            return _deny(f"tenuo: not approved before the warrant expired ({e})",
                         decision.details, code="approval-unavailable")
        if outcome.denied is not None:
            return _deny(f"tenuo: approval refused: {outcome.denied}", decision.details,
                         code="approval-refused")
        try:
            signed = [SignedApproval.from_bytes(base64.b64decode(a)) for a in outcome.approvals]
        except Exception as e:
            return _deny(f"tenuo: an approval couldn't be read ({e})", decision.details,
                         code="approval-unreadable")
        return check(signed)

    setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, "tenuo_evaluator")
    return evaluate
