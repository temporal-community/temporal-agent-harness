# Observable agent state — investigation status and open bugs

**Branch:** `reactive-agent-state`  ·  **Base commit:** `2548bd3`  ·  **Written:** 2026-09-14
·  **Updated:** 2026-09-14 (three of the four confirmed bugs fixed; see §3)

A handoff document, not a design document. [`docs/design/observable-agent-state.md`](../design/observable-agent-state.md)
says what the feature *is*; [`docs/design/rendering-agent-state.md`](../design/rendering-agent-state.md)
covers the rendering side. This one says **where the work stands, what is known to be broken, what
was investigated and cleared, and what has to be decided before this can ship.**

Read §1 and §2, then §3. Everything after that is reference.

---

## 1. What is on the branch

Committed in `2548bd3`:

- **The state layer** — `temporal_agent_harness/harness/state/` (~1,600 lines). Deeply immutable
  Pydantic values; `with ref.mutate() as d:` hands out a draft; a committed block publishes RFC 6902
  ops. Leaf package: stdlib + pydantic only.
- **Wire types** — `AgentStateSnapshot` / `AgentStatePatch` in `agent_protocol/events.py`, riding the
  existing `turn_events` topic. No server change was needed; `web/app.py` is a generic passthrough.
- **The opt-in** — `AgentWorkflowRunner.state()` in `agent_workflow.py`.
- **The console pane** — `ui/src/lib/state/agentState.ts` (the fold), `ui/src/lib/state/jsonPatch.ts`
  (the applier), `ui/src/lib/components/agent/AgentStatePanel.svelte` (the pane), plus a `state` kind
  in the pane registry and rows in the replay log.
- **An example on top of it** — `examples/monty/trip_board.py`, wired into `MontyDynamicAgent` and
  `MontyChatAgent`, plus `MontyDynamicAgent` added to `examples/monty/agents.toml`.
- **Three incidental fixes** (see §6) — `stream_merge/gates.py`, `code_mode/stubs.py`, `tests/__init__.py`.

Uncommitted working-tree delta (~800 insertions, ~337 deletions across 15 files):

- **Sets removed from the layer** (§5.1) — `FrozenSet`/`DraftSet` deleted, `set`/`frozenset` rejected
  by the schema checker, tests converted.
- **Eleven failing tests** pinning the confirmed bugs in §3, plus passing tests pinning cleared claims.
  (All eleven now pass; see §3.)
- Two leftover cleanups: `__imul__` re-indent, dead `DraftSession.ref` removed.

**Since then**, all four confirmed bugs in §3 have been fixed and every pin flipped to green. The
design doc gained the sentences the fixes owe it (substitutable drafts, the dict-key rule, the
draft-class lifecycle, and the commit-time-validator reentrancy note from §4.1).

**Test state right now:** `uv run pytest -q` → **491 passed, 0 failed**.
`uv run pytest tests/state tests/harness/test_observable_state.py -q` → **159 passed**; the §3.1
xfail is now an ordinary passing test.

---

## 2. Decisions needed before this ships

Nothing below is a matter of finding more bugs. These are calls someone has to make.

| # | Decision | Status |
|---|---|---|
| **D1** | **Is `Draft[C]` substitutable for `C`?** | **DECIDED: yes.** `Draft[C].__eq__`/`__hash__` compare against `C`. §3.3 fixed on top of it. |
| **D2** | **How to fix the draft-class cache** (§3.1) | **DONE** — eager, hung off the state class, as recommended. |
| **D3** | **Reject ambiguous dict keys at class-definition time?** (§3.4) | **DONE** — and the rule turned out to need to be wider than unions. |
| **D4** | **Is this one PR or several?** | Still open. Three incidental fixes (§6) are independently valuable and independently reviewable. |
| **D5** | **Secrets policy** (§8) | Still open, and still nobody has asked. |

All four confirmed bugs in §3 are now fixed. What is left in this table is D4 (how to slice the
PR) and D5 (whether authors are owed a redaction story), neither of which is a defect.

### D1 in detail — the substitutability question

The layer currently says **yes** for typing and **no** for equality:

- `Draft[C]` is a real subclass of `C`, so `isinstance` holds and mypy sees `C`. That is deliberate —
  it is why `d.field = ...` typechecks. See the `HarnessState` docstring on why `frozen=True` is not used.
- Pydantic's `BaseModel.__eq__` requires `type(a) is type(b)` **and** equal `__pydantic_private__`,
  so `Draft[C] != C` on both counts.

§3.2 was caused by the *yes* half (pydantic keeps a draft where a `C` was expected) and is fixed.
§3.3 is caused by the *no* half (`in` / `count` / `==` give wrong answers) and is not.

