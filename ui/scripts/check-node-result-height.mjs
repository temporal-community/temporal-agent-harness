// ABOUTME: Asserts that a graph node's geometry does not depend on how much text it holds. The flow
// re-renders on every streamed delta while someone scrubs, so a node sized by its content moves
// under the cursor: measured on the run this came from (1,805 reply deltas), the Output card stepped
// 96px -> 137px as the reply crossed four lines, and the reply was clipped to 4% of itself with no
// way to reach the rest. Both halves are checked here — what flowProjection RESERVES for a card, and
// what AgentStateNode's Result region can RESOLVE to — because a fix to either one alone leaves the
// node moving.
//   node ui/scripts/check-node-result-height.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile } from "svelte/compiler";
import "./libAlias.mjs";

/* Dynamic, because a static import is resolved before the alias hook above has
   run. Same reason the other checks in here reach for their modules this way. */
const { buildAgentGraph } = await import("../src/lib/state/flowProjection.ts");

const frame = (event, data) => ({ event, data: { type: event, ...data } });

/* One turn that uses every node kind the runtime row can hold, with a reply of a
   given length. Only the reply length varies between runs. */
function run(replyChars) {
  const text = "x".repeat(replyChars);
  return [
    frame("turn_started", { user_message: "plan something", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("thought_summary", { delta: { content: { text: "thinking ".repeat(40) } } }),
    frame("tool_requested", {
      tool_id: "t1",
      tool_name: "search",
      tool_input: { query: "q".repeat(200) }
    }),
    frame("tool_end", { tool_id: "t1", tool_name: "search", tool_output: "o".repeat(900) }),
    frame("subagent_started", {
      subagent_id: "s1",
      workflow_id: "wf-1",
      agent_key: "researcher"
    }),
    frame("model_interaction_ended", { model: "gpt-5.1" }),
    ...(replyChars > 0 ? [frame("reply_delta", { text })] : []),
    frame("reply", { output: { text } })
  ];
}

/* What the graph is actually laid out from. Positions and dimensions only — a
   node's TEXT is expected to differ, its geometry is not. */
function geometry(graph) {
  return graph.nodes.map((node) => ({
    id: node.id,
    x: node.position.x,
    y: node.position.y,
    size: node.data.size ?? "default",
    width: node.data.nodeWidth ?? null,
    height: node.data.nodeHeight ?? null,
    boundaryWidth: node.data.boundaryWidth ?? null,
    boundaryHeight: node.data.boundaryHeight ?? null
  }));
}

// --- reserved geometry is invariant to reply length -------------------------
// A reply arrives one delta at a time, so every length between these is a frame
// the user scrubs through. If geometry tracks length, the node moves all the way.
{
  const lengths = [0, 1, 40, 400, 2487, 3515, 50_000];
  const baseline = geometry(buildAgentGraph(run(lengths[0])));

  assert.ok(baseline.length > 1, "the fixture should build a real graph");
  for (const length of lengths.slice(1)) {
    assert.deepEqual(
      geometry(buildAgentGraph(run(length))),
      baseline,
      `a ${length}-char reply moved the graph; node geometry must not track text length`
    );
  }

  /* The Output node in particular: the one the complaint was about. It must
     carry a reserved height at all, or the card is sized by whatever it holds. */
  const output = baseline.find((node) => node.id === "output");
  assert.ok(output, "the fixture should produce an Output node");
  assert.ok(
    typeof output.height === "number" && output.height > 0,
    "the Output card must reserve a height rather than growing to fit its reply"
  );
}

// --- an empty reply reserves the same card as a full one --------------------
// The region used to render only once there was text in it, so the card grew the
// moment a turn started streaming — a jump every single turn.
{
  const empty = geometry(buildAgentGraph(run(0))).find((n) => n.id === "output");
  const full = geometry(buildAgentGraph(run(3515))).find((n) => n.id === "output");
  assert.deepEqual(empty, full, "an empty reply slot must reserve the same card as a full one");
}

// --- the rendered region cannot resolve to a content-driven height ----------
/* ponytail: ceiling = this reads the component's compiled CSS and rejects the
   values that make a used height depend on content. It does not run a layout
   engine, so it proves the height CANNOT be content-driven, not the pixel it
   lands on — and it does not execute the component's own guard on whether the
   region renders at all, because AgentStateNode imports @xyflow/svelte, which
   does not resolve under plain node (ERR_UNSUPPORTED_DIR_IMPORT: it ships
   extensionless directory imports for a bundler to fix up). Both gaps are
   measured in ui/tools/nodereply/probe.mjs, which needs a browser and a dev
   server and so cannot live in `just app-check`. Upgrade path = move these
   assertions there if a headless browser ever becomes a dependency of this UI. */
{
  const source = readFileSync(
    new URL("../src/lib/components/flow/AgentStateNode.svelte", import.meta.url),
    "utf8"
  );
  const { css } = compile(source, { generate: "client", css: "external" });
  const code = css?.code ?? "";

  /* Every declaration block whose selector mentions the Result region. */
  const blocks = [...code.matchAll(/([^{}]*\.result-body[^{}]*)\{([^}]*)\}/g)].map((m) => ({
    selector: m[1].trim(),
    body: m[2]
  }));
  assert.ok(blocks.length > 0, "AgentStateNode should still style .result-body");

  const declared = (property) =>
    blocks
      .flatMap((block) => [...block.body.matchAll(new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]*)`, "g"))])
      .map((m) => m[1].trim());

  /* A definite height. `var(--x, 132px)` counts — the fallback is a length and
     the custom property is only ever set to one in this file. */
  const heights = declared("height");
  assert.ok(heights.length > 0, ".result-body must declare a height, or it is sized by its content");
  for (const value of heights) {
    assert.ok(
      /(^|\s|,)\d+(\.\d+)?(px|rem|em)\b/.test(value),
      `.result-body height "${value}" resolves to no fixed length, so the box grows with its text`
    );
    assert.ok(
      !/\b(auto|fit-content|max-content|min-content)\b/.test(value),
      `.result-body height "${value}" is content-driven`
    );
  }

  /* A cap is the defect, not the fix: under a max-height the box is exactly as
     tall as its text right up until the cap, which is the 96px -> 137px step. */
  assert.deepEqual(
    declared("max-height"),
    [],
    ".result-body must not be capped by max-height — under a cap the box still grows with its text"
  );

  /* Clipped is fine; unreachable is not. */
  const overflow = [...declared("overflow-y"), ...declared("overflow")];
  assert.ok(
    overflow.some((value) => /\b(auto|scroll)\b/.test(value)),
    "the text past the fixed height must stay reachable by scrolling"
  );

  /* Neither the height nor the follow-scroll may be animated: the region is
     rewritten on every delta, and a transition restarted every few milliseconds
     never arrives — it reads as lag. */
  for (const block of blocks) {
    const transition = [...block.body.matchAll(/(?:^|;)\s*transition\s*:([^;]*)/g)].map((m) => m[1]);
    for (const value of transition) {
      assert.ok(
        !/\b(height|all)\b/.test(value),
        `.result-body must not animate its height (${block.selector}: ${value.trim()})`
      );
    }
  }
  assert.ok(
    !/scroll-behavior\s*:\s*smooth/.test(code),
    "the follow-scroll must jump, not glide: it fires once per streamed delta"
  );
}

console.log(
  "check-node-result-height: reserved geometry ignores reply length, " +
    "the Result region cannot resolve to a content-driven height"
);
