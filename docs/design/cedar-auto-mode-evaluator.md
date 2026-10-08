# Cedar policy as an auto mode evaluator, with script analysis for Code Mode

**Status:** implemented on `worktree-cedar-auto-mode-design`. Section 9 lists what was built
and where; section 11 records how the open questions were settled. Where implementation changed a
detail, the sections below describe what was built.

**Read first:** `examples/auto_mode/README.md` for auto mode's three parts (the policy switch,
the criteria, the evaluator), and the `tool-call verification` section of
[02strich/temporal-untrusted-workers @ `nexus-command-verifier`](https://github.com/02strich/temporal-untrusted-workers/blob/nexus-command-verifier/README.md#tool-call-verification)
for the `VerifyToolCalls` Nexus contract and its Cedar implementation (`verifiers/cedar`).

---

## 1. Goal

Let an agent's gated tool calls be judged by a Cedar policy served over Nexus, the same
`VerifyToolCalls` operation the untrusted-workers proxy calls, instead of (or alongside) Jev.

Constraints:

1. **No evaluator per agent.** The harness ships one evaluator, `cedar_evaluator(...)`. An agent
   configures it (endpoint, service); it never subclasses or rewrites it. Which tools are allowed,
   and with which arguments, lives in the Cedar policy files.
2. **Scripts are judged too.** A Code Mode tool call carries a whole script. Cedar can't read a
   script, so the evaluator sends Cedar *facts about* the script instead: what code analysis
   finds, and optionally what Jev classifies. This works for any `code_mode_tool`, on any agent.
3. **Script analysis is opt-in.** The harness tells an evaluator that a call is a Code Mode
   script and which host functions it can reach. It does not analyze the script itself. An
   evaluator that wants facts calls `analyze_script(script, host_functions)` explicitly; Jev's
   evaluator, for one, has no use for them.
4. **Fail toward the human.** Every failure (Nexus down, malformed answer, unparseable script,
   classifier error) ends at the human gate, never at an approval.

## 2. What exists today

### 2.1 The hook

`AgentWorkflowRunner(auto_mode_evaluator=...)` takes any
`async (AutoApprovalContext) -> AutoApprovalDecision` (`AutoModeEvaluator`,
`harness/agent_workflow.py`). The harness already provides everything around it:

- it calls the evaluator only when the live `ToolApprovalPolicy` has auto mode on, the call was
  not allow-listed, and a non-empty criteria set covers the tool (`_auto_mode_context`);
- it brackets every evaluation with `auto_approval_evaluation_started` and one of
  `_ended` / `_superseded` / `_error`;
- it cancels the evaluator when a human answers first;
- an evaluator that raises escalates to the human gate.

`AutoApprovalContext` carries `tool_name`, `tool_input` (model-facing arguments),
`inherently_safe`, `tool_description` and the resolved criteria. It does **not** say whether the
call is a Code Mode script, so a generic evaluator can't tell `run_travel_code` from any other
tool except by its name or by parsing its generated docstring.

### 2.2 The Cedar service

`VerifyToolCalls` (proto `temporal_untrusted_workers.toolpolicy.v1`) takes:

```json
{
  "caller": {"namespace": "...", "subject": "...", "taskQueue": "...", "workflowId": "..."},
  "toolCalls": [{"name": "<prefix>/<tool>", "parameters": [<google.protobuf.Any>, ...]}]
}
```

and answers `{"allowed": bool, "reason": string}`. The Cedar verifier turns each tool call into
one authorization request: `principal = Worker::"<subject>"`,
`action = Action::"<name>"`, `resource = TaskQueue::"<taskQueue>"`, and
`context.parameters.argN = {type, data}`. Things to note for a Python caller:

- **Input encoding is ignored.** The handler parses the payload bytes as JSON
  (`verifiers/cedar/src/main.rs`), so a dict sent as `json/plain` works.
- **The response is `json/protobuf` with `messageType`
  `temporal_untrusted_workers.toolpolicy.v1.VerifyToolCallsResponse`.** The Python SDK's
  `JSONProtoPayloadConverter` looks that name up in the protobuf symbol database and raises
  `Unknown Protobuf type` if it isn't registered. The decode runs while the workflow activation
  is applied (`_apply_resolve_nexus_operation`), not inside the evaluator's coroutine, so an
  unregistered type **fails the workflow task repeatedly** instead of reaching the harness's
  escalate-on-error path. Registering the generated type is required (section 4.4).
- **Two kinds of "no".** An explicit `forbid` produces
  `tool X denied by Cedar policy <id>`; no matching `permit` produces
  `tool X not permitted by any Cedar policy`. The proto doesn't distinguish them (section 6).
- **Cedar drops floats and nulls**, arrays become sets, and protobuf-JSON 64-bit integers
  arrive as strings.
- **Cancellation is not implemented** by the handler (`NOT_IMPLEMENTED`). The operation is
  synchronous, so this only matters when the evaluator is superseded.

## 3. Shape of the design

```
gated call ──▶ _apply_approval_policy ──▶ AutoApprovalContext(code_mode=CodeModeCall | None)
                                                   │
                                          cedar_evaluator(ctx)
                                                   │
                     ┌─────────────────────────────┴──────────────────────────────┐
              ctx.code_mode is None                                      ctx.code_mode set
                     │                                                            │
     arg0 = Value(ctx.tool_input)                 facts = analyze_script(script, host_functions)
                     │                            classification = await classifier(ctx, facts)   (optional)
                     │                            arg0 = Value({facts, classification})
                     └─────────────────────────────┬──────────────────────────────┘
                                                   │
                       workflow Nexus call: tool-verifier / tool-policy / VerifyToolCalls
                                                   │
                         allowed → APPROVE · explicit forbid → DENY · no permit → ESCALATE
```

The host calls a script makes are **still gated one by one**, as today: each enters the gate
under its own tool name with concrete arguments, and `cedar_evaluator` judges it as an ordinary
call. The script-level check is for what no single call shows: which capabilities a script
combines, whether it fans out, how much it does.

## 4. Components

### 4.1 Harness: `AutoApprovalContext.code_mode`

```python
@dataclass(frozen=True)
class CodeModeCall:
    """The call is a Code Mode script: its source, and the host functions it can reach."""

    script: str
    host_functions: frozenset[str]
```

```python
@dataclass
class AutoApprovalContext:
    ...
    code_mode: CodeModeCall | None = field(default=None, kw_only=True)
```

Host-function names are the harness tool names (`code_mode_tool` keys its tools by
`__name__`), so they line up with the names the same tools carry when their host calls reach the
gate.

Plumbing, smallest change:

- `code_mode_tool` builds its tool through an internal `_tool_defn(...,
  code_mode_host_functions=frozenset(tools_by_name))`. Public `tool_defn` delegates to it, so its
  signature doesn't change.
- The wrapper builds `CodeModeCall(script=model_input["script"], host_functions=...)` and passes
  it to `_apply_approval_policy(..., code_mode=...)`, which hands it to `_auto_mode_context`.
- `code_mode_tool` also stamps `__code_mode_host_functions__` on the tool it returns, beside the
  existing `__code_mode_stubs__`, and `code_mode_host_functions(tool)` reads it back, so tooling
  outside the gate (`cedar_schema`, section 4.5) can find the same set.
- Nothing is analyzed here. The field is plain data; an evaluator decides what to do with it.

`code_mode_tool` defaults to `inherently_safe=True`, so the script call only reaches the
evaluator under a policy that does not allow-list inherently-safe tools (the `auto_mode`
posture), or when the agent passes `inherently_safe=False`. That stays the agent's choice.

### 4.2 `analyze_script(script, host_functions) -> ScriptFacts`

New module `harness/code_mode/analysis.py`. Pure and deterministic (stdlib `ast`), so it is safe
in workflow code and testable without a worker.

```python
class HostFunctionUse(BaseModel, frozen=True):
    call_sites: int    # direct calls: `book_flight(...)`
    in_loop: bool      # any call site inside for / while / a comprehension
    concurrent: bool   # any call site inside asyncio.gather(...) or create_task(...)
    indirect: bool     # the name is used other than as a direct call, e.g. `f = book_flight`


class ScriptFacts(BaseModel, frozen=True):
    uses: dict[str, HostFunctionUse]   # only the host functions the script references
    has_while_loop: bool
    dynamic_code: bool                 # the script names eval, exec or compile
```

The facts over-approximate: every one errs toward "the script may do more".

- Every `Name` load matching a host function counts as a reference, not only call sites. An
  alias still shows up in `uses`, with `indirect=True`.
- Loop and concurrency context follows calls into the script's own functions, to a fixed point:
  a host call inside a helper that is called in a loop is `in_loop`. Code the analysis can't
  follow (a helper passed around by reference, a lambda, a method, a recursive function) counts
  as both `in_loop` and `concurrent`.
- **`eval` reaches host functions.** Probing Monty showed that `eval("book_flight('a')")`
  dispatches a real host call, with the name only inside a string. So a script that names
  `eval`, `exec` or `compile` reports `dynamic_code`, and every host function is listed in
  `uses` as in a loop, concurrent and indirect.
- `SyntaxError` propagates. An evaluator that lets it escape gets the harness's
  escalate-on-error behavior, which is the right answer for a script that can't run anyway.
- Literal argument values are left out. Each host call is judged with its concrete arguments
  when it runs, so copying literals into the script-level facts would duplicate that check.

### 4.3 Jev classification of a script (optional)

`JevApprovalRequest` already takes any set of questions in one request. `JevApprovalAnswer` and
`jev_tool_approval` hard-code `verdict` and `irreversible`, so classification needs a sibling
activity:

```python
JEV_CLASSIFY_ACTIVITY = "jev_classify"

class JevClassificationAnswer(BaseModel):
    model: str
    request_id: str
    answers: dict[str, float]   # question id -> probability "yes"
    input_tokens: int | None = None
    output_tokens: int | None = None
```

`jev_classify` takes the existing `JevApprovalRequest`. `AgentHarnessPlugin` registers it beside
`jev_tool_approval`.

The workflow side is a classifier factory with configurable questions:

```python
class ScriptFeature(BaseModel, frozen=True):
    key: str        # attribute name in Cedar: context.parameters.arg0.data.classification.<key>
    question: str   # a yes/no question about the script
    yes: str        # what "yes" means, sent as the question's criteria
    no: str         # what "no" means


# Keys: data_flows_between_tools, unbounded_iteration, mixes_reads_and_writes,
# names_unmentioned_entities. The questions are in jev_approvals/classifier.py.
DEFAULT_SCRIPT_FEATURES: tuple[ScriptFeature, ...]

# In code_mode/analysis.py, so it belongs to no one evaluator.
ScriptClassifier = Callable[[AutoApprovalContext, ScriptFacts], Awaitable[Mapping[str, int]]]

def jev_script_classifier(
    features: Sequence[ScriptFeature] = DEFAULT_SCRIPT_FEATURES,
    *, model: str | None = DEFAULT_JEV_MODEL, activity_config: ActivityConfig | None = None,
) -> ScriptClassifier: ...
```

- One `jev_classify` call per script asks every feature at once. Jev's state includes the script,
  the host-function descriptions and the `ScriptFacts`, so the classifier sees what code
  analysis already established. It is also told that comments, strings and names in the script
  were written by the same agent and are not evidence of what the code does.
- A missing answer raises, which escalates the call.
- Answers are returned as **integers 0–100**. Cedar has no floats, and integers leave the
  threshold ("how sure is sure enough") to the policy.
- The classifier is optional because it costs a model call per script and reads text the agent's
  model wrote. Code analysis can't be steered by a comment in the script; a classifier can.
  Policies should lean on `ScriptFacts` for anything they can express with them.

### 4.4 `cedar_evaluator(...)`

New package `harness/cedar_approvals/`, mirroring `jev_approvals/`. Workflow-safe.

```python
def cedar_evaluator(
    *,
    endpoint: str,
    service: str = "tool-policy",
    operation: str = "VerifyToolCalls",
    action_prefix: str | None = None,             # default: the workflow's task queue
    subject: str | None = None,                   # default: the workflow type
    script_classifier: ScriptClassifier | None = None,
    timeout: timedelta = timedelta(seconds=5),
) -> AutoModeEvaluator: ...
```

Per call:

1. Build the caller from `workflow.info()`: namespace, task queue, workflow ID, and `subject`.
2. Action name: `f"{action_prefix or info.task_queue}/{ctx.tool_name}"`. The task-queue default
   matches the proxy's `<taskQueue>/<activityType>` convention (section 8). Agents whose task
   queue is set per deployment should pin `action_prefix` so the policy file doesn't depend on
   it.
3. Parameter `arg0`, a `google.protobuf.Value`:
   - ordinary call: `ctx.tool_input`;
   - Code Mode call: `facts = analyze_script(ctx.code_mode.script, ctx.code_mode.host_functions)`,
     then `classification = await script_classifier(ctx, facts)` if one is configured, then
     `{"calls": sorted(facts.uses), "uses": ..., "has_while_loop": ..., "classification": ...}`.
4. `workflow.create_nexus_client(service=service, endpoint=endpoint).execute_operation(operation,
   request, output_type=VerifyToolCallsResponse, schedule_to_close_timeout=timeout,
   cancellation_type=NexusOperationCancellationType.ABANDON)`. `ABANDON` because the handler
   can't cancel, and a superseded evaluation shouldn't wait for it.
5. Map the answer (section 6). `details` records the action name, the parameter sent, the reason,
   and `criteria_set_name` / `criteria_version`, so the stream shows exactly what Cedar was asked.
6. Stamp `AUTO_MODE_EVALUATOR_ATTR = "cedar_evaluator"`.

**Response decoding.** The package declares its own `toolpolicy.proto` holding only
`VerifyToolCallsResponse`, with the upstream package, message name and field numbers, so the
`messageType` matches. The request needs no type: it goes out as plain JSON, which the verifiers
parse whatever its encoding. `toolpolicy_pb2.py` is generated with protoc 3.20 (`just
generate-toolpolicy`), the same generation temporalio ships, so it loads on every protobuf runtime
temporalio supports. The evaluator module imports it through the sandbox pass-through, so the
type is registered before any result is decoded.

**Criteria.** Cedar ignores `approve_when` / `deny_when` text, but the harness still calls an
evaluator only for tools a non-empty criteria set covers. One catch-all set is enough:

```python
auto_approval_criteria_default=AutoApprovalCriteria(
    sets={"cedar": AutoApprovalCriteriaSet(effect="Judged by the Cedar tool policy.")},
    default="cedar",
)
```

**Mixing evaluators.** An agent that wants Jev for some tools and Cedar for others composes them
in a few lines, routing on `ctx.criteria_set_name`. Nothing in the harness needs to know.

### 4.5 `cedar_schema(...)`

The Cedar schema has to declare every action and the shape of its parameters, and the
script-level shape depends on the host functions and features. Writing it by hand invites drift,
and drift fails closed in confusing ways (default deny on an undeclared action, a `has` check
that is never true). A generator removes that:

```python
def cedar_schema(
    *,
    tools: Sequence[Callable[..., Awaitable[Any]]],
    action_prefix: str,
    script_features: Sequence[ScriptFeature] = (),
) -> str: ...
```

- Ordinary tools: one action each, with `arg0.data` derived from the tool's JSON schema (the
  same schema `function_param` gives the model). Float fields are omitted with a comment, since
  Cedar can't see them.
