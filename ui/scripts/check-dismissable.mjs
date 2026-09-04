// ABOUTME: Asserts the dismissable attachment's layer logic — which layer answers Escape and an
// outside press, what counts as inside, and where focus lands when a layer goes away. No framework
// and no DOM package: the module touches five DOM surfaces, so those five are stubbed here and the
// logic is exercised directly, which is the only way this can run in the check suite at all — the
// three call sites (the transport's now-card, the pane launcher, the session menu) each need a
// browser to drive, and what would break is shared and lives here.
//   node ui/scripts/check-dismissable.mjs

import assert from "node:assert/strict";

/* --- the smallest DOM the module needs ---------------------------------- */
class FakeNode {
  constructor(tag = "div", parent = null) {
    this.tagName = tag;
    this.parent = parent;
    this.classes = new Set();
    this.isConnected = true;
    this.focused = false;
  }
  contains(other) {
    for (let n = other; n; n = n.parent) if (n === this) return true;
    return false;
  }
  closest(selector) {
    const want = selector.replace(/^\./, "");
    for (let n = this; n; n = n.parent) if (n.classes.has(want)) return n;
    return null;
  }
  focus() {
    global.document.activeElement = this;
  }
}

const listeners = new Map();
global.Node = FakeNode;
global.Element = FakeNode;
global.HTMLElement = FakeNode;
global.window = {
  addEventListener: (type, fn, capture) => listeners.set(`${type}:${capture}`, fn),
  removeEventListener: (type, _fn, capture) => listeners.delete(`${type}:${capture}`)
};
const body = new FakeNode("BODY");
global.document = { body, activeElement: body };

const fire = (type, event) => listeners.get(`${type}:true`)?.(event);
const key = (k) => fire("keydown", { key: k, stopPropagation() {} });
const press = (target) => fire("pointerdown", { target });

const { dismissable } = await import("../src/lib/state/dismissable.svelte.ts");

/* --- 1. Escape and outside press dismiss; inside press does not ---------- */
let closed = 0;
const anchor = new FakeNode("BUTTON", body);
anchor.classes.add("anchor");
const panel = new FakeNode("SECTION", body);
const inner = new FakeNode("BUTTON", panel);
const elsewhere = new FakeNode("DIV", body);

let teardown = dismissable({ ondismiss: () => closed++, keep: ".anchor" })(panel);
press(inner);
assert.equal(closed, 0, "a press inside the layer must not dismiss it");
press(anchor);
assert.equal(closed, 0, "a press on the anchor must not dismiss — it toggles for itself");
press(elsewhere);
assert.equal(closed, 1, "a press outside dismisses");
key("Enter");
assert.equal(closed, 1, "only Escape dismisses");
key("Escape");
assert.equal(closed, 2, "Escape dismisses");
teardown();

/* --- 2. Only the innermost layer answers --------------------------------- */
let outer = 0;
let innerCount = 0;
const dialog = new FakeNode("DIALOG", body);
const menu = new FakeNode("DIV", dialog);
const downOuter = dismissable({ ondismiss: () => outer++ })(dialog);
const downInner = dismissable({ ondismiss: () => innerCount++ })(menu);
key("Escape");
assert.deepEqual([outer, innerCount], [0, 1], "Escape closes the innermost layer only");
press(elsewhere);
assert.deepEqual([outer, innerCount], [0, 2], "an outside press closes the innermost layer only");
downInner();
key("Escape");
assert.deepEqual([outer, innerCount], [1, 2], "the outer layer answers once the inner one is gone");
downOuter();

/* --- 3. The listener is torn down with the last layer -------------------- */
assert.equal(listeners.size, 0, "no layers left means no window listeners left");

/* --- 4. Focus goes back to the opener, but only if the layer had it ------ */
document.activeElement = anchor;
const stranding = dismissable({ ondismiss: () => {} })(panel);
document.activeElement = inner; // focus moved into the layer
stranding();
assert.equal(document.activeElement, anchor, "focus returns to whatever opened the layer");

document.activeElement = anchor;
const moved = dismissable({ ondismiss: () => {} })(panel);
document.activeElement = elsewhere; // the reader clicked something else
moved();
assert.equal(document.activeElement, elsewhere, "focus is not yanked back from a deliberate move");

/* --- 5. `keep` also covers a companion surface the layer hangs off -------
   The transport's case, and the reason `keep` is one option and not two: an anchor
   that toggles for itself and a surface that stays usable want the same answer, so a
   selector matching a whole footer behaves exactly as one matching a button. */
let ignored = 0;
const footer = new FakeNode("FOOTER", body);
footer.classes.add("step-controller");
const scrubber = new FakeNode("INPUT", footer);
const down = dismissable({ ondismiss: () => ignored++, keep: ".step-controller" })(panel);
press(scrubber);
assert.equal(ignored, 0, "scrubbing the transport under the card leaves the card open");
press(elsewhere);
assert.equal(ignored, 1, "a press anywhere else still dismisses");
down();

console.log("check-dismissable: layer stacking, what counts as inside, and focus return hold");
