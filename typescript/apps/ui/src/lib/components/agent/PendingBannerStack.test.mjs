// ABOUTME: Pins the attention queue above the composer: every approval and callback the agent is
// blocked on in one stack, oldest first, under one "N need you" count. One request at a time in
// front of two peeking banners, a compact ⌃ 1/3 ⌄ pager in the queue's header that browses
// without deciding, and only the decision Chips in each card's footer. Callback questions with
// options become SchemaForm's radios or checkboxes, counted inside the card, and only a pointer
// pick on a single choice moves on by itself, never to sending.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it, vi } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import { answeredFronts, stackAnswer } from "../../motion/stackAnswer.ts";
import { toastClose } from "../../motion/toastClose.ts";
import { countPendingRequests } from "../../state/pendingRequests.ts";
import AgentChatPanel from "./AgentChatPanel.svelte";

const source = (file) => readFileSync(new URL(file, import.meta.url), "utf8");
const row = (i, event, extra) => ({
  id: `tool-${i}`, index: i, ordinal: i, turnNumber: 1, sourceTurnNumber: 1, turnId: "turn-1",
  timestamp: 1_700_000_000, event, actor: "tool", tone: "warn", label: event,
  toolId: `tool-${i}`, toolName: `tool_${i}`, input: {}, ...extra
});
const chatHtml = (logs) =>
  stripComments(
    render(AgentChatPanel, {
      props: { items: [], logs, agentLabel: "Planner", sessionId: "wf-root", onApproveTool: () => {}, onCallbackResult: async () => {} }
    }).body
  );