**One claim in the original write-up does not survive contact with the code:** the smuggling
detection does *not* rely on `Draft[C] != C`. Every draft test in the layer — `meta_of`, `wrap`,
`commit`, `assert_no_drafts` — asks `cls.__dict__.get("__harness_is_draft__")`, and the two
identity guards in `draft_set_field` / `DraftList.__setitem__` use `is`. Nothing compares a draft
to a value with `==` to decide whether it is a draft. So making `__eq__` substitutable does not
cost the §3.2 fix anything; what it does cost is spelled out in §3.3, option 3.

---

## 3. The four confirmed bugs

All four were verified empirically — divergent output captured, not reasoned about. Three are now
fixed and their pins pass; §3.3 is still open because it is D1. Each subsection keeps the original
diagnosis (it is what a reviewer needs in order to check the fix) and ends with what was done.

### 3.1 The draft-class cache grew without bound — ✅ FIXED

**Where:** `drafts.py`, `draft_class()` / `_DRAFT_CLASSES`.

`_DRAFT_CLASSES` is a module-level `dict[type, type]` keyed on the author's state class. Whether it
leaks depends entirely on the import pattern, because Temporal's sandbox re-imports per workflow
*instance* whatever is not passed through:

| Pattern | `drafts` module | state class | Result |
|---|---|---|---|
| A. Nothing passed through | per-instance | per-instance | Bounded — own cache per instance, discarded with it |
| B. Harness **and** models passed through (`examples/monty`) | shared | shared | Bounded — 40 instances → `len == 2` |
| C. Harness passed through, state class in the workflow's own module | **shared** | **per-instance** | **Leaks linearly, forever** |

Pattern C is what `class MyState(HarnessState)` next to your `@workflow.defn` gets by default.

```
40 sandboxed instances      -> 80 cache entries, 80 live Draft[...] classes
cost per entry              -> 28,878 B tracemalloc / 368 gc objects / ~107 kB RSS
=> ~214 kB retained RSS per workflow instance (2 state classes/agent)
```

~18 GB/day at 1 workflow/sec; ~300 MB/day at 1/minute. Never evicted — an OOM, not a plateau.
Proven that the cache is the sole retainer: `cache.clear()` reclaimed 28 MB and dropped live classes
from 31 to 2. The Temporal sandbox itself releases the re-imported module correctly.

**Pinned by:** `tests/harness/test_observable_state.py::test_the_draft_class_cache_does_not_grow_per_workflow_instance`.

**Fix options, both tested:**

- ❌ **`WeakKeyDictionary` does not work.** `Draft[C].__bases__` holds `C` strongly and the dict holds
  `Draft[C]` as its value, so the weak key stays reachable. Measured: 60 classes dropped, 60 entries
  retained, zero collected. The weak-value variant *does* collect — immediately, at the end of every
  `mutate()` block — so it trades a leak for a cold `type()` per mutation.
- ✅ **Hang the draft class off the state class.** `C ↔ Draft[C]` is one gc-collectable cycle; 0 of 60
  retained. Two spellings: lazily in `draft_class` via `cls.__dict__.get("__harness_draft_class__")`
  — **must** be `__dict__`, not `getattr`, or a subclass silently inherits its parent's draft class
  and mutations land on the wrong fields — or eagerly in `__pydantic_init_subclass__`.

**What was done:** the eager version. `HarnessState.__pydantic_init_subclass__` builds `Draft[C]` and
`drafts.build_draft_class` hangs it on `C` as `__harness_draft_class__`; `draft_class` reads it out of
`cls.__dict__` and still builds on demand for anything that reached it without the hook. The
module-level `_DRAFT_CLASSES` dict is gone.

⚠️ **One trap, found the hard way, and it is the reason the eager version is not a two-line change.**
The obvious spelling — a deferred `from .drafts import build_draft_class` inside
`__pydantic_init_subclass__` — **breaks every sandboxed workflow**, and breaks it a long way from the
import. `workflow.unsafe.imports_passed_through()` only covers the imports written inside it. An
import that fires later, from a hook, is inside nothing, so the sandbox's import machinery is active
and hands back a *sandboxed copy* of `drafts` while the caller is holding the passed-through one.
Two `DraftList` classes then exist; `commit()`'s `type(value) is DraftList` says no; a draft container
survives into at-rest state; and the *next* `mutate()` block dies on `RevokedDraftError` from a
session that closed a block ago. The workflow task then fails and retries forever, so the symptom is
a hung test, not an error. `base.py` now takes `build_draft_class` (and `draft_set_field`, which had
the same latent import) through a `bind_drafts()` registration at import time — the same cycle-break
`containers.bind` already uses, for a second and sharper reason. **Do not reintroduce a
function-level import anywhere in this package.**

