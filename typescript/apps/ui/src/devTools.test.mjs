// ABOUTME: Pins the dev tools button: drawn from the console's own primitives and tokens, which
// corner a dragged button lands in, what switching fixtures keeps, the errors and dev-server state
// it reports, and that it lists the fixtures from their catalog, in dev builds only.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface, stripComments } from "../tests/support/controllerHarness.mjs";
import DevTools, { clearSavedState, fixtureUrl, liveUrl, snapCorner, withRecent } from "./DevTools.svelte";
import { RECENT, scenarioSections } from "./ScenarioPicker.svelte";
import { clearDevErrors, collectDevErrors, devErrors, recordDevError } from "./devErrors.svelte.ts";

const main = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
const source = readFileSync(new URL("./DevTools.svelte", import.meta.url), "utf8");

const catalog = [
  { name: "demo", label: "Demo JSON", group: "Chat" },
  { name: "sessionsbreak", label: "Pinned and archived", group: "Sessions", variants: { demo: "Demo", worst: "Worst case", huge: "10,000" } },
  { name: "agents", label: "11 agents", group: "Agents" }
];
const props = { catalog, current: "agents", variant: "" };
const listed = (sections) => sections.map((section) => `${section.group}: ${section.rows.map((row) => (row.data === undefined ? row.label : `↳${row.label}`)).join(", ")}`);

