// ABOUTME: Asserts the one rule Chip and IconButton both obey about caller props — callers own
// description, the primitive owns behaviour. `{...rest}` is spread first, so an attribute the
// primitive is answerable for is written after it and wins, and a behaviour a caller legitimately
// needs is a named prop instead. This is the check that fails when someone moves a spread back
// after the attributes, or re-seals IconButton: the composer's send button is a real submit button
// only because IconButton can say so, and Enter-to-send is native implicit submission, which
// silently stops working the moment that button stops being type="submit". No type checker sees it.
// `class` is the documented exception, and the second half of this check: it is destructured out
// of `rest` and merged, so a caller can add one without being able to take the primitive's away.
//   node ui/scripts/check-primitive-props.mjs

import assert from "node:assert/strict";
import "./svelteLoader.mjs";
import { render } from "svelte/server";

const Chip = (await import("../src/lib/components/primitives/Chip.svelte")).default;
const IconButton = (await import("../src/lib/components/primitives/IconButton.svelte")).default;

const html = (Component, props) => render(Component, { props }).body;

/* --- the primitive owns behaviour ---------------------------------------- */

// The send button. Without this, Enter in the composer submits nothing.
assert.match(html(IconButton, { label: "Send", type: "submit" }), /type="submit"/);

// ...and nothing else becomes a submit button by accident.
assert.match(html(IconButton, { label: "Reset" }), /type="button"/);

// Chip has no reason to submit anything, so it does not take `type` and a caller
// cannot reach past it to say otherwise. This is the assertion that fails if the
// spread moves back after the attributes.
assert.match(
  html(Chip, { label: "Approve", onclick: () => {}, type: "submit" }),
  /type="button"/,
  "Chip let rest choose its type"
);

// The `rel` that makes target="_blank" safe is the primitive's to keep.
const link = html(Chip, { label: "Docs", href: "https://example.com", rel: "" });
assert.match(link, /rel="noreferrer noopener"/, "a caller stripped the tabnabbing guard");

/* --- callers own description --------------------------------------------- */

// Both primitives let a descriptive attribute through, which is the whole point
// of the rest spread and the half IconButton used to refuse: an icon-only
// popover trigger needs aria-expanded, and until now had to be a Chip to get it.
for (const [name, Component, props] of [
  ["Chip", Chip, { label: "Usage", onclick: () => {} }],
  ["IconButton", IconButton, { label: "Usage" }]
]) {
  const described = html(Component, { ...props, "aria-expanded": true });
  assert.match(described, /aria-expanded="true"/, `${name} swallowed aria-expanded`);
}

// The tooltip is where the two legitimately differ, and the rule says which
// wins: an icon-only button always has one, so IconButton names it `tip` and
// therefore owns `data-tip`. Chip shows its own label and leaves it to callers.
assert.match(html(IconButton, { label: "Usage", tip: "Open" }), /data-tip="Open"/);
assert.match(
  html(IconButton, { label: "Usage", "data-tip": "Open" }),
  /data-tip="Usage"/,
  "rest reached past the tip prop"
);
assert.match(html(Chip, { label: "Usage", onclick: () => {}, "data-tip": "Open" }), /data-tip="Open"/);

/* --- a button that is not a toggle does not claim to be one --------------- */

assert.doesNotMatch(html(IconButton, { label: "Send" }), /aria-pressed/);
assert.match(html(IconButton, { label: "Follow", pressed: false }), /aria-pressed="false"/);
assert.match(html(IconButton, { label: "Follow", pressed: true }), /aria-pressed="true"/);

/* --- except `class`, which merges ---------------------------------------- */

// The one attribute the spread order got wrong. A caller adding a class has
// nothing to say about `type`, so `class` is pulled out of `rest` and merged
// with the primitive's own instead of being beaten by it. Without this the
// session popover's refresh button cannot be an IconButton and still spin, and
// three adoption attempts hand-rolled the CSS again rather than reshape a
// primitive from a call site.
// Read as a class LIST, not as a substring: `chip-label` on the inner span
// contains "chip" and would vouch for a root element that had lost it.
const rootClasses = (markup) => (markup.match(/class="([^"]*)"/)?.[1] ?? "").split(/\s+/);

for (const [name, Component, own, props] of [
  ["Chip", Chip, "chip", { label: "Tag" }],
  ["IconButton", IconButton, "icon-button", { label: "Refresh" }]
]) {
  const classes = rootClasses(html(Component, { ...props, class: "spinning" }));
  assert.ok(classes.includes("spinning"), `${name} dropped the caller's class: ${classes}`);
  assert.ok(classes.includes(own), `${name} let the caller's class replace its own: ${classes}`);
}

// ...and a call that carries a class is still a call `rest` is spread from, so
// merging must not reopen the door the spread order closed.
const dressedChip = html(Chip, {
  label: "Approve",
  onclick: () => {},
  class: "wide",
  type: "submit"
});
assert.match(dressedChip, /\bwide\b/);
assert.match(dressedChip, /type="button"/, "a class in the same call let rest choose Chip's type");

const dressedButton = html(IconButton, {
  label: "Refresh",
  class: "spinning",
  "aria-pressed": "true"
});
assert.match(dressedButton, /\bspinning\b/);
assert.doesNotMatch(
  dressedButton,
  /aria-pressed/,
  "a class in the same call let rest invent a toggle state"
);

console.log("primitive props: chip and icon button agree");
