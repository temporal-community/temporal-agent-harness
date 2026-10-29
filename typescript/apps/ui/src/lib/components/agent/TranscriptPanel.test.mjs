// ABOUTME: Asserts the Logs pane holds still while the reader scrubs. Two things made it bounce.
// The pane used to OPEN the cursor's row on every cursor change and close the one before it, and
// those detail blocks are tall — 130..350px each on the mock run, so one tick of the scrubber moved
// up to 700px of content past the reader, and the follower then chased a row whose position had
// just moved. It also rendered its rows UNKEYED, while a scrub genuinely inserts into the middle
// of the list: rows arrive in frame order but render grouped by turn, and the two orders disagree,
// so a tick can move 63 rows that are already on screen. Both halves are pinned here, plus the
// data fact that makes the key load-bearing, because fixing either alone leaves the pane moving.
// It also pins the row's grammar: the chat trace's TraceRow, one per event.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import TranscriptPanel from "./TranscriptPanel.svelte";
import { jsonReplyScenarios } from "$lib/mock/jsonReplyScenarios.ts";
import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import { realisticQaScenario } from "$lib/mock/scenarios.ts";
import { buildReplayLog, rowCovers } from "$lib/state/replayLog.ts";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";

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

  /* The follower treats a cursor row the window has unmounted as still followed, so any re-run
     of the follow effect pulls a reader who scrolled away back to the cursor. Folding a head
     re-ran it while the lines were read tracked. */
  it("scrolls only because the cursor moved, never because a head folded", () => {
    for (const effect of [...nodes(ast.instance)].filter(
      (node) => node.type === "CallExpression" && text(node.callee).startsWith("$effect")
    )) {
      const untracked = [...nodes(effect.arguments)].filter(
        (node) => node.type === "CallExpression" && text(node.callee) === "untrack"
      );
      for (const node of nodes(effect.arguments)) {
        if (node.type !== "CallExpression" || text(node.callee) !== "linesFor") continue;
        assert.ok(
          untracked.some((call) => node.start > call.start && node.end < call.end),
          "an $effect may read the lines only inside untrack(), or folding a head scrolls the pane"
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

    /* Windowed: the virtual item's key is the turn number (getItemKey). */
    assert.equal(
      keyed.get("$turns.getVirtualItems()"),
      "item.key",
      "turn groups must be keyed, or a scrub rewrites them in place"
    );
    assert.match(source, /getItemKey: \(index\) => visibleGroups\[index\]\?\.turnNumber/);
    /* Lines are windowed too, keyed through WindowedRows' getItemKey on what getKey returns:
       a row's id, or its step's for a head. */
    assert.match(
      source,
      /<WindowedRows\s+items=\{linesFor\(group\)\}\s+getKey=\{\(line\) => line\.id\}/,
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

  /* --- and only the turns in view are drawn ----------------------------------- */
  it("draws a screenful of a 2,500-turn log, not all of it", () => {
    const { groups } = buildReplayLog(scaleScenario("transcript5k").frames);
    const html = render(TranscriptPanel, {
      props: { groups, activeTurnNumber: null, activeOrdinal: null }
    }).body;
    const drawn = html.match(/class="turn-group/g)?.length ?? 0;
    assert.ok(groups.length > 2_000, "sanity: the fixture is long");
    assert.ok(drawn > 0 && drawn < 20, `drew ${drawn} of ${groups.length} turns`);
  });

  /* Each turn's rows are a virtualizer of their own on the pane's one scroller, and a new one
     writes its starting offset back to that scroller as it mounts. Starting at 0, wheeling into
     a turn not yet drawn threw the reader back to the top, so the log could not be read past
     its first turn. */
  it("mounts a turn's rows at the scroller's own offset, not at the top", () => {
    const windowed = readFileSync(new URL("../primitives/WindowedRows.svelte", import.meta.url), "utf8");
    assert.match(windowed, /initialOffset: \(\) => scroller\?\.scrollTop \?\? 0,/);
  });

  /* Windowing whole turns drew every row of one in view: 533k DOM nodes for a dense turn. */
  it("draws a screenful of a 100,000-row turn, not all of it", () => {
    const { groups } = buildReplayLog(scaleScenario("dense50k").frames);
    const html = render(TranscriptPanel, {
      props: { groups, activeTurnNumber: null, activeOrdinal: null }
    }).body;
    const rows = groups.reduce((sum, group) => sum + group.rows.length, 0);
    const drawn = html.match(/class="log-line /g)?.length ?? 0;
    assert.ok(Math.max(...groups.map((group) => group.rows.length)) > 50_000, "sanity: one turn is dense");
    assert.ok(drawn > 0 && drawn < 60, `drew ${drawn} of ${rows} rows`);
  });
});

/* The filter used to let the cursor's row through whatever it was, so Errors on a run with
   none read "1/215 events" over a lone "Turn ended". A filter shows what matches it. */
describe("the Logs pane's filter", () => {
  const draw = (groups, filter, row) =>
    stripComments(
      render(TranscriptPanel, {
        props: { groups, filter, activeTurnNumber: row.turnNumber, activeRowId: row.id, activeOrdinal: row.ordinal }
      }).body
    );

  const isError = (row) => row.tone === "error" || row.actor === "error";
  /* The cursor on one row of every kind of event the run has. */
  const cursors = (rows) => [...new Map(rows.map((row) => [row.event, row])).values()];

  it("shows nothing under Errors on a run with no errors, wherever the cursor is", () => {
    for (const scenario of [jsonReplyScenarios().markdown, scaleScenario("timelineworst")]) {
      const { groups, rows } = buildReplayLog(scenario.frames);
      assert.ok(!rows.some(isError), "sanity: the run has no errors");
      for (const row of cursors(rows)) {
        const html = draw(groups, "error", row);
        assert.match(html, /No events match this filter\./, `cursor on ${row.event}`);
        assert.match(html, new RegExp(`>\\s*0/${rows.length.toLocaleString("en-US")} events\\s*<`), `cursor on ${row.event}`);
      }
    }
  });

  it("still shows every real error, and nothing else, wherever the cursor is", () => {
    for (const scenario of [scaleScenario("reply-failed"), scaleScenario("logsworst")]) {
      const { groups, rows } = buildReplayLog(scenario.frames);
      const errors = rows.filter(isError);
      assert.ok(errors.length > 0, "sanity: the run has errors");
      for (const row of cursors(rows)) {
        const html = draw(groups, "error", row);
        assert.match(html, new RegExp(`>\\s*${errors.length}/${rows.length.toLocaleString("en-US")} events\\s*<`), `cursor on ${row.event}`);
        assert.ok(html.includes(`id="log-row-${errors[0].id}"`), `cursor on ${row.event}: the first error is drawn`);
      }
    }
  });
});

/* The Logs rows wore their own chrome: the kind as a bordered chip and then as coloured caps, a
   boxed status chip, a boxed actor icon and a boxed ">". They are the chat trace's TraceRow now,
   one per event: a state glyph only where there is a state, the label in plain words, the tool in
   mono, the clock at the end. The fixture has done, awaiting and failed rows, so every glyph is
   drawn. */
describe("the Logs pane's rows", () => {
  const { groups } = buildReplayLog(scaleScenario("reply-failed").frames);
  const html = stripComments(
    render(TranscriptPanel, { props: { groups, activeTurnNumber: null, activeOrdinal: null } }).body
  );
  const chunks = (prefix) =>
    html.split(`<article id="${prefix}`).slice(1).map((chunk) => chunk.slice(0, chunk.indexOf("</article>")));
  const rows = chunks("log-row-");
  const heads = chunks("log-step-");
  const rule = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return source.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`))?.[1] ?? "";
  };

  it("draws every event as one TraceRow, a button only where it has details", () => {
    assert.ok(rows.length > 10, `sanity: drew ${rows.length} rows`);
    let toggles = 0;
    for (const row of rows) {
      assert.match(row, /class="trace-row\b/);
      const buttons = row.match(/<button\b[^>]*>/g) ?? [];
      assert.ok(buttons.length <= 1, "a collapsed row is at most one button");
      if (buttons.length === 0) {
        assert.doesNotMatch(row, /aria-expanded|disclosure/, "a row with nothing to open is no toggle");
        continue;
      }
      toggles++;
      assert.match(buttons[0], /class="trace-line disclosure/);
      assert.match(buttons[0], /aria-expanded="false"/);
      assert.match(buttons[0], /aria-controls="log-row-[^"]+-details"/);
    }
    assert.ok(toggles > 0 && toggles < rows.length, `some rows open and some do not (${toggles}/${rows.length})`);
  });

  it("boxes nothing and draws no icon but the state glyph and the disclosure chevron", () => {
    for (const row of rows) {
      assert.doesNotMatch(row, /class="chip\b|class="badge\b|kicker|line-kind|actor-icon/);
      const svgs = row.match(/<svg\b/g)?.length ?? 0;
      const allowed = (row.match(/class="state-glyph/g)?.length ?? 0) + (row.match(/disclosure-chevron/g)?.length ?? 0);
      assert.equal(svgs, allowed, "only StateGlyph and the chevron may draw an icon");
    }
    const glyphs = rows.join("").match(/class="state-glyph/g)?.length ?? 0;
    assert.ok(glyphs > 0 && glyphs < rows.length, `the glyph marks some rows' state, not every row (${glyphs}/${rows.length})`);
  });

  /* The failed call is signal, so its step is open: the head names the tool once, in mono, and
     the events under it are plain words. */
  it("writes the label in plain words, the tool in mono once on its head, and the clock in the tip", () => {
    const tool = rows.find((row) => />Tool started</.test(row)) ?? "";
    assert.match(tool, /<span class="trace-prefix[^"]*" dir="auto">Tool started<\/span>/);
    assert.doesNotMatch(tool, /<strong/, "an event under a head does not repeat the tool's name");
    assert.match(tool, /<span class="trace-meta[^"]*" dir="auto">(\+[^<]+)?<\/span>/, "the end of the row is an offset, or nothing");
    assert.match(tool, /data-tip="\d\d:\d\d:\d\d [AP]M/);
    const head = heads.find((chunk) => /aria-expanded="true"/.test(chunk) && /class="mono/.test(chunk)) ?? "";
    assert.match(head, /<strong class="mono[^"]*" dir="auto">\w+_\w+<\/strong>/);
    assert.match(rule(".log-line :global(.trace-prefix)"), /color: var\(--text-2\);/, "the label is words, not a muted prefix");
  });

  /* A row of any height but the one WindowedRows estimates makes the windowed list jump as it
     measures, and the rows no longer have a rule under them to make up the difference. */
  it("keeps a folded line at the height WindowedRows estimates for it, its focus ring inside", () => {
    const controlHeight = readFileSync(new URL("../../../app.css", import.meta.url), "utf8").match(/--control-height: (\d+)px;/)?.[1];
    assert.match(source, new RegExp(`const LINE_HEIGHT = ${controlHeight};`));
    assert.match(rule(".log-line :global(.trace-line)"), /min-height: var\(--control-height\);/);
    assert.doesNotMatch(rule(".log-line"), /border/);
    assert.match(source, /estimateSize=\{LINE_HEIGHT\}/);
    assert.match(rule(".log-line :global(.trace-line:focus-visible)"), /outline-offset: -2px;/);
  });

  it("draws signal bright and routine dim", () => {
    const failed = rows.find((row) => />Tool failed</.test(row)) ?? "";
    const started = rows.find((row) => />Turn started</.test(row)) ?? "";
    const classes = (chunk) => chunk.match(/^[^"]*" class="([^"]*)"/)?.[1] ?? "";
    assert.match(classes(failed), /\bsignal\b/, "an error is signal");
    assert.doesNotMatch(classes(failed), /\broutine\b/, "an error is never dimmed");
    assert.match(classes(started), /\broutine\b/, "a turn starting is routine");
    assert.doesNotMatch(classes(started), /\bsignal\b/);
    assert.match(rule(".log-line.signal :global(:is(.trace-prefix, strong))"), /color: var\(--text-1\);/);
    assert.match(rule(".log-line.routine :global(:is(.trace-prefix, strong))"), /color: var\(--text-3\);/);
  });

  /* The lines of each turn in the order drawn, heads and rows alike. */
  const turnLines = html
    .split('<section class="turn-group')
    .slice(1)
    .map((turn) => turn.split('<article id="log-').slice(1).map((chunk) => chunk.slice(0, chunk.indexOf("</article>"))));
  const meta = (line) => line.match(/<span class="trace-meta[^"]*" dir="auto">([^<]*)<\/span>/)?.[1] ?? null;

  /* Every row wrote the wall clock, most of them the same second as the row above. */
  it("writes each line's offset into its turn only where it changes, the clock in its tip", () => {
    const offsets = turnLines.flat().map(meta);
    assert.ok(offsets.every((offset) => offset != null && /^(\+\d[\dhms ]*)?$/.test(offset)), offsets.join("|"));
    assert.ok(offsets.some((offset) => offset === ""), "a line at the same offset as the one above draws none");
    assert.ok(offsets.some((offset) => offset !== ""), "a later line draws its offset");
    for (const lines of turnLines) {
      const shown = lines.map(meta).filter(Boolean);
      for (let i = 1; i < shown.length; i++) assert.notEqual(shown[i], shown[i - 1], "an offset is never repeated");
    }
    for (const line of turnLines.flat()) {
      assert.match(line, /class="trace-line[^"]*"[^>]*data-tip="\d\d:\d\d:\d\d [AP]M/, "the wall clock is in the tip");
    }
  });

  it("keeps every line to one line: the parts fade, the offset column holds its width", () => {
    assert.match(rule(".log-line :global(.trace-meta)"), /min-width: 5ch;\s*white-space: nowrap;/);
    for (const line of turnLines.flat()) {
      assert.doesNotMatch(line, /class="trace-(prefix|note)(?![^"]*fade-end)[^"]*"/, "a part that can clip fades");
    }
  });

  it("draws the turn's lifecycle quiet and its calls at full weight", () => {
    const classes = (label) => rows.find((row) => new RegExp(`>${label}<`).test(row))?.match(/^[^"]*" class="([^"]*)"/)?.[1] ?? "";
    const lifecycle = ["User message received", "Turn started", "Handler started", "Reply streaming", "Handler reply", "Turn ended"]
      .filter((label) => classes(label));
    assert.ok(lifecycle.length >= 2, `sanity: drew ${lifecycle.join(", ")}`);
    for (const label of lifecycle) assert.match(classes(label), /\broutine\b/, label);
    assert.doesNotMatch(classes("Tool started"), /\broutine\b/, "a tool's event is not lifecycle");
    const model = heads.find((chunk) => /<strong class="mono[^"]*" dir="auto">gemini/.test(chunk)) ?? "";
    assert.ok(model, "sanity: a model call is a head");
    assert.doesNotMatch(model.match(/^[^"]*" class="([^"]*)"/)?.[1] ?? "", /\broutine\b/);
  });

  /* "gemini-3.8-flash 4,097 tok · 5s" lost its end to an ellipsis at the narrowest pane. */
  it("shows a model call's tokens as its one figure and its duration in the tip", () => {
    const models = heads.filter((chunk) => /<strong class="mono[^"]*" dir="auto">gemini/.test(chunk));
    assert.ok(models.some((chunk) => /<span class="trace-figure[^"]*">[\d,]+ tok<\/span>/.test(chunk)));
    for (const chunk of models) {
      assert.doesNotMatch(chunk, /class="trace-note/, "the duration is not drawn beside the tokens");
      if (/trace-figure[^"]*">[\d,]+ tok</.test(chunk) && /took /.test(chunk)) assert.match(chunk, /data-tip="[^"]*· took \d/);
    }
    assert.ok(models.some((chunk) => /data-tip="[^"]*· took \d/.test(chunk)), "some model call's duration is in its tip");
  });
});

/* The turn header was a bold title and a clock with seconds, an ellipsized message, then
   "29s · 28 events · 2 model · 4 tools · 1 approvals · 10,295 tok" wrapping onto a fourth line
   at the narrowest pane. */
describe("the Logs pane's turn header", () => {
  const { groups } = buildReplayLog(scaleScenario("reply-failed").frames);
  const draw = (turns) => stripComments(render(TranscriptPanel, { props: { groups: turns, activeTurnNumber: null, activeOrdinal: null } }).body);
  const head = (body) => body.match(/<header class="turn-head[\s\S]*?<\/header>/g) ?? [];
  const html = draw(groups);
  const busy = head(html).find((chunk) => /turn-stats/.test(chunk)) ?? "";
  /* The text nodes, read out rather than tags stripped. */
  const visible = (chunk) => (chunk.match(/>[^<]*</g) ?? []).join(" ");
  const rule = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return source.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`))?.[1] ?? "";
  };

  it("draws the counts as icons with a number, each named and tipped in full", () => {
    for (const [words, icon] of [["3 model calls", "cpu"], ["6 tool calls", "wrench"], ["4 approvals", "shield-alert"], ["4 errors", "circle-x"]]) {
      assert.match(busy, new RegExp(`role="img" aria-label="${words}" data-tip="${words}"><svg[^>]*lucide-${icon}`), words);
    }
    assert.match(busy, /aria-label="9,810 tokens" data-tip="9,810 tokens">\s*<span class="fade-end[^"]*">9\.8K tok<\/span>/);
    assert.doesNotMatch(busy, /class="[^"]*chip/);
    for (const [, body] of source.matchAll(/\n\s*\.turn-(stats|figure)[^{]*\{([^}]*)\}/g)) {
      assert.doesNotMatch(body, /\bborder\b|\bbackground\b/, "a turn figure is not boxed");
    }
  });

  it("keeps the event count out of sight and in the duration's tip", () => {
    assert.doesNotMatch(visible(busy), /event/);
    assert.match(busy, /class="turn-duration[^"]*" data-tip="Took 17s · 40 events">\s*17s\s*</);
  });

  it("writes every count in its own number and draws no zero", () => {
    const [group] = groups;
    const one = { ...group, summary: { ...group.summary, durationSeconds: 0, eventCount: 1, modelCalls: 1, toolCalls: 0, approvals: 1, errors: 0, tokens: 0 } };
    const chunk = head(draw([one]))[0] ?? "";
    assert.match(chunk, /aria-label="1 model call"/);
    assert.match(chunk, /aria-label="1 approval"/);
    assert.doesNotMatch(chunk, /tool call|tokens|error|turn-duration/, "a zero count and a 0s turn draw nothing");
    assert.match(chunk, /aria-label="Turn \d+ summary, 1 event"/);
  });

  it("fades the message at its end, the whole of it in the tip", () => {
    assert.match(busy, /<div class="turn-preview[^"]*" data-tip="Plan 4 nights in Paris[^"]*">\s*<span class="fade-end[^"]*" dir="auto">Plan 4 nights in Paris/);
    assert.doesNotMatch(rule(".turn-preview"), /ellipsis/);
  });

  it("writes the start as a short time, the full date and time in its tip", () => {
    assert.match(busy, /<time class="turn-time[^"]*" datetime="\d{4}-[^"]+" data-tip="[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d\d:\d\d [AP]M">\s*\d{1,2}:\d\d [AP]M\s*<\/time>/);
  });

  it("never wraps: the line and the strip are nowrap, and only the token total may shrink", () => {
    assert.match(rule(".turn-line"), /white-space: nowrap;/);
    assert.match(rule(".turn-stats"), /flex-wrap: nowrap;[\s\S]*white-space: nowrap;/);
    assert.match(rule(".turn-figure"), /flex: none;/);
    assert.match(rule(".turn-figure.tokens"), /flex: 0 1 auto;\s*min-width: 0;/);
    assert.match(rule(".turn-duration,\n  .turn-time"), /flex: none;\s*font-variant-numeric: tabular-nums;/);
  });

  it("writes the toolbar's event count as plain text, not a badge", () => {
    assert.match(html, /<span class="event-count[^>]*>\s*\d+ events\s*<\/span>/);
    assert.doesNotMatch(source, /<Badge\b/);
    assert.doesNotMatch(rule(".event-count"), /\bborder\b|\bbackground\b/);
  });
});