**Pin:** `test_the_draft_class_cache_does_not_grow_per_workflow_instance` is no longer `xfail`. It
now counts live `Draft[...]` classes out of the gc rather than reading a cache's `len()`, and
measures a *plateau* — two equal batches of workflow instances, the second must add zero — because
the sandbox legitimately retains one module copy of its own. Verified to still fail (3 per 5 runs)
when a module-level dict is temporarily reintroduced.

### 3.2 A draft could be smuggled into at-rest state through a model — ✅ FIXED

**Where:** `drafts.py::assert_no_drafts` (`if isinstance(value, BaseModel): return`), with
`containers.py::freeze` (returns models untouched) and `HarnessState.model_config`
(`revalidate_instances="never"`).

Four layers, none of which looks inside a freshly built model:

1. Author writes `Group(name="copy", todos=[draft_todo])` inside a `mutate()` block.
2. `revalidate_instances="never"` → pydantic sees a `Draft[Todo]`, a real `Todo` subclass, passes the
   isinstance check, and **keeps the live draft object**.
3. `freeze()` returns any `BaseModel` untouched.
4. `assert_no_drafts` early-returns on any `BaseModel`; `commit()` falls through to `return value` for
   a non-draft model.

```
ref.current.groups[1].todos[0] type : Draft[Todo]      is it a draft? True
ref.current.groups[1].todos   type  : FrozenList
```

Not silently mutable — *inert-but-explosive*. Ordinary at-rest reads raise `RevokedDraftError`, and a
later `mutate()` poisons on the same path because `wrap()` sees `__harness_is_draft__` and hands back
the dead node.

**Severity: the published stream diverges from the committed value.** Mutate the draft after
smuggling it and the op is recorded against its *original* path only, while both locations share the
value by identity:

```
current groups: [('g', ['CHANGED AFTER SMUGGLING']), ('smuggler', ['CHANGED AFTER SMUGGLING'])]
replay  groups: [('g', ['CHANGED AFTER SMUGGLING']), ('smuggler', ['orig'])]
replay == current ?  False
```

**Reachability refinement:** the *container* shape is **not** reachable — pydantic's list/dict
validators always build a fresh container, so a bare `DraftList` dies at the boundary. But if its
elements were already read (hence wrapped) before the container was handed over, the elements survive
and it collapses back into the model shape.

**Pinned by** (`tests/state/test_lifecycle.py`, all four green since the fix):
`test_draft_model_cannot_be_smuggled_inside_a_newly_built_model`,
`test_draft_model_cannot_be_smuggled_through_a_model_typed_field`,
`test_at_rest_state_never_holds_a_draft_node`,
`test_smuggled_draft_does_not_desynchronize_the_op_stream`.

The last two `return` early if `DraftAliasError` fires, so they pass under either fix strategy.

**What was done:** the full recursive walk in `assert_no_drafts`. It now descends into
`BaseModel.__dict__` (and `__pydantic_extra__`) instead of returning at the first model, with a
`type(value) in _LEAF_TYPES` early-out so scalars cost one dict lookup. `revalidate_instances` was
left alone, so the structural-sharing promise in §9 of the design doc is intact, and the error still
fires at the assignment line where it is actionable rather than at commit where the ops are already
wrong.

⚠️ The "fast path" that skips `Frozen*` containers is **still** wrong and is deliberately not there —
`_freeze_containers` runs on models built inside the block too, so in the repro
`new_group.__dict__["todos"]` is a `FrozenList` holding the draft. There is a comment in
`assert_no_drafts` saying so.

**Cost, measured** (`tests/state/bench_state.py`, medians): every existing benchmark is unchanged
within noise — `append_to_a_large_list` 1.33 → 1.35 ms, `deep_nested_mutation` 36.2 → 35.5 µs. The
worst shape the walk can have is assigning a whole large container at once, which none of the
benchmarks covered, so `test_assign_a_large_list_of_models` was added: 10,000 models in one
assignment costs **7.7 → 9.0 ms, +17%**. That is the honest ceiling, and it is paid against a
`validate_python` call on the same value that dominates it.

### 3.3 Lazy wrapping broke value equality on draft lists — ✅ FIXED (D1 decided: yes)

**Where:** `containers.py::DraftList` — `_read()` replaces an element with its draft wrapper *in
place*, so a list's contents change class as the author reads it.

The only difference between these two is one earlier read:

```
WITHOUT a prior read:  d.todos.remove(ref.current value)  ->  OK, ops: remove /groups/0/todos/0
WITH    a prior read:  same call                          ->  ValueError: Todo(id='a', ...) is not in list
```

**The loud half is the harmless half.** `remove`/`index` raise. The damage is that `in`, `count` and
`==` return wrong answers **with no exception**:

```
if x not in d.list: d.list.append(x)
  read_first=False  ->  committed ['a', 'b']
  read_first=True   ->  committed ['a', 'b', 'a']
  replay_ok=True in both cases
```

