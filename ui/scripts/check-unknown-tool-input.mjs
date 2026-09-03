// ABOUTME: Asserts the UI keeps the distinction the backend went to the trouble of making on
// tool_requested: tool_input={} is a fact ("the model called this tool with no arguments") and
// tool_input=null is an absence of one ("the model streamed arguments, and we could not parse
// them"). Collapsing them renders a lost payload as a confident claim that nothing was passed —
// the same error as an unreplayable run showing $0.0000 instead of an em dash. Both directions are
// pinned, because only checking the null side leaves the reverse open: an argument-less call
// drifting into claiming "unknown" about a call we watched take no arguments is equally a lie, and
// on the backend the equivalent reverse assertion is what caught that there was nothing catching it.
//   node ui/scripts/check-unknown-tool-input.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import "./libAlias.mjs";
import "./svelteLoader.mjs";
import { render } from "svelte/server";

/* ponytail: ceiling = @xyflow/svelte is stubbed, so the connection handles AgentStateNode draws
   are not exercised here. It ships extensionless directory imports that only a bundler resolves,
   and teaching node the whole of vite's resolution to render two decorative dots is a worse trade
   than naming the gap. The component under test is the real one — only this dependency is faked,
   and it contributes nothing to the text asserted below. Upgrade path = a shared resolver hook
   beside svelteLoader.mjs, once a second check needs a flow component. */
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier !== "@xyflow/svelte") return nextResolve(specifier, context);
    const stub =
      "export function Handle() {}\n" +
      'export const Position = { Left: "left", Right: "right", Top: "top", Bottom: "bottom" };';
    return { url: `data:text/javascript,${encodeURIComponent(stub)}`, shortCircuit: true };
  }
});

const { formatLogValue, UNKNOWN_TOOL_INPUT } = await import("../src/lib/state/logValue.ts");
const { buildReplayLog } = await import("../src/lib/state/replayLog.ts");
const { buildAgentGraph } = await import("../src/lib/state/flowProjection.ts");
const AgentStateNode = (await import("../src/lib/components/flow/AgentStateNode.svelte")).default;

/* The note has to actually read as unknown rather than as a value. Pinned here so that
   rewording it stays a deliberate act and every assertion below keeps agreeing with the
   panels — that single shared spelling is the reason the formatter was lifted out of the two
   components instead of edited twice. */
assert.match(UNKNOWN_TOOL_INPUT, /^— /, "the unknown marker is a leading em dash, as formatCost uses");
assert.ok(
  UNKNOWN_TOOL_INPUT.length > 20,
  "an em dash on its own does not tell a reader why the arguments are missing"
);

/* --- the formatter both panels import ------------------------------------ */

/* undefined and null used to be one branch (`value == null`), which is the bug. They are
   different questions: undefined is "this row has no such field", null is "the field is here
   and its value is unknown". */
assert.equal(formatLogValue(undefined), "", "an absent field still has nothing to say");
assert.equal(
  formatLogValue(null),
  UNKNOWN_TOOL_INPUT,
  "unknown arguments must say so, not render blank"
);

/* The reverse direction, and the half that is easy to leave open: a call that genuinely took no
   arguments must keep saying so. If this ever returns the em-dash note, the UI has started
   claiming ignorance about a call whose arguments we know were empty. */
assert.equal(formatLogValue({}), "{}", "a call with no arguments took no arguments");
assert.notEqual(formatLogValue({}), UNKNOWN_TOOL_INPUT, "{} is a fact, not an unknown");
assert.equal(formatLogValue({ q: "cats" }), '{\n  "q": "cats"\n}');

/* --- the same distinction, carried by the real projections --------------- */

let offset = 0;
const meta = (turn = 1) => ({
  agent_id: "agent",
  turn_id: "turn-1",
  turn_number: turn,
  timestamp: 1_700_000_000 + offset,
  resume_offset: `${offset++}`
});

const toolRequested = (toolId, toolInput) => ({
  event: "tool_requested",
  data: {
    ...meta(),
    type: "tool_requested",
    tool_id: toolId,
    tool_name: "search",
    tool_input: toolInput
  }
});

/* Both shapes in one run, so a projection that flattens them cannot pass by coincidence of
   being handed only one. */
const FRAMES = [
  { event: "turn_started", data: { ...meta(), type: "turn_started", user_message: "hi" } },
  toolRequested("call_UNKNOWN", null),
  toolRequested("call_BARE", {})
];

const rows = buildReplayLog(FRAMES).rows;
const rowFor = (toolId) => {
  const row = rows.find((candidate) => candidate.toolId === toolId);
  assert.ok(row, `no log row built for ${toolId}`);
  return row;
};

/* Item 3's half: the row type is nullable, so null has to survive the projection. A truthiness
   guard anywhere on the way in (`if (frame.data.tool_input)`) would turn it into undefined here
   and the panel would render blank again with every assertion above still passing. */
assert.equal(rowFor("call_UNKNOWN").input, null, "null must reach the row as null, not undefined");
assert.deepEqual(rowFor("call_BARE").input, {}, "an argument-less call must reach the row as {}");

assert.equal(formatLogValue(rowFor("call_UNKNOWN").input), UNKNOWN_TOOL_INPUT);
assert.equal(formatLogValue(rowFor("call_BARE").input), "{}");

/* --- what the flow node actually renders --------------------------------- */

/* SSR the real component rather than asserting on the projection alone: `JSON.stringify(null)`
   is the string "null", which is a plausible-looking value on a node and reads as something the
   model sent. */
const graph = buildAgentGraph(FRAMES);
const nodeDataFor = (toolId) => {
  const node = graph.nodes.find((candidate) => candidate.data?.toolId === toolId);
  assert.ok(node, `no flow node built for ${toolId}`);
  return node.data;
};

const nodeHtml = (toolId) => render(AgentStateNode, { props: { data: nodeDataFor(toolId) } }).body;

const unknownHtml = nodeHtml("call_UNKNOWN");
assert.ok(
  unknownHtml.includes("stream ended before they could be parsed"),
  "the flow node should explain that the arguments are unknown"
);
assert.ok(unknownHtml.includes("—"), "the flow node should carry the unknown marker");
assert.ok(
  !/>\s*null\s*</.test(unknownHtml),
  'the flow node rendered the literal text "null" as if it were a value'
);

/* And again the reverse: the node for a genuinely argument-less call still shows {}. */
const bareHtml = nodeHtml("call_BARE");
assert.ok(bareHtml.includes("{}"), "an argument-less call should still render as {}");
assert.ok(
  !bareHtml.includes("stream ended before they could be parsed"),
  "an argument-less call must not be described as unknown"
);

/* --- one formatter, not two ---------------------------------------------- */

/* The two panels carried byte-identical private copies of formatLogValue and both needed this
   same change. Nothing but this assertion stops a third copy reappearing and quietly going back
   to `value == null`, at which point every assertion above still passes and the panel is wrong. */
for (const path of [
  "../src/lib/components/agent/TranscriptPanel.svelte",
  "../src/lib/components/agent/AgentChatPanel.svelte"
]) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  assert.ok(
    source.includes('import { formatLogValue } from "$lib/state/logValue"'),
    `${path} should import the shared formatter`
  );
  assert.ok(
    !/function formatLogValue\b/.test(source),
    `${path} has grown its own copy of formatLogValue again`
  );
}

console.log(
  "check-unknown-tool-input: null reads as unknown, {} still reads as no arguments, one formatter"
);
