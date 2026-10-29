import { mount } from "svelte";
import App from "./App.svelte";
import type { AgentApi } from "./lib/api/client";
import { keepTipsInView, quietProgrammaticTips } from "./lib/components/primitives/tipFocus";
import { writeOperatorPrefs } from "./lib/state/agentRunStorage";
import "./app.css";

quietProgrammaticTips(document);
keepTipsInView(document);

const target = document.getElementById("app");
if (!target) throw new Error("Missing #app mount target");

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

/* Dev-only `?mock=subagents|sessionsbreak&data=` toggles, in place of the fixture bar below. */
const dataToggles: Record<string, Record<string, string>> = {
  subagents: { demo: "Demo", worst: "Worst case", empty: "Empty", one: "One", many: "Many (60)" },
  sessionsbreak: { demo: "Demo", worst: "Worst case", empty: "Empty", allarchived: "All archived", single: "Single", one: "One each", many: "200", huge: "10,000" }
};
const toggle = import.meta.env.DEV ? dataToggles[new URLSearchParams(location.search).get("mock") ?? ""] : undefined;
if (toggle) {
  const mock = new URLSearchParams(location.search).get("mock");
  const current = new URLSearchParams(location.search).get("data") ?? "demo";
  const bar = document.createElement("nav");
  bar.setAttribute("aria-label", "Mock data");
  bar.style.cssText =
    "position:fixed;bottom:52px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;gap:2px;padding:2px;border-radius:8px;background:#d4d4d8;font:12px system-ui";
  for (const [name, label] of Object.entries(toggle)) {
    const link = document.createElement("a");
    link.textContent = label;
    link.href = `?mock=${mock}&data=${name}`;
    link.style.cssText = `padding:3px 10px;border-radius:6px;color:#111;text-decoration:none;white-space:nowrap;${name === current ? "background:#fff" : ""}`;
    if (name === current) link.setAttribute("aria-current", "page");
    bar.append(link);
  }
  document.body.append(bar);
} else if (import.meta.env.DEV && new URLSearchParams(location.search).has("mock")) {
  const current = new URLSearchParams(location.search).get("mock");
  const bar = document.createElement("nav");
  bar.setAttribute("aria-label", "Mock data");
  bar.style.cssText =
    "position:fixed;bottom:52px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;gap:2px;padding:2px;border-radius:8px;background:#d4d4d8;font:12px system-ui";
  const labels: Record<string, string> = {
    worst: "Worst case",
    huge: "1,000 rows",
    sessions200: "200 sessions",
    sessions10k: "10,000 sessions",
    sessionsworst: "Worst sessions",
    transcript5k: "5,000 messages",
    frames100k: "100,000 frames",
    run100k: "100,000-frame run",
    dense50k: "50,000 spans",
    okf2k: "2,000 concepts",
    okf10k: "10,000 concepts",
    agents: "11 agents",
    agents300: "300 agents",
    longcontent: "Long content",
    soak: "Soak",
    timelineworst: "Worst timeline",
    logsworst: "Worst logs"
  };
  for (const name of ["demo", "worst", "empty", "one", "huge", "markdown", "longcontent", "images", "attachments", "queue", "script", "sessions200", "sessions10k", "sessionsworst", "transcript5k", "frames100k", "run100k", "dense50k", "okf2k", "okf10k", "agents", "agents300", "soak", "timelineworst", "logsworst"]) {
    const link = document.createElement("a");
    link.textContent = labels[name] ?? name[0].toUpperCase() + name.slice(1);
    link.href = `?mock=${name}`;
    link.style.cssText = `padding:3px 10px;border-radius:6px;color:#111;text-decoration:none;${name === current ? "background:#fff" : ""}`;
    if (name === current) link.setAttribute("aria-current", "page");
    bar.append(link);
  }
  document.body.append(bar);
}
