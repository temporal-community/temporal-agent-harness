# Human-in-the-Loop Tool Approvals

**Status:** ✅ Implemented (see `harness/test_tool_approvals.py`, `harness/test_runner_builder.py`).
**Scope:** `harness/` (decorators, runner, builder, protocol, `jev_approvals/`), `google_genai_plugin/` (schema adapter), and the agent tool definitions + worker wiring.
**Also see:** `harness/test_jev_approvals.py` for the builtin AI approval gate.

> ## ⚠️ Read first — approvals are SAFE-BY-DEFAULT and POLICY-DRIVEN
>
> This feature was **inverted** after the original plan below was written. The plan's
> "a tool opts into approval with `approval_required=True`" model is **gone** — it was
> fail-open (forget the flag → a dangerous tool runs unilaterally) and wrongly let the
> *tool* decide enforcement. The current design (this section is the source of truth;
> §1–§12 below are the original plan, kept for rationale but superseded wherever they
> conflict):
>
> - **A tool only asserts `inherently_safe`.** `@agent.activity_tool_defn(inherently_safe=True)`
>   / `@agent.tool_defn(inherently_safe=True)` is a *static hint* meaning "this tool is
>   never, under any input, unsafe." It is **not** a decision to skip approval. Default is
>   `False`. There is no `approval_required` anymore.
> - **An agent-level `ToolApprovalPolicy` decides gating, not the tool.** It is a single
>   *composable, serializable, frozen* pydantic model
>   (`harness/agent_protocol/agent_interface.py`) whose layers `auto_approves(...)` checks
>   in priority order:
>     0. `dangerously_skip_all_approvals` — approve EVERYTHING (disables the guardrail).
>     1. `auto_approve_inherently_safe` — approve tools that declared `inherently_safe`.
>     2. `auto_approve_tools: frozenset[str]` — approve these tool names (additive).
>   A call no layer approved goes to **auto mode** (layer 3, `auto_mode_enabled`, off by
>   default) if the policy enables it, and is otherwise **gated** for a human. Named presets:
>   `always_require_human_approval()` (the safe-by-default baseline — gate everything for a human,
>   even safe tools, even with an evaluator wired), `allow_inherently_safe()`,
>   `allow_tools([...], also_inherently_safe=False)`,
>   `auto_mode(pre_approved_tools=..., pre_approve_inherently_safe=...)`,
>   `dangerously_skip_all()`.
> - **The runner REQUIRES a default policy.** `AgentWorkflowRunner(config, stream=...,
>   approval_policy_default=...)` — the kwarg is mandatory (no harness baseline; the author
>   must choose deliberately). The builder this bullet used to describe is deleted; the
>   runner is constructed directly. `auto_mode_evaluator=` is optional and, on its own,
>   inert: auto mode also needs the policy's switch on and criteria configured for the tool.
>   See **Auto mode is a POLICY MODE** below.
> - **A caller can override the default per session.** `AgentConfig.approval_policy:
>   ToolApprovalPolicy | None` — caller value wins over the agent default (so an operator
>   can start a session that gates *everything*). The auto mode **evaluator** is not part of
>   `AgentConfig` (non-serializable; never overridable) — only its criteria are.
> - **Runtime updates.** `runner.set_approval_policy(policy)` swaps the live policy
>   (re-evaluating pending approvals — see below). A `tool_approval` decision with
>   `remember=True` ("approve, and stop asking me about this tool") allow-lists the tool,
>   which cascades to any *other* pending call of that tool. The live policy is surfaced on
>   `AgentStatus.approval_policy` (plus `has_auto_approval_evaluator: bool`) so a client
>   can read and persist it and replay it via `AgentConfig.approval_policy` next session.
> - **Relaxing a policy releases pending calls.** `_apply_policy_update` re-evaluates every
>   still-PENDING approval against the new policy; any now auto-approved is resolved
>   (`reason="auto-approved by updated policy"`). It does NOT publish — each parked gate's
>   own `wait_condition` wakes on the status flip and publishes its own
>   `ToolApprovalResolved`, exactly as on the manual approve path.
> - **The gate helper is `_apply_approval_policy(tool_name, tool_input, *, inherently_safe,
>   tool_description=None)`** (renamed from `_await_tool_approval`). Three layers, in order:
>   (1) `runner._policy_auto_approves(...)` — if the serializable policy approves it returns
>   immediately, dispatching with **no gate and no events** (an allow-listed call is not an
>   approval, it is simply not gated); (2) otherwise it registers PENDING, publishes
>   `ToolApprovalRequested`, and *then* awaits `runner._run_auto_mode_evaluator(...)`;
>   (3) whatever is still unresolved runs the same unbounded `wait_condition` gate as before.
> - **Public API:** `from harness.agent import ToolApprovalPolicy, AutoApprovalContext,
>   AutoModeEvaluator, AutoApprovalVerdict, AutoApprovalDecision, jev_evaluator`
>   (re-exported alongside the decorators). `client.approve_tool(..., remember=False)`.
>
> ### Auto mode evaluators — async, three-valued
>
> `auto_mode_evaluator: Callable[[AutoApprovalContext], Awaitable[AutoApprovalDecision]]`.
> `AutoApprovalContext` carries `tool_name`, `tool_input`, `inherently_safe`, and
> `tool_description` (the tool's docstring — the same prose the model saw when it chose the
> call).
>
> - **`AutoApprovalDecision(verdict, reason, details)`** is the ONE accepted shape. A bare
>   `bool` is deliberately rejected: `False` would have to mean "escalate", not "deny", and a
>   falsy value that means neither no nor yes is exactly the ambiguity the three-valued
>   verdict removes. `AutoApprovalVerdict` is `APPROVE` / `DENY` (the call never runs;
>   `ToolApprovalDenied` carries `reason` to the model) / `ESCALATE` (abstain — wait for a
>   human, exactly as with no evaluator). `ESCALATE`, not `DENY`, is the right answer to "I
>   don't know": denying on uncertainty trains operators to switch the guardrail off.
> - **It MUST be async**, enforced at runner construction by
>   `_assert_async_auto_mode_evaluator` so the error lands next to the developer's own
>   wiring. Two reasons, the second load-bearing: asking a model is an activity call no
>   synchronous predicate can make; and **only a coroutine can be cancelled**, which the gate
>   does when a human beats it (see below). A callable object with an `async def __call__`
>   counts.
>
> Three behaviours are load-bearing and were chosen deliberately:
>
> 1. **The call is registered PENDING and `ToolApprovalRequested` published BEFORE the
>    evaluator is consulted** (a policy auto-approval still publishes nothing). So an
>    in-flight evaluation is visible on `agent_status`, a human can decide while it
>    deliberates, and the verdict + `reason` land as a normal `tool_approval_resolved` — an
>    approval nobody consented to has to be auditable. **First decision wins.**
> 2. **A decision that beats the evaluator CANCELS it.** The evaluator runs as its own
>    asyncio task, raced against the gate being settled by anyone (a `tool_approval`, a policy
>    cascade, agent close). If the gate settles first the task is cancelled and awaited out,
>    and the bracket closes on `..._superseded`. An evaluator left running would keep paying
>    for a model call — and holding an activity worker slot — to answer a question nobody is
>    waiting for.
> 3. **A raising evaluator — or one returning a non-`AutoApprovalDecision`, the same class of
>    bug — ESCALATES.** The guardrail must not fail open, and must not wedge a turn a person
>    could unblock. Logged and published as `..._error`, never swallowed.
> 4. **The policy-update cascade does not consult the evaluator** — only the policy. Every
>    pending entry already escalated once; re-asking cannot change the answer, would cost an
>    extra model call per pending call per policy change, and is impossible anyway from a sync
>    update handler.
>
> ### The evaluation bracket — `auto_approval_evaluation_started` / `_ended` / `_superseded` / `_error`
>
> The harness publishes a **bracket** around EVERY auto mode evaluator, whatever it decided.
> Published by the runner, so a developer gets it by wiring a fallback at all, never by
> remembering to instrument one:
>
> ```
> tool_approval_requested
>   auto_approval_evaluation_started      evaluator named; nothing decided yet
>      … arbitrarily slow work …
>   auto_approval_evaluation_ended        verdict + reason + details
>   auto_approval_evaluation_superseded   …or: cancelled, the gate was decided first
>   auto_approval_evaluation_error        …or: it raised; escalate substituted
> tool_approval_resolved                  only if a verdict settled the gate
> ```
>
> **Why a bracket and not one terminal event.** An evaluator does real, arbitrarily slow
> work — the Jev approver spends a model round-trip per gated call. Only a bracket makes
> that work visible *while it happens* (a consumer can tell "an approver is deliberating"
> from "this is waiting for a person"; the pending gate alone cannot express the
> difference), times it exactly from the two envelope timestamps rather than by inferring a
> delta from a neighbouring event of a different kind, and surfaces a hung or timing-out
> evaluator as an open bracket instead of silence. It also splits the approval span the UI
> already draws from `requested → resolved`, which otherwise fuses model latency and human
> latency into one bar (`stepTimeline.ts` gained a fourth `SpanKind`, `"evaluation"`, that
> outranks `"approval"` in the exclusive rollup precisely so the leftover *is* the human's
> wait).
>
> **Why it must cover `ESCALATE`.** An escalate resolves nothing, so without its own event
> the only trace of an evaluator that ran, cost a round-trip and declined to decide would be
> the human decision that eventually follows — attributed entirely to the human. "Why is
> this in my approval queue?" was unanswerable.
>
> - **`evaluation_id`** pairs start with terminal. `tool_id` is NOT enough: an agent may
>   chain several evaluators over one gated call. (`ModelInteractionStarted`/`Ended` carry no
>   such id and are genuinely ambiguous under concurrency — deliberately not copied.)
> - **`evaluator`** is read off the CALLABLE (`__auto_mode_evaluator__`, else its qualname),
>   not off a returned decision — two of the three events have no decision to read it from.
> - **`details`** is the evaluator's free-form structured reasoning, from
>   `AutoApprovalDecision.details`. Empty for an evaluator that fills in nothing.
> - **`..._superseded`** is the cancellation terminal, and carries the verdict the evaluator
>   had reached if the race was tight enough that it reached one — an evaluator that would
>   have denied a call a human waved through is exactly what an audit is looking for. A
>   verdict on `..._ended` is therefore always one that was ACTED ON, which is why there is
>   no `applied` flag.
> - **`..._error`** is a separate terminal, mirroring `tool_error` against `tool_end`. A
>   TypeSafe outage is a *failed* evaluation, not an abstention, and flattening the two would
>   hide "the approver was broken and everything silently went to humans for three hours".
>   Its `message` renders the exception's `__cause__` chain, because a bare
>   `str(ActivityError)` is the useless "Activity task failed".
> - **The policy layer gets no bracket.** Brackets exist to expose work that takes time; the
>   policy layer is a frozen-model dict lookup on the hottest path.
>
> ### Auto mode is a POLICY MODE, not a fallback
>
> THE WHOLE STORY: a call is either allow-listed by the policy, or it goes to a human. Auto mode
> is one of the ways to allow-list — the dynamic one — not a stage after the static ones.
>
> | | field | allow-lists by |
> | --- | --- | --- |
> | 1 | `auto_approve_inherently_safe` | the tool's own static safety claim |
> | 2 | `auto_approve_tools` | name — what "approve & remember" grows |
> | 3 | `auto_mode_enabled` | **the evaluator's per-call judgement of the arguments** |
> | — | `dangerously_skip_all_approvals` | nothing; it removes the gate rather than scoping it |
>
> `always_require_human_approval()` is the absence of all three, which is why it does not
> "combine" with auto mode: auto mode IS allow-listing, so a policy doing it is not a policy
> that sends everything to a person. Enabling it selects a different policy rather than
> modifying that one, and the preset clears all three explicitly to make that legible.
>
> `auto_approves()` answers layers 0–2 (dispatch ungated, publish nothing);
> `consults_auto_mode()` answers layer 3. The gate reads the second only after the first
> says no, and `AgentWorkflowRunner._policy_consults_auto_mode` additionally requires an
> evaluator to actually be wired — because whether one is is a property of the agent's code,
> not of its serializable policy.
>
> - **It filters the human gate, it does not replace it.** `ESCALATE`, a low-confidence verdict,
>   an evaluator failure and an ungoverned tool all leave the call PENDING on the same
>   `wait_condition` a human answers — so enabling auto mode can only reduce how many calls reach
>   a person. There is correspondingly nothing to make mutually exclusive with "a human approves
>   everything": that posture is just the all-defaults policy, and enabling auto mode means the
>   policy is no longer it.
> - **Off by default, even with an evaluator wired.** Wiring `auto_mode_evaluator=` used
>   to activate it for every gated call. It no longer does: an operator who wants nothing but
>   their explicit allow-list plus their own eyes leaves layer 3 off and no model is ever
>   asked. The runner logs a warning for the wired-but-disabled combination, rather than
>   raising, because "available now, switched on later by a handler" is a legal posture.
> - **On with no evaluator is REJECTED.** The reverse — `auto_mode_enabled=True` on an agent
>   with no `auto_mode_evaluator` — would report auto mode on while nothing judged, and a
>   caller's `AgentConfig` or a handler's `set_approval_policy` can ask for it, not just the
>   author. `_assert_auto_mode_has_an_evaluator` raises a non-retryable `ApplicationError`
>   (`type="AutoModeWithoutEvaluator"`) wherever a policy is installed: from
>   `@workflow.init` that fails the workflow cleanly, from a handler it fails that message
>   and leaves the live policy untouched.
> - **The allow-list outranks the machine.** A call layers 0–2 approved never reaches the
>   evaluator, so an explicit human "always allow this tool" (`remember=True`) permanently
>   takes that tool out of auto mode's hands — and costs no model call.
> - **Toggling is a modification of the live policy**, via `with_auto_mode(bool)` off
>   `runner.approval_policy`. Replacing the policy wholesale would discard the allow-list a
>   user had built up.
>
> ### `AutoApprovalCriteria` — named criteria sets, selected per tool
>
> The RULEBOOK, split from the MECHANISM. `AutoApprovalCriteriaSet` is one named, reusable
> set of rules (`effect` prose + `approve_when` / `deny_when` / `escalate_when` lists +
> optional per-set `min_confidence` / `escalate_if_irreversible_above`);
> `AutoApprovalCriteria` holds `sets` (name → set), `tools` (tool name → set name),
> `default` (the catch-all), `context`, and the agent-wide thresholds.
>
> - **Tools declare a NAME, not rules.** `@agent.tool_defn(auto_approval_criteria="financial")`
>   on all four tool decorators, plus `code_mode_tool` and `as_harness_mcp_server`. A tool
>   author is well placed to say what KIND of thing a tool is and badly placed to say how sure
>   a machine must be before acting unattended, so the bodies live in configuration where the
>   deploying operator — usually the end user, not the agent's author — can redefine what
>   `"financial"` means without touching code.
> - **Resolution, highest first:** `criteria.tools[name]` (config or a runtime assignment) →
>   the decorator's declared name → `criteria.default`. Applied once, in
>   `set_name_for`/`for_tool`, and read through `AutoApprovalContext.criteria_set` so no
>   evaluator can forget the precedence. The declared name rides on the call because the
>   runner holds **no tool registry** to look it up from.
> - **Addressing by NAME is what covers tools nobody wrote.** An MCP server's tools arrive
>   already defined with no decorator to hang anything on, and an end user setting their own
>   posture at runtime cannot edit a decorator at all.
> - **Runtime updates:** `runner.set_auto_approval_criteria(criteria)` swaps the rulebook and
>   bumps `criteria_version`; `runner.assign_tool_criteria(tool, set_name)` retargets one tool
>   without touching bodies. Takes effect on the next gated call — an in-flight evaluation
>   completes against the criteria it started with, and records the version it applied.
>   Unlike a policy update this does **not** cascade over pending approvals.
> - **Nothing to judge against means ask a person, enforced by the HARNESS.** No assignment
>   anywhere, a name that is not registered, or a registered-but-empty set all resolve to
>   `None` — and `AgentWorkflowRunner._auto_mode_context` then returns `None`, so **no
>   evaluator is called at all** and the call falls through to the human gate. No bracket is
>   published either, since nothing was evaluated (same as auto mode being off, and unlike an
>   ESCALATE, which is a verdict).
>
>   This is deliberately **not** an evaluator's responsibility. "Nobody wrote rules for this
>   tool" is not a judgment call, and delegating it would make a safety property of auto mode
>   depend on every evaluator author remembering to check — while handing a model an empty
>   rulebook, which is the input most likely to come back a confident approve. It is why
>   `AutoApprovalContext.criteria_set` and `.criteria_set_name` are **required, non-optional
>   fields**: an `Optional` would invite exactly the defensive
>   `if ctx.criteria_set is None: escalate` branch an implementation could forget or get
>   wrong. `jev_evaluator` consequently has no such branch.
> - **A dangling name is logged, not silently cautious.** The gate warns (in-workflow, so
>   Temporal dedups across replays) naming the unregistered set, because an assignment to a
>   name nobody registered is a typo costing the operator automation they thought they had
>   configured. A tool with nothing assigned and no catch-all is a deliberate posture and
>   warns nothing.
> - **NOT surfaced on `AgentStatus`.** A tool's declared set is a decorator argument in code
>   and the runner has no registry to enumerate those, so any per-tool map published would be
>   silently incomplete for exactly the tools whose author already picked one. What a client
>   needs — whether the machine is deciding — rides on `approval_policy.auto_mode_enabled`.
>   Revisit if tools ever register with the runner up front.
>
> ### `jev_evaluator` — the builtin AI auto mode evaluator
>
> `harness/jev_approvals/`, exported as `agent.jev_evaluator(model="jev-latest",
> extra_state=None, activity_config=None)`. Returns an `AutoModeEvaluator`. Optional
> `jev` extra (`typesafe-sdk`), **worker-side only**.
>
> - **It holds no rules and no thresholds.** Those were constructor args (`policy=`,
>   `min_confidence=`, `escalate_if_irreversible_above=`) and are now the operator's, read off
>   `AutoApprovalContext`. That is what makes evaluators interchangeable: an LLM-backed or
>   hand-rolled one reads the same criteria, so swapping strands no configuration.
> - **And it holds no safety checks of its own.** It reads `ctx.criteria_set` directly with no
>   null branch, because the gate guarantees one exists. An evaluator's job is to judge the
>   call it was given; deciding *whether* a call may be judged at all is the harness's.
> - **Two questions, one request.** `verdict`: a Choice over exactly `approve`/`deny`/
>   `escalate`, so the three-way outcome is guaranteed by construction rather than parsed;
>   `irreversible`: a Noul. Both over the same state (the governing criteria set's rules as
>   `approval_criteria`, the criteria's `operating_context`, the tool's name + docstring +
>   `inherently_safe` hint, the exact `call_arguments`, and any `extra_state` as
>   `agent_state`).
> - **An unwritten rule is an absent key**, not an empty list: absence reads as "the operator
>   said nothing", which is the truth and pushes toward escalate, where `[]` invites the model
>   to read "nothing is forbidden".
> - **Raw judgments reach the stream.** `decide` puts the model id + request id, the
>   verdict's confidence and full distribution, the irreversibility judgment, token usage,
>   the thresholds applied, AND which criteria set at which version produced them into
>   `AutoApprovalDecision.details`, which the harness publishes on
>   `auto_approval_evaluation_ended`. The thresholds are what let a stored decision be re-read
>   under different ones; the set name and version are what keep it readable once the rules
>   behind it have been edited. It does NOT carry the request state — the decision is what is
>   being audited, not the prompt (the activity's input in history is the prompt).
> - **A TypeSafe failure is not caught** by the approver. It propagates to the runner, which
>   publishes `auto_approval_evaluation_error` and substitutes an escalate — so a failure
>   stays typed as a failure on the stream instead of being flattened into a verdict.
> - **Model judges, code decides.** `build_request` and `decide` are pure and public.
>   `decide` checks confidence *first* (an unsure deny is still an unsure decision), then the
>   label, then downgrades an APPROVE whose effect looks irreversible. Every non-confident
>   path ends at ESCALATE. Its thresholds are now parameters rather than defaults, because
>   they belong to the criteria set that governed the call — a refund and a lookup do not
>   share one confidence bar.
> - **Not reachable by the agent.** Dispatched as a bare `workflow.execute_activity` by name,
>   never through `run_tool` — so it is not a tool, the model cannot call it or influence what
>   is asked about its own call, and the gate does not recurse into itself.
> - **Registered unconditionally by `AgentHarnessPlugin`**, like the Code Mode activities, with
>   the extra checked per call: an unregistered activity name is a *retryable* Temporal error,
>   which would hang every gated call mid-approval instead of failing once.
> - Splits along the workflow boundary: `approver.py`/`models.py` are workflow-safe (plain
>   dicts, dispatch by name); only `activity.py` touches TypeSafe.

### Implementation notes (deviations from the plan as written)

- **The old `@agent.tool()` and `activity_as_tool` are both fully removed.** Every tool —
  `tools.py`, `forum_tools.py`, and the Monty dynamic-workflow agent
  (`agent/python/monty_dynamic_workflow/`) — now uses `@agent.activity_tool_defn` /
  `@agent.tool_defn`. Monty's `_run_activity_tool` collapsed from a hand-rolled
  `execute_activity` + `AgentToolContext` closure to a single `run_tool(call_id, tool,
  request)`; its `ALL_ACTIVITIES` is now built with the `tool_activity(...)` helper (see
  below) rather than reaching for a raw `.activity` attribute.
- **Worker registration goes through a `tool_activity(tool)` helper, not raw
  `tool.activity`.** The decorator returns the in-workflow *dispatcher*, typed as the
  developer's own `Callable[_P, Awaitable[_R]]` so the model-facing call signature is
  preserved for editors; its durable activity body lives on a `.activity` attribute that
  is invisible to type checkers. `tool_activity(tool)` reads that attribute without a
  per-call `# type: ignore`, and raises `TypeError` if handed something that isn't an
  `@agent.activity_tool_defn` tool (a `tool_defn` inline tool, which has no activity, or a
  plain function). Workers register `activities=[tool_activity(t) for t in (*DOCS_TOOLS,
  *FORUM_TOOLS)]`.
- **The `activity_tool_defn` dispatcher passes `result_type`.** Because it dispatches
  `execute_activity` by activity *name*, Temporal can't infer the return type, so a
  model/dataclass return would come back as a raw `dict`. The decorator resolves the
  tool's declared return type (via `get_type_hints`) and passes it as `result_type`, so
  the dispatcher honors its own `-> _R` signature. Monty depends on this (it uses
  `resp.flights`); the QA agent stringifies tool results, which is why this only surfaced
  once Monty migrated.
- **Close-while-pending is verified via a query, not the post-close stream.**
  `WorkflowStreamClient` events are served by the *live* workflow; once the workflow
  COMPLETES (which closing does), the trailing `tool_approval_resolved` /
  `message_handler_end` / `turn_end` are no longer replayable. The test therefore asserts the durable outcome
  with a `last_reply` query after completion. The approve/deny/concurrent tests read the
  live stream normally (their workflow stays running).
- The E2E tests (`harness/test_tool_approvals.py`) run against a real workflow + activity
  on the time-skipping server and cover the full policy matrix (safe-auto-approve under
  `allow_inherently_safe`; safe still gated under `always_require_human_approval`; allow-list;
  `dangerously_skip_all`; `AgentConfig` override; auto mode; `remember=True` cascade;
  plus the original gate lifecycle: approve/deny/status/idempotent/concurrent/close/inline).
  Fast unit tests in `harness/test_runner_builder.py` cover policy resolution
  (config-over-default), the runner requiring a default policy, `set_approval_policy`
  re-resolving pending calls, and the auto mode evaluator being consulted only as the last
  layer.

---

> **NOTE (superseded framing).** §1–§12 are the ORIGINAL plan. The mechanism they describe
> for *how a gated call waits* (unbounded in-workflow `wait_condition`, concurrent
> independent waits, denied → error result, never in the activity) is still exactly right
> and worth reading. What changed is *which calls get gated*: not a per-tool
> `approval_required` flag, but the agent's `ToolApprovalPolicy` (see the boxed section at
> the top). Wherever the text below says `approval_required` / `_await_tool_approval`, read
> `inherently_safe` + the policy / `_apply_approval_policy`.

