// ABOUTME: Pins the chat pane's CSS that no SSR render can see. Each grid in the chat pins its one
// column to `minmax(0, 1fr)`: left implicit, the column is `auto` and grows to its widest child's
// min-content — an approval card, a 600px reply table — which carried the whole message column
// 75px past a 403px pane. A wide table has to scroll inside its own wrapper instead.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = (file) => readFileSync(new URL(file, import.meta.url), "utf8");
const chat = source("./AgentChatPanel.svelte");
const markdown = source("../chat/MarkdownMessage.svelte");

/** The declarations of the rule whose selector line is exactly `selector`. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`\\n\\s*${escaped} \\{([^}]*)\\}`));
  assert.ok(match, `no rule for ${selector}`);
  return match[1];
}

describe("the chat pane stays inside its pane", () => {
  it("pins every single-column grid to the pane's width", () => {
    for (const selector of [
      ".chat-shell",
      ".pending-stack",
      ".activity-feed"
    ]) {
      assert.match(rule(chat, selector), /grid-template-columns: minmax\(0, 1fr\);/, selector);
    }
    assert.match(rule(source("./PendingCard.svelte"), ".pending-card"), /grid-template-columns: minmax\(0, 1fr\);/);
    assert.match(
      rule(markdown, ".markdown-message"),
      /grid-template-columns: minmax\(0, 1fr\);/
    );
  });

  it("scrolls a wide table inside its wrapper", () => {
    const wrap = rule(markdown, ".markdown-message :global(.md-table-wrap)");
    assert.match(wrap, /max-width: 100%;/);
    assert.match(wrap, /overflow-x: auto;/);
  });

  /* `width: 100%; min-width: 520px` measured a two-cell table 520px wide in a 355px bubble, so
     every tiny table carried a scrollbar and a stretched last column. */
  it("sizes a small table to its content, with no scrollbar until it overflows", () => {
    const wrap = rule(markdown, ".markdown-message :global(.md-table-wrap)");
    const table = rule(markdown, ".markdown-message :global(table)");
    assert.match(wrap, /width: fit-content;/, "the border hugs the table");
    assert.doesNotMatch(wrap, /overflow-x: scroll|scrollbar-gutter/, "no track until a table overflows");
    assert.doesNotMatch(table, /width:/, "the table takes its content's width");
  });

  it("draws the replay strip opaque, over the messages and out of flow", () => {
    const strip = rule(chat, ".replay-strip");
    assert.match(strip, /position: absolute;/);
    assert.match(strip, /background: var\(--surface-0\);/, "the chat's own background, no alpha");
    assert.match(strip, /z-index: 2;/);
    assert.match(rule(chat, ".agent-chat"), /background: var\(--surface-0\);/);
    assert.match(rule(chat, ".message-list"), /isolation: isolate;/);
  });

  /* One line at every width, so a collapsed step keeps one height: the note gives way first,
     then the name, and the status word and the turn's figures never do. */
  it("keeps a trace step on one line, cutting its note before its name", () => {
    assert.match(chat, /<TraceRow\b/, "the steps are TraceRows, which pin their own line");
    assert.match(rule(chat, ".turn-tokens"), /flex: none;/);
    assert.doesNotMatch(chat, /flex-wrap: wrap;\s*row-gap: 2px;/, "a nested step no longer wraps");
  });

  /* A settled header read "Ran 1 tool · 3.1s total 667 tok" and then nothing, which looked
     broken: the total sits at the row's end with the timestamp, and the label is what gives. */
  it("pushes a turn's token total to the end of its header and cuts the label first", () => {
    assert.match(rule(chat, ".activity-heading"), /white-space: nowrap;/);
    const tokens = rule(chat, ".turn-tokens");
    assert.match(tokens, /margin-inline-start: auto;/);
    assert.match(tokens, /flex: none;/);
    assert.match(tokens, /font-variant-numeric: tabular-nums;/);
    const label = rule(chat, ".activity-heading > strong");
    assert.match(label, /min-width: 0;/);
    assert.match(label, /text-overflow: ellipsis;/);
    assert.match(chat, /\.activity-summary \{[^}]*grid-template-columns: minmax\(0, 1fr\);\s*grid-auto-flow: column;/, "a hidden timestamp leaves no column gap");
  });

  /* The pane clips its bottom edge, so a row that outgrows it is gone for good: two pending
     stacks in one capped row left the second clipped to the top edges of its banners, empty
     magenta frames with nothing to answer. The queue now takes no row at all. */
  it("keeps the composer in its row and lays the attention queue over the messages", () => {
    const shell = rule(chat, ".agent-chat.headerless .chat-shell");
    assert.match(shell, /grid-template-rows: minmax\(0, 1fr\) auto auto;/);
    assert.match(shell, /grid-template-areas: "messages" "banner" "composer";/);
    for (const [selector, area] of [
      [".message-list", "messages"],
      [".error-banner", "banner"],
      [".closed-banner", "banner"],
      [".pending-stack", "messages"],
      [".composer-wrap", "composer"]
    ]) {
      assert.match(rule(chat, selector), new RegExp(`grid-area: ${area};`), selector);
    }
    const stack = rule(chat, ".pending-stack");
    assert.match(stack, /position: absolute;/, "out of flow: opening it never resizes the list");
    assert.match(stack, /z-index: 1;/, "over the messages, under the replay strip's 2");
    assert.match(stack, /max-height: 100%;[\s\S]*overflow-y: auto;/, "a tall card scrolls inside it");
    const queue = rule(source("./PendingBannerStack.svelte"), ".queue");
    assert.match(queue, /background: var\(--surface-0\);/, "opaque, on the queue itself so it comes and goes with it");
    assert.match(queue, /box-shadow: 0 -1px 0 var\(--border\);/, "a hairline on top");
    assert.equal(chat.match(/<PendingBannerStack\b/g)?.length, 1, "one queue, not a stack per kind");
    assert.match(rule(chat, ".error-banner"), /max-height: 6lh;[\s\S]*overflow-y: auto;/);
  });

  it("lines the attention queue up with the messages' side inset", () => {
    assert.match(rule(chat, ".agent-chat"), /--chat-inset: clamp\(18px, 5vw, 72px\);/);
    assert.match(rule(chat, ".agent-chat.embedded"), /--chat-inset: var\(--pane-inset\);/, "a pane's chat is inset like every other pane");
    assert.match(rule(chat, ".agent-chat.embedded .composer-wrap"), /margin: 0 var\(--chat-inset\) 12px;/);
    assert.match(rule(chat, ".message-list"), /padding: 22px var\(--chat-inset\);/);
    assert.match(rule(chat, ".agent-chat.embedded .message-list"), /padding-top: 14px;/);
    assert.doesNotMatch(rule(chat, ".agent-chat.embedded .message-list"), /padding: /, "a shorthand here puts the track back inside the end inset");
    assert.match(rule(chat, ".pending-stack"), /inset: auto var\(--chat-inset\) 0;/, "edge to edge with the messages above");
    assert.doesNotMatch(chat, /padding-inline: 16px;/, "no second inset for the list alone");
  });

  /* The queue is placed in the list's grid area, whose box includes the list's scrollbar; the
     messages are inset inside it. Inset from the area alone, three approvals in a 360px pane ran
     the card 10px past the messages and onto the scrollbar, and at 200% zoom the card's time
     held its width and pushed the header's action out, scrolling the queue sideways. */
  it("keeps the attention queue inside the messages' column, clear of the list's scrollbar", () => {
    assert.match(chat, /listGutter = box\.offsetWidth - box\.clientWidth;/, "measured as the list resizes, as its scrollbar comes and goes");
    assert.match(chat, /style:--list-gutter=\{`\$\{listGutter\}px`\}/);
    assert.match(rule(chat, ".message-list"), /padding-inline-end: calc\(var\(--chat-inset\) - var\(--scrollbar-size\)\);/, "the list's track is its end inset");
    assert.match(rule(chat, ".pending-stack"), /inset-inline-end: calc\(var\(--chat-inset\) - var\(--scrollbar-size\) \+ var\(--list-gutter, 0px\)\);/);
    const when = rule(source("./PendingCard.svelte"), ".when");
    assert.match(when, /flex: 0 1 auto;\s*min-width: 0;\s*overflow: hidden;\s*text-overflow: ellipsis;/, "the time gives way in a narrow card");
  });

  it("keeps the folded queue clear at the end of the messages, and all of a lone action card", () => {
    assert.match(rule(chat, ".message-list"), /padding-bottom: calc\(22px \+ var\(--queue-reserve, 0px\)\);/);
    assert.match(rule(chat, ".agent-chat.embedded .message-list"), /padding-bottom: calc\(14px \+ var\(--queue-reserve, 0px\)\);/);
    assert.match(chat, /bind:headHeight=\{queueHead\}/, "measured from the header, all a folded stack is");
    assert.match(
      chat,
      /const queueReserve = \$derived\(\s*queueHead === 0 \? 0 : actionRow && pendingRows\.length === 0 \? queueCover : queueHead \+ 18\s*\);/,
      "a blocked request leaves room for its header; an action card alone answers the reply above it"
    );
    const stack = source("./PendingBannerStack.svelte");
    assert.match(rule(stack, ".queue"), /padding-top: 8px;/, "the 18px the reserve adds: 8 above");
    assert.match(rule(stack, ".queue::after"), /height: 10px;/, "and 10 below, as content so the sticky footer sits flush");
    assert.match(rule(stack, ".stack-head"), /min-height: var\(--control-height\);/, "as tall folded as open, so folding never moves the reserve");
    assert.match(stack, /return \(\) => \{\s*observer\.disconnect\(\);\s*headHeight = 0;/, "cleared only once the queue has left");
    assert.match(chat, /scrollPaddingEnd: queueReserve/, "keyboard paging lands a message clear of it");
    // A pre effect runs as the component is set up; reading a const declared below it throws.
    assert.ok(
      chat.indexOf("const queueReserve = $derived(") < chat.indexOf("scrollPaddingEnd: queueReserve"),
      "the list's options effect comes after the reserve it reads"
    );
    assert.match(rule(chat, ".message-list :global(.jump-latest)"), /bottom: var\(--queue-cover, 0px\);/, "Jump to latest rides above it, open or folded");
  });

  /* A hairline box means pressable. Every message once sat in one, so the reply, the trace and
     the user's own words all read as the same weight and the signal was hard to find. */
  it("frames only what is pressed or answered, and the user's own message", () => {
    for (const selector of [".bubble", ".message.assistant .bubble", ".activity-feed"]) {
      assert.doesNotMatch(rule(chat, selector), /border:|background:/, selector);
    }
    assert.doesNotMatch(chat, /\.activity-feed:hover|\.agent-chat\.embedded \.message\.assistant \.bubble \{/);
    const user = rule(chat, ".message.user .bubble");
    assert.match(user, /border: 1px solid color-mix\(in srgb, var\(--accent\) 32%, transparent\);/);
    assert.match(user, /background: color-mix\(in srgb, var\(--accent\) 12%, var\(--surface-2\)\);/);
    const summary = rule(chat, ".activity-summary.disclosure:hover");
    assert.match(summary, /background: var\(--control-hover\);/, "the header is pressable, by a hover tint like a tree row");
    const queue = rule(source("./PendingBannerStack.svelte"), ".queue");
    assert.match(queue, /background: var\(--surface-0\);[\s\S]*box-shadow: 0 -1px 0 var\(--border\);/, "approvals and callbacks are answered, so their queue keeps its frame");
    assert.doesNotMatch(rule(chat, ".reply-error"), /border|background/, "an error is said by its icon and tone");
    assert.match(chat, /<p class="reply-error">\s*<CircleAlert /);
    const copy = rule(chat, ".message-copy :global(.icon-button.copy)");
    assert.match(copy, /border-color: transparent;/);
    assert.doesNotMatch(copy, /display: none|visibility: hidden/, "faded, never removed, so Tab still reaches it");
    assert.match(chat, /\.message:focus-within \.message-copy :global\(\.copy\)/, "focus reveals it");
  });

  /* Unboxed, a settled header still started its label 23px in, after a caret cell that was
     empty at rest, and read at the reply's own weight above a reply still streaming. */
  it("starts a turn header's label one gap after its caret, and quiets it once the turn ends", () => {
    const app = source("../../../app.css");
    assert.match(rule(app, ".trace-glyph"), /width: 15px;/, "the caret's cell");
    const header = rule(chat, ".activity-summary");
    assert.match(header, /margin-inline-start: calc\(-1 \* var\(--gap-sm\)\);\s*padding: 0 var\(--gap-sm\);/, "the hover bleed nets to zero, so the cell sits on the reply's edge");
    assert.match(rule(chat, ".activity-heading"), /gap: var\(--gap-sm\);/, "settled label");
    assert.match(rule(source("./ReplyStatus.svelte"), ".reply-status"), /gap: var\(--gap-sm\);/, "working label");
    assert.match(rule(chat, ".turn-summary.disclosure:not(.live) :global(.disclosure-chevron)"), /opacity: 1;/, "the caret shows at rest");

    assert.match(chat, /class=\{\["activity-summary activity-line turn-summary disclosure", working && "active live"\]\}/, "active only while working");
    assert.match(rule(chat, ".activity-line"), /color: var\(--text-3\);/, "finished and collapsed: quiet");
    assert.doesNotMatch(rule(chat, ".activity-copy strong"), /color:/, "the label takes the row's tone");
    assert.match(
      chat,
      /\n\s*\.activity-line\.active,\n\s*\.activity-summary\[aria-expanded="true"\],\n\s*\.activity-summary:focus-visible \{\s*color: var\(--text-2\);/,
      "working, open or focused: normal"
    );
    assert.match(rule(chat, ".activity-summary.disclosure:hover"), /color: var\(--text-2\);/, "hovered: normal");

    assert.match(rule(chat, ".reply-error"), /color: var\(--error\);/, "an error keeps its tone");
    assert.match(chat, /\.trace-row:is\(\.glyph-error, \.glyph-approval\) > \.trace-line :is\(strong, \.trace-note\)\) \{\s*color: var\(--text-1\);/);
  });

  /* A 434px pane spent a fifth of a long reply's height on 11px block gaps, 7px list gaps, a
     line height of 1.6 and rules with the browser's margins. Narrowing the measure would have
     made it taller, so the rhythm is what tightens. */
  it("sets a reply's blocks one gap apart, its lists close, and caps its measure in ch", () => {
    const reply = rule(chat, ".message.assistant .bubble");
    assert.match(reply, /--markdown-block-gap: var\(--gap-md\);/);
    assert.match(reply, /--markdown-list-gap: var\(--gap-xs\);/);
    assert.match(reply, /--markdown-body-line-height: 1\.5;/);
    assert.doesNotMatch(reply, /--markdown-(block|list)-gap: \d/, "gaps are tokens, not the old 11px and 7px");
    const hr = rule(chat, ".message.assistant .bubble :global(hr)");
    assert.match(hr, /height: 1px;\s*margin: 0;\s*border: 0;\s*background: var\(--border\);/, "a hairline, spaced by the block gap alone");
    const heading = rule(chat, ".message.assistant .bubble :global(.markdown-message > [data-lead]:not(:first-child))");
    assert.match(heading, /^\s*margin-top: var\(--gap-sm\);\s*$/, "a little more above, nothing extra below");
    assert.match(chat, /:global\(\.markdown-message > :not\(:first-child\) > :is\(h3, h4, h5, h6\)\),\n\s*\.message\.assistant \.bubble :global\(\.markdown-message > \[data-lead\]/);
    assert.match(rule(chat, ".message.assistant .bubble :global(li > :is(ul, ol))"), /gap: calc\(var\(--gap-xs\) \/ 2\);/, "nested lists closer still");
    assert.match(rule(chat, ".message.assistant .bubble :global(.markdown-message)"), /max-width: 72ch;/);

    const shared = rule(markdown, ".markdown-message");
    assert.match(shared, /gap: var\(--markdown-block-gap, 9px\);/, "the file viewer keeps the shared defaults");
    assert.doesNotMatch(markdown, /:global\(hr\)|max-width: \d+ch|\[data-lead\]/, "rules, headings and the measure are the chat's alone");
    assert.match(markdown, /\.markdown-message :global\(p\),\n\s*\.markdown-message :global\(ul\)/, "a list item's paragraph keeps the shared zero margin");
    assert.match(rule(markdown, ".markdown-message :global(h6)"), /margin: 0;/);
  });

  it("keeps the handler and choice icons whole before the send control", () => {
    assert.match(rule(chat, ".composer-controls :global(.choice-button)"), /flex: none;/);
    assert.match(rule(chat, ".composer-control-spacer"), /margin-inline-start: auto;/);
  });
});