describe("the dev tools button", () => {
  let freshStorage;
  beforeAll(() => ({ freshStorage } = installBrowserSurface()));

  it("draws its panel from the console's head, IconButton and Chip, with the scenario picker", () => {
    freshStorage();
    sessionStorage.setItem("temporal-agent-ui.dev-tools-open", "1");
    const body = stripComments(render(DevTools, { props: { ...props, badge: "11 agents" } }).body);
    assert.match(body, /<header class="surface-head[^"]*">\s*<h2 class="head-name[^"]*"><span class="kicker head-kind[^"]*">Dev tools<\/span>/);
    assert.match(body, /<button[^>]*class="[^"]*icon-button[^"]*"[^>]*aria-label="Hide dev tools"/, "Hide is the shared IconButton");
    assert.match(body, /<input[^>]*placeholder="Search 3 scenarios"[^>]*aria-label="Search scenarios"[^>]*role="combobox"/, "a search field over the list");
    assert.match(body, /<select aria-label="Group">\s*<option value="" selected="">All groups<\/option>/, "the group menu is the shared Select");
    assert.match(body, /role="listbox" aria-label="Scenarios"/);
    assert.match(body, /<span aria-hidden="true" class="chip [^"]*">\s*11 agents<\/span>/, "the badge is a Chip");
    assert.match(body, /aria-label="Dev tools, 11 agents" aria-expanded="true"/);
  });

  it("colors and spaces itself with the console's tokens, not values of its own", () => {
    const style = source.slice(source.indexOf("<style>"));
    assert.doesNotMatch(style, /#[0-9a-f]{3,8}\b|rgba?\(/i, "a color outside the tokens");
  });

  /* Built from the console's parts, it must still not be taken for one of them. */
  it("marks itself as development tooling, apart from the console's own chrome", () => {
    const style = source.slice(source.indexOf("<style>"));
    assert.match(style, /--dev-accent: var\(--success\);/, "one phosphor hue, defined once");
    assert.match(style, /\.dev-tools-button \{[^}]*background:\s*repeating-linear-gradient/, "scanlines on the chip");
    assert.doesNotMatch(style, /\.dev-tools-button::(before|after)/, "its pseudo-elements are its tooltip's");
    assert.match(style, /\.dev-tools-launcher :global\(\.chip\) \{\s*opacity: 0;/, "the fixture's name waits for a hover");
    assert.match(style, /\[aria-expanded="true"\] \.dev-tools-mark \.v \{[^}]*stroke-dasharray: 52\.4 48\.1;/, "open, the rings fold into the heart");
    assert.match(style, /\.dev-tools-panel \{[^}]*repeating-linear-gradient/, "a panel under a hazard band");
    freshStorage();
    sessionStorage.setItem("temporal-agent-ui.dev-tools-open", "1");
    const body = stripComments(render(DevTools, { props }).body);
    assert.match(body, /Development only · not in production builds/);
  });

  it("snaps to the corner nearest where it was let go", () => {
    assert.equal(snapCorner(100, 100, 0, 0, 1000, 800), "top-left");
    assert.equal(snapCorner(900, 100, 0, 0, 1000, 800), "top-right");
    assert.equal(snapCorner(100, 700, 0, 0, 1000, 800), "bottom-left");
    assert.equal(snapCorner(900, 700, 0, 0, 1000, 800), "bottom-right");
  });

  it("carries a throw past the middle into the far corner", () => {
    assert.equal(snapCorner(450, 700, 0, 0, 1000, 800), "bottom-left", "let go just left of center");
    assert.equal(snapCorner(450, 700, 1, 0, 1000, 800), "bottom-right", "thrown right at 1px/ms, it lands 150px further");
  });

  it("switches fixtures without the last one's variant or open session, keeping the panes", () => {
    const next = new URL(fixtureUrl("http://h/?mock=demo&data=worst&s=agent-session-mock-2&p=graph%2Cchat&devtools", "http://h/?mock=agents"));
    assert.equal(next.searchParams.get("mock"), "agents");
    assert.equal(next.searchParams.has("data"), false);
    assert.equal(next.searchParams.has("s"), false, "a session from another fixture opens nothing in this one");
    assert.equal(next.searchParams.get("p"), "graph,chat");
    assert.equal(next.searchParams.has("devtools"), true);
  });

  it("switches to the live server without the fixture or its parameters, keeping the panes", () => {
    const live = new URL(liveUrl("http://h/?mock=sessionsbreak&data=worst&s=agent-session-mock-2&p=graph%2Cchat"));
    assert.deepEqual([...live.searchParams.keys()], ["p"]);
    assert.equal(live.searchParams.get("p"), "graph,chat");
  });

  it("resets what the console saved, but not where the dev tools were put", () => {
    freshStorage();
    localStorage.setItem("temporal-agent-ui.prefs.v1", "{}");
    localStorage.setItem("temporal-agent-ui.dev-tools-corner", "bottom-left");
    localStorage.setItem("agent-kit-client-tool:abc", "answered");
    localStorage.setItem("someone-else", "1");
    sessionStorage.setItem("temporal-agent-ui.mock-seeded", "demo:");
    sessionStorage.setItem("temporal-agent-ui.dev-tools-open", "1");
    clearSavedState([localStorage, sessionStorage]);
    assert.equal(localStorage.getItem("temporal-agent-ui.prefs.v1"), null);
    assert.equal(localStorage.getItem("agent-kit-client-tool:abc"), null, "answered client tools ask again");
    assert.equal(sessionStorage.getItem("temporal-agent-ui.mock-seeded"), null, "the fixture seeds its layout again");
    assert.equal(localStorage.getItem("temporal-agent-ui.dev-tools-corner"), "bottom-left");
    assert.equal(sessionStorage.getItem("temporal-agent-ui.dev-tools-open"), "1", "the panel is still open after the reload");
    assert.equal(localStorage.getItem("someone-else"), "1", "another app's keys on the same origin");
  });

  it("loads on every dev page, live or mock, so ⌥D always has something to bring back", () => {
    assert.match(main, /\nif \(import\.meta\.env\.DEV\) \{[\s\S]*\(await import\("\.\/devTools"\)\)\.mountDevTools\(\{ catalog: entries, current: mock \?\? "", variant, badge \}\);\n\}/);
    assert.equal(scenarioSections(catalog, { current: "", recent: [], query: "", group: "" })[0].rows[0].label, "Live API", "the live API is the first choice");
    assert.doesNotMatch(main, /createElement\("nav"\)/, "no fixture bar of its own on the page");
  });

  it("lists the recent fixtures first, then every group, with the loaded one's variants under it", () => {
    const sections = scenarioSections(catalog, { current: "sessionsbreak", recent: ["sessionsbreak", "demo"], query: "", group: "" });
    assert.deepEqual(listed(sections), [
      `${RECENT}: Pinned and archived, ↳Demo, ↳Worst case, ↳10,000, Demo JSON`,
      "Live: Live API",
      "Chat: Demo JSON",
      "Sessions: Pinned and archived",
      "Agents: 11 agents"
    ]);
    assert.equal(listed(scenarioSections(catalog, { current: "", recent: [], query: "", group: "" }))[0], "Live: Live API", "no Recent until something is loaded");
  });

  it("narrows by search, a variant's name included, and by group", () => {
    assert.deepEqual(listed(scenarioSections(catalog, { current: "agents", recent: [], query: "10,000", group: "" })), ["Sessions: Pinned and archived, ↳10,000"], "only the variant that matches");
    assert.deepEqual(listed(scenarioSections(catalog, { current: "agents", recent: ["agents"], query: "", group: "Chat" })), ["Chat: Demo JSON"], "a group hides Recent");
    assert.deepEqual(scenarioSections(catalog, { current: "", recent: [], query: "nothing like it", group: "" }), []);
    const variantRow = scenarioSections(catalog, { current: "sessionsbreak", recent: [], query: "worst", group: "" })[0].rows[1];
    assert.deepEqual([variantRow.entry.name, variantRow.data], ["sessionsbreak", "worst"], "a variant row loads its fixture with its data");
  });

  it("keeps the last four loaded, newest first, and ignores anything stored that isn't a name", () => {
    assert.deepEqual(withRecent(["a", "b", "c", "d"], "c"), ["c", "a", "b", "d"]);
    assert.deepEqual(withRecent(["a", "b", "c", "d"], "e"), ["e", "a", "b", "c"]);
    assert.deepEqual(withRecent({ not: "a list" }, "a"), ["a"]);
    assert.deepEqual(withRecent(["a", 3, null], ""), ["", "a"], "the live API is a recent choice too");
  });

  it("keeps the page's errors, newest first, one entry per message with a count", () => {
    clearDevErrors();
    recordDevError("a", 1);
    recordDevError("b", 2);
    recordDevError("a", 3);
    assert.deepEqual(devErrors.map((e) => [e.message, e.count, e.last]), [["a", 2, 3], ["b", 1, 2]]);
    for (let i = 0; i < 60; i++) recordDevError(`e${i}`);
    assert.equal(devErrors.length, 50, "the browser console keeps the rest");
    clearDevErrors();
    assert.equal(devErrors.length, 0);
  });

  it("counts uncaught errors and unhandled rejections from the window", () => {
    clearDevErrors();
    const target = new EventTarget();
    collectDevErrors(target);
    target.dispatchEvent(Object.assign(new Event("error"), { message: "x is not a function" }));
    target.dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: new Error("fetch failed") }));
    assert.deepEqual(devErrors.map((e) => e.message), ["Unhandled rejection: fetch failed", "x is not a function"]);
    clearDevErrors();
  });

  it("badges the button with the error count, and lists them in the panel", () => {
    freshStorage();
    clearDevErrors();
    recordDevError("x is not a function");
    recordDevError("x is not a function");
    recordDevError("Unhandled rejection: fetch failed");
    const closed = stripComments(render(DevTools, { props: { ...props, badge: "11 agents" } }).body);
    assert.match(closed, /aria-label="Dev tools, 11 agents, 2 errors"/);
    assert.match(closed, /<span class="dev-tools-count[^"]*" aria-hidden="true">2<\/span>/);
    sessionStorage.setItem("temporal-agent-ui.dev-tools-open", "1");
    const opened = stripComments(render(DevTools, { props }).body);
    assert.doesNotMatch(opened, /dev-tools-count/, "open, the heart outranks the badge; the panel lists them");
    assert.match(opened, /<span class="dev-tools-error-message[^"]*">x is not a function<\/span><span class="dev-tools-error-times[^"]*">×2<\/span>/);
    clearDevErrors();
  });

  it("reads the dev server's own events: an update turns the mark, a lost connection greys it", () => {
    for (const event of ["vite:beforeUpdate", "vite:afterUpdate", "vite:ws:disconnect", "vite:ws:connect"]) {
      assert.match(source, new RegExp(`\\["${event}", `), `listens for ${event}`);
    }
    assert.match(source, /const hot = import\.meta\.hot;\s*if \(!hot\) return;/, "nothing outside vite dev");
    assert.match(source, /onanimationiteration=\{\(\) => !updating && \(turning = false\)\}/, "stops upright, after the update lands");
    assert.match(source, /\.dev-tools-button\[data-offline\] \{\s*--dev-accent: var\(--text-3\);/);
    assert.match(main, /collectDevErrors\(\);[\s\S]*mount\(App,/, "collecting starts before the console mounts");
  });

  it("lists the fixtures from the catalog, not a list of its own", () => {
    assert.match(main, /const catalog = \(await import\("\.\/lib\/mock\/catalog"\)\)\.mockCatalog\(\);/);
    assert.doesNotMatch(main, /const (labels|groups|variants): Record</, "no hand-kept names, groups or variants");
    assert.match(main, /const variants = listed\?\.variants;/, "variants come from the fixture's own listing");
  });
});