const approvals = (count) => chatHtml(Array.from({ length: count }, (_, i) => row(i, "tool_approval_requested")));
const button = (html, name) => html.match(new RegExp(`<button[^>]*aria-label="${name}"[^>]*>`))?.[0] ?? "";
const head = (html) => html.match(/<header class="stack-head[\s\S]*?<\/header>/)?.[0] ?? "";
const card = (html) => html.match(/<article class="pending-card[\s\S]*?<\/article>/)?.[0] ?? "";
const cardHead = (html) => card(html).match(/<header class="head[\s\S]*?<\/header>/)?.[0] ?? "";
const foot = (html) => card(html).match(/<div class="queue-foot[^"]*">[\s\S]*<\/div>/)?.[0] ?? "";
/* The footer's decisions in order, each a Chip with its icon ahead of the label. */
const decisions = (html) => [...foot(html).matchAll(/<button[^>]*>\s*<span class="chip-lead[^"]*"[^>]*>[\s\S]*?<\/span>\s*<span class="chip-label[^"]*">([^<]+?)<\/span>\s*<\/button>/g)].map((m) => m[1]);

describe("the attention queue", () => {
  const mixed = (...events) => chatHtml(events.map((event, i) => row(i, event)));

  it("merges approvals and callbacks into one queue, oldest first, under one count", () => {
    const html = mixed("tool_approval_requested", "callback_requested", "tool_approval_requested", "callback_requested");
    assert.equal(html.match(/role="region"/g)?.length, 1);
    assert.equal(html.match(/class="stack[ "]/g)?.length, 1, "no second stack");
    assert.match(html, /<button[^>]*aria-expanded="true"[^>]*>[\s\S]*?<span dir="auto">4 need you<\/span>[\s\S]*?<\/button>\s*<span class="breakdown[^"]*" dir="auto">\s*· 2 approvals · 2 responses<\/span>/);
    assert.match(html, /tool_0<\/strong>/);
    assert.match(html, /aria-live="polite" aria-atomic="true">Request 1 of 4</);
    assert.match(html, /aria-label="Next request"/);

    const calledFirst = mixed("callback_requested", "tool_approval_requested");
    assert.match(calledFirst, /aria-label="Response: tool_0"/, "the oldest is in front, whatever its kind");
    assert.match(calledFirst, /· 1 approval · 1 response</, "kinds keep one order, whichever is oldest");
    assert.match(mixed("callback_requested"), /1 needs you[\s\S]*?· 1 response</, "zero parts are left out");
    assert.doesNotMatch(mixed("callback_requested"), /class="breakdown[^>]*>[^<]*approval/);
  });

  it("shows the oldest in front of two plates, paged from the queue's header", () => {
    const html = approvals(250);
    assert.equal(html.match(/class="layer[^"]*" data-depth="[12]"/g)?.length, 2);
    assert.match(html, /tool_0<\/strong>/);
    assert.doesNotMatch(html, /tool_1<\/strong>/, "one approval at a time");
    assert.match(html, /aria-live="polite" aria-atomic="true">Approval 1 of 250</);
    assert.match(head(html), /class="head-pager[\s\S]*?class="sr-only[^"]*">Approval 1 of 250</, "the pager counts the queue, in its header");
    assert.match(button(head(html), "Previous approval"), /disabled/);
    assert.doesNotMatch(button(head(html), "Next approval"), /disabled/);
    assert.doesNotMatch(card(html), /data-pager|Next approval/, "no pager on the card");
    assert.doesNotMatch(html, />\s*Skip\s*<|aria-label="(Dismiss|Close)/, "Next skips; folding replaces dismiss");
    assert.match(html, /250 need you[\s\S]*?· 250 approvals/);
    assert.match(source("./PendingBannerStack.svelte"), /\.stack-head:hover \.head-pager,\s*\.stack-head:focus-within \.head-pager \{\s*opacity: 1;[\s\S]*?@media \(hover: none\) \{\s*\.head-pager \{\s*opacity: 1;/, "dimmed until reached for, always on touch");
  });

  it("holds only the decisions in the footer, primary first and destructive last", () => {
    const html = approvals(2);
    assert.deepEqual(decisions(html), ["Approve", "Always allow", "Reject"]);
    assert.equal(foot(html).match(/<button/g)?.length, 3, "nothing but the decisions");
    assert.match(foot(html).match(/<button[^>]*>(?:(?!<button)[\s\S])*Reject/)?.[0] ?? "", /class="chip [^"]*\berror\b/);

    const callback = chatHtml([row(0, "callback_requested", { outputSchema: { type: "string" } }), row(1, "tool_approval_requested")]);
    assert.deepEqual(decisions(callback), ["Send", "Decline"]);
    assert.equal(foot(callback).match(/<button/g)?.length, 2);
    assert.match(foot(callback), /<button[^>]*class="chip [^"]*"[^>]*type="submit"[^>]*>[\s\S]*?Send/, "Send is a submit Chip");
    assert.match(foot(callback).match(/<button[^>]*>(?:(?!<button)[\s\S])*Decline/)?.[0] ?? "", /class="chip [^"]*\berror\b[^"]*"[^>]*type="button"/, "Decline in the Reject red, never a submit");
    assert.doesNotMatch(source("./PendingCallbackCard.svelte"), /<button|\.send\b/, "no raw buttons on the card");
  });

  /* The plates were keyed by their depth slot, so paging never changed any plate's
     data-depth and the stack's shift, a CSS transition on it, never ran. */
  it("moves each plate a depth when paging, as a decision does", () => {
    const stack = source("./PendingBannerStack.svelte");
    assert.match(stack, /\{#each behind as row, depth \(keyOf\(row\)\)\}\s*<div class="layer" data-depth=\{depth \+ 1\}/, "a plate is its row");
    const expression = stack.match(/const behind = \$derived\(([\s\S]*?)\);\n/)?.[1];
    assert.ok(expression);
    const behind = new Function("rows", "index", "PEEKING", `return ${expression.replace(/!/g, "")};`);
    const depths = (rows, index) => Object.fromEntries(behind(rows, index, 2).map((row, d) => [row, d + 1]));
    const rows = ["a", "b", "c", "d", "e"];

    assert.deepEqual(depths(rows, 0), { b: 1, c: 2 });
    assert.deepEqual(depths(rows, 1), { c: 1, d: 2 }, "Next brings c forward");
    assert.deepEqual(depths(rows.slice(1), 0), depths(rows, 1), "the same as deciding a");
    assert.deepEqual(depths(rows, 0), { b: 1, c: 2 }, "Previous sends c back");
    assert.deepEqual(depths(rows, 4), { a: 1, b: 2 }, "the last wraps to the first, never fewer plates");
    assert.deepEqual(depths(["a", "b", "c"], 2), { a: 1, b: 2 });
  });

  /* Approving ran the paging slide inside a front banner that never moved: the answered row
     rose and blurred out in place, the next one rose from below, and the plate it had been
     vanished. */
  it("drops an answered banner below the stack as the plate behind comes forward, in one pass", () => {
    const stack = source("./PendingBannerStack.svelte");
    assert.match(stack, /\{#key answered\}\s*<div\s+class="layer"\s+data-depth="0"[^>]*\bout:answer\b/, "the front banner is a new one per answer");
    assert.match(stack, /const answered = \$derived\(countAnswered\(activeId, rows\.map\(keyOf\)\)\);/);

    const count = answeredFronts();
    const rows = ["a", "b", "c", "d"];
    assert.equal(count("a", rows), 0);
    assert.equal(count("b", rows), 0, "paging keeps the banner");
    assert.equal(count("a", rows), 0, "and paging back");
    assert.equal(count("b", rows.slice(1)), 1, "answering a replaces it with b, the plate one back");

    const classes = new Set(["layer"]);
    const answered = { inert: false, classList: { add: (name) => void classes.add(name), toggle: (name, on) => void (on ? classes.add(name) : classes.delete(name)) } };
    vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: (name) => (name === "--duration-settle" ? "350ms" : "") }));
    const drop = stackAnswer(answered);
    vi.unstubAllGlobals();
    assert.ok(classes.has("is-answered") && answered.inert, "dropping and unclickable before the next banner is styled");
    assert.equal(drop.duration, 350, "on the stack's clock");

    const rule = (selector) => stack.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} \\{([^}]*)\\}`))?.[1].replace(/\s+/g, " ").trim();
    assert.match(rule(".layer:global(.is-answered)"), /position: absolute;[\s\S]*z-index: 4; transform: translateY\(calc\(100% \+ var\(--step\)\)\);$/, "over the next banner, a whole banner and a step down, unfaded");
    assert.equal(rule('.layer:global(.is-answered) ~ .layer[data-depth="0"]'), rule('.layer[data-depth="1"]').replace(" z-index: 2;", ""), "the next banner starts where its plate was");
  });

  /* The decision's own resolution, and the burst it set off, land while the answered banner
     is still dropping; neither may restart the drop or the advance, nor strand the class. */
  it("keeps the drop and the advance through live updates landing mid-transition", () => {
    const count = answeredFronts();
    count("a", ["a", "b", "c"]);
    assert.equal(count("b", ["b", "c"]), 1);
    assert.equal(count("b", ["b", "c", "d", "e"]), 1, "a burst arriving keeps the advancing banner");
    assert.equal(count("b", ["b", "c", "d", "e"]), 1, "so does the same queue drawn again");
    assert.equal(count("c", ["c", "d", "e"]), 2, "a second answer drops the advancing one in turn");

    const classes = new Set(["layer"]);
    const answered = { inert: false, classList: { add: (name) => void classes.add(name), toggle: (name, on) => void (on ? classes.add(name) : classes.delete(name)) } };
    vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: () => "350ms" }));
    const drop = stackAnswer(answered);
    vi.unstubAllGlobals();
    drop.tick(1, 0);
    assert.ok(classes.has("is-answered"), "its first tick keeps it dropping");
    drop.tick(0.4, 0.6);
    assert.ok(classes.has("is-answered"));
    answered.inert = false;
    drop.tick(1, 0);
    assert.ok(!classes.has("is-answered"), "a banner Svelte keeps goes back in flow");
    assert.match(source("./PendingBannerStack.svelte"), /tick: into \? undefined : \(t: number\) => node\.classList\.toggle\("is-leaving", t < 1 \|\| node\.inert\)/, "and so does a row paged straight back");
  });

  /* At the chat's 320px minimum, Reject wrapped under Approve and Always allow. */
  it("keeps the decisions on one row in a narrow card, as icons named and tipped by their words", () => {
    for (const name of ["Approve", "Always allow", "Reject"]) {
      assert.match(button(foot(approvals(1)), name), new RegExp(`data-tip="${name}"`), name);
    }
    assert.match(button(foot(chatHtml([row(0, "callback_requested", { outputSchema: { type: "string" } })])), "Decline"), /data-tip="Decline"/);

    const card = source("./PendingCard.svelte");
    assert.match(card, /\.pending-card \{\s*container: pending-card \/ inline-size;/);
    const narrow = card.match(/@container pending-card \(width < 320px\) \{[\s\S]*?\n {2}\}\n/)?.[0] ?? "";
    assert.match(narrow, /:global\(\.chip\.interactive\) \{\s*width: var\(--control-height\);\s*justify-content: center;\s*padding: 0;/, "a square icon button, 28px");
    assert.match(narrow, /:global\(\.chip-label\) \{\s*position: absolute;[^}]*clip-path: inset\(50%\);/, "the word is read out, not drawn");
    assert.match(narrow, /@media \(pointer: coarse\) \{[^}]*width: 32px;\s*height: 32px;/, "32px on touch");
    assert.match(card, /@container pending-card \(width >= 320px\) \{\s*\.queue-foot :global\(\.chip\[data-tip\]::before\),\s*\.queue-foot :global\(\.chip\[data-tip\]::after\) \{\s*content: none;/, "no tip repeating a drawn word");
    assert.match(card, /<strong class="fade-end"/, "a long tool name fades rather than wrapping the header");
    assert.match(card, /\.head strong \{\s*flex: 1 1 0;/, "and gives way before the time");
  });

  it("puts the optional reason in the approval card's header, out of the way", () => {
    const html = approvals(1);
    assert.match(cardHead(html), /aria-label="Reject with a reason"[^>]*aria-pressed="false"/);
    assert.doesNotMatch(card(html), /class="reject-reason/, "no reason field until asked for");
    assert.match(source("./PendingCard.svelte"), /\.pending-card:hover \.action,\s*\.pending-card:focus-within \.action/);
  });

  /* Opening the reason grew the row at once while the banner's height followed a frame late
     on a 350ms transition, so the sticky footer was pinned over the new field and slid off it
     as the card rose; on a short pane focusing the field also scrolled the queue 161px. */
  it("opens the reason in the sticky footer without moving the room the chat keeps or scrolling", () => {
    const chat = source("./AgentChatPanel.svelte");
    const stack = source("./PendingBannerStack.svelte");
    assert.match(chat, /querySelector<HTMLInputElement>\("\.reject-reason"\)\?\.focus\(\{ preventScroll: true \}\)/);
    assert.match(stack, /<header class="stack-head" \{@attach measureHead\}>/, "measured from the header, never the card");
    assert.match(stack, /style:height=\{paging && height !== null \? `\$\{height\}px` : null\}/, "at rest the banner is its row's height, in the same frame");
    assert.match(stack, /onintrostart=\{\(\) => \(paging = true\)\}\s*onintroend=\{\(\) => \(paging = false\)\}/, "held to a measured height only while a row slides in");
    assert.match(
      source("./PendingCard.svelte"),
      /<div class="queue-foot">\s*\{#if status\}<div class="status">\{@render status\(\)\}<\/div>\{\/if\}\s*\{@render foot\(\)\}\s*<\/div>/,
      "the reason rides the sticky footer, in view on a card taller than the queue"
    );
  });

  /* The banner draws the waiting-on-you glyph and names the kinds, so a card repeating it was
     noise; with no glyph of its own, the card says its kind in its name. */
  it("draws one header for every kind: the name in mono flush at the start, the time, no glyph", () => {
    const cases = [
      [approvals(1), "Approval: tool_0"],
      [chatHtml([row(0, "callback_requested", { outputSchema: { type: "string" } })]), "Response: tool_0"],
      [
        stripComments(render(AgentChatPanel, {
          props: { items: [], logs: [row(0, "callback_requested")], agentLabel: "Planner", sessionId: "wf-root", clientToolStatuses: { "tool-0": { state: "answering" } } }
        }).body),
        "Client tool: tool_0, answered by the console"
      ]
    ];
    for (const [html, name] of cases) {
      assert.match(card(html), new RegExp(`^<article class="pending-card[^"]*" aria-label="${name}"`));
      assert.match(cardHead(html), /^<header class="head[^"]*">\s*<strong[^>]*>tool_0<\/strong>\s*<span class="when[^"]*">Turn 1 · /);
      assert.doesNotMatch(cardHead(html), /state-glyph/);
    }
    assert.doesNotMatch(source("./PendingCard.svelte"), /StateGlyph/);
    assert.match(source("./PendingCard.svelte"), /\.head strong \{[^}]*font-family: var\(--font-mono\);/);
  });

  it("hides the pager for one request, and draws nothing for none", () => {
    assert.doesNotMatch(approvals(1), /Next approval|head-pager/);
    const empty = approvals(0);
    assert.doesNotMatch(empty, /role="region"|class="layer|stack-toggle|class="breakdown/);
  });

  it("keeps the approval shield off a queue of questions only", () => {
    const shield = (html) => /stack-toggle[\s\S]*?class="state-glyph/.test(head(html));
    assert.ok(shield(approvals(1)));
    assert.ok(!shield(mixed("callback_requested", "callback_requested")), "two responses, no decision");
    assert.ok(shield(mixed("callback_requested", "tool_approval_requested")));
  });

  it("draws a callback with no output schema as a whole card, never an empty frame", () => {
    const html = chatHtml([row(0, "callback_requested", { outputSchema: undefined, input: { question: "Which one?" } })]);
    const front = html.match(/data-depth="0"[\s\S]*$/)?.[0] ?? "";
    assert.match(front, /<article class="pending-card[^"]*" aria-label="Response: tool_0"/);
    assert.match(front, /Which one\?/);
    assert.match(front, /<button[^>]*form="[^"]+"[^>]*class="chip [^"]*"[^>]*type="submit"/, "the footer's Send submits the card's form");
  });

  it("decides through the same handlers, with an optional reason, and folds only on Escape", () => {
    const chat = source("./AgentChatPanel.svelte");
    assert.match(chat, /resolveApproval\(event, request, true\)/);
    assert.match(chat, /resolveApproval\(event, request, true, true\)/);
    assert.match(chat, /onclick=\{\(event\) => void resolveApproval\(event, request, false\)\}/, "Reject is one click");
    assert.match(chat, /label="Reject with a reason"/);
    assert.match(chat, /label=\{`Approval: \$\{title\}`\}/, "the approval card is named by its kind and tool");
    assert.match(source("../../state/agentRun.svelte.ts"), /reason: approved \? null : reason\?\.trim\(\) \|\| "Rejected in chat\."/);
    const stack = source("./PendingBannerStack.svelte");
    assert.doesNotMatch(stack, /"Enter"|onpointerenter/, "no key decides, nothing opens on hover");
    assert.match(stack, /onkeydowncapture=\{fold\}/);
    assert.match(stack, /if \(event\.key !== "Escape" \|\| !open\) return;/);
    assert.match(stack, /card\?\.focus\(\{ preventScroll: true \}\)/, "a decision parks focus on the card");
    assert.match(chat, /onempty=\{\(\) => composerInput\?\.focus\(\)\}/, "the last answer hands focus back to the composer");
    assert.doesNotMatch(stack, /\.focus\(\)[^;]*;\s*\}\s*\);?\s*\$effect\(/, "nothing focuses the stack on arrival");
  });

  it("drops the slide, the roll, the exit and the plate transforms under reduced motion", () => {
    const card = source("./PendingBannerStack.svelte");
    assert.match(card, /function move[\s\S]*?if \(prefersReducedMotion\.current\) return \{ duration: 0 \};/);
    assert.match(source("../../motion/toastClose.ts"), /if \(prefersReducedMotion\.current\) return \{\};/, "the queue leaves at once when reduced");
    assert.match(source("./QueuePager.svelte"), /if \(prefersReducedMotion\.current\) return \{ duration: 0 \};/);
    const reduced = card.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n  \}/)?.[1] ?? "";
    assert.match(reduced, /^\s*\.layer,\s*\.queue,\s*\.fold \{\s*transition: none !important;\s*transform: none !important;\s*filter: none !important;\s*\}/, "restacking, open, fold and close are instant, with no rise or blur");
    assert.match(reduced, /\.layer\[data-depth="1"\] \{\s*inset: calc\(var\(--plates-height\) - var\(--step\)\) 3% 0;/, "still a visible stack, by position rather than by transform");
    assert.match(reduced, /\.layer\[data-depth="2"\] \{\s*inset: calc\(var\(--plates-height\) - var\(--step\) \* 2\) 6% 0;/);
    assert.match(reduced, /\.viewport,\s*\.caret,\s*\.head-pager \{\s*transition: none !important;/);
  });

  it("opens and closes the whole queue like a toast, and folds its card into the count the same way", () => {
    const stack = source("./PendingBannerStack.svelte");
    const html = approvals(2);
    const fold = html.match(/<div id="([^"]+)" class="fold is-open[^"]*"[^>]*>/);
    assert.match(html, /<div class="queue is-open[^"]*">\s*<header class="stack-head/, "it arrives open, header and card together");
    assert.ok(fold, "the card is open under the count");
    assert.doesNotMatch(fold[0], /inert/);
    assert.match(head(html), new RegExp(`aria-controls="${fold[1]}"`), "the count chip opens and folds it");
    assert.doesNotMatch(approvals(0), /class="queue[ "]/, "nothing to open for none");

    assert.match(stack, /\.queue,\s*\.fold \{\s*--haze: 2px;\s*opacity: 0;\s*transform: translateY\(var\(--rise\)\) scale\(var\(--motion-scale\)\);\s*filter: blur\(var\(--haze\)\);/, "hidden is faded, blurred, smaller and off");
    assert.match(stack, /\.queue \{[^}]*--rise: 16px;/, "the queue rises into place");
    assert.match(stack, /@starting-style \{\s*\.queue\.is-open \{\s*opacity: 0;\s*transform: translateY\(var\(--rise\)\) scale\(var\(--motion-scale\)\);\s*filter: blur\(var\(--haze\)\);/, "appears from its hidden state");
    assert.match(stack, /\.queue\.is-open,\s*\.fold\.is-open \{\s*opacity: 1;\s*transform: translateY\(0\) scale\(1\);\s*filter: blur\(0\);\s*transition-duration: var\(--duration-settle\);/, "shows sharp, on the settle clock");
    assert.match(stack, /\.queue \{[^}]*transition:\s*opacity var\(--duration-pane\) var\(--ease-out\),\s*transform var\(--duration-pane\) var\(--ease-out\),\s*filter var\(--duration-pane\) var\(--ease-out\);/, "and hides on the quicker pane clock");
    assert.match(stack, /class=\{\["fold", \{ "is-open": open \}\]\} inert=\{!open\}/, "folds and unfolds by its class, inert while folded");
    assert.match(stack, /\.fold \{[^}]*grid-template-rows: 0fr;[^}]*overflow: clip;/, "folded, it gives up its height");
    assert.match(stack, /\.fold \{[^}]*transition:\s*grid-template-rows var\(--duration-pane\)/, "its height folds with it");
    assert.match(stack, /\.fold\.is-open \{\s*grid-template-rows: 1fr;\s*\}/);
    /* Folded, the card rested 16px down, so its clipped box hung 10px past the queue's foot and
       the queue's scroller showed an empty scrollbar beside the count. */
    assert.match(stack, /\.fold \{[^}]*--rise: calc\(var\(--gap-md\) \* -1\);/, "it folds up into the count, never past the queue's foot");
    assert.match(source("./AgentChatPanel.svelte"), /\.pending-stack \{[^}]*overflow-y: auto;/, "the queue scrolls only a card taller than the room");
    assert.doesNotMatch(source("./AgentChatPanel.svelte"), /\.pending-stack \{[^}]*scrollbar-gutter/, "no gutter held for a folded queue");
    assert.match(stack, /<div class="queue is-open" out:toastClose>/);
    assert.doesNotMatch(source("../../motion/toastClose.ts"), /focus/, "closing moves no focus");
    assert.match(stack, /onclick=\{\(\) => \(open = !open\)\}/, "the chip only toggles; focus stays on it");

    /* Approving a Code Mode script answered the last request, and its host calls asked for ten
       more before the queue finished closing. Svelte kept the closing node, but its `is-open`
       was gone, so the queue was back with all ten and drawn at opacity 0 until a reload. */
    const classes = new Set(["queue", "is-open"]);
    const queue = { inert: false, classList: { toggle: (name, on) => void (on ? classes.add(name) : classes.delete(name)) } };
    vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: () => "220ms" }));
    const closing = toastClose(queue);
    vi.unstubAllGlobals();
    assert.equal(queue.inert, true, "unclickable as it goes");
    assert.equal(closing.duration, 220, "on the close clock");
    closing.tick(0.6, 0.4);
    assert.ok(!classes.has("is-open"), "closes by its class");
    queue.inert = false;
    closing.tick(1, 0);
    assert.ok(classes.has("is-open"), "a burst landing mid-close opens the same node again");

    const burst = approvals(10);
    assert.match(burst, /<div class="queue is-open[^"]*">/);
    assert.match(burst, /10 need you[\s\S]*?· 10 approvals/, "every request of the burst");
    assert.match(burst, />Approval 1 of 10</);
    assert.match(cardHead(burst), /tool_0<\/strong>/, "the oldest in front");
  });
});

describe("callback questions with options", () => {
  const callback = (outputSchema) => chatHtml([row(0, "callback_requested", { outputSchema, input: { question: "Which?" } })]);

  it("asks a single choice as radios with a free-text row, nothing preselected", () => {
    const html = callback({ type: "string", enum: ["Oregon", "Maine"] });
    assert.equal(html.match(/type="radio"/g)?.length, 3);
    assert.doesNotMatch(html, /type="radio"[^>]*checked/);
    assert.match(html, /<fieldset class="choices/, "through SchemaForm's list");
    assert.match(html, /Something else…/);
    const cardSource = source("./PendingCallbackCard.svelte");
    assert.match(cardSource, /<SchemaForm[^>]*choices="list"/);
    assert.doesNotMatch(cardSource, /type=\{[^}]*"radio"|<fieldset|\.choice\b/, "no card-local radio markup");
  });

  it("asks several questions one at a time, counted inside the card", () => {
    const html = callback({
      type: "object",
      properties: { cabin: { title: "Cabin", enum: ["Economy"] }, extras: { title: "Extras", type: "array", items: { enum: ["Lounge"] } } },
      required: ["cabin"]
    });
    const form = card(html).match(/<form[\s\S]*?<\/form>/)?.[0] ?? "";
    assert.match(form, /class="sr-only[^"]*" aria-live="polite">Question 1 of 2</);
    assert.match(form, />Question&nbsp;|>Question /, "spelled out where it is seen");
    assert.match(button(form, "Next question"), /<button/);
    assert.doesNotMatch(head(html), /Question \d|Next question/, "the header counts requests, not questions");
    assert.doesNotMatch(html, /Extras/, "the next question waits its turn");
    assert.deepEqual(decisions(html), ["Send", "Decline"]);
    assert.match(source("./PendingCallbackCard.svelte"), /if \(pointerPick && input\.type === "radio" && !input\.hasAttribute\("data-other"\) && step < last\)/, "only a pointer pick on a single choice, never from the last");
  });
});

describe("requests from subagents that reuse a tool id", () => {
  it("resolves each agent's request on its own, and the header counts what the queue shows", () => {
    const rows = [
      row(0, "tool_approval_requested", { toolId: "t1", workflowId: "wf-child-a" }),
      row(1, "tool_approval_requested", { toolId: "t1", workflowId: "wf-child-b" }),
      row(2, "tool_approval_requested", { toolId: "p1" }),
      row(3, "tool_approval_resolved", { toolId: "t1", workflowId: "wf-child-a" })
    ];
    assert.deepEqual(countPendingRequests(rows, "wf-root"), { approvals: 2, responses: 0 });
    assert.match(head(chatHtml(rows)), /<span dir="auto">2 need you<\/span>/);
  });
});
