import { mount } from "svelte";
import App from "./App.svelte";
import { quietProgrammaticTips } from "./lib/components/primitives/tipFocus";
import "./app.css";

quietProgrammaticTips(document);

const target = document.getElementById("app");
if (!target) throw new Error("Missing #app mount target");

mount(App, { target });

/* Dev-only fixture toggle for `?mock=` (see createAgentRunController). */
if (import.meta.env.DEV && new URLSearchParams(location.search).has("mock")) {
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
    transcript5k: "5,000 messages"
  };
  for (const name of ["demo", "worst", "empty", "one", "huge", "markdown", "sessions200", "sessions10k", "transcript5k"]) {
    const link = document.createElement("a");
    link.textContent = labels[name] ?? name[0].toUpperCase() + name.slice(1);
    link.href = `?mock=${name}`;
    link.style.cssText = `padding:3px 10px;border-radius:6px;color:#111;text-decoration:none;${name === current ? "background:#fff" : ""}`;
    if (name === current) link.setAttribute("aria-current", "page");
    bar.append(link);
  }
  document.body.append(bar);
}
