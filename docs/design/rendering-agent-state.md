# Rendering agent state

How a workflow author says what a piece of observable state *means*, so that a client can draw
it as a checklist or a table instead of as JSON — without the author writing any UI, and
without the harness knowing what a checklist looks like.

**Status:** design; not built. Builds on
[`observable-agent-state.md`](observable-agent-state.md), which is.

## The gap this fills

Observable state put the document on the wire: one snapshot, then RFC 6902 ops, every one
addressed by JSON Pointer. The console folds them and draws the result as a JSON tree —
because a JSON tree is all it can honestly draw. Nothing on the wire says that `remaining` is
a checklist, that `flights` are rows of one table, or that `description` is the line that names
a trip.

The author knows every one of those things. There is no channel for them to travel down.

The values travel, and where each change landed travels. What a field *is* does not — the
document carries its JSON type and nothing else.

## What the author writes

```python
class Booking(HarnessState):
    kind: BookingKind
    confirmation_code: str
    detail: Annotated[str, Display(as_="heading")]
    price_usd: float


class Trip(HarnessState):
    description: Annotated[str, Display(as_="heading")]
    remaining: Annotated[list[TripTask], Display(as_="checklist")]
    flights: Annotated[list[Booking], Display(as_="table")]
    hotels: Annotated[list[Booking], Display(as_="accordion")]
```

That is the entire opt-in: one `Annotated[...]` per field worth saying something about, chosen
from a closed list the harness declares. No parameters, no UI, no second document. A field with
nothing to say stays a plain annotation and renders the way everything else does.

Two things in that example are worth reading twice.

`flights` and `hotels` hold the **same item type rendered two ways** — a table of rows and a
stack of expandable summaries. So the hint that decides an item's shape is the container's, not
the item's; `Booking` never has to know which of its use sites it is standing in.

`Booking.detail` is marked `heading`, and that one word does three jobs: the title of a
`Booking` card, the collapsed line of the `hotels` accordion, and the label of a tab if anyone
ever renders it as tabs. Which is why `accordion` needs no `summary=` parameter to say what the
collapsed line should be — it asks the item what names it. That is a line worth holding: the
moment one term takes an argument, they all start to, and the vocabulary stops being a list of
words and becomes a layout language.

## Why the hint sits on the field

The alternative is a view document — a component tree, bound to the state by pointer, declared
separately. It is more expressive: arbitrary layout, fields reordered and composed at will.

It also **drifts**, and that is disqualifying. A view declared elsewhere is a second artifact
that has to be kept in step with the model by hand: add a field and it silently does not appear;
rename one and a binding dangles. A hint on the field cannot drift, because there is nowhere for
it to drift to. Add a field and it shows up with a default rendering; delete it and the hint
goes with it. The repo already prefers this shape — `PANE_KINDS` is derived from `PANE_META`
"so a new kind is in both by existing."

The cost is real and accepted: presentation is coupled to the schema, so this cannot express a
layout that disagrees with the shape of the data. For a state document being read, that is not
a layout anyone wants.

## The vocabulary

Grouped by what each term may be applied to, because that grouping is also the validation rule.

| applies to | terms |
|---|---|
| `list[...]`, `set[...]` | `table`, `checklist`, `accordion` |
| scalars | `heading`, `status` |
| any field | `hidden` |

Six words, and five of them are there because the Monty trip board asks for them by name:
`Trip.description` and `Booking.detail` → `heading`, `Trip.status` → `status`,
`remaining` → `checklist`, `flights` → `table`, `hotels` → `accordion`. Six of that board's
fourteen fields earn a hint, which is about the right hit rate for a vocabulary meant to be
reached for rather than filled in.

`hidden` is the exception, and justified differently: nothing asks for it yet, but the
alternative to having it is an author deleting a field from their state to keep a pane readable,
which is a worse trade than one unused word.

Each term is defined by what the reader gets, never by how it is drawn:

- **`table`** — uniform records, one row each, fields as columns.
- **`checklist`** — items that are done or not; the not-done ones are the point.
- **`accordion`** — one line per item, detail on demand.
- **`heading`** — this field names the thing that contains it.
- **`status`** — one of a small closed set of values, read as a state.
- **`hidden`** — not worth the reader's attention by default.

Nothing in that list carries a dimension, a colour, or a pixel, and nothing should. A term that
can only be honoured by one kind of surface is not a term, it is CSS.

### Left out of v0 on purpose

`chips`, `progress`, `code`, `markdown`, `duration`, `bytes`, `timestamp`, `count`, `bars`,
`sparkline`.