## 1. Goal

When the model requests a tool, the call **pauses in-workflow for a human approval decision
before it executes** *unless the agent's `ToolApprovalPolicy` auto-approves it*. While
paused, many such calls can wait **concurrently**; the instant any one is approved it
dispatches, regardless of the order requests arrived. A denied call feeds an error result
back to the model so the agent loop continues. The wait is unbounded (it must be able to
sit for hours/days), so it runs as a workflow `wait_condition` — never inside the tool's
activity, whose `start_to_close_timeout` must cover only real execution.

```python
# The tool only asserts inherent safety (default False); the agent's policy decides gating.
@agent.activity_tool_defn(
    inherently_safe=False,
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=2)),
)
async def delete_workflow(store_display_name: Injected[str], workflow_id: str) -> str:
    ...

# Builder must set a default policy; a caller can override it via AgentConfig.approval_policy.
runner = (
    AgentWorkflowRunner.builder(config=config)
    .set_stream(WorkflowStream())
    .set_approval_policy_default(ToolApprovalPolicy.allow_inherently_safe())
    .build()
)
```

---

## 2. The decorator redesign (the foundation this feature sits on)

Today a tool is authored as a stack of three things, with one wrapper that branches at
runtime on *where* it is executing:

```python
@activity.defn          # google_genai_plugin / Temporal
@agent.tool()           # harness — ONE wrapper, branches on in_activity()/in_workflow()
async def get_page_outline(store_display_name: Injected[str], page_url: str) -> str: ...

DOCS_TOOLS = [activity_as_tool(get_page_outline, activity_config=...), ...]  # plugin, separate site
```