A dedup guard writes a duplicate, the op stream is internally consistent, replay agrees, nothing
reports a problem. The committed tree depends on whether an unrelated earlier line iterated the list.

**Blast radius** (all tested):

| Surface | Verdict |
|---|---|
| `remove(x)` with `x` from `ref.current` | BROKEN after any read — `ValueError` |
| `remove(node)` with `node` read from that list | SAFE — identity short-circuit; op index correct |
| `x in d.todos` | **BROKEN SILENTLY** — `True` before a read, `False` after |
| `d.todos.index(x)` | BROKEN — `ValueError` |
| `d.todos.count(x)` | **BROKEN SILENTLY** — returns `0` |
| `d.todos == [...]`, `ref.current.todos == d.todos` | **BROKEN SILENTLY** |
| `DraftDict.__eq__` with model values | **BROKEN SILENTLY** — the one dict-side casualty |
| `DraftDict` key-driven methods (`in`, `pop`, `setdefault`, `update`, `del`) | SAFE — keys are never wrapped |
| `DraftList.__setitem__` identity guard | UNAFFECTED — it is `is`, not `==` |
| `DraftList.sort(key=...)` on a partly-wrapped list | SAFE — `_rekey(0)` runs, live child drafts follow |
| Op index when `remove` succeeds | CORRECT |
| `list[list[int]]`, `list[dict[str,int]]`, scalars | IMMUNE — `list`/`dict` equality ignores class |
| `dict[str, list[Todo]]` | BROKEN — model elements anywhere hit it |

**Specific to model elements.** Container wrappers are safe precisely because `list`/`dict` equality
compares contents and not the class — which is exactly what pydantic does not do.

`remove` never removes wrong *data*: with duplicates plus partial wrapping it can pick a later index
(violating remove-first semantics), but the resulting multiset, the emitted op and replay all agree.

**Pinned by** (`tests/state/test_property.py`, green since the fix):
`test_value_equality_survives_lazy_wrapping`, `test_reading_a_list_does_not_change_what_it_equals`,
`test_containment_guard_does_not_append_a_duplicate_after_a_read`, and
`test_nested_containers_are_immune_to_lazy_wrapping` (passes — localises the defect).

**Fix options:**

1. **Unwrap on demand inside the comparing methods** — give `DraftList` a `_find(value)` used by
   `remove`/`index`/`__contains__`/`count`. Contained to `containers.py`; fixes the silent cases.
   Note a *dirty* draft's `meta.original` is stale, so it must compare against the draft's **current**
   field data, not the original. Leaves `d.todos == [...]` still wrong.
2. **Compare against `meta.original`** — simplest, but wrong for any element already mutated in the
   block. Avoid.
3. **Make `Draft[C].__eq__` compare against its at-rest class** — the only option that also fixes
   `d.todos == [...]`, `DraftDict.__eq__`, and hand-written comparisons. If taken, define `__hash__`
   deliberately rather than inheriting: defining `__eq__` on `_ModelDraftMixin` without it sets
   `__hash__ = None` and makes every draft unhashable.

This was **D1**, and option 3 is what was taken. Two corrections to the original framing, both
checked against the code rather than reasoned about, are what made the choice safe:

- **Option 3 does not cost the §3.2 fix anything.** Nothing in the layer detects a draft by
  comparing it to a value; every check is `cls.__dict__.get("__harness_is_draft__")` or `is`. See
  the D1 section in §2.
- **The reflected-operand question resolves the right way.** `Draft[C]` is a proper subclass of
  `C`, so Python gives `Draft[C].__eq__` priority even when the at-rest value is on the left, and
  `list.__contains__` / `index` / `remove` / `count` all go through `PyObject_RichCompareBool`.
  One `__eq__` on the mixin therefore covers every surface in the blast-radius table above,
  including `ref.current.todos == d.todos`.

What option 3 genuinely costs is `__hash__`: a draft is mutable, so a hash that tracks its fields
moves under the author mid-block. Today that is mostly academic — `DraftList` inherits
`list.__hash__ = None`, so a draft with any container field is already unhashable — but a
scalar-only draft *is* hashable today, and the decision should be made on purpose.

**What was done:** option 3. `_ModelDraftMixin` defines `__eq__` (compare against
`__harness_at_rest__`, on `__dict__` plus `__pydantic_extra__`, never on
`__pydantic_private__`, which holds the `DraftMeta`) and `__hash__` (the at-rest class plus the
field values, so a draft and its at-rest twin land in the same bucket).

It fixes every row of the blast-radius table at once, including the two the contained fix could
not reach (`d.todos == [...]`, `DraftDict.__eq__`), and equality tracks the draft's *current*
value, so a node that has been changed inside the block correctly stops equalling what it was
drafted from. `__hash__` is explicit rather than inherited, because a class defining `__eq__`
without it gets `__hash__ = None`; a draft of a model with a container field stays unhashable, as
it already was (`DraftList` inherits `list.__hash__`, which is `None`), and that is the honest
answer for a value the author is in the middle of changing.

