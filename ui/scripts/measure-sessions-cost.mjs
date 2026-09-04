#!/usr/bin/env node
/**
 * A1: measure GET /api/sessions before changing poll cadence.
 * Usage: node ui/scripts/measure-sessions-cost.mjs [baseUrl]
 */
const base = (process.argv[2] ?? "http://127.0.0.1:8000").replace(/\/$/, "");

async function once(path) {
  const t0 = performance.now();
  const res = await fetch(`${base}${path}`);
  const body = await res.json();
  const ms = performance.now() - t0;
  return { status: res.status, ms, count: Array.isArray(body) ? body.length : body?.sessions?.length ?? null, body };
}

const full = [];
for (let i = 0; i < 3; i++) full.push(await once("/api/sessions"));
const lite = [];
for (let i = 0; i < 3; i++) {
  try {
    lite.push(await once("/api/sessions?view=ids"));
  } catch (e) {
    lite.push({ status: 0, ms: 0, count: null, error: String(e) });
  }
}

const avg = (rows) => rows.reduce((a, r) => a + r.ms, 0) / rows.length;
console.log(JSON.stringify({
  base,
  full: { samples: full.map((r) => ({ status: r.status, ms: +r.ms.toFixed(1), count: r.count })), avgMs: +avg(full).toFixed(1) },
  lite: { samples: lite.map((r) => ({ status: r.status, ms: +r.ms.toFixed(1), count: r.count })), avgMs: +avg(lite.filter((r) => r.status)).toFixed(1) || null },
  note: "Full enrich on a swept ~10-session registry was ~15–20ms locally (2026-09-04). Cost scales with row count + vanished corpses; lite must stay a manager query only.",
}, null, 2));