- Code Mode tools (detected by `__code_mode_host_functions__`, section 4.1): one action with
  `calls: Set<String>`, `uses: { <host_function>?: { call_sites: Long, in_loop: Bool,
  concurrent: Bool, indirect: Bool }, ... }`, `has_while_loop: Bool`, and
  `classification: { <key>?: Long, ... }`.
- Every attribute is optional (`?`) so policies must check with `has`, and a missing value fails
  closed.

Output is text the operator commits next to their `.cedar` file and passes to the verifier as
`CEDAR_SCHEMA_FILE`.

## 5. What Cedar sees, with policies

An ordinary host call, `book_flight` (tools take one request model, so the arguments are nested
under its parameter name):

```json
{"name": "travel-agent/book_flight",
 "parameters": [{"@type": "type.googleapis.com/google.protobuf.Value",
                 "value": {"request": {"flight_id": "UA123", "traveler_name": "Ada"}}}]}
```

A script call, `run_travel_code`:

```json
{"name": "travel-agent/run_travel_code",
 "parameters": [{"@type": "type.googleapis.com/google.protobuf.Value",
                 "value": {
                   "calls": ["book_flight", "record_booking", "search_flights"],
                   "uses": {"book_flight": {"call_sites": 1, "in_loop": false, "concurrent": false, "indirect": false},
                            "search_flights": {"call_sites": 2, "in_loop": false, "concurrent": true, "indirect": false},
                            "record_booking": {"call_sites": 1, "in_loop": false, "concurrent": false, "indirect": false}},
                   "has_while_loop": false,
                   "dynamic_code": false,
                   "classification": {"data_flows_between_tools": 91, "unbounded_iteration": 3,
                                      "mixes_reads_and_writes": 88, "names_unmentioned_entities": 6}}}]}
```