`agent_workflow.tool()`'s `wrapper` (`harness/agent_workflow.py:1093-1181`) carries an
`if activity.in_activity(): … if not workflow.in_workflow(): …` split, and the
workflow-side dispatch (bind model args, fill injected, append `AgentToolContext`,
`execute_activity`) lives in a *different* module (`google_genai_plugin/workflow.py:activity_as_tool`).
Adding an approval gate to this shape forces either approval logic into the generic
`run_tool`, or propagating an `__agent_tool_requires_approval__` attribute through
`activity_as_tool` — both smelly.

**New shape: two sibling decorators, each a single code path, each owning its own concern.**

| Decorator | Tool kind | Returns | Worker registration |
|---|---|---|---|
| `@agent.activity_tool_defn(...)` | durable, activity-backed | a callable tool object whose `__call__` runs **in-workflow** (dispatcher); its durable activity body lives on a type-checker-invisible `.activity` attribute, read via `tool_activity(tool)` | `activities=[tool_activity(t) for t in TOOLS]` |
| `@agent.tool_defn(...)` | runs inline in the workflow | a callable tool object whose `__call__` runs **in-workflow** | none |

- The **activity** decorator auto-applies `@activity.defn` to an internally-generated
  activity body and exposes it as `.activity`. Authors no longer stack `@activity.defn`
  themselves — `@agent.activity_tool_defn` *replaces* it.
