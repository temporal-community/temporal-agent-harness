// ABOUTME: Asserts the Logs pane holds still while the reader scrubs. Two things made it bounce.
// The pane used to OPEN the cursor's row on every cursor change and close the one before it, and
// those detail blocks are tall — 130..350px each on the mock run, so one tick of the scrubber moved
// up to 700px of content past the reader, and the follower then chased a row whose position had
// just moved. It also rendered its rows UNKEYED, while a scrub genuinely inserts into the middle
// of the list: rows arrive in frame order but render grouped by turn, and the two orders disagree,
// so a tick can move 63 rows that are already on screen. Both halves are pinned here, plus the
// data fact that makes the key load-bearing, because fixing either alone leaves the pane moving.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse } from "svelte/compiler";
import { describe, it } from "vitest";

import { realisticQaScenario } from "$lib/mock/scenarios.ts";
import { buildReplayLog, rowCovers } from "$lib/state/replayLog.ts";

const source = readFileSync(new URL("./TranscriptPanel.svelte", import.meta.url), "utf8");
const ast = parse(source, { modern: true });

/** Every AST node, once. Cheaper than a walker dependency for two questions. */
function* nodes(root) {
  const seen = new Set();
  const stack = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (node == null || typeof node !== "object" || seen.has(node)) continue;
    seen.add(node);
    if (typeof node.type === "string") yield node;
    for (const value of Object.values(node)) {
      if (value != null && typeof value === "object") stack.push(value);
    }
  }
}

const text = (node) => source.slice(node.start, node.end);

describe("the Logs pane during a scrub", () => {
  /* --- the cursor follows the row; it does not open it ---------------------- */
  it("never opens or closes a row because the cursor moved", () => {
    const effects = [...nodes(ast.instance)].filter(
      (node) =>
        node.type === "CallExpression" &&
        text(node.callee).startsWith("$effect")
    );
    assert.ok(effects.length > 0, "the pane should still follow the playhead from an $effect");

    for (const effect of effects) {
      for (const node of nodes(effect.arguments)) {
        const target =
          node.type === "AssignmentExpression"
            ? node.left
            : node.type === "UpdateExpression"
              ? node.argument
              : null;
        if (target == null) continue;
        assert.doesNotMatch(
          text(target),
          /expandedRows/,
          "an $effect must not write expandedRows: the cursor moves many times per frame, " +
            "and each write closes one ~300px detail block and opens another under the reader"
        );
      }
    }
  });

  /* Only a reader's own click may fold a row, so the pane keeps what they opened. The
     check above allows a write from anywhere that is not an effect; this names the one
     place that is meant to have it, so deleting the toggle is not a silent pass. */
  it("folds a row only from the reader's own click", () => {
    const writes = [...nodes(ast.instance)]
      .filter((node) => node.type === "AssignmentExpression" && text(node.left) === "expandedRows")
      .map((node) => node.start);
    assert.equal(writes.length, 1, "expandedRows should have exactly one writer");

    const toggle = [...nodes(ast.instance)].find(
      (node) => node.type === "FunctionDeclaration" && node.id?.name === "toggleRow"
    );
    assert.ok(toggle, "toggleRow should still exist");
    assert.ok(
      writes[0] > toggle.start && writes[0] < toggle.end,
      "the only writer of expandedRows should be toggleRow"
    );
  });

  /* --- the rows are keyed --------------------------------------------------- */
  it("keys the turn groups and the log rows", () => {
    const keyed = new Map(
      [...nodes(ast.fragment)]
        .filter((node) => node.type === "EachBlock")
        .map((node) => [text(node.expression), node.key == null ? null : text(node.key)])
    );

    assert.equal(
      keyed.get("visibleGroups"),
      "group.turnNumber",
      "turn groups must be keyed, or a scrub rewrites them in place"
    );
    assert.equal(
      keyed.get("group.rows"),
      "row.id",
      "log rows must be keyed, or a scrub rewrites every row after a folded reply run"
    );
  });

  /* --- and the key is load-bearing ------------------------------------------ */
  it("scrubs a list that reorders, not one that only grows", () => {
    const frames = realisticQaScenario.frames;
    const full = buildReplayLog(frames);

    /* The order the each-blocks actually render: rows grouped by turn, not the flat
       frame order the log builds them in. The two differ, which is the whole reason a
       key is needed — the flat list only ever grows at its end. */
    const rendered = (log) => log.groups.flatMap((group) => group.rows.map((row) => row.id));

    /* The tick that moves the most surviving rows. Zero here would mean the list only
       ever gains rows at its end, and the key above would be dead weight. */
    let worst = 0;
    let previous = null;
    for (let viewIndex = 1; viewIndex <= frames.length; viewIndex += 1) {
      const ids = rendered(buildReplayLog(frames.slice(0, viewIndex)));
      if (previous != null) {
        const place = new Map(ids.map((id, index) => [id, index]));
        const moved = previous.filter((id, index) => {
          const now = place.get(id);
          return now != null && now !== index;
        }).length;
        worst = Math.max(worst, moved);
      }
      previous = ids;
    }

    assert.ok(
      worst > 1,
      `one tick of the scrubber should move rows already on screen, but the most any ` +
        `tick moved was ${worst} — if grouping now follows frame order, revisit the keys above`
    );

    /* And the row the cursor lands on is a real row of that list, so following it is a
       lookup and not a guess. */
    const covered = full.rows.filter((row) => rowCovers(row, 1));
    assert.equal(covered.length, 1, "exactly one row should cover a given cursor index");
  });
});