Example policies (the example's full policy is `examples/auto_mode_cedar/policies/travel.cedar`):

```cedar
// Scripts that only read run unattended.
@id("read-only-scripts")
permit (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 && context.parameters.arg0.data has calls &&
  ["search_flights", "search_hotels", "get_trip_summary", "read_trip_board"]
    .containsAll(context.parameters.arg0.data.calls)
};

// Never book in a loop, in parallel, through an alias, or more than once per script.
@id("no-bulk-booking")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 && context.parameters.arg0.data has uses &&
  context.parameters.arg0.data.uses has book_flight &&
  (context.parameters.arg0.data.uses.book_flight.in_loop ||
   context.parameters.arg0.data.uses.book_flight.concurrent ||
   context.parameters.arg0.data.uses.book_flight.indirect ||
   context.parameters.arg0.data.uses.book_flight.call_sites > 1)
};

// A script the classifier thinks may run away is refused.
@id("no-runaway-scripts")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 && context.parameters.arg0.data has classification &&
  context.parameters.arg0.data.classification has unbounded_iteration &&
  context.parameters.arg0.data.classification.unbounded_iteration >= 30
};
```

With only these, a script that books once matches no `permit` and escalates, and then its booking
call escalates again on its own: the user is asked twice. The example avoids that by permitting
every script without `dynamic_code` and relying on the `forbid`s, so the single booking reaches
the user once, as its own call.

## 6. Verdicts and failures

| Cedar / runtime outcome | Verdict | Why |
|---|---|---|
| `allowed: true` | `APPROVE` | A policy permits it and none forbids it. |
| denied, reason names a policy (`denied by Cedar policy <id>`) | `DENY` | An operator wrote a rule against this. The reason is the error text the model sees. |
| denied, no policy named (`not permitted by any Cedar policy`) | `ESCALATE` | Nobody wrote a rule for this. Silence is not a refusal; a person decides. |
| Nexus operation fails, times out, or returns `BAD_REQUEST` | raise → escalate | Published as `auto_approval_evaluation_error`. |
| `analyze_script` raises `SyntaxError` | raise → escalate | The script can't run as written. |
| script classifier fails | raise → escalate | No partial facts are sent. A policy that ignored the missing classification could otherwise approve. |
| a human answers first | superseded | The Nexus operation is abandoned. |

The middle two rows depend on the reason text. The fix belongs upstream: add a
`decision` enum (`ALLOW`, `FORBID`, `NO_PERMIT`) to `VerifyToolCallsResponse`. The proxy reads only
`allowed`, so the change is backward-compatible. Until then the evaluator matches the reason
prefix, and a test pins both strings.

## 7. Wiring an agent

```python
self._runner = AgentWorkflowRunner(
    config,
    stream=WorkflowStream(),
    approval_policy_default=ToolApprovalPolicy.auto_mode(pre_approved_tools=BOARD_TOOL_NAMES),
    auto_approval_criteria_default=AutoApprovalCriteria(
        sets={"cedar": AutoApprovalCriteriaSet(effect="Judged by the Cedar tool policy.")},
        default="cedar",
    ),
    auto_mode_evaluator=cedar_evaluator(
        endpoint="tool-verifier",
        action_prefix="travel-agent",
        script_classifier=jev_script_classifier(),   # omit for code facts only
    ),
)
```

Worker: no new activity unless a classifier is used (`jev_classify`, registered by
`AgentHarnessPlugin`). The Nexus endpoint must allow the agent's namespace as a caller. Calling a
Nexus operation from a workflow does not need the standalone-Nexus dynamic config; only the
proxy's client-started operations do.

## 8. Relation to the proxy

- The evaluator is the agent asking. The proxy is enforcement. The proxy verifies only work that
  leaves the worker's task queue, and harness tools run as activities on the agent's own queue,
  so today the proxy never sees them.
- Action names default to `<taskQueue>/<tool>` so one policy file can serve both. If tools later
  move to a trusted task queue behind the proxy, the proxy verifies the same names. Parameter
  shapes differ: the proxy sends one parameter per activity argument (the request model), the
  evaluator sends the model-facing arguments as one object.
- An agent worker running behind the proxy makes its own `VerifyToolCalls` call as a Nexus
  operation, which the proxy verifies as a tool call. The policy needs a `permit` for
  `tool-verifier/tool-policy/VerifyToolCalls`.

## 9. What was built

| Step | What | Where |
|---|---|---|
| 1 | `CodeModeCall`, `AutoApprovalContext.code_mode`, plumbing through `_tool_defn` and the gate; `code_mode_host_functions(tool)` | `agent_protocol/agent_interface.py`, `agent_workflow.py`, `code_mode/tool.py` |
| 2 | `analyze_script`, `ScriptFacts`, `HostFunctionUse`, the `ScriptClassifier` type | `code_mode/analysis.py` |
| 3 | `cedar_evaluator`, `script_parameter`, `decide`, the response proto and its generated bindings | `harness/cedar_approvals/` (`evaluator.py`, `toolpolicy.proto`, `toolpolicy_pb2.py`); `just generate-toolpolicy` |
| 4 | `jev_classify` activity, `JevClassificationAnswer`, `jev_script_classifier`, `ScriptFeature`, `DEFAULT_SCRIPT_FEATURES` | `harness/jev_approvals/` (`classifier.py`, `activity.py`, `models.py`) |
| 5 | `cedar_schema` | `harness/cedar_approvals/schema.py` |
| 6 | The Monty travel agent under Cedar: `policies/travel.cedar`, the generated `policies/schema.cedarschema`, and recipes for the Nexus endpoint, the Rust verifier and the schema | `examples/auto_mode_cedar/` |

`agent.cedar_evaluator`, `agent.jev_script_classifier` and `agent.CodeModeCall` are on the
`agent` facade. Upstream: the `decision` field from section 6.

## 10. Testing

- `tests/harness/test_code_mode_analysis.py`: call sites, loops (`for`, `async for`, `while`,
  comprehensions), `gather` / `create_task`, helpers called in loops, recursion, lambdas,
  methods, helpers passed by reference, aliasing, `eval` / `exec` / `compile`, unknown names, and
  `SyntaxError`.
- `tests/harness/test_code_mode_approval_context.py`: a real Code Mode call reaches the evaluator
  with `code_mode` set; the host call it makes doesn't.
- `tests/harness/test_cedar_approvals.py`: the verdict mapping (both reason strings pinned), the
  script parameter, and decoding a payload built exactly as the Rust verifier's
  `response_payload` builds it. End to end on the dev server, against a Python `VerifyToolCalls`
  handler that answers with a `json/protobuf` payload: allow, forbid, no permit, handler failure,
  an unparseable script, and a script judged by its facts (with `jev_script_classifier` over a
  stand-in `jev_classify` activity) followed by each host call judged by its arguments.
- `tests/harness/test_jev_script_classifier.py`: the request it builds, feature validation, and
  the activity's projection of TypeSafe's answers.
- `tests/harness/test_cedar_schema.py`: declared shapes, fields Cedar can't see, self-referencing
  models, Code Mode actions.
Nothing in this repo runs Cedar or the verifier: the policy service lives in
temporal-untrusted-workers. Running the example against the real verifier is a manual test,
described in `examples/auto_mode_cedar/README.md`.

Not covered by the suite: a verifier timeout and an evaluation superseded by a human (both go
through the harness's generic paths, tested elsewhere).

## 11. How the open questions were settled

1. **Indirect lookups in Monty.** Probed against `pydantic-monty` 1.0: an alias
   (`f = book_flight`) raises `NameError` at run time, and `globals()`, `vars()` and `getattr`
   don't exist. But `eval("book_flight('a')")` dispatches a host call, so `analyze_script` reports
   `dynamic_code` and treats every host function as reachable (section 4.2).
2. **The proto copy.** Not needed. The request goes out as plain JSON, so the harness only
   declares the response message, in its own `toolpolicy.proto` (section 4.4).
3. **Context for per-call checks.** Still open. A host call could carry the facts of the script
   it came from, so a per-call policy could say "book only from scripts that don't loop". Left
   out until a policy needs it.