- The dispatcher (`__call__`) subsumes everything `activity_as_tool` does today
  (model-arg binding, injected-param filling, `AgentToolContext` trailing arg,
  `execute_activity`) — now co-located with the tool definition, and so is
  `activity_config`, which moves off the separate `DOCS_TOOLS` call site onto the
  decorator.
- Both decorators share **one** internal approval-gate helper. `approval_required` is a
  closure variable inside each decorator — never an attribute that has to be propagated.
- `run_tool` stays exactly as it is: a thin per-call funnel that parks
  `_CURRENT_TOOL_ID` / `_CURRENT_RUNNER` / injections and `await`s the callable. The gate
  reads that same ambient state. **No approval logic leaks into the runner.**

### What each decorator produces

`@agent.activity_tool_defn` builds two functions over the user fn:

1. **Dispatcher** (the returned object, runs in-workflow), single path:
   ```
   model_input = bind(model_sig, *args, **kwargs)            # visible args only
   if approval_required:
       await _await_tool_approval(name, model_input)         # ← in-workflow gate (§4)
   activity_args = [<model+injected in activity param order>, AgentToolContext.for_current_tool_id()]
   return await workflow.execute_activity(activity_name, args=activity_args, **config)
   ```
   Carries model-facing `__name__` / `__doc__` / `__signature__` / `__annotations__`
   (self + `Injected[...]` stripped) so the schema adapter introspects it directly.
   Does **not** set `__wrapped__` (see §6). Exposes `.activity`.