New pins in `test_property.py`: `test_a_draft_equals_its_at_rest_value_in_both_directions`,
`test_a_draft_stops_equalling_the_value_once_it_is_changed`,
`test_a_draft_is_not_equal_to_a_different_state_class`,
`test_a_draft_hashes_as_the_value_it_drafts`, and
`test_substitutable_equality_does_not_blind_the_aliasing_guard` — the last of which exists
because the original write-up assumed it would.

### 3.4 Dict pointer segments collided — ✅ FIXED (and the rule had to be wider)

**Where:** `containers.py::DraftDict._seg` — `escape(str(self._key.adapter.dump_python(key, mode="json")))`.

Two distinct bugs, one of which needs no union at all.

**(a) `dict[bool, V]` — a plain rendering bug, broken on the first write.** `_seg` uses the *value*
serializer plus `str()`, but pydantic serializes dict **keys** through a different path:

```
str(TypeAdapter(bool).dump_python(True, mode='json'))   -> 'True'
TypeAdapter(dict[bool,str]).dump_python(...)            -> {'true': ..., 'false': ...}
```

Every op on a bool-keyed dict points at a key that does not exist in the snapshot. Needs no decision —
just fix `_seg` to render the key the way the dict-key serializer does.

Related slip in the same method: `_seg(vkey)` renders the *validated* key while `dict.__setitem__`
keeps the *pre-existing equal* key. That is what produces a hard `JsonPatchConflict` in the
`int|bool` and `float|int` cases. Fixing `_seg` to render the actually-stored key resolves both.

**(b) Union keys — a design call (D3).** `_check_annotation` expands a union and checks each member
independently, so `dict[int | str, V]` sails through. Then:

```
python dict: {1: 'int-one', '1': 'str-one'}   len=2
model_dump : {'mapping': {'1': 'str-one'}}    len=1     <- snapshot already lossy

del d.mapping[1]
ops        : [{'op': 'remove', 'path': '/mapping/1'}]
replayed   : {'mapping': {}}                            <- consumer deleted the WRONG entry
```

| annotation | verdict |
|---|---|
| `dict[bool, V]` | BROKEN, no union needed — (a) above |
| `dict[int, V]`, `dict[SomeIntEnum, V]` | ok |
| `dict[int \| bool, V]`, `dict[float \| int, V]` | BROKEN — hard `JsonPatchConflict` in the consumer |
| `dict[str \| StrEnum, V]`, `dict[UUID \| str, V]`, `dict[date \| str, V]` | BROKEN — snapshot loses one; delete removes the wrong entry |
| `dict[int \| SomeIntEnum, V]` | benign (same Python key *and* same JSON key) but covered by a blanket rule |
| every single non-union type in `_ALLOWED_SCALARS` | ok — **`bool` is the sole non-union offender** |

**Pinned by** (`tests/state/test_paths.py`, green since the fix): `test_bool_dict_keys_use_their_json_rendering`,
`test_union_dict_keys_are_rejected[int-or-str|date-or-str|float-or-int]`.

**What was done, (a):** `DraftDict._seg` renders `True`/`False` as `true`/`false`. Verified against
pydantic's actual dict-key serializer for every type in `_ALLOWED_SCALARS` plus `StrEnum`/`IntEnum` —
**`bool` is the only one of them that ever disagreed**, and `test_a_pointer_segment_is_the_key_pydantic_would_serialize`
now parametrizes over all of them so the next divergence is caught in whichever type introduces it
rather than in production. (`_seg` renders the key directly instead of dumping a one-entry dict per
op; that is the performance decision the drift test exists to protect.) The `_seg(vkey)`-vs-stored-key
slip resolves with it: with unions gone, every stored key of a `dict[K, V]` has been through the same
validator, so no two equal keys can render differently.

**What was done, (b):** rejected at class definition — but the union rule alone turned out to be a
hole, so `_check_dict_key` is now a **positive** rule: a dict key must be a scalar, an `Enum`, or a
`Literal` of those. Two further cases were found while implementing it, both confirmed the same way
as the originals:

```
dict[Literal[1, "1"], str]     python {1: 'int-one', '1': 'str-one'}   snapshot {'1': 'str-one'}
                               ops    [add /m/1, add /m/1]              <- no union needed
dict[SomeHarnessState, str]    snapshot key  "id='a'"   pointer  /m/{'id': 'a'}
                               <- the two spellings do not even agree with each other
```

