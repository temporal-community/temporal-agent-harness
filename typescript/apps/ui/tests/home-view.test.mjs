// ABOUTME: With no session open the console shows the agents there are to start in place of the
// desk, with the sessions there are to open in the sidebar, the way Temporal's UI lists workflows before one is
// opened. A link that names a session goes straight to the desk. Boot's half (opening only the
// linked session, starting nothing) is pinned in bootSession.test.mjs; this reads App's wiring.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import HomeView, { agentsWithoutWorker } from "../src/lib/components/chat/HomeView.svelte";
import { createPaneStack, makePane } from "../src/lib/state/paneStack.svelte.ts";
import { stripComments } from "./support/controllerHarness.mjs";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

describe("the console with no session open", () => {
  it("shows it whenever no session is open, except while boot opens a linked one", () => {
    assert.match(app, /const linkedAtLoad = readUrlSessionId\(\) != null;/);
    assert.match(app, /const home = \$derived\(!run\.session && !\(booting && linkedAtLoad\)\);/);
  });

  /* Not a copy of the desk: the one rail, drawer and transport row draw the home stacks with
     no session open, so every behavior and every fix reaches both. A fixed stack only hides
     close and pin. */
  it("draws the home panes on the session desk's own rail, drawer and transport row", () => {
    assert.match(app, /const deskStack = \$derived\(home \? homeStack : stack\);/);
    assert.match(app, /const deskDrawer = \$derived\(home \? homeDrawer : drawer\);/);
    assert.match(app, /<PaneRail\s+bind:this=\{rail\}\s+stack=\{deskStack\}/);
    assert.match(app, /\{#if deskDrawer\.groups\.length > 0\}\s*<section class="drawer"[\s\S]*?stack=\{deskDrawer\}/);
    assert.equal(app.match(/^\s*<PaneRail$/gm)?.length, 2, "one rail and one drawer, whichever desk is up");
    assert.match(
      app,
      /\{#if home\}[\s\S]*?<HomeView part="status" \{\.\.\.homeProps\} \{drawerOpen\} onToggleDrawer=\{homeDrawer\.groups\.length > 0 \? toggleDrawer : undefined\} \/>\s*\{:else\}\s*<StepController/,
      "the status line stands in the transport's row and carries its drawer switch"
    );
    for (const use of ["drawerOpen = $derived(deskDrawer", "const holding = deskDrawer.groups", "deskDrawer.foldAll(", "deskStack.exitBleed()", "class:has-drawer={deskDrawer"]) {
      assert.ok(app.includes(use), `the drawer logic reads the desk on screen: ${use}`);
    }
    assert.match(
      app,
      /initial: \[\s*\[\{ \.\.\.makePane\(\{ kind: "setup" \}\), split: true \}, \{ \.\.\.makePane\(\{ kind: "releases" \}\), split: true \}\],\s*\[\{ \.\.\.makePane\(\{ kind: "bay" \}\), split: true \}, \{ \.\.\.makePane\(\{ kind: "capabilities" \}\), split: true \}\]\s*\],\s*fixed: true/,
      "setup over releases, then the bay over features"
    );
    const kinds = readFileSync(new URL("../src/lib/panes/registry.ts", import.meta.url), "utf8");
    assert.match(kinds, /capabilities: \{[^}]*flexible: true/, "features stretches over spare room, as the state flow does");
    assert.doesNotMatch(kinds, /\bbrief: \{/, "no brief: the home desk is built from the rail's own panes");
    assert.doesNotMatch(kinds, /bay: \{[^}]*flexible: true/);
    assert.match(app, /createPaneStack\(\{ initial: \[\], single: true, fixed: true, peer: homeStack \}\)/, "releases opens on demand, not in the drawer");
    assert.match(app, /onToggleDrawer=\{homeDrawer\.groups\.length > 0 \? toggleDrawer : undefined\}/);
    assert.match(app, /onStart: startSession,/);
    assert.match(
      app,
      /for \(const kind of \["capabilities", "bay", "releases", "setup"\] as const\) \{\s*if \(!homeStack\.has\(kind\) && !homeDrawer\.has\(kind\)\) homeStack\.openPane\(\{ kind \}\);/,
      "a saved home layout missing a pane gets it back"
    );
    const registry = readFileSync(new URL("../src/lib/panes/registry.ts", import.meta.url), "utf8");
    assert.match(registry, /PANE_KINDS = \(Object\.keys\(PANE_META\) as PaneKind\[\]\)\.filter\(\(kind\) => !PANE_META\[kind\]\.home\)/, "a session's launcher never offers a home pane");
  });

  /* Every row and count is the server's: nothing in the view names an agent or a session. */
  it("racks each registered agent as a row that opens New session on it", () => {
    const props = {
      agents: [
        { key: "a", label: "Alpha", workflow_type: "AlphaAgent", task_queue: "alpha-q", description: "Answers.", worker: { status: "ready" } },
        { key: "b", label: "Beta", workflow_type: "BetaAgent", task_queue: "beta-q", description: "", worker: { status: "no_worker" } }
      ],
      sessions: [{ workflow_id: "w1", closed: false }, { workflow_id: "w2", closed: true }],
      onStart() {}
    };
    const part = (name) => stripComments(render(HomeView, { props: { ...props, part: name } }).body);
    const bay = part("bay");
    assert.equal(bay.match(/<th scope="row"[^>]*>\s*<button type="button" class="row-button[ "]/g)?.length, 2, "each agent's name heads its row and holds its button");
    assert.match(bay, /Alpha<\/span>[\s\S]*?Answers\.[\s\S]*?alpha-q[\s\S]*?Beta<\/span>/);
    assert.match(bay, /No worker[\s\S]*?Beta<\/span>[\s\S]*?BetaAgent/, "a row's worker status leads it, and a blank description falls back to the workflow type");
    assert.doesNotMatch(bay, />#<|>01</, "rows are not numbered: the order is the registry's and nothing refers to it");
    const setup = part("setup");
    assert.doesNotMatch(setup, /metric-strip|No worker|Running/, "setup leaves the counts to the bay and the Sessions sidebar");
    assert.match(setup, /Start the three services[\s\S]*?Each in its own terminal/, "the step is the README's, by its own name");
    assert.match(setup, /Temporal service[\s\S]*?temporal server start-dev[\s\S]*?Session manager, API and console[\s\S]*?Your Python agent worker/, "each command says which service it starts");
    const features = part("capabilities");
    assert.equal(features.match(/<a class="cap[^"]*" href="https:\/\/github\.com\/[^"]+\/docs\/guides\/[a-z]+\.md#/g)?.length, 6, "each feature links the guide section that explains it");
    assert.doesNotMatch(setup, />\s*Examples\s*<|>\s*PyPI\s*</, "setup keeps only its quickstart; examples and the package page live elsewhere");
    assert.match(setup, /Start the three services[\s\S]*?Quickstart/, "the quickstart link sits on the step it mirrors");
    const releases = part("releases");
    assert.match(releases, /href="https:\/\/pypi\.org\/project\/temporal-agent-kit\/"[\s\S]*?temporal-agent-kit on PyPI/, "each package links its registry page");
    assert.match(releases, /href="https:\/\/www\.npmjs\.com\/package\/@temporalio\/agent-kit"[\s\S]*?@temporalio\/agent-kit on npm/);
    assert.match(releases, /href="https:\/\/github\.com\/[^"]+\/releases"[\s\S]*?Release notes/);
    assert.doesNotMatch(releases, /\d+\.\d+\.\d+/, "no version is typed in: the registries and GitHub have the current one");
    assert.match(part("status"), /aria-label="Examples"/, "examples join the status line's other links");
    for (const name of ["bay", "setup", "releases", "capabilities"]) {
      assert.doesNotMatch(part(name), /<footer/, `${name} has no pinned footer to crowd a stacked pane`);
    }
    const status = part("status");
    assert.doesNotMatch(status, /without a worker|PyPI|npm/, "each pane's status rides in its own head, not the status line");
    assert.doesNotMatch(status, /running/, "sessions open from the header's sidebar button, not the status line");
    assert.equal(agentsWithoutWorker(props.agents), 1);
    assert.match(app, /case "bay": \{[\s\S]*?title: PANE_META\.bay\.kindLabel,/, "the bay is named by its kind alone; its rows show how many agents there are");
    assert.match(
      app,
      /case "bay": \{\s*const idle = agentsWithoutWorker\(run\.agents\);[\s\S]*?statusLabel: idle > 0 \? `\$\{idle\} without a worker` : null,\s*statusTone: idle > 0 \? "--live" : null/,
      "the bay's head carries its agents without a worker, as the rows' chips do"
    );
    const view = readFileSync(new URL("../src/lib/components/chat/HomeView.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(view, /\.setup \{\s*display: grid;\s*grid-template-columns/, "the prompt keeps its own row at any width, the services below it");
    assert.match(view, /id: "agent"[\s\S]*?id: "worker"[\s\S]*?id: "running"[^\n]*size: 64,[\s\S]*?id: "queue"[\s\S]*?id: "seen"/, "the agent first, then whether it can start, and Running wide enough for its header wherever it is moved");
    assert.match(view, /<DataTable\s+class=\{`bay-cols density-\$\{bayDensity\.key\}`\}/, "the bay is the console's DataTable, its columns sortable");
    assert.match(view, /onRowClick=\{\(row\) => \(picked = row\.agent\.workflow_type\)\}/);
    assert.match(view, /<StartSessionDialog[\s\S]*?preferredAgentType=\{picked \|\| undefined\}[\s\S]*?\{onStart\}/);
  });

  /* The bay says how many of each agent's sessions run; the sessions themselves are the Sessions
     sidebar's, the one list of them, open by default when there is no session to show. */
  it("counts each agent's running sessions and leaves the list to the sidebar", () => {
    const props = {
      agents: [{ key: "a", label: "Alpha", workflow_type: "AlphaAgent", task_queue: "alpha-q", description: "", worker: { status: "ready" } }],
      sessions: [
        { workflow_id: "old", agent_workflow_type: "AlphaAgent", created_at: 100, label: "Old run", closed: false },
        { workflow_id: "new", agent_workflow_type: "AlphaAgent", created_at: 200, label: "New run", closed: false },
        { workflow_id: "boom", agent_workflow_type: "AlphaAgent", created_at: 150, label: "Boom", closed: true, execution_status: "TIMED_OUT" }
      ],
      onStart() {}
    };
    assert.match(stripComments(render(HomeView, { props: { ...props, part: "bay" } }).body), /class="count live[^"]*">2</, "Alpha's two running sessions, in its Running column");
    const kinds = readFileSync(new URL("../src/lib/panes/registry.ts", import.meta.url), "utf8");
    assert.doesNotMatch(kinds, /\bsessions: \{/, "no sessions pane: the sidebar lists them");
    assert.match(app, /sessionManagerParam == null \? readUrlSessionId\(\) == null : sessionManagerParam === "1"/, "the sidebar opens with no session to show");
    assert.match(app, /if \(open === home\) url\.searchParams\.delete\("sm"\);\s*else url\.searchParams\.set\("sm", open \? "1" : "0"\);/, "a link carries the sidebar only where it differs from its desk's default");
    assert.match(app, /\$effect\(\(\) => persistSessionManager\(sessionManagerHeld\)\);/, "and keeps saying so as the desk changes");
  });

  /* A failed request is not an empty server: with nothing listed, the panes say they couldn't
     ask, and only say there is nothing once the server has answered. */
  it("says the server couldn't be reached rather than that nothing is registered", () => {
    const part = (name, extra) => stripComments(render(HomeView, { props: { agents: [], sessions: [], onStart() {}, onRefreshAgents() {}, part: name, ...extra } }).body);
    const down = { agentsError: "Request failed (500)" };
    assert.match(part("bay", down), /Couldn't reach the server\.[\s\S]*?Request failed \(500\)[\s\S]*?Retry/);
    assert.doesNotMatch(part("bay", down), /No agents are registered/);
    assert.match(part("bay", { ...down, refreshingAgents: true }), /Loading agents…/, "a retry in flight is still loading");
    assert.match(part("bay"), /No agents are registered yet\./, "an answer with no agents is still empty");
    const listed = stripComments(
      render(HomeView, {
        props: { part: "bay", agentsError: "Request failed (500)", sessions: [], onStart() {}, agents: [{ key: "a", label: "Alpha", workflow_type: "AlphaAgent", task_queue: "q", description: "", worker: { status: "ready" } }] }
      }).body
    );
    assert.match(listed, /role="alert">Request failed \(500\)<[\s\S]*?Alpha/, "agents already listed stay, under the error");
  });

  it("offers no close on any pane of the fixed home desk", () => {
    const kinds = ["setup", "capabilities", "releases", "bay"];
    const desk = createPaneStack({ initial: kinds.map((kind) => [makePane({ kind })]), fixed: true });
    for (const kind of kinds) assert.equal(desk.canClose(kind), false, `${kind} has no close button`);
  });

  /* Setup chooses an SDK the way the header toggles a view. */
  it("draws Setup's SDK switch as the header's toggles are", () => {
    const view = readFileSync(new URL("../src/lib/components/chat/HomeView.svelte", import.meta.url), "utf8");
    assert.equal(view.match(/\{@render sdkToggle\(/g)?.length, 1);
    assert.match(view, /<div class="row-of sdk-switch"[\s\S]*?<Chip size="xs" fill="bare" active=\{item\.key === chosen\}/, "one SDK is chosen, not filtered: bare options");
    assert.match(view, /\.sdk-switch :global\(\.chip\.bare\.active\) \{\s*border-color: var\(--border-strong\);\s*background: transparent;/, "the chosen one outlined, as the header's toggles are when on");
  });

  /* The console types in no release history: Releases names its pane, and the view never asks
     GitHub on load. */
  it("keeps release history where it is published", () => {
    const view = readFileSync(new URL("../src/lib/components/chat/HomeView.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(view, /fetch\(|api\.github\.com/, "nothing is fetched from GitHub on load");
    assert.match(app, /case "releases":\s*return \{ title: PANE_META\.releases\.kindLabel \};/);
  });

  /* The lists are the page with no session open, so the session list keeps itself current there,
     and a session open can always be left for them without ending it. */
  it("refreshes the session list on the home desk and leads back to it with Home", () => {
    assert.match(app, /if \(!home \|\| booting\) return;[\s\S]*?if \(!document\.hidden\) void run\.ensureSessionsEnriched\(\);[\s\S]*?setInterval\(refresh, 15_000\)[\s\S]*?addEventListener\("focus", refresh\)/);
    assert.match(app, /<PaneMinimap[\s\S]*?onHome=\{\(\) => run\.closeSessionView\(\)\}\s*atHome=\{home\}/, "the Temporal mark leads home");
    assert.doesNotMatch(app, /aria-label="Home"/, "no separate Home button beside it");
    const minimap = readFileSync(new URL("../src/lib/panes/PaneMinimap.svelte", import.meta.url), "utf8");
    assert.match(minimap, /\{#if onHome\}\s*<!--[\s\S]*?-->\s*<button\s+type="button"\s+class="brand-home"\s+aria-label="Home"\s+aria-current=\{atHome \? "page" : undefined\}[\s\S]*?onclick=\{onHome\}\s*>\s*\{@render mark\(\)\}/);
  });

  /* With no session the header keeps Sessions, New session and the pane map, which maps the
     home desk like any other, and drops what only a session has: its views, the replay
     shortcuts, and a Temporal link to "unknown-session". */
  it("strips the header to what applies before a session starts", () => {
    assert.match(app, /<PaneMinimap[\s\S]*?canLaunch=\{!home\}/);
    assert.match(app, /\{#snippet trail\(\)\}[\s\S]*?\{#if !home\}\s*<button[\s\S]*?Replay keyboard shortcuts/);
    assert.doesNotMatch(app, /sessionId=\{run\.runInfo\.sessionId\}\s*connecting/, "the header and list must not be handed the placeholder id");
    const minimap = readFileSync(new URL("../src/lib/panes/PaneMinimap.svelte", import.meta.url), "utf8");
    assert.match(minimap, /<div class="minimap-trail">\s*<div\s+class="ticks"/, "the pane map is drawn for every desk");
    assert.match(minimap, /\{#if canLaunch\}\s*<div class="launcher">/);
  });

  /* Nothing is hidden behind the home desk: there is one desk on screen, and the grid rows
     a session's uses. The status line takes the transport's column beside the Session Manager. */
  it("lays the home desk out on the session desk's own grid", () => {
    assert.doesNotMatch(app, /\.app\.home[\s>{]/, "no separate layout for home");
    assert.match(app, /\.app\.has-session-drawer > \.drawer,\s*\.app\.has-session-drawer > :global\(\.home-status\) \{\s*grid-column: 2;/);
  });
});