2. **`.activity` body** (runs in the activity worker), single path — today's
   `in_activity()` branch, now unconditional:
   ```
   *user_args, tool_ctx = args                               # peel trailing AgentToolContext
   async with AgentWorkflowRunner.publisher_from_activity(tool_ctx.stream_context) as pub:
       pub.publish(ToolStartEvent(...))
       try: result = await user_fn(*user_args)
       except Exception as e: pub.publish(ToolErrorEvent(...)); raise
       pub.publish(ToolEndEvent(...)); return result
   ```
   Its runtime `__signature__` = user signature **+ trailing `tool_ctx: AgentToolContext = None`**
   (current `_signature_with_tool_ctx`), so `@activity.defn` serializes the context as the
   last arg. Activity name defaults to `user_fn.__name__`.

`@agent.tool_defn` builds one in-workflow callable, single path — today's
`in_workflow()` branch: fill injected from ambient injections, `[gate]`, publish
`tool_start`, run `user_fn` inline, publish `tool_end`/`tool_error`.

---

## 3. Settled decisions

1. **Denial path → raise.** The gate raises `ToolApprovalDenied`; `_run_one_tool`'s
   existing `except Exception` (`agent/python/workflow.py:453`) already converts it into a
   `{is_error: true, result: "<denial reason>"}` function_result fed back to the model. No
   new wiring on the dispatch side.
2. **Scope → static `approval_required: bool` first.** The gate helper is shaped so it can
   later accept a predicate over the model args (in-workflow conditional approval) without
   an API break. Not built now.
3. **On close/cancel while pending → treat as denied.** The gate's `wait_condition` also
   wakes on `runner._closed`; an unresolved approval at close resolves to *denied* (reason
   "agent closed before approval"), raising `ToolApprovalDenied`. The workflow winds down
   cleanly instead of hanging.

> **Ordering note:** Temporal's `WorkflowStream` guarantees event ordering across
> workflow- and activity-published events, so `tool_approval_requested` (workflow) ↔
> `tool_requested` (streaming activity) need no special handling — causal order is the
> observed order.

---

## 4. The approval gate (shared internal helper)

New in `harness/agent_workflow.py`. Reads the ambient state `run_tool` parks; touches only
the runner and workflow state.