The model-key case is the worse of the two and was reachable from a plain `dict[Todo, str]`
annotation. Neither has any correct behaviour available, so neither costs an author anything real;
both are pinned (`test_a_literal_key_whose_members_collide_as_json_is_rejected`,
`test_model_dict_keys_are_rejected`, `test_container_dict_keys_are_rejected`), along with the
counter-case that the rule is about collisions and not about `Literal` keys as such
(`test_a_literal_key_whose_members_stay_distinct_is_fine`).

`Any` as a key still only warns — it is the documented escape hatch and the warning already says
what it buys. Do **not** try to disambiguate pointers: any such scheme describes a document
`model_dump` does not produce.

---

## 4. Investigated and CLEARED — do not re-open

Recorded so nobody spends the effort twice.

### 4.1 A commit that raises leaves the ref half-unwound — **REFUTED**

The inline comment on `ref.py` (`# may raise; current stays untouched`) is true and complete.

```
propagated?                        True (ValidationError)
ref.current is before              True
ref.current.items is before_items  True    <- untouched subtree, by identity
ref.version                        0 (was 0)
ref._open is None                  True
```

`__exit__` clears `_open` *before* `commit()`, so a failed commit cannot wedge the ref. Next block
goes **0 → 1, not 0 → 2**. Abandoned ops die with the session. Failures interleaved with successes
gave contiguous versions `[0,1,2,3]` and `replay(events) == current`. `commit()` is purely
constructive, so a half-committed tree keeps identity throughout; `_restore_untouched` cannot run on a
half-built object (verified by instrumenting: 0 calls on the failing commit). Sessions are per-block,
so ops never leak between blocks.

Two tests added (`test_a_failed_commit_leaves_no_trace_and_the_ref_fully_usable`,
`test_failed_commits_never_desynchronize_the_patch_stream`) — **both pass**, pinning correct behaviour.

*One genuinely new finding, on the **success** path:* because `_open` is cleared before `commit()`,
the ref is unlocked while a commit-time validator runs. A validator that reentrantly mutates its own
`StateRef` loses its update while its patch stays on the stream (`replay == current: False`).
Pathological — needs a validator with a side effect on the state it validates — but real. **Worth one
sentence in the design doc**, not a fix.

*On telling failure modes apart:* only from the traceback (the commit case points at the `with` line
and passes through `ref.py:__exit__`), but the observable outcome is identical, so no recovery
decision depends on it. **Do not wrap in a `CommitError`** — a commit-time `ValidationError` is
exactly the signal a validator exists to produce, and `except ValidationError` is a reasonable thing
to write deliberately. If better diagnosis is wanted, `e.add_note(...)` preserves that.

### 4.2 Dynamic class creation under the sandbox — **SAFE**

`type(f"Draft[{cls.__name__}]", ...)` runs inside a workflow task with no interference from the
sandbox's import hooks or restricted builtins; pydantic's `ModelMetaclass` runs fine;
`__pydantic_init_subclass__` early-returns for the draft subclass as designed. Verified against the
**default sandboxed runner** on temporalio 1.32.0 / CPython 3.13.

Note: `tests/harness/test_observable_state.py` previously used `UnsandboxedWorkflowRunner`, so this
path had **never once run in CI**. Now covered by four tests under the default runner.

⚠️ **What is *not* safe, discovered while fixing §3.1: a deferred import inside a function.**
`workflow.unsafe.imports_passed_through()` covers the imports written inside it and nothing else, so
a `from .drafts import ...` that fires later — from `__pydantic_init_subclass__`, or from any hook
running under the sandbox — is served a *sandboxed copy* of a module the caller already holds the
passed-through copy of. Two `DraftList` classes, `commit()`'s identity check fails, a draft container
survives commit, the next block raises `RevokedDraftError`, the workflow task retries forever and
the test hangs instead of failing. The package now has no function-level imports: `base.py` gets what
it needs from `drafts` through `bind_drafts()` at import time. Keep it that way.

Determinism: a `PYTHONHASHSEED` sweep gives byte-identical `__name__`, `__qualname__`, MRO,
`__harness_at_rest__`, `model_fields` order, adapters, and published ops/snapshot. Nothing about the
generated class leaks into the payloads.

Cost (cold, per distinct class, medians): flat 4-field 174 µs, nested 106 µs, 8-deep 231 µs; whole
first `mutate()` block 169 µs median / 1.66 ms max. Sub-millisecond against a 10 s workflow task
timeout — a **non-issue**. Move it to registration time for the cache's sake (§3.1), not for latency.

### 4.3 `_DRAFT_CLASSES` thread race — **REAL, CONSEQUENCES BENIGN**

**Closed by the §3.1 fix** (class creation is now serialized behind the metaclass at definition
time), but the analysis is kept because it is what says the residue was harmless.

`draft_class` was a non-atomic read-modify-write and Temporal runs workflow tasks on a thread pool.
Forced with a barrier and a 1 µs switch interval: **400/400 trials produced more than one distinct
`Draft[C]`**. Without the barrier, 200 trials × 32 threads produced zero races.

