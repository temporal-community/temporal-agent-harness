import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { render } from "svelte/server";
import { beforeEach, describe, it } from "vitest";

import { ApiError } from "../src/lib/api/httpClient.ts";
import AgentChatPanel from "../src/lib/components/agent/AgentChatPanel.svelte";
import SessionControls from "../src/lib/components/chat/SessionControls.svelte";
import { AgentRunController } from "../src/lib/state/agentRun.svelte.ts";
import {
  MAX_ARCHIVED_SESSIONS,
  MAX_PINNED_SESSIONS,
  readArchivedSessions,
  readPinnedSessions,
  toggleArchivedSession,
  togglePinnedSession,
  validArchivedSessions,
  validPinnedSessions
} from "../src/lib/state/agentRunStorage.ts";
import { createPaneStack } from "../src/lib/state/paneStack.svelte.ts";
import { SESSION_HEADING_HEIGHT, SESSION_ROW_HEIGHT, lastSeenPending, rememberPending } from "../src/lib/state/sessionHistory.ts";
import { installBrowserSurface, sleep, stripComments, waitFor } from "./support/controllerHarness.mjs";

const appSource = readFileSync(
  fileURLToPath(new URL("../src/App.svelte", import.meta.url)),
  "utf8"
);
const drawerSource = readFileSync(
  fileURLToPath(
    new URL("../src/lib/components/primitives/DockedDrawer.svelte", import.meta.url)
  ),
  "utf8"
);

describe("Session Manager drawer", () => {
  beforeEach(() => {
    installBrowserSurface();
  });

  it("treats the former sessions pane URL as unknown and keeps the default desk", () => {
    window.location.search = "?p=sessions";

    const stack = createPaneStack();
    stack.hydrateFromQuery();

    assert.deepEqual(stack.panes.map((pane) => pane.id), ["graph", "chat"]);
    assert.deepEqual(stack.unknownPanes, {
      tokens: [{ written: "sessions", meant: null }],
      fellBack: true
    });
  });

  it("uses the shared docked drawer on the left without duplicating the manager", () => {
    /* Two instances, one list: the launcher in the chrome names the session, and the
       manager it opens is the only thing that lists them. `session-launcher.test.mjs`
       holds the other half of this — that the launcher is still there at all. */
    assert.equal(appSource.match(/<SessionControls\b/g)?.length, 2);
    assert.equal(appSource.match(/display="pane"/g)?.length, 1);
    assert.equal(appSource.match(/<DockedDrawer\b/g)?.length, 2);
    assert.match(appSource, /<DockedDrawer\s+edge="left"/);
    assert.match(appSource, /<DockedDrawer\s+edge="bottom"/);
    assert.match(appSource, /aria-controls="session-manager-drawer"/);
    /* The drawer and the list inside it are both regions; one name for both read as a
       nested duplicate to a screen reader. */
    assert.match(appSource, /<DockedDrawer\s+edge="left"\s+label="Session Manager"/);
    assert.match(
      stripComments(render(SessionControls, { props: { display: "pane" } }).body),
      /<section class="session-manager[^"]*" aria-label="Sessions">/
    );
    assert.match(appSource, /aria-expanded=\{sessionDrawerOpen\}/);
    assert.doesNotMatch(appSource, /@attach dismissable/);
    assert.match(
      appSource,
      /\.app\.has-session-drawer\s*\{[\s\S]*?grid-template-columns:/
    );
  });

  it("labels the Session flow pane closed once its session has ended, unless it failed", () => {
    const graphCase = appSource.match(/case "graph": \{[\s\S]*?\n      \}/)?.[0] ?? "";
    assert.match(graphCase, /const closed = Boolean\(run\.session\?\.closed\) && run\.graph\.status !== "error";/);
    assert.match(graphCase, /statusLabel: closed \? "closed" : run\.graph\.status/);
  });

  it("mirrors one resize implementation across bottom and left edges", () => {
    assert.match(drawerSource, /type DrawerEdge\n[^;]*from "\.\/resizeKeys"/);
    assert.match(drawerSource, /case "bottom":[\s\S]*?rect\.bottom - event\.clientY/);
    assert.match(drawerSource, /case "left":[\s\S]*?event\.clientX - rect\.left/);
    assert.match(drawerSource, /const _exhaustive: never = edge/);
  });
});

