import { mount } from "svelte";
import App from "./App.svelte";
import type { AgentApi } from "./lib/api/client";
import { watchScrollActivity } from "./lib/components/primitives/scrollActivity";
import { keepTipsInView, quietProgrammaticTips } from "./lib/components/primitives/tipFocus";
import { writeOperatorPrefs } from "./lib/state/agentRunStorage";
import "./app.css";

quietProgrammaticTips(document);
keepTipsInView(document);
watchScrollActivity(document);

const target = document.getElementById("app");
if (!target) throw new Error("Missing #app mount target");

/* Dev-only: count the page's errors from the start, for the dev tools button. */
if (import.meta.env.DEV) (await import("./devErrors.svelte")).collectDevErrors();

/* Dev-only `?mock=` fixtures. Imported dynamically: a static import keeps their top-level
   fixture data in the production bundle even though this branch is dropped from it. */
let api: AgentApi | undefined;
if (import.meta.env.DEV && new URLSearchParams(location.search).has("mock")) {
  const [{ MockAgentApi }, { jsonReplyScenarios }, { scaleScenario }] = await Promise.all([
    import("./lib/api/mockClient"),
    import("./lib/mock/jsonReplyScenarios"),
    import("./lib/mock/scaleScenarios")
  ]);
  const name = new URLSearchParams(location.search).get("mock") ?? "";
  const scenario = jsonReplyScenarios()[name] ?? scaleScenario(name);
  if (scenario) api = new MockAgentApi(scenario);
  /* Once per variant, so pins and archives made while looking survive a reload. */
  const seeded = `${name}:${new URLSearchParams(location.search).get("data") ?? ""}`;
  if (scenario?.prefs && sessionStorage.getItem("temporal-agent-ui.mock-seeded") !== seeded) {
    writeOperatorPrefs(scenario.prefs);
    sessionStorage.setItem("temporal-agent-ui.mock-seeded", seeded);
  }
}

mount(App, { target, props: { api } });

/* Dev-only: the live server and the `?mock=` fixtures to switch between, in the dev tools
   button's panel. On every dev page, so the way back to a fixture is always there. */
if (import.meta.env.DEV) {
  const params = new URLSearchParams(location.search);
  const mock = params.get("mock");
  /* Listed from the fixtures themselves, grouped as each one says, so a new fixture needs no
     entry here. One reached by a link the catalog cannot list (a `mixed…` or `reply-…`
     family) still shows, under Other, so the panel never names a fixture that isn't on screen. */
  const catalog = (await import("./lib/mock/catalog")).mockCatalog();
  const listed = catalog.find((entry) => entry.name === mock);
  const entries = mock != null && !listed ? [...catalog, { name: mock, label: mock, group: "Other" }] : catalog;
  /* A fixture's first variant is its default, with or without `&data=`. */
  const variants = listed?.variants;
  const firstVariant = variants ? Object.keys(variants)[0] : undefined;
  const data = params.get("data") ?? "";
  const variant = data === firstVariant ? "" : data;
  const label = mock == null ? "Live API" : (listed?.label ?? mock);
  const variantLabel = variants ? (variants[data] ?? variants[firstVariant!]) : undefined;
  const badge = variantLabel ? `${label} · ${variantLabel}` : label;
  (await import("./devTools")).mountDevTools({ catalog: entries, current: mock ?? "", variant, badge });
}