Nothing downstream compares draft classes by identity — `meta_of`/`wrap` test
`cls.__dict__.get("__harness_is_draft__")`, `commit` goes through `cls.__harness_at_rest__`, and
`isinstance`/`issubclass` hold for both. Residue is one orphaned class. The one wrinkle: two nodes in
the same block wrapped by *different* draft classes would compare unequal (the §3.3 hazard again) —
transient and vanishingly unlikely, but not structurally excluded. The eager fix in §3.1 closes it.

### 4.4 Two leftovers — **FIXED**

- `containers.py::__imul__`'s loop body was over-indented by four spaces. Cosmetic only (it was the
  loop's sole statement); re-indented, behaviour verified unchanged.
- `session.py::DraftSession.ref` was assigned and never read. Removed, along with the `__slots__`
  entry, the constructor parameter, and the `TYPE_CHECKING` import of `StateRef` it existed to type.
  `session.py` now has no sibling imports at all, which suits the package's leaf discipline.

*Asked and answered:* the field was **not** needed for recursive types. Those already work —
`Node(next=Node(next=...))` mutates four levels deep with exact pointers (`/next/next/value`) and
`replay == current`. What does not work is a recursive *structure* (a cycle or a shared node), and a
back-pointer would not help: JSON Pointer paths are tree paths and `model_dump` cannot represent a
cycle. `DraftAliasError` already refuses it. The field's plausible intent was error messages naming
the `state_id`; easy to re-add with a caller if wanted.

---

## 5. Design changes already made

### 5.1 Sets removed from `HarnessState` (uncommitted)

**Decision taken and implemented.** `set[...]` and `frozenset[...]` are rejected at class-definition
time with a message that teaches the reason. Rationale, in the schema checker and the design doc:
JSON has no set and RFC 6902 has no set operation, so a set could only ever be published by re-sending
the whole collection — the coarsest op in a layer whose claim is that a change costs what it touched.

**215 lines deleted, 45 added.** `containers.py` went 674 → 503 lines (25% of the file), losing
`FrozenSet` (40), `DraftSet` (129), `_apply`'s four-way dispatch and eight in-place operator
overloads, every `sorted()`-on-read, and both `try/except TypeError` hash-order fallbacks.

Four confirmed determinism bugs were **retired rather than fixed**:

- `DraftSet.pop()` returned a different element per process (six seeds, six different elements).
- `frozenset[E]` fields published **hash-ordered snapshots** — the base every patch applies to.
- Unorderable elements (`set[str | None]`, `set[int | str]`) fell through to hash order in `__iter__`
  and `_dump_all`.
- `DraftSet._coerce` skipped both `assert_no_drafts` and `freeze`; combined with
  `HarnessState.__hash__` hashing `tuple(self.__dict__.values())`, a draft member's hash moved
  mid-block, fell out of its bucket, and re-adding duplicated it — the published patch carried a set
  serialized with two identical members while `commit` collapsed it back to one.

It also **improved the op stream**: adding a tag used to re-send every tag; it is now `add /tags/-`.
The property test's assertion changed from `/groups/0/todos/0/tags` to `/groups/0/todos/0/tags/-`.

Test conversions worth knowing about:

- The `pop()` determinism pin became `test_every_op_is_byte_identical_across_processes` — the
  `PYTHONHASHSEED=0..5` subprocess harness was **kept** and pointed at every remaining container.
  The claim it guards got *stronger*: determinism used to hold "when the elements are orderable" and
  now holds outright.
- The two set-smuggling tests became `test_a_set_field_cannot_be_declared_at_all`, which pins the
  assumption the deletion rests on.

One survivor: `d.meta.update(some_local_set)` — passing a set as *input* is still possible even with
no set-typed fields, and emits ops in divergent order. Arguably caller error; stdlib `dict.update`
accepts it silently too.

---

## 6. Incidental fixes made along the way (candidates for splitting out — D4)

All three are unrelated to observable state, independently valuable, and already committed in `2548bd3`.

1. **`stream_merge/gates.py` — turn-0 child events were stranded.** Registering state in
   `@workflow.init` puts a `turn_number=0` event at offset 0 of a *subagent's* stream. The open gate
   held any child event whose turn had no bracket — and nothing can ever open a bracket for turn 0, so
   it was not a delay but a strand; because the engine holds a gated event as its cursor's **head**,
   the child's entire stream queued behind it. Symptom was remote from the cause: "expected 2 child
   turn_ends, got 1" (turn 2 only arrived because it mounts a fresh cursor past the stranded head).
   Turn-0 child events are now exempt — which operator commands on a subagent needed too.
   Regression tests at both the gate and engine level.

