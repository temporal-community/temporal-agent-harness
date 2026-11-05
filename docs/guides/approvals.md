# Auto mode — letting code, or a model, decide

A tool call is either **allow-listed** by `ToolApprovalPolicy`, or it goes to a human. That's the
whole branch. **Auto mode** is the third way to allow-list — the dynamic one, which judges a call's
arguments rather than just its name — alongside the static "inherently safe" and by-name lists.

A call auto mode declines to approve — `ESCALATE`, a low-confidence verdict, an evaluator failure,
a tool you wrote no criteria for — is just a call that wasn't allow-listed, so it goes to a human
exactly as if auto mode were off. Turning it on reduces how many calls a person sees; it never
takes the final say on an unclear call away from them.

`ToolApprovalPolicy.always_require_human_approval()` is the absence of all three allow-lists, so
it never consults an evaluator even when one is wired.

It is still a layer **you switch on**, not a hook that activates because you wired an evaluator.
Leave it off and you get your explicit allow-list plus your own eyes on everything else — and an
explicit "approve and stop asking" always outranks the machine.

Auto mode has three separable parts, so the person who owns the *rules* need not be the one who
wrote the *agent*:

```python
AgentWorkflowRunner(
    config,
    # THE SWITCH — a policy layer, off unless you say otherwise. `pre_approved_tools` are
    # approved ABOVE auto mode, so they never reach the evaluator and never cost a call.
    approval_policy_default=ToolApprovalPolicy.auto_mode(pre_approved_tools=["get_order"]),
    # THE RULES — named, reusable criteria sets, and which set judges which tool.
    auto_approval_criteria_default=AutoApprovalCriteria(
        sets={
            "read_only": AutoApprovalCriteriaSet(
                effect="Reads and returns records. Changes nothing.",
                approve_when=("the record is inside the requesting user's own workspace",),
                min_confidence=0.7,
            ),
            "financial": AutoApprovalCriteriaSet(
                effect="Moves money. Cannot be taken back once the processor accepts it.",
                escalate_when=("the amount exceeds an order total discussed in this chat",),
                deny_when=("the destination is not the account that placed the order",),
                min_confidence=0.95,
            ),
        },
        tools={"some_mcp_tool": "read_only"},   # by NAME, so it covers tools you didn't write
        default="read_only",                     # the catch-all
    ),
    # THE MECHANISM — `jev_evaluator` (from `temporal_agent_harness.ai_sdks.jev`, with the
    # `typesafe` extra) puts a model in the seat, via one fast typed judgment from Jev
    # (https://docs.typesafe.ai) rather than a prompt to parse.
    auto_mode_evaluator=jev_evaluator(),
)
```

A tool names the set it should be judged against — a label, never the rules themselves:

```python
@agent.activity_tool_defn(auto_approval_criteria="financial")
async def issue_refund(order_id: str, amount_cents: int) -> Receipt:
    """Refund a customer's order to the card that paid for it."""
```

That indirection is the point. A tool author is well placed to say *what kind of thing this tool
is* and badly placed to guess your risk appetite — so the rules behind the name are configuration.
An operator overrides them per session via `AgentConfig.auto_approval_criteria`, or at runtime from
a message handler, letting an end user set their own security posture mid-session:

```python
# model_callable=False is not boilerplate here: a handler that can move the guardrail
# must never become a tool a driving model can call to move it.
@agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
async def set_posture(self, msg: Posture) -> Ack:
    self._runner.set_approval_policy(
        self._runner.approval_policy.with_auto_mode(msg.auto_mode)  # the switch
    )
    self._runner.set_auto_approval_criteria(msg.criteria)               # redefine the rules
    self._runner.assign_tool_criteria("run_sql", "cautious")            # retarget one tool
    return Ack()
```

It can't fail open: low confidence, an irreversible effect, a raise, or a TypeSafe outage all
land at the human gate rather than approving.

And the harness won't even *ask* unless the call is governed. A tool with no criteria set
assigned, or one assigned to a name nobody registered, is escalated by the harness itself — no
evaluator is invoked and no model call is spent, so an unconfigured auto mode costs nothing.
That's enforced in the gate rather than left to each evaluator, which is why an evaluator
receives its governing rules as a required, non-optional field and needs no defensive check of
its own. Needs the `typesafe` extra, with its `TypeSafePlugin` registered on the worker before
`AgentHarnessPlugin`.

Every evaluator — yours or Jev's — is bracketed on the event stream
(`auto_approval_evaluation_started` → `_ended` / `_superseded` / `_error`), so its verdict,
reasoning and latency stay auditable even when it *escalates* and resolves nothing. A human who
answers first **cancels** it, and the console gives the evaluation its own card and its own share
of the approval wait.

[`python/examples/auto_mode`](../../python/examples/auto_mode) runs all of this end to end: the Monty travel agent,
with lookups auto-approved and bookings still coming to you.