```python
class ToolApprovalDenied(Exception):
    """Raised in-workflow when a gated tool call is denied (or the agent closes while
    it is pending). Caught by the agent loop's per-call error handling and surfaced to
    the model as an is_error function_result."""
    def __init__(self, tool_name: str, reason: str | None) -> None: ...

async def _await_tool_approval(tool_name: str, tool_input: dict[str, Any]) -> None:
    runner = _CURRENT_RUNNER.get()
    tool_id = _current_tool_id()                      # raises outside a run_tool call
    ctx = runner.current_stream_context               # turn_id + turn_number
    runner._status.register_pending_approval(tool_id, tool_name, tool_input, ctx.turn_number)
    runner._pub(ctx.turn_id, ctx.turn_number,
                ToolApprovalRequested(tool_id=tool_id, tool_name=tool_name, tool_input=tool_input))

    await workflow.wait_condition(
        lambda: runner._status.is_approval_resolved(tool_id) or runner._closed
    )

    decision = runner._status.finalize_approval(tool_id, closed=runner._closed)  # → (approved, reason)
    runner._pub(ctx.turn_id, ctx.turn_number,
                ToolApprovalResolved(tool_id=tool_id, tool_name=tool_name,
                                     approved=decision.approved, reason=decision.reason))
    if not decision.approved:
        raise ToolApprovalDenied(tool_name, decision.reason)
```