2. **`code_mode/stubs.py` — defaulted model fields rendered as required.** A pydantic field with a
   default is optional at the boundary, but the generated `TypedDict` marked it required, so the
   sandbox type-checker rejected scripts that omitted a key the tool would have accepted. Now
   `NotRequired[...]`. One existing assertion updated, one new test.

3. **`tests/__init__.py` — the full suite was red before any of this work.** `tests/state/__init__.py`
   (needed, because those modules import each other relatively) made pytest put `tests/` on
   `sys.path`, and `tests/` contains `examples/`, which shadowed the repo's real top-level `examples`
   package. Symptom was remote: `ModuleNotFoundError: No module named 'examples.monty.workflow'` from
   the workflow sandbox, **only** in a full-suite run, and only once a `tests/state` module had been
   collected first. Verified on a pristine `HEAD` worktree: 330 passed clean vs 9 errors in the
   working tree. The file carries the explanation.

---

## 7. Known and accepted limitations

Already recorded in the design doc; listed here so a reviewer can confirm they are acceptable rather
than rediscover them.

- **`AgentStatus` is untouched** — the obvious next step is making some of it observable state so the
  UI stops polling, but that edits the runner's core and the Svelte client.
- **Subagent state is not on the canvas.** `state()` works in a subagent and the console keys its
  documents per agent, so a subagent's state renders beside the root's — but nothing in the *flow
  canvas* draws it.
- **Root must be a `HarnessState`** — `runner.state("plan", [])` is not supported.
- **No op coalescing** — `d.x = 1; d.x = 2` emits two ops.
- **Reentrancy during commit-time validators** (§4.1) — ~~needs a doc sentence~~ now has one, in
  "What is not done" in the design doc.

---

## 8. Gaps nobody has looked at

Honest list. None of these has been investigated at all.

- **No tests of the Svelte pane itself.** The projection (`agentState.ts`, `jsonPatch.ts`) is well
  covered; the component is not. Its one real bug — `structuredClone` throwing `DataCloneError` on
  Svelte's `$state` proxies, which made the pane silently fail to open — was found by driving a real
  browser, not by a test. A regression test for the *proxy* case now exists in `agentState.test.mjs`,
  but nothing renders the component.
- ~~**Benchmarks have not been re-run since sets were removed.**~~ Re-run, before and after the §3.2
  fix; numbers in §3.2. Every existing benchmark is unchanged within noise. One benchmark was added
  (`test_assign_a_large_list_of_models`) because the shape the recursive walk is worst on — assigning
  a whole large container at once — was not covered by any of them; it costs +17%.
- **Nothing has run against a long-lived worker.** Everything is the time-skipping test server. §3.1
  is precisely the class of defect that only appears there.
- **Secrets / PII (D5).** State documents go onto a durable event stream and are rendered in a
  browser. Nobody has asked whether an author might put a credential in agent state, or whether the
  layer owes them a redaction story. Much cheaper to answer now than after the first agent writes an
  API key into a `scratch` dict.

---

## 9. Reproducing things

```bash
# The layer + its end-to-end test. Green.
uv run pytest tests/state tests/harness/test_observable_state.py -q

# The whole suite (CI runs this).
uv run pytest -q

# The example, end to end against the time-skipping server.
uv run pytest tests/examples/monty/test_trip_board.py -q

# The console.
cd ui && npx vitest run && npx svelte-check --tsconfig ./tsconfig.json
# ui/dist is a COMMITTED build and CI fails if it drifts:
cd ui && npm run build
```

Driving the real stack locally — this is how the pane bug was found, and it is worth repeating for
anything UI-facing. **Use `temporal.local.toml` explicitly**; the `just` recipes source `.env.local`,
which may point at real Temporal Cloud:

```bash
temporal server start-dev --port 7233 --ui-port 8234
env -u TEMPORAL_PROFILE TEMPORAL_CONFIG_FILE=$PWD/temporal.local.toml \
    uv run --group examples temporal-agent-harness session-manager
env -u TEMPORAL_PROFILE TEMPORAL_CONFIG_FILE=$PWD/temporal.local.toml GEMINI_API_KEY=dummy \
    uv run --group examples python -m examples.monty.worker
env -u TEMPORAL_PROFILE TEMPORAL_CONFIG_FILE=$PWD/temporal.local.toml \
    uv run --group examples temporal-agent-harness serve examples/monty/agents.toml --port 8001
```

Then start a **Monty (Dynamic)** session (no API key needed), open the **AGENT STATE** pane from the
`+` on the pane rail, and send the script in `examples/monty/README.md`. If the session manager's
registry looks stale after editing `agents.toml`, terminate the `session-manager` workflow — it caches
the registry as its init arg.

A cross-process determinism check (the `PYTHONHASHSEED` sweep) lives at
`tests/state/test_property.py::_run_under_hash_seeds` and is the right tool for anything that might
read hash order.