describe("Session Manager rows", () => {
  const controls = readFileSync(
    fileURLToPath(new URL("../src/lib/components/chat/SessionControls.svelte", import.meta.url)),
    "utf8"
  );
  const appCss = readFileSync(fileURLToPath(new URL("../src/app.css", import.meta.url)), "utf8");
  const rule = (selector) =>
    controls.match(new RegExp(`\\n\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} \\{([^}]*)\\}`))?.[1] ?? "";
  const session = (id, extra = {}) => ({
    workflow_id: id,
    agent_workflow_type: "Hello",
    created_at: Date.now() / 1000 - 120,
    initial_user_message: `Title ${id}`,
    ...extra
  });
  const pane = (props) =>
    stripComments(render(SessionControls, { props: { display: "pane", onEndSession: async () => {}, ...props } }).body);
  const rowOf = (body, id) => {
    const row = body.split('<div class="session-item').find((chunk) => chunk.includes(`id="session-option-${id}"`));
    assert.ok(row, `no row for ${id}`);
    return row;
  };
  const glyphOf = (row) => row.match(/<span class="session-glyph[^"]*"[^>]*>([\s\S]*?)<\/span>\s*<span class="session-status-words/)?.[1] ?? "";

  /* A 107px card showed six sessions in a 900px drawer. One 28px line shows four times that. */
  it("estimates each row and heading at exactly its CSS height, fine pointer and coarse", () => {
    assert.equal(Number(appCss.match(/--control-height: (\d+)px;/)?.[1]), SESSION_ROW_HEIGHT.fine);
    assert.equal(Number(appCss.match(/--control-height-xs: (\d+)px;/)?.[1]), SESSION_HEADING_HEIGHT);
    assert.match(rule(".session-list"), /--session-row-height: var\(--control-height\);/);
    assert.equal(
      Number(controls.match(/@media \(pointer: coarse\) \{\s*\.session-list \{\s*--session-row-height: (\d+)px;/)?.[1]),
      SESSION_ROW_HEIGHT.coarse
    );
    assert.match(controls, /new MediaQuery\("pointer: coarse"\)/);
    assert.match(
      controls,
      /estimateSize: \(index\) =>\s*entries\[index\]\?\.kind === "heading" \|\| entries\[index\]\?\.kind === "archive"\s*\? SESSION_HEADING_HEIGHT\s*: coarsePointer\.current \? SESSION_ROW_HEIGHT\.coarse : SESSION_ROW_HEIGHT\.fine/
    );
    const row = rule(".session-row");
    assert.match(row, /min-height: var\(--session-row-height\);/);
    assert.match(row, /padding-block: 0;/);
    assert.match(row, /border: 0;/);
    assert.match(row, /display: flex;/);
    assert.match(rule(".group-heading"), /min-height: var\(--control-height-xs\);/);
    assert.doesNotMatch(rule(".group-heading"), /padding:|padding-block/);
    assert.doesNotMatch(rule(".session-entry"), /padding|margin/, "rows sit flush; the tint carries hover");
    assert.doesNotMatch(controls, /session-preview|session-row-foot|session-row-meta|line-clamp|StatusChip label=/);
  });

  it("draws status as a shape: filled pip open, hollow closed, a glyph for needs-you and error", () => {
    const sessions = [session("wf-open"), session("wf-closed", { closed: true }), session("wf-now")];
    const body = pane({ sessions, sessionId: "wf-now", pendingLabel: "1 response needed" });
    const open = glyphOf(rowOf(body, "wf-open"));
    assert.match(open, /<span class="session-pip[^"]*"><\/span>/);
    assert.doesNotMatch(open, /hollow|<svg/);
    assert.match(glyphOf(rowOf(body, "wf-closed")), /class="session-pip hollow[^"]*"/);
    assert.match(glyphOf(rowOf(body, "wf-now")), /lucide-shield-alert/);
    assert.match(rowOf(body, "wf-now"), /style="--status-color: var\(--live\);"/);
    assert.match(rowOf(body, "wf-closed"), /class="session-row closed[^"]*"/, "a closed title recedes");

    const failed = pane({ sessions, sessionId: "wf-now", error: "boom" });
    assert.match(glyphOf(rowOf(failed, "wf-now")), /lucide-circle-x|lucide-x-circle/);
    assert.match(rowOf(failed, "wf-now"), /--status-color: var\(--error\)/);
    assert.match(body, /class="session-glyph[^"]*" aria-hidden="true"/);
  });

  /* A session a reader never opened has no live stream, only its last known
     `execution_status`: that is the one thing the row can say about it without opening it, so
     a session that broke must not read the same as one that simply ended. */
  it("flags another session's own failure at rest, not just the open one's stream error", () => {
    const sessions = [
      session("wf-failed", { execution_status: "FAILED" }),
      session("wf-done", { execution_status: "COMPLETED", closed: true })
    ];
    const body = pane({ sessions, sessionId: "wf-open" });
    const failedRow = rowOf(body, "wf-failed");
    assert.match(glyphOf(failedRow), /lucide-circle-x|lucide-x-circle/);
    assert.match(failedRow, /--status-color: var\(--error\)/);
    assert.doesNotMatch(failedRow, /class="session-row closed/, "a failure stays legible, not faded like an ordinary close");
    assert.match(failedRow, /<span class="visually-hidden[^"]*">, Needs attention,<\/span>/);
    // Still a hollow pip, not the failure glyph: closing on purpose is not breaking.
    assert.match(glyphOf(rowOf(body, "wf-done")), /class="session-pip hollow/);
    // Rendered by `render()` with no pointer or focus event dispatched: a hover-only CSS rule
    // revealing the glyph would leave this assertion with an empty `.session-glyph`, since SSR
    // markup never carries a `:hover` state.
    assert.doesNotMatch(controls, /:hover[^{]*\{[^}]*(session-pip|session-glyph|state-glyph)/);
  });

  /* Only the open session's stream says it is waiting, and the list carries no pending state,
     so leaving a waiting session used to drop its shield to a plain pip. */
  it("keeps a waiting session's shield after switching away, as of when last seen, until it closes", () => {
    lastSeenPending.clear();
    lastSeenPending.set("wf-wait", { label: "earlier", at: 0 });
    assert.equal(rememberPending("wf-wait", null, true), undefined);
    assert.equal(lastSeenPending.get("wf-wait")?.label, "earlier", "an empty view still connecting forgets nothing");
    lastSeenPending.clear();
    /* Connecting: a stream held on an approval stays open, so it reads as connecting too. */
    const leave = rememberPending("wf-wait", "1 approval needed", true);
    assert.equal(lastSeenPending.has("wf-wait"), false, "nothing while it is open: the live view says it");
    leave();
    lastSeenPending.set("wf-wait", { ...lastSeenPending.get("wf-wait"), at: Date.now() - 6 * 60_000 });

    const open = pane({ sessions: [session("wf-wait"), session("wf-hi")], sessionId: "wf-hi", pendingLabel: null });
    const row = rowOf(open, "wf-wait");
    assert.match(glyphOf(row), /lucide-shield-alert/, "the open row's shape");
    assert.match(row, /style="--status-color: var\(--live\);"/, "and its tone");
    assert.match(row, /<span class="visually-hidden[^"]*">, 1 approval needed when last seen,<\/span>/);
    assert.match(row, /class="tip-card-value[^"]*" dir="auto">1 approval needed · as of 6 minutes ago</, "the card says it may be stale");

    const closed = rowOf(pane({ sessions: [session("wf-wait", { closed: true, execution_status: "COMPLETED" }), session("wf-hi")], sessionId: "wf-hi" }), "wf-wait");
    assert.match(glyphOf(closed), /class="session-pip hollow/, "the list's closed status wins");
    assert.doesNotMatch(closed, /approval needed/);

    assert.equal(rememberPending("wf-wait", null, false), undefined);
    assert.equal(lastSeenPending.has("wf-wait"), false, "reopened with nothing pending, it is forgotten");
  });

  it("says each row's status in words, in its name and its tip", () => {
    const body = pane({ sessions: [session("wf-open"), session("wf-closed", { closed: true })], sessionId: "" });
    assert.match(rowOf(body, "wf-open"), /<span class="visually-hidden[^"]*">, Open,<\/span>/);
    assert.match(rowOf(body, "wf-closed"), /<span class="visually-hidden[^"]*">, Closed,<\/span>/);
    assert.match(rowOf(body, "wf-open"), /<span class="visually-hidden[^"]*">, 2 minutes ago<\/span>/);
    assert.match(rowOf(body, "wf-open"), /<time aria-hidden="true"[^>]*>2m<\/time>/);
  });

  /* The tip spent half its width on "STATUS", "STARTED" and "ID" over and over, and left out
     the title the row cuts short. Icons say what each line is, so the values get the room. */
  it("tips a card of the whole title, then icon-led lines with no labels, leaving out what is missing", () => {
    const title = `${"顧客データ ".repeat(50)}<b>end</b>`;
    const agents = [{ workflow_type: "MontyConversationAgent", label: "Monty" }];
    const body = pane({
      sessions: [session("wf-a", { created_at: 1_790_000_000, closed: true, execution_status: "TIMED_OUT", initial_user_message: title, agent_workflow_type: "MontyConversationAgent" })],
      agents,
      sessionId: ""
    });
    const row = rowOf(body, "wf-a");
    assert.match(row, /role="option"[^>]*aria-describedby="session-option-wf-a-tip"[^>]*data-tip=""[^>]*data-tip-side="right"/);
    const card = row.match(/<span[^>]*id="session-option-wf-a-tip"[^>]*>[\s\S]*<\/span>\s*<\/button>/)?.[0] ?? "";
    assert.match(card, /role="tooltip"/);
    assert.match(card, /aria-hidden="true"/, "kept out of the row's name; aria-describedby still reads it");
    assert.match(card, /style="--tip-card-heading-lines: 3;?"/);
    const tipCard = readFileSync(fileURLToPath(new URL("../src/lib/components/primitives/TipCard.svelte", import.meta.url)), "utf8");
    assert.match(
      tipCard,
      /\n  :global\(\[data-tip\]\) > \.tip-card \.tip-card-heading \{\s*-webkit-line-clamp: var\(--tip-card-heading-lines, 2\);/,
      "its own rule, so the values' clamp cannot outrank it"
    );
    assert.doesNotMatch(tipCard, /:is\(\.tip-card-heading, \.tip-card-value:not\(\.mono\)\) \{[^}]*line-clamp: 2/);
    assert.match(card, /class="tip-card-heading[^"]*" dir="auto">顧客データ (顧客データ ){49}&lt;b>end&lt;\/b><\/span>/, "the full title, as text");

    const icons = [...card.matchAll(/<span class="tip-card-icon[^"]*" aria-hidden="true">([\s\S]*?)<\/span>\s*<span class="tip-card-value/g)].map((m) => m[1]);
    /* Status first, as the row leads with its glyph; then the agent and the start. */
    assert.equal(icons.length, 3);
    assert.match(icons[0], /class="session-glyph[\s\S]*lucide-circle-x|lucide-x-circle/, "the row's own status shape");
    assert.match(icons[1], /class="icon-agent/);
    assert.match(icons[2], /lucide-clock/);
    assert.doesNotMatch(card, /tip-card-key|>(Agent|Status|Started|ID)</, "no text labels");
    const values = [...card.matchAll(/class="tip-card-value[^"]*" dir="auto">([^<]*)</g)].map((m) => m[1]);
    assert.deepEqual(values.slice(0, 2), ["Needs attention · timed out", "Monty · MontyConversationAgent"]);
    assert.match(values[2], /ago · .*2026/, "relative and absolute");
    /* An unbroken id wrapped the card; it is gone, not hidden. Search still matches on it. */
    assert.equal(values.length, 3);
    assert.doesNotMatch(card, /lucide-hash|wf-a</);
    assert.doesNotMatch(controls, /idIcon|Hash\b|key: "ID"/);
    assert.doesNotMatch(controls.slice(controls.indexOf('<div class="session-item">')), /\stitle=/, "one bubble, not a title beside it");

    const unknown = rowOf(pane({ sessions: [session("wf-b", { created_at: 0 })], sessionId: "" }), "wf-b");
    assert.equal(unknown.match(/class="tip-card-icon/g)?.length, 2, "a missing start draws no line and no orphan icon");
    assert.doesNotMatch(unknown, /lucide-clock|Unknown start time/);
    assert.match(unknown, /class="tip-card-value[^"]*" dir="auto">Hello</, "an agent named by its type is not repeated");
  });

  /* Arrowing moves aria-activedescendant, not focus, so the highlighted row draws no tip. */
  it("does not tip the arrow-highlighted row", () => {
    assert.doesNotMatch(appCss + controls, /\.highlighted[^{]*::after|aria-selected="true"\][^{]*::after/);
  });

  /* After a click on another session, aria-current moved but aria-selected stayed on the first row. */
  it("moves the selection to the session a click or Enter opens", () => {
    assert.match(controls, /async function openSession\(nextSessionId: string\): Promise<void> \{\s*if \(!onSelectSession\) return;\s*arrowedId = nextSessionId;/);
    assert.match(controls, /onclick=\{\(\) => void openSession\(item\.workflow_id\)\}/, "click");
    assert.match(controls, /pick: \(index\) => \{\s*void openSession\(filteredIds\[index\]\);/, "Enter");
    assert.match(controls, /move: \(index\) => \(arrowedId = filteredIds\[index\]\)/, "arrows unchanged");
    assert.ok(
      controls.indexOf(":has(.session-row.highlighted) .session-actions") < controls.indexOf(":has(.session-row.active) .session-actions"),
      "the opened row's fade keeps its selected fill, like the row"
    );
  });

  /* The age is how long a session has run, and a reader hovering to act on it still wants it:
     End used to cover it. The actions now sit together just before it, pin first like a pane
     header: abutting 24px targets for 14px glyphs, one gap-sm to the age. Archive is offered
     only once a session is over, so an open row has pin and End, a closed one pin and archive. */
  it("clusters pin with End or archive before the age, never over it, at a 24px pitch, in Tab order", () => {
    const body = pane({ sessions: [session("wf-open"), session("wf-closed", { closed: true }), session("wf-gone", { execution_status: "NOT_FOUND", closed: true })], sessionId: "" });
    const open = rowOf(body, "wf-open");
    const actionsOf = (row) => row.match(/<div class="session-actions[^"]*">([\s\S]*)<\/div>/)?.[1] ?? "";
    const actions = actionsOf(open);
    const drawn = (html) => [...html.matchAll(/<button[^>]*class="icon-button[^"]*(session-pin|session-archive|session-end)/g)].map((m) => m[1]);
    assert.deepEqual(drawn(actions), ["session-pin", "session-end"], "an open row: pin, then End by the age, no archive");
    assert.deepEqual(drawn(rowOf(body, "wf-closed")), ["session-pin", "session-archive"], "a closed row: pin and archive, nothing left to end");
    assert.deepEqual(drawn(rowOf(body, "wf-gone")), ["session-pin", "session-archive"], "a gone row: pin and archive");
    assert.doesNotMatch(actions, /tabindex=/, "Tab follows the order drawn");
    const paneShell = readFileSync(fileURLToPath(new URL("../src/lib/panes/PaneShell.svelte", import.meta.url)), "utf8");
    const header = [...(paneShell.match(/<header[\s\S]*?<\/header>/)?.[0] ?? "").matchAll(/onclick=\{(onTogglePin|onToggleCollapse|onClose)\}/g)].map((m) => m[1]);
    assert.deepEqual(header, ["onTogglePin", "onToggleCollapse", "onClose"], "the pin first, as this follows");
    assert.equal(actions.match(/width="14"/g)?.length, 2, "14px glyphs");
    assert.ok(open.indexOf("</button>") < open.indexOf('class="session-actions'), "after the option, beside it, never in it");
    assert.doesNotMatch(open, /at-rest|session-kept-gap/, "an open row paints nothing at rest and reserves nothing");
    assert.doesNotMatch(rowOf(body, "wf-closed"), /at-rest|session-kept-gap/, "a closed row's archive shows only on hover or focus");
    assert.match(rowOf(body, "wf-gone"), /class="icon-button[^"]*session-archive at-rest/, "a gone row keeps its archive in sight");

    const cluster = rule(".session-actions");
    assert.match(cluster, /position: absolute;[\s\S]*inset-inline-end: calc\(var\(--gap-xs\) \+ var\(--session-age-width\) \+ var\(--gap-sm\)\);/, "ends one gap-sm before the age");
    assert.doesNotMatch(cluster, /\bgap:/, "targets abut: a 24px pitch");
    assert.match(rule(".session-list"), /--session-action-width: calc\(var\(--session-row-height\) - var\(--gap-xs\)\);/, "a 24px target");
    assert.match(rule(".session-list"), /--session-age-width: 24px;/, "as wide as \"59m\", no wider");
    assert.match(rule(".session-actions :global(.icon-button.icon-button)"), /width: var\(--session-action-width\);[\s\S]*padding: 0;/, "no padding squeezing the glyph");
    assert.match(rule("time"), /flex: none;[\s\S]*width: var\(--session-age-width\);[\s\S]*tabular-nums/, "the age keeps its fixed slot");
    assert.doesNotMatch(controls, /\btime \{[^}]*(visibility|opacity|margin-inline-start)/, "nothing hides or shifts the age");
    assert.doesNotMatch(controls, /inset-inline-end: 0;/, "nothing is laid over the age's slot");
    assert.doesNotMatch(controls, /:is\(:hover, :focus-within\)[^{]*\.session-row \{[^}]*padding/, "hover moves no text");
    assert.match(controls, /linear-gradient\(to right, transparent, var\(--actions-bg\) var\(--fade-width\)\)/, "the text fades out under it");
  });

  /* The Pinned heading already says a row is pinned: a filled pin on every one of them was the
     same word again, and reserved a track that cut the title short for it. */
  it("draws no pin at rest; Pinned rows are ordinary rows whose pin is pressed on hover", () => {
    const { storage } = installBrowserSurface();
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ pinnedSessions: ["wf-pin", "wf-pin-arch"], archivedSessions: ["wf-pin-arch"], sessionArchiveOpen: true }));
    const body = pane({ sessions: [session("wf-pin"), session("wf-pin-arch"), session("wf-plain")], sessionId: "" });
    const pinOf = (row) => row.match(/<button[^>]*class="icon-button[^"]*session-pin[^"]*"[^>]*>/)?.[0] ?? "";
    const pinned = rowOf(body, "wf-pin");
    assert.doesNotMatch(pinned, /session-kept-gap/, "no action space reserved at rest");
    assert.doesNotMatch(pinOf(pinned), /at-rest/, "hidden until hover or focus");
    assert.match(pinOf(pinned), /class="icon-button default pressed session-pin[^"]*"[^>]*aria-pressed="true"/, "pressed, so hover shows how to unpin");
    assert.match(pinned, /<svg[^>]*fill="currentColor"[^>]*lucide-pin"/, "filled while shown");
    assert.doesNotMatch(pinOf(rowOf(body, "wf-pin-arch")), /at-rest/, "an archived pin follows the archived rule");
    assert.doesNotMatch(controls, /icon-button\.pressed\)[^{]*\{\s*opacity: 1/, "pressed is not a reason to show at rest");
    assert.match(controls, /\.session-item:is\(:hover, :focus-within\) \.session-actions :global\(\.icon-button\),\s*\.session-actions :global\(\.icon-button\.at-rest\) \{\s*opacity: 1;/);
    storage.local.clear();
  });

  /* Archived and gone rows keep their archive in sight at rest, over a slot of its own so it
     covers no text; nothing else is drawn at rest, so nothing else is reserved. The archive is
     always last, so even an archived row still running, which has End too, keeps one slot. */
  it("reserves the archive's slot on archived and gone rows only, so it covers no text", () => {
    const { storage } = installBrowserSurface();
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ pinnedSessions: ["wf-pin"], archivedSessions: ["wf-arch", "wf-arch-done"], sessionArchiveOpen: true }));
    const body = pane({ sessions: [session("wf-pin"), session("wf-arch"), session("wf-arch-done", { closed: true }), session("wf-gone", { execution_status: "NOT_FOUND", closed: true }), session("wf-plain")], sessionId: "" });
    const gap = new RegExp(`<span class="session-kept-gap(?: svelte-[\\w-]+)?" aria-hidden="true"></span>\\s*<time`);
    const drawn = (row) => [...row.matchAll(/<button[^>]*class="icon-button[^"]*(session-pin|session-archive|session-end)/g)].map((m) => m[1]);
    for (const id of ["wf-arch", "wf-arch-done", "wf-gone"]) {
      assert.match(rowOf(body, id), /class="icon-button[^"]*session-archive at-rest/, id);
      assert.match(rowOf(body, id), gap, `${id}: one slot, the archive by the age`);
    }
    for (const id of ["wf-arch", "wf-arch-done"]) assert.match(rowOf(body, id), /aria-label="Unarchive session — [^"]*" aria-pressed="true"/, `${id}: always restorable, open or closed`);
    assert.deepEqual(drawn(rowOf(body, "wf-arch")), ["session-pin", "session-end", "session-archive"], "still running: End on hover, before the archive at rest");
    assert.deepEqual(drawn(rowOf(body, "wf-arch-done")), ["session-pin", "session-archive"]);
    for (const id of ["wf-plain", "wf-pin"]) assert.doesNotMatch(rowOf(body, id), /session-kept-gap|at-rest|session-archive/, id);
    assert.match(rule(".session-kept-gap"), /flex: none;\s*width: calc\(var\(--session-action-width\) - var\(--gap-sm\)\);/, "one target and a gap-sm, with the row's gaps");
    assert.doesNotMatch(controls, /session-kept-gap\.ends|"ends"/, "no End slot reserved: End is never drawn at rest");
    assert.doesNotMatch(controls, /\.session-actions\.kept/, "no solid cover at rest: the title never reaches under the archive");
    assert.doesNotMatch(controls, /:hover[^{]*\{[^}]*session-kept-gap/, "hover never moves layout");
    storage.local.clear();
  });

  it("formats the header count, and skips an agent type that repeats its name in the card", () => {
    const many = Array.from({ length: 1_234 }, (_, i) => session(`wf-${i}`));
    assert.match(pane({ sessions: many, sessionId: "" }), /<span class="session-count[^"]*">1,234<\/span>/);
    assert.match(controls, /\{countFormat\.format\(entry\.count\)\}/, "group counts too");

    const agents = [{ workflow_type: "ResearchAgent", label: "ResearchAgent" }];
    const card = rowOf(pane({ sessions: [session("wf-r", { agent_workflow_type: "ResearchAgent" })], agents, sessionId: "" }), "wf-r");
    assert.match(card, /class="tip-card-value[^"]*" dir="auto">ResearchAgent</);
    assert.doesNotMatch(card, /ResearchAgent · ResearchAgent/);
  });

  it("mutes the Untitled session fallback so it doesn't read as a real title", () => {
    const body = pane({ sessions: [session("wf-none", { initial_user_message: null, label: "" }), session("wf-real")], sessionId: "" });
    assert.match(rowOf(body, "wf-none"), /class="session-title fade-end untitled[^"]*" dir="auto">Untitled session</);
    assert.doesNotMatch(rowOf(body, "wf-real"), /untitled/);
    assert.match(rule(".session-title.untitled"), /color: var\(--text-3\);/);
  });

  it("starts opposite-direction text at the row's start, drops the agent under 300px, keeps the nav status whole", () => {
    assert.match(rule(".session-title"), /text-align: match-parent;/);
    assert.match(rule(".session-agent"), /text-align: match-parent;/);
    assert.match(controls, /@container \(width < 300px\) \{\s*\.session-agent \{[^}]*clip-path: inset\(50%\);/);
    assert.match(rule(".session-state"), /flex: none;\s*max-width: 24ch;/);
    assert.match(appCss, /\n\.kicker:dir\(rtl\) \{\s*letter-spacing: normal;/, "tracking breaks Arabic joins");
  });

  /* PR #177's card showed every session's status in words and its start time; the one-line row
     keeps them for a list wide enough to give them columns. */
  it("adds a status column from 480px and a started column from 640px, leaving narrow rows alone", () => {
    const base = rule(".session-status-words,\n  .session-started");
    assert.match(base, /display: none;[\s\S]*flex: none;[\s\S]*font-variant-numeric: tabular-nums;/, "absent, so a narrow row lays out as before");
    assert.match(controls, /@container \(width >= 480px\) \{\s*\.session-status-words \{\s*display: block;/);
    assert.match(controls, /@container \(width >= 640px\) \{\s*\.session-rows \{\s*--session-status-width: \d+px;\s*\}\s*\.session-started \{\s*display: block;/, "and the status whole");
    assert.match(rule(".session-status-words"), /width: var\(--session-status-width\);/, "a fixed width lines the column up");
    assert.match(controls, /\n  \.session-started \{\s*width: var\(--session-started-width\);/);
    assert.doesNotMatch(controls, /\.session-(status-words|started)[^{]*\{[^}]*text-overflow: ellipsis/);

    const year = new Date().getFullYear();
    const sessions = [
      session("wf-done", { execution_status: "TIMED_OUT", closed: true, created_at: new Date(year, 0, 2, 9, 5).getTime() / 1000 }),
      session("wf-old", { created_at: new Date(year - 2, 4, 6, 9, 5).getTime() / 1000 }),
      session("wf-zero", { created_at: 0 })
    ];
    const body = pane({ sessions, sessionId: "" });
    const done = rowOf(body, "wf-done");
    assert.match(done, /<span class="session-status-words fade-end[^"]*" aria-hidden="true" dir="auto">Needs attention · timed out<\/span>/, "the card's wording");
    assert.match(done, /<span class="session-started fade-end[^"]*" aria-hidden="true">Jan 2, 9:05/);
    assert.match(rowOf(body, "wf-old"), new RegExp(`class="session-started[^"]*" aria-hidden="true">May 6, ${year - 2}<`), "an older year in place of the time");
    assert.match(rowOf(body, "wf-zero"), /class="session-started[^"]*" aria-hidden="true"><\/span>/, "no start, no made-up date");
    assert.doesNotMatch(rowOf(body, "wf-old"), />wf-old</, "no column repeats the workflow id the card dropped");
  });

  it("pins sessions into a Pinned group on top that persists, and rejects malformed stored pins", () => {
    const { storage } = installBrowserSurface();
    const key = "temporal-agent-ui.prefs.v1";
    const sessions = [session("wf-a"), session("wf-b", { created_at: Date.now() / 1000 - 60 }), session("wf-c")];
    const headings = (body) => [...body.matchAll(/<div class="group-heading[^"]*" aria-hidden="true">([^<]*?) <span/g)].map((m) => m[1]);
    const order = (body) => [...body.matchAll(/role="option"[^>]*aria-posinset="(\d+)"/g)].map((m) => Number(m[1]));
    const ids = (body) => [...body.matchAll(/id="session-option-([^"]+)" class="session-row/g)].map((m) => m[1]);
    assert.deepEqual(headings(pane({ sessions, sessionId: "" })), ["Today"]);

    assert.deepEqual(togglePinnedSession("wf-c"), ["wf-c"]);
    assert.deepEqual(togglePinnedSession("wf-a"), ["wf-a", "wf-c"], "most recently pinned first");
    const body = pane({ sessions, sessionId: "" });
    assert.deepEqual(headings(body), ["Pinned", "Today"]);
    assert.deepEqual(ids(body), ["wf-a", "wf-c", "wf-b"]);
    assert.deepEqual(order(body), [1, 2, 3], "positions follow the drawn order, pins first");
    assert.match(
      controls,
      /rows\(\)\.setOptions\(\{ count: list\.length, getItemKey: \(index\) => list\[index\]\?\.key \?\? index \}\)/,
      "a pin reorders rows at the same count, so the windowed list re-lays out on new entries"
    );
    assert.deepEqual(JSON.parse(storage.local.getItem(key)).pinnedSessions, ["wf-a", "wf-c"]);

    const pinnedRow = rowOf(body, "wf-a");
    assert.match(pinnedRow, /<button[^>]*class="icon-button[^"]*session-pin[^"]*"[^>]*aria-label="Unpin session — Hello — Title wf-a" aria-pressed="true"/);
    assert.match(rowOf(body, "wf-b"), /class="icon-button[^"]*session-pin[^"]*"[^>]*aria-label="Pin session — Hello — Title wf-b" aria-pressed="false"/);
    const option = pinnedRow.match(/<button[^>]*role="option"[\s\S]*?<\/button>/)?.[0] ?? "";
    assert.ok(option, "the row is an option");
    assert.doesNotMatch(option.slice(1), /<button/, "the pin sits beside the option, never inside it");

    /* Unlisted pins survive other toggles: a filter or a shorter page only hides them. */
    togglePinnedSession("wf-gone");
    assert.deepEqual(togglePinnedSession("wf-a"), ["wf-gone", "wf-c"]);
    assert.deepEqual(headings(pane({ sessions, sessionId: "" })), ["Pinned", "Today"]);

    for (const [stored, expected] of [
      ["{not json", []],
      [JSON.stringify({ pinnedSessions: "wf-a" }), []],
      [JSON.stringify({ pinnedSessions: { 0: "wf-a" } }), []],
      [JSON.stringify({ pinnedSessions: [1, null, {}, "", "  ", "wf-b", "wf-b"] }), ["wf-b"]]
    ]) {
      storage.local.setItem(key, stored);
      assert.deepEqual(readPinnedSessions(), expected, stored);
    }
    assert.equal(validPinnedSessions(Array.from({ length: 150 }, (_, i) => `wf-${i}`)).length, MAX_PINNED_SESSIONS);
    storage.local.setItem(key, JSON.stringify({ pinnedSessions: [42] }));
    assert.deepEqual(headings(pane({ sessions, sessionId: "" })), ["Today"]);
  });

  it("archives into a shut Archived group that persists, keeps the listbox valid, and unarchives back", () => {
    const { storage } = installBrowserSurface();
    const key = "temporal-agent-ui.prefs.v1";
    const sessions = [session("wf-a", { closed: true }), session("wf-b"), session("wf-gone", { execution_status: "NOT_FOUND", closed: true })];
    const headings = (body) => [...body.matchAll(/class="group-heading[^"]*"[^>]*>([^<]*?) <span/g)].map((m) => m[1]);
    const ids = (body) => [...body.matchAll(/id="session-option-([^"]+)" class="session-row/g)].map((m) => m[1]);
    const options = (body) => [...body.matchAll(/<button[^>]*role="option"[^>]*aria-setsize="(\d+)" aria-posinset="(\d+)"[\s\S]*?<\/button>/g)];

    const archiveOf = (row) => row.match(/<button[^>]*class="icon-button[^"]*session-archive[^"]*"[^>]*>/)?.[0] ?? "";
    const before = pane({ sessions, sessionId: "" });
    assert.match(archiveOf(rowOf(before, "wf-a")), /aria-label="Archive session — Hello — Title wf-a" aria-pressed="false"/);
    assert.match(archiveOf(rowOf(before, "wf-gone")), /session-archive at-rest/, "a gone row shows it at rest");
    assert.doesNotMatch(archiveOf(rowOf(before, "wf-a")), /at-rest/);
    assert.match(rowOf(before, "wf-a"), /lucide-archive/);

    togglePinnedSession("wf-a");
    assert.deepEqual(toggleArchivedSession("wf-a"), ["wf-a"]);
    assert.deepEqual(toggleArchivedSession("wf-gone"), ["wf-gone", "wf-a"]);
    assert.deepEqual(JSON.parse(storage.local.getItem(key)).archivedSessions, ["wf-gone", "wf-a"]);
    const shut = pane({ sessions, sessionId: "" });
    assert.deepEqual(headings(shut), ["Today", "Archived"], "out of Pinned and its day, into a group at the bottom");
    assert.deepEqual(ids(shut), ["wf-b"], "shut by default, so its rows are not drawn or arrowed to");
    assert.match(shut, /<button type="button" class="group-heading archive-toggle disclosure[^"]*" aria-expanded="false">Archived <span[^>]*>2<\/span>\s*<svg[^>]*disclosure-chevron/);
    assert.deepEqual(options(shut).map((m) => [m[1], m[2]]), [["1", "1"]]);

    storage.local.setItem(key, JSON.stringify({ ...JSON.parse(storage.local.getItem(key)), sessionArchiveOpen: true }));
    const open = pane({ sessions, sessionId: "" });
    assert.match(open, /aria-expanded="true">Archived <span[^>]*>2</);
    assert.deepEqual(ids(open), ["wf-b", "wf-a", "wf-gone"], "newest first, like every other group");
    const drawn = options(open);
    assert.deepEqual(drawn.map((m) => [m[1], m[2]]), [["3", "1"], ["3", "2"], ["3", "3"]], "positions count only drawn rows");
    for (const [option] of drawn) assert.doesNotMatch(option.slice(1), /<button/, "nothing pressable inside an option");
    assert.match(archiveOf(rowOf(open, "wf-a")), /aria-label="Unarchive session — Hello — Title wf-a" aria-pressed="true"/);
    assert.match(rowOf(open, "wf-a"), /lucide-archive-restore/);
    assert.match(controls, /if \(archivedIds\.has\(workflowId\)\) onArchiveSession\?\.\(workflowId\);/, "archiving stops its stream");

    assert.deepEqual(toggleArchivedSession("wf-a"), ["wf-gone"]);
    assert.deepEqual(headings(pane({ sessions, sessionId: "" })), ["Pinned", "Today", "Archived"], "back to Pinned, its pin kept");
    toggleArchivedSession("wf-unlisted");
    assert.deepEqual(readArchivedSessions(), ["wf-unlisted", "wf-gone"], "an unlisted id is kept");

    for (const [stored, expected] of [
      ["{not json", []],
      [JSON.stringify({ archivedSessions: "wf-a" }), []],
      [JSON.stringify({ archivedSessions: { 0: "wf-a" } }), []],
      [JSON.stringify({ archivedSessions: [1, null, {}, "", "  ", "wf-b", "wf-b"] }), ["wf-b"]]
    ]) {
      storage.local.setItem(key, stored);
      assert.deepEqual(readArchivedSessions(), expected, stored);
    }
    assert.equal(validArchivedSessions(Array.from({ length: 6_000 }, (_, i) => `wf-${i}`)).length, MAX_ARCHIVED_SESSIONS);
    storage.local.clear();
  });

  /* A search whose only hit was archived drew a lone "Archived 1"; arrows and Enter skipped it. */
  it("draws archived search matches with Archived shut, where arrows and Enter reach them", () => {
    /* The search box is component state, which server rendering can't type into. */
    assert.match(controls, /archiveOpen \|\| sessionSearchTerm \? archivedItems : archivedItems\.filter\(\(session\) => session\.workflow_id === sessionId\)/);
    assert.match(controls, /const drawnGroups = \$derived\(\s*archivedDrawn\.length \? \[\.\.\.sessionGroups, \{ label: ARCHIVED, sessions: archivedDrawn \}\] : sessionGroups/);
    assert.match(controls, /const filteredIds = \$derived\(drawnGroups\.flatMap/, "arrows and Enter walk what is drawn");
    assert.match(controls, /archiveToggles = \$derived\(!sessionSearchTerm && /, "a plain heading while searching, not a toggle that does nothing");
    assert.match(controls, /group\.label === ARCHIVED && archiveToggles\s*\? \{ kind: "archive"/);
    assert.match(controls, /if \(!archivedDrawn\.length && archivedItems\.length\) out\.push\(/, "the shut toggle stays when nothing archived is drawn");
  });

  /* Archiving the session open in chat with Archived shut left nothing marking what was in chat. */
  it("keeps the open session's row in sight after it is archived into a shut group", () => {
    const { storage } = installBrowserSurface();
    const sessions = [session("wf-a"), session("wf-open"), session("wf-other")];
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ archivedSessions: ["wf-open", "wf-other"] }));
    const body = pane({ sessions, sessionId: "wf-open" });
    const ids = [...body.matchAll(/id="session-option-([^"]+)" class="session-row/g)].map((m) => m[1]);
    assert.deepEqual(ids, ["wf-a", "wf-open"], "only the open one of the archived rows");
    assert.ok(body.indexOf('aria-expanded="false">Archived') < body.indexOf('id="session-option-wf-open"'), "under its shut toggle, in its archived place");
    assert.match(body, /aria-expanded="false">Archived <span[^>]*>2</, "the count is still the whole group");
    assert.equal(body.match(/archive-toggle/g)?.length, 1, "one toggle");
    storage.local.clear();
  });

  /* "Archived 1" drew a collapsed chevron over the one archived row, the open one, listed under it. */
  it("draws a plain heading, no collapsed toggle, when the open session is all a shut group holds", () => {
    const { storage } = installBrowserSurface();
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ archivedSessions: ["wf-open"] }));
    const body = pane({ sessions: [session("wf-a"), session("wf-open")], sessionId: "wf-open" });
    assert.match(body, /<div class="group-heading[^"]*" aria-hidden="true">Archived <span[^>]*>1<\/span><\/div>/);
    assert.doesNotMatch(body, /archive-toggle/, "nothing hidden, so nothing to open");
    assert.match(body, /id="session-option-wf-open"/);
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ archivedSessions: ["wf-open"], sessionArchiveOpen: true }));
    assert.match(pane({ sessions: [session("wf-a"), session("wf-open")], sessionId: "wf-open" }), /aria-expanded="true">Archived/, "an open group can still be shut");
    storage.local.clear();
  });

  /* Seven sessions, all archived, drew a blank list over a lone "Archived 7". */
  it("says every session is archived when nothing is left above the Archived group", () => {
    const { storage } = installBrowserSurface();
    const sessions = [session("wf-a"), session("wf-b")];
    storage.local.setItem("temporal-agent-ui.prefs.v1", JSON.stringify({ archivedSessions: ["wf-a", "wf-b"] }));
    const body = pane({ sessions, sessionId: "" });
    assert.match(body, /<div class="session-empty[^"]*"><strong>Every session is archived<\/strong><\/div>/);
    assert.match(body, /aria-expanded="false">Archived <span[^>]*>2</);
    assert.doesNotMatch(pane({ sessions: [session("wf-c"), ...sessions], sessionId: "" }), /Every session is archived/);
    storage.local.clear();
  });

  /* 2,000 archived of 10,000 kept only 500: the rest silently came back to the list. */
  it("keeps 2,000 archives, and prunes gone ids only against a whole list", () => {
    const { storage } = installBrowserSurface();
    const key = "temporal-agent-ui.prefs.v1";
    const many = Array.from({ length: 2_000 }, (_, i) => `wf-${i}`);
    storage.local.setItem(key, JSON.stringify({ archivedSessions: many }));
    assert.ok(MAX_ARCHIVED_SESSIONS >= 5_000);
    assert.equal(toggleArchivedSession("wf-new").length, 2_001, "nothing falls off the end");

    assert.deepEqual(toggleArchivedSession("wf-x", ["wf-x", "wf-1", "wf-new"]), ["wf-x", "wf-new", "wf-1"], "a whole list drops ids with no session");
    toggleArchivedSession("wf-unlisted");
    assert.deepEqual(toggleArchivedSession("wf-y"), ["wf-y", "wf-unlisted", "wf-x", "wf-new", "wf-1"], "no list, no pruning");

    /* Only a list the server sent, with no error, under the discovery limit, is whole. */
    assert.match(controls, /if \(!sessionsLoaded \|\| sessionsError\) return undefined;/);
    assert.match(controls, /session\.is_discovered\)\.length >= DISCOVERY_LIMIT\) return undefined;/);
    assert.match(controls, /toggleArchivedSession\(workflowId, wholeList\(\)\)/);
    assert.match(appSource, /sessionsLoaded=\{run\.sessionsLoaded\}/);
    storage.local.clear();
  });

  it("drops the agent label from sight when one agent is listed or one is filtered", () => {
    const single = pane({ sessions: [session("wf-a"), session("wf-b")], sessionId: "" });
    assert.match(single, /class="session-agent visually-hidden[^"]*" dir="auto">Hello</);
    const mixed = pane({ sessions: [session("wf-a"), session("wf-b", { agent_workflow_type: "Other" })], sessionId: "" });
    assert.match(mixed, /class="session-agent[^"]*" dir="auto">Hello</);
    assert.doesNotMatch(mixed, /session-agent visually-hidden/);
    assert.match(controls, /const showAgent = \$derived\(!agentFilter && agentOptions\.length > 1\);/);
    assert.match(single, /class="session-title[^"]*" dir="auto">Title wf-a</);
  });

  it("fades a clipped title and agent toward their end instead of an ellipsis", () => {
    const body = pane({ sessions: [session("wf-a"), session("wf-b", { agent_workflow_type: "Other" })], sessionId: "" });
    assert.match(body, /class="session-title fade-end[^"]*"/);
    assert.match(body, /class="session-agent fade-end[^"]*"/);
    assert.doesNotMatch(rule(".session-title"), /text-overflow/);
    assert.doesNotMatch(rule(".session-agent"), /text-overflow/);
    const fade = appCss.slice(appCss.indexOf("\n.fade-end {"), appCss.indexOf("@keyframes fade-end-rtl {") + 400);
    for (const end of ["right", "left"]) {
      assert.match(fade, new RegExp(`-webkit-mask-image: linear-gradient\\(to ${end}, #000 calc\\(100% - var\\(--fade-width\\)\\), transparent\\);`));
      assert.match(fade, new RegExp(`\\n\\s*mask-image: linear-gradient\\(to ${end}, #000 calc\\(100% - var\\(--fade-width\\)\\), transparent\\);`));
    }
    assert.match(fade, /\.fade-end:dir\(rtl\) \{\s*animation-name: fade-end-rtl;/, "RTL text fades on the left");
    assert.match(fade, /animation-timeline: scroll\(self inline\);/, "text that fits is never faded");
  });

  it("reads every row's age from one minute clock", () => {
    assert.equal(controls.match(/everyMinute\(/g)?.length, 1);
    assert.match(controls, /\$effect\(\(\) => \(display === "pane" \? everyMinute\(\(ms\) => \(now = ms\)\) : undefined\)\);/);
    assert.match(controls, /sessionAge\(item\.created_at, now\)/);
    assert.match(controls, /sessionDay\(session\.created_at, today\)/);
  });

  it("puts search and the agent filter on one line that wraps in a narrow drawer", () => {
    const body = pane({ sessions: [session("wf-a"), session("wf-b", { agent_workflow_type: "Other" })], sessionId: "" });
    assert.match(body, /<div class="session-filters[^"]*"><label class="field[^"]*">[\s\S]*?aria-label="Search sessions"[\s\S]*?<\/label>\s*<span class="field select[^"]*">\s*<select aria-label="Agent"><option value="" selected="">All agents<[\s\S]*?<\/span><\/div>/);
    assert.doesNotMatch(body, /select-lead/);
    assert.match(rule(".session-filters"), /display: flex;\s*flex-wrap: wrap;/);
    assert.match(rule(".session-filters > .field"), /flex: 1 1 12rem;/);
    assert.match(rule(".session-filters > :global(.field.select)"), /flex: 0 1 11rem;/);
  });
});

/* After the dev server restarts, every cached session's workflow is NOT_FOUND. A stopped
   worker is different: the workflow is there and only slow, so it keeps its "still
   connecting" state. Only a definitive NotFound makes a session gone. */
describe("a session whose workflow is gone", () => {
  const live = (id) => ({ workflow_id: id, agent_workflow_type: "Hello", created_at: Date.now() / 1000 - 60, initial_user_message: `Title ${id}`, execution_status: "RUNNING", closed: false });

  function boot({ status, agentStatus }) {
    const calls = { attach: 0, agentStatus: 0 };
    const api = {
      async listAgents() { return { agents: [] }; },
      async listSessions() { return []; },
      async agentInterface() { return []; },
      async agentStatus() {
        calls.agentStatus += 1;
        return agentStatus();
      },
      async workflowStatus(workflowId) {
        const execution_status = status();
        return { workflow_id: workflowId, execution_status, closed: execution_status !== "RUNNING" };
      },
      // eslint-disable-next-line require-yield
      async *attach() {
        calls.attach += 1;
      }
    };
    const controller = new AgentRunController(api);
    controller.idlePollMs = 20;
    controller.sessions = [live("wf-a")];
    return { controller, calls };
  }

  beforeEach(() => {
    installBrowserSurface();
  });

  it("is marked by a NotFound, not by a generic 500 or a network error, and stops polling", async () => {
    let execution = "RUNNING";
    const { controller, calls } = boot({
      status: () => execution,
      agentStatus: () => {
        throw calls.agentStatus % 2 ? new ApiError("Internal Server Error", null, 500) : new TypeError("Failed to fetch");
      }
    });
    void controller.selectSession("wf-a");
    await waitFor("the idle poll to fail a few times", () => calls.agentStatus >= 4);
    assert.equal(controller.sessionGone, false, "a stopped worker or a blip is not gone");
    assert.equal(controller.sessionClosed, false);

    execution = "NOT_FOUND";
    await waitFor("Temporal to say the workflow is gone", () => controller.sessionGone);
    assert.equal(controller.sessionClosed, true);
    assert.equal(controller.sessions[0].execution_status, "NOT_FOUND", "the row reads it too");
    const polled = calls.agentStatus;
    await sleep(200);
    assert.equal(calls.agentStatus, polled, "no more polls at a gone workflow");
    assert.equal(calls.attach, 1);
  });

  it("is marked straight from a status call that answers NotFound", async () => {
    const { controller, calls } = boot({
      status: () => "RUNNING",
      agentStatus: () => {
        throw new ApiError("RPCError: (5, 'workflow not found for ID: wf-a')", null, 500);
      }
    });
    void controller.selectSession("wf-a");
    await waitFor("the session to read as gone", () => controller.sessionGone);
    await sleep(100);
    assert.equal(calls.agentStatus, 1);
    await assert.rejects(controller.endSession("wf-a"), /no longer exists/, "nothing is sent to it");
  });

  it("opens read-only: no stream, composer disabled, approvals off, an empty state with no cache", async () => {
    const { controller, calls } = boot({ status: () => "NOT_FOUND", agentStatus: () => ({}) });
    await controller.selectSession("wf-a");
    assert.equal(controller.sessionGone, true);
    assert.equal(controller.sessionClosed, true);
    assert.equal(calls.attach, 0, "nothing to stream from");
    await assert.rejects(controller.approveTool("wf-a", "t1", true), /no longer exists/);

    const chat = stripComments(render(AgentChatPanel, {
      props: { items: [], logs: [], agentLabel: "Hello", sessionId: "wf-a", closed: controller.sessionClosed }
    }).body);
    assert.match(chat, /<textarea[^>]*placeholder="Session ended"[^>]*disabled/);

    const app = readFileSync(fileURLToPath(new URL("../src/App.svelte", import.meta.url)), "utf8");
    assert.match(app, /onApproveTool=\{run\.sessionGone \? undefined :/, "approval buttons read as busy");
    assert.match(app, /onCallbackResult=\{run\.sessionGone \? undefined :/);
    assert.match(app, /pane\.kind === "chat" && run\.sessionGone && run\.frames\.length === 0\}\s*<EmptyState title="This session's workflow no longer exists and nothing was cached" \/>/);
  });

  /* It drew the same hollow pip as a session a person closed, though it can't even be opened. */
  it("draws its row muted with its own unavailable glyph, in row, card and nav, and says why in the tip", () => {
    const gone = { ...live("wf-gone"), execution_status: "NOT_FOUND", closed: true };
    const failed = { ...live("wf-failed"), execution_status: "FAILED", closed: true };
    const body = stripComments(render(SessionControls, {
      props: { display: "pane", sessions: [gone, failed, live("wf-b")], sessionId: "", onEndSession: async () => {} }
    }).body);
    const rowOf = (id) => body.split('<div class="session-item').find((chunk) => chunk.includes(`id="session-option-${id}"`));
    const row = rowOf("wf-gone");
    assert.match(row, /class="session-row closed[^"]*"/);
    const glyphs = [...row.matchAll(/<span class="session-glyph[^"]*"[^>]*>([\s\S]*?)<\/span>/g)];
    assert.equal(glyphs.length, 2, "the row and its card");
    for (const [whole] of glyphs) {
      assert.match(whole, /--status-color: var\(--text-4\)/, "the closed tone, no new color");
      assert.match(whole, /lucide-circle-x/);
      assert.doesNotMatch(whole, /session-pip/, "not the closed pip");
    }
    assert.match(rowOf("wf-failed"), /--status-color: var\(--error\)/, "a failure keeps the error tone");
    assert.match(row, /<span class="visually-hidden[^"]*">, Unavailable,<\/span>/);
    assert.match(row, /class="tip-card-value[^"]*" dir="auto">Workflow no longer exists in Temporal</);
    assert.doesNotMatch(row, /session-end/, "nothing to end");

    const launcher = stripComments(render(SessionControls, {
      props: { display: "launcher", sessions: [gone], sessionId: "wf-gone", closed: true, gone: true }
    }).body);
    assert.match(launcher, /class="kicker session-state[^"]*">Unavailable</);
    assert.match(launcher, /class="session-glyph[^"]*"[^>]*--status-color: var\(--text-4\)[^>]*>[\s\S]*?lucide-circle-x/, "the nav draws the same glyph");
    assert.match(launcher, /Workflow no longer exists in Temporal\. What this browser cached is shown read-only\./);
  });
});