**Concurrency (requirement #4) is satisfied by construction.** `_handle_user_turn` runs the
turn's tool calls under `asyncio.gather` (`agent/python/workflow.py:231`), so each
`run_tool` → dispatcher → `_await_tool_approval` runs as its own asyncio task with its own
copied `contextvars.Context`, each awaiting a `wait_condition` keyed on **its own**
`tool_id`. Temporal re-evaluates all registered conditions after every update, so whichever
`tool_id` is approved first unblocks and dispatches immediately, independent of request
order.

> The per-call *execution* unblocks independently, but the *turn* still doesn't return
> results to the model until the whole `gather` batch completes — that's the existing
> contract (the model gets all function_results together) and approvals don't change it.

**Gate placement vs. ambient ContextVars.** The gate runs at the very top of the
dispatcher, *before* the model args are turned into activity args, and the existing
`_CURRENT_*` ContextVars are parked by `run_tool` for the whole call — same as today's
`await tool_callable(...)` window, which already spans the (long) `execute_activity` await.
The gate needs only `_CURRENT_RUNNER` + `_CURRENT_TOOL_ID`, both read synchronously at the
start; nothing about the indefinite wait defeats the `Token.reset()` in `run_tool`'s
`finally`, because set and reset stay in the same task/frame.

---

## 5. Component-by-component changes

### 5a. `harness/agent_protocol/events.py` — two new events

Add enum members to `AgentEventType` (document them inline, per the file's convention; the
existing `TOOL_REQUESTED` doc already anticipates this gap):

```python
TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
"""A gated tool call is awaiting a human approval decision. Published after
TOOL_REQUESTED and before TOOL_START; carries the same tool_id. A UI renders an
approve/deny affordance off this event. See :class:`ToolApprovalRequested`."""

TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
"""A pending tool approval was resolved (approved or denied, including auto-denied
because the agent closed). On approval, TOOL_START follows; on denial, the call ends
here with no execution. See :class:`ToolApprovalResolved`."""
```

Two payloads (both extend `ToolEvent`, inheriting `tool_id` + `tool_name`):

```python
class ToolApprovalRequested(ToolEvent[Literal[AgentEventType.TOOL_APPROVAL_REQUESTED]]):
    type: Literal[AgentEventType.TOOL_APPROVAL_REQUESTED] = AgentEventType.TOOL_APPROVAL_REQUESTED
    tool_input: dict[str, Any] = Field(default_factory=dict)

class ToolApprovalResolved(ToolEvent[Literal[AgentEventType.TOOL_APPROVAL_RESOLVED]]):
    type: Literal[AgentEventType.TOOL_APPROVAL_RESOLVED] = AgentEventType.TOOL_APPROVAL_RESOLVED
    approved: bool
    reason: str | None = None
```

Add both to the `AgentStreamItem` union (`events.py:311`) and to
`harness/agent_protocol/__init__.py` exports.

### 5b. `harness/agent_protocol/agent_interface.py` — update contract + status

```python
TOOL_APPROVAL_UPDATE = "tool_approval"     # protocol constant, next to USER_INPUT_UPDATE

@dataclass
class ToolApprovalDecision:                 # update payload (client → workflow)
    tool_id: str
    approved: bool
    reason: str | None = None

@dataclass
class ToolApprovalResult:                   # update return
    tool_id: str
    accepted: bool                          # True once recorded

@dataclass
class PendingApproval:                       # surfaced in AgentStatus
    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    turn_number: int
```

Extend `AgentStatus` (`agent_interface.py:139`):

```python
pending_approvals: list[PendingApproval] = field(default_factory=list)
```

> **Why status, not just the event:** `ToolApprovalRequested` may be published before a
> given client is attached to the stream. A client must be able to discover outstanding
> approvals via the `agent_status` query and reconcile — so this field is load-bearing,
> not cosmetic.

### 5c. `_WorkflowStatus` — the pending-approval registry (`agent_workflow.py:507`)

```python
class _ApprovalStatus(StrEnum):
    PENDING = "pending"; APPROVED = "approved"; DENIED = "denied"

@dataclass
class _ApprovalEntry:
    tool_id: str; tool_name: str; tool_input: dict[str, Any]
    turn_number: int; status: _ApprovalStatus; reason: str | None = None
```

In `_WorkflowStatus`: `self._approvals: dict[str, _ApprovalEntry] = {}` plus methods —
`register_pending_approval(...)`, `resolve_approval(tool_id, approved, reason)`,
`is_approval_resolved(tool_id) -> bool`, `finalize_approval(tool_id, *, closed) -> Decision`
(if still PENDING and `closed`, mark DENIED reason "agent closed before approval"),
`approval_entry(tool_id) -> _ApprovalEntry | None`, and `pending_approvals() ->
list[PendingApproval]`. Resolved entries are **retained** (status flips, not deleted) so the
validator can distinguish "unknown id" from "already resolved" — see §5d. Bounded by the
number of gated calls in the workflow's life; prune per-turn later if it ever matters.

`to_agent_status` includes `pending_approvals=self.pending_approvals()`.

### 5d. Runner — register the update handler (`agent_workflow.py:667`)

> **Design invariant — keep `tool_approval` separate from `user_input`, and never advertise it.**
> These two updates are intentionally distinct and must not be merged:
> - **`user_input`** is the agent's *front door*: it propagates a message into the agent
>   author's own turn code and is published in the `accepted_message_types` discovery
>   contract, so any caller — including a **parent (non-human) agent** — can drive the
>   agent through it.
> - **`tool_approval`** is handled *entirely by the harness* (the author never sees it;
>   `_await_tool_approval` resolves the gate in-process). It is the human-in-the-loop
>   guardrail.
>
> It is therefore **deliberately excluded from `accepted_message_types`**. The whole point
> of approvals is to stop the model acting unilaterally on a gated tool — so an automating
> parent agent, which speaks only the discovered front-door contract, must *not* be able to
> approve its child's tool calls (that would be the AI rubber-stamping its own dangerous
> calls). Approvals come from a human/operator surface out-of-band (the UI's approve/deny),
> not the agent-to-agent channel. This is recorded at the code sites too
> (`_handle_tool_approval`, `_handle_accepted_message_types`).

In `__init__`, alongside the existing handlers:

```python
workflow.set_update_handler(
    TOOL_APPROVAL_UPDATE, self._handle_tool_approval, validator=self._validate_tool_approval
)
```

```python
def _validate_tool_approval(self, decision: ToolApprovalDecision) -> None:
    entry = self._status.approval_entry(decision.tool_id)
    if entry is None:
        raise ApplicationError("no pending approval for tool_id",
                               type="UnknownToolApproval", non_retryable=True)
    if entry.status is not _ApprovalStatus.PENDING:        # idempotency / double-submit (requirement #2)
        raise ApplicationError("tool approval already resolved",
                               type="ToolApprovalAlreadyResolved", non_retryable=True)

async def _handle_tool_approval(self, decision: ToolApprovalDecision) -> ToolApprovalResult:
    self._status.resolve_approval(decision.tool_id, approved=decision.approved, reason=decision.reason)
    return ToolApprovalResult(tool_id=decision.tool_id, accepted=True)
```

The handler only mutates state; the gate's `wait_condition` observes it on the next
workflow task and unblocks.

### 5e. The two decorators + the activity body (`agent_workflow.py`)

Replace `tool()` (and its single branching `wrapper`) with `activity_tool_defn(...)` and
`tool_defn(...)`, plus the shared `_await_tool_approval`. Reuse the existing
`_injected_param_names`, `_signature_with_tool_ctx`, `_tool_input`, and the model-facing
signature/annotation stripping that currently lives in `activity_as_tool`
(`google_genai_plugin/workflow.py:111-133`) — that logic moves here.

Signatures:

```python
def activity_tool_defn(
    *, approval_required: bool = False,
    activity_config: ActivityConfig | None = None,
    name: str | None = None,                       # optional activity-name override
) -> Callable[[Callable[_P, Awaitable[_R]]], _ActivityTool[_P, _R]]: ...

def tool_defn(
    *, approval_required: bool = False,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]: ...
```

The activity decorator returns a callable that is still typed as the developer's own
`Callable[_P, Awaitable[_R]]` for editor checking, with `.activity` attached. (Concretely:
build a closure `dispatch`, set its model-facing dunders, set `dispatch.activity =
activity.defn(name=...)(activity_body)`, and `dispatch.__agent_activity_tool__ = True`.)
Because `.activity` is invisible to type checkers, worker registration goes through a
module-level helper rather than touching the attribute directly:

```python
def tool_activity(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Return the registrable Temporal activity for an activity_tool_defn tool.
    Raises TypeError if `tool` wasn't produced by activity_tool_defn (e.g. a tool_defn
    inline tool, which has no activity, or a plain function)."""
    ...  # reads tool.activity, guarded by tool.__agent_activity_tool__
```

### 5f. `run_tool` — unchanged

Still parks `_CURRENT_TOOL_ID` / `_CURRENT_RUNNER` / `_CURRENT_TOOL_INJECTIONS` and `await`s
the callable. The dispatcher it awaits now happens to run a gate first. Nothing to change
here, and `agent/python/workflow.py:_run_one_tool` keeps calling
`runner.run_tool(call.id, tool, injections=..., **call.arguments)` verbatim — it passes the
decorated object as `tool` (which is what `callables_by_name` will now hold; see §7).

---

## 6. `google_genai_plugin` changes

- **`activity_as_tool` (`workflow.py`) is removed.** Its dispatch/stripping logic moved into
  `@agent.activity_tool_defn`. (Optionally keep a thin deprecated shim for one release; this
  is a prototype, so deleting it and migrating call sites in the same change is cleaner.)
- **`function_param` (`_interactions_workflow.py`) stays** — it is the only Gemini-specific
  piece (it emits an Interactions-API `ToolParam`). It introspects the decorated object's
  model-facing `__signature__` exactly as it introspects the `activity_as_tool` wrapper
  today, so it needs **no behavioral change**. The decorated object deliberately does **not**
  set `__wrapped__`, so `function_param`'s `getattr(fn, "__wrapped__", fn)` falls through to
  `fn` and reads the clean (injected/self-stripped) signature. The `__wrapped__` branch in
  `function_param` (added to strip `tool_ctx` from a raw `@agent.tool` activity) becomes
  vestigial and can be simplified away, since no bare `@agent.tool` activity is ever passed
  to it anymore.
- **Constraint preserved:** tool modules must keep avoiding `from __future__ import
  annotations` so `FunctionDeclaration.from_callable_with_api_option` →
  `get_type_hints` resolves the (concrete) annotations the decorator copies onto the
  dispatcher. (See `agent/python/tools.py` header and the existing memory on this trap.)

---

## 7. Migration

| File | Change |
|---|---|
| `agent/python/tools.py` | Drop `@activity.defn` + `@agent.tool()` stacks → `@agent.activity_tool_defn(activity_config=…)`. Move each tool's per-tool timeout from the old `DOCS_TOOLS = [activity_as_tool(fn, activity_config=…)]` list onto its decorator. `DOCS_TOOLS` becomes just `[get_page_outline, read_section, …]` (the decorated objects). Drop the `activity_as_tool` import. |
| `agent/python/forum_tools.py` | Same migration; `FORUM_TOOLS = [read_forum_thread, get_forum_accepted_answer]`. |
| `agent/python/worker.py` | Register `activities=[tool_activity(t) for t in (*DOCS_TOOLS, *FORUM_TOOLS)]` (imports `tool_activity` from `harness.agent`) instead of importing the raw functions (`worker.py:91-99`). |
| `agent/python/workflow.py` | `callables_by_name = {fn.__name__: fn for fn in setup.tool_functions}` already keys on `__name__` (preserved on the decorated object) and `function_param(fn)` still works — likely **no change** beyond importing nothing new. `_run_one_tool` unchanged. |
| `harness/agent.py` | Export `activity_tool_defn`, `tool_defn`, `tool_activity` (and keep `Injected`, `AgentToolContext`, `defn`); update `__all__` and the module docstring's usage examples. Remove `tool` once all call sites migrate. |
| `harness/agent_protocol/__init__.py` | Export the new events, `TOOL_APPROVAL_UPDATE`, `ToolApprovalDecision`, `ToolApprovalResult`, `PendingApproval`. |
| `harness/agent_client.py` | Add a client method to send the approval update + expose pending approvals (see §8). |
| `google_genai_plugin/__init__.py`, `workflow.py` | Remove `activity_as_tool` export/impl; keep `function_param`. Update `test_activity_as_tool.py` to target the new decorator (it pins the injected/self hiding contract — keep that assertion, retarget the subject). |
| `harness/test_agent_tool.py` | Retarget to the split decorators (it currently simulates `activity_as_tool` appending an `AgentToolContext`). Add approval-path cases (see §9). |
| `agent/python/monty_dynamic_workflow/` | **Migrated** (follow-up pass). `activities.py` tools → `@agent.activity_tool_defn(name=…)`; `ALL_ACTIVITIES` → `[tool_activity(t) for t in …]`; `workflow.py:_run_activity_tool` collapsed to `run_tool(call_id, tool, request)` (the hand-rolled `AgentToolContext`/`execute_activity` closure is gone). |

---

## 8. Client API additions (`harness/agent_client.py`)

- `async def approve_tool(self, tool_id, *, approved, reason=None) -> ToolApprovalResult:`
  → `handle.execute_update(TOOL_APPROVAL_UPDATE, ToolApprovalDecision(...), result_type=ToolApprovalResult)`.
  The non-retryable validator means a double-submit (already-resolved) or bogus `tool_id`
  surfaces as a clean update failure the caller can show.
- `get_status()` already returns `AgentStatus`; it now carries `pending_approvals`, so a
  client that (re)attaches can render outstanding approvals immediately without replaying
  the stream.

---

## 9. Event lifecycle (gated call)

```
model emits function_call{name, args, id=X}
  │
streaming activity ── publish ToolRequested(id=X)                        (existing)
  │
_run_one_tool ── runner.run_tool(X, tool, injections={store_display_name}, **args)
  │                  parks _CURRENT_TOOL_ID=X, _CURRENT_RUNNER, injections
  ▼
dispatcher (in-workflow):
  approval_required → _await_tool_approval(name, model_input):
        register pending ; publish ToolApprovalRequested(id=X)            ← NEW (status query also reflects it)
        await wait_condition( resolved(X) or _closed )                    ← unbounded, concurrent
        ── client sends tool_approval update {tool_id=X, approved} ──
        publish ToolApprovalResolved(id=X, approved)                      ← NEW
        if denied: raise ToolApprovalDenied  ─────────────────────────────┐
  approved →                                                              │
    execute_activity(name, args=[…injected…, AgentToolContext{X}])        │
       activity body: publish ToolStartEvent(id=X)                        │ (existing, from inside the activity)
                      run user_fn ; publish ToolEndEvent(id=X)            │
  return result ──────────────────────────────────────────────────────── │
                                                                          ▼
_run_one_tool except → function_result{call_id=X, is_error:true, result:"<reason>"}
  → fed back to the model ; turn continues
```

---

## 10. Testing plan

- **Decorator unit tests** (retarget `test_agent_tool.py` / `test_activity_as_tool.py`):
  model schema hides `Injected[...]` + `self`; `.activity` is a registrable
  `@activity.defn`; activity body peels the trailing `AgentToolContext` and publishes
  start/end/error; in-workflow `tool_defn` publishes in-process.
- **Approval gate (workflow tests, time-skipping env):**
  - approved → tool executes; events ordered requested → approval_requested →
    approval_resolved(approved) → start → end.
  - denied → no `execute_activity`; events end at approval_resolved(approved=False);
    model receives an is_error result.
  - **concurrency:** request A then B (both gated); approve B first → B dispatches before A;
    then approve A. Assert B's `tool_start` precedes A's.
  - **idempotency:** second `tool_approval` update for the same `tool_id` fails the
    validator (`ToolApprovalAlreadyResolved`); unknown `tool_id` fails (`UnknownToolApproval`).
  - **status:** `agent_status` lists the pending approval while waiting and drops it after
    resolution.
  - **close while pending:** `close` signal → gate auto-denies → workflow winds down.
- **End-to-end:** mark one `DOCS_TOOLS`/`FORUM_TOOLS` member `approval_required=True`, drive
  a turn, approve via the client, confirm the reply reflects the tool output.

---

## 11. Future (explicitly out of scope now)

- **Conditional approval — DONE.** The auto mode evaluator judges a call against the
  operator's own ruleset (it receives `tool_input`, so "only gate `delete` of protected
  ids" is expressible), is async, and answers approve / deny / escalate.
  `agent.jev_evaluator` is the builtin AI implementation of it. Still future: predicates
  attached per-tool, and serializable conditional rules.
- **Deny-list / `remember` on denial.** `remember=True` only allow-lists on *approval*
  today; a "never allow this tool" deny-list is not built.
- **Approval metadata:** richer `ToolApprovalRequested` (risk level, human-readable summary)
  and approver identity on `ToolApprovalDecision`. Partly covered by the fallback's `reason`
  on `ToolApprovalResolved` (which is where a Jev verdict and its numbers land), but there is
  still no structured *who/what-risk* on the request itself.
- **Timeout-to-deny / escalation policy** for approvals that sit too long.

---

## 12. Implementation order

1. Protocol: events (§5a), update contract + `AgentStatus` field (§5b), exports.
2. `_WorkflowStatus` approval registry (§5c).
3. Runner: update handler + validator (§5d); `_await_tool_approval` + `ToolApprovalDenied` (§4).
4. Decorators: `activity_tool_defn` + `tool_defn` + shared activity-body/dispatcher builders
   (§5e); confirm `run_tool` untouched (§5f).
5. Plugin: remove `activity_as_tool`, keep/simplify `function_param` (§6).
6. Migrate `tools.py`, `forum_tools.py`, `worker.py`, `agent.py` exports (§7).
7. Client: `approve_tool` + status surfacing (§8).
8. Tests (§10).
9. Mark one real tool `approval_required=True` and verify end-to-end.
```