Each is plausible and none is needed yet. A vocabulary designed against one state model and one
renderer is how three terms nobody wanted get in and the two everyone needs get missed — so v0
is what the trip board asks for plus one escape hatch, and the seventh term waits for a second
state or a second renderer to ask for it.

`chips` is the near miss, and the clearest illustration of that rule: a `set[str]` rendered as a
row of tokens is the most obvious hint on the list, and no state in this repo holds a `set[str]`.
The PoC demo state did, and the harness supports the type, so it will be back — but not before
something is actually holding one.

`order_by` is excluded by rule rather than by priority: it cannot be said in one word.

Two of the deferred terms are worth remembering for a different reason: each is blocked on
something other than demand. `bars` describes a whole model rather than one field, so it cannot
be said until class-level hints exist — it is the case that will eventually motivate them.
`sparkline` needs the op stream rather than the document, because it plots one scalar across the
run; the trajectory of `/tokens_used` is already on the wire and no renderer of the current value
can draw it.

Neither should ever be *inferred* from shape. `TokenUsage` has four numeric fields, and
`UsageReading.svelte` goes out of its way to stop a reader adding them up — cached is a slice of
input, thought is usually a slice of output. Auto-charting an all-numeric model would produce a
picture that is arithmetically confident and wrong. `Coordinates(x, y)` fails the same way.
Shape is a coincidence; a reading is a declaration.

## What a renderer is promised, and what it is free to do

**The hint is advisory.** A renderer that ignores every one of them still shows the whole
document, correctly. This is the invariant that keeps the raw tree honest and keeps the
vocabulary from becoming load-bearing.

**An unimplemented term and an absent one are the same case.** A renderer that has never heard
of `accordion` draws that field the way it draws everything else. So there is no fallback chain
to specify, no version to negotiate, and a renderer is never wrong for being incomplete — only
plainer.

**How a term is realised belongs entirely to the renderer.** `accordion` is an accordion in
HTML, expandable rows in a TUI, a drill-in on a phone. The definition travels; the widget does
not.

**Reader-local controls are the renderer's own business.** A checklist of two hundred items
wants collapsing whatever the author declared, and that is the renderer exercising its
affordances, not overriding a declaration. Three layers, and they do not conflict: the author
says what the data means, the renderer decides how to accomplish that on its surface, the reader
adjusts within the renderer.

**`hidden` is presentation, not redaction.** It says "not worth attention by default", never
"must not be seen". A field that must not reach a client does not belong in the state. Worth
writing down before someone reaches for it as a privacy mechanism, because it will not hold.

## Defaults

A `HarnessState` with nothing said about it renders as a **card**, at every depth. `runner.state()`
can be called more than once, so a panel holds several documents, and each one needs its own
delimiter titled with its `state_id` — and once a card is the right answer for a root and for a
list item, making it the answer by default everywhere is one rule instead of three.

What keeps that from producing four nested borders around one booking is that card **weight**
attenuates with depth, not that the component changes: a full card at depth 0, a hairline and no
fill further down. The console's tokens already ladder that way
(`--surface-1`/`--surface-2`, `--border`/`--border-strong`). One component, one rule, and nesting
depth stays legible at a glance.

### Three tiers, most specific wins

A card is the last resort, not a law. Three things can have an opinion about how a model renders,
and they resolve in one direction:

1. **The container's hint.** `table` yields rows, `accordion` yields summary lines. A hint on the
   container is an instruction about item shape, so it replaces whatever the item class would have
   preferred — the use site knows something about *this* use that the class cannot.
2. **The model class's own hint**, once class-level hints exist. Consulted when the container has
   no opinion, which is the default stack, and when the model is a root or a single nested field.
3. **A card.**

So `flights` and `hotels` hold the same `Booking` and read as two different things, and a
`Booking` that declared `bars` for itself still renders as a row inside `flights`. Same
specificity ordering as CSS: the narrower context wins, and the broader declaration is not wrong
for being overridden.

One thing the tiers do not reach: the per-state frame the panel draws — the title bar carrying the
`state_id`, the owning agent and the version. A panel showing three documents has to label which
is which whatever any model declares, so that frame belongs to the renderer. A class-level hint
governs what goes *inside* it.

## On the wire

One new field, and no new event type:

```python
class AgentStateSnapshot(StreamEvent[...]):
    state_id: str
    version: int
    value: dict[str, Any]
    # `model_json_schema()`, hints included. NOT `schema`: that name shadows an attribute of
    # pydantic's own BaseModel and the model emits a warning at class definition.
    value_schema: dict[str, Any] | None
```

The schema is published once, with the snapshot, because that is exactly when a consumer needs
it. `Display` carries itself into that schema by implementing pydantic's
`__get_pydantic_json_schema__`, adding an `x-harness-display` key to the field it annotates — so
`model_json_schema()` produces the hints with no post-processing pass, and nothing in the state
layer has to know the vocabulary exists.

Riding in the schema rather than in a payload of its own buys two things beyond the hints. Field
docstrings come along, so a renderer with no hint support still has labels and tooltips. And the
schema is a standard, self-describing artifact, so a second renderer needs no harness-specific
knowledge to start.

## Validation

A hint applied to a field it cannot describe — `table` on a `str`, `heading` on a `list` — is a
`StateSchemaError` at class-definition time, naming the field and the fix, exactly as
`_check_field_types` already does for a field the harness cannot own. `_check_annotation`
already walks every annotation and classifies it as scalar, container, or model, so the display
check is a few lines inside a walk that is already happening, paid once per class rather than
once per render.

This is the same bargain the rest of the state layer makes: the error fires at the line that got
it wrong, not at the client that could not draw it.

## Why this is not A2UI

[A2UI](https://a2ui.org/) is the obvious neighbour and was the starting point for this design,
so it is worth saying plainly why it is not the answer here.

A2UI has the **agent ship a component list**, bound by JSON Pointer into a data model it also
ships. The fit looked uncanny at first — A2UI's bindings are RFC 6901 pointers, and an
observable-state document is precisely a "plain JSON object" data model addressed by RFC 6901
pointers, so the two halves appear to snap together. Two things stop it.

Its data transport is strictly weaker than the one already here. `updateDataModel{path, value}`
has upsert semantics: no array insert (nothing that means `add /trips/-`), no add-versus-replace
distinction, and `null` overloaded as delete, so a field cannot be set to null. Translating the
RFC 6902 stream into it would lose information for no gain.

And A2UI exists for *interactive* agent-driven interfaces — Button, CheckBox, TextField, actions,
`sendDataModel` echoing the model back from the renderer. State here is owned by the agent and
single-writer by design; a client never mutates it. Strip the interaction and most of the spec
is inert, while the part that remains points the wrong way: A2UI's agent declares structure,
whereas here the agent labels its own data and renderers decide structure. Those are different
architectures, and the one in this document has the better drift properties.

A2UI stays available downstream: schema plus hints could be compiled into an A2UI component list
for a client that speaks it. Native clients read schema plus hints directly and do strictly
better, because they also have the op stream — which is where the marking, the scrubbing, and
the history of a pointer come from.

## In the console

The state pane stacks every registered state as a card rather than showing one at a time behind
a chip selector, so nothing is hidden behind a click — which matters more now that the fold keys
per agent, and a parent plus two subagents each holding a `plan` is three documents that need
telling apart. The chip row becomes a jump; the version/commits reading moves into each card.

The raw JSON tree stays, permanently, as a toggle. A styled view can hide a field by omitting it;
the tree is the thing that cannot. And because the schema and the hints are both on the wire, the
pane can say so out loud — "this view shows 9 of 12 fields" — and distinguish the two reasons a
field is missing: `hidden`, which the author chose, from a field this renderer simply has nothing
to draw yet. A view that cannot tell those apart has no business replacing the tree.

## What is not done

- **Class-level hints** — tier 2 above, which nothing populates yet. `Trip` would declare once how
  it renders where its container has no opinion, instead of every use site repeating it, and
  `bars` needs them to exist at all. There is an obvious home:
  `__pydantic_init_subclass__` already inspects the class at definition time. The precedence rule
  is written for three tiers so that adding them changes no other decision here.
- **The ten deferred terms**, and any term that needs an argument.
- **Anything derived from the op stream.** `sparkline` is the first candidate and needs a second
  input the schema does not carry: which pointers to keep history for.
- **A second renderer.** Until one exists, "renderers decide" is an assertion rather than a
  property, and the vocabulary should stay marked unstable.

## Reference

- Depends on: [`observable-agent-state.md`](observable-agent-state.md)
- Would live in: `temporal_agent_harness/harness/state/display.py`, exported from
  `harness.state`; validated in `base.py` alongside `_check_field_types`
- Wire: a `value_schema` field on `AgentStateSnapshot` in `agent_protocol/events.py`
- Console: `ui/src/lib/components/agent/AgentStatePanel.svelte` and a schema-to-render
  compiler beside `ui/src/lib/state/agentState.ts`
