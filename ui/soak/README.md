# Frontend UI soak tests

Slow, machine-noisy checks that sit **outside** `just app-check` (the
`ui/scripts/check-*.mjs` glob). They drive the real `AgentRunController` under
Node, plus one Bun.WebView (Chrome CDP) DOM ceiling.

## Run

```bash
just soak-ui
```

Or one file:

```bash
node ui/soak/soak-live-hitch.mjs
node ui/soak/soak-sessions-load.mjs
node ui/soak/soak-catchup-reopen.mjs
node ui/soak/soak-session-switch.mjs
bun ui/soak/soak-dom-ceiling.mjs   # needs Chrome / Playwright chromium
```

## What each soak pins

| Soak | Runtime | Gate |
|------|---------|------|
| `soak-live-hitch` | Node | Live-path commit cost near-linear at 2× size; 5k commit & ingest budgets |
| `soak-sessions-load` | Node | Stable revision → existence only; revision bump / age-gate → one enrich |
| `soak-catchup-reopen` | Node | Large cache hydrate stays chunked; `connecting` clears ≪ retry budget |
| `soak-session-switch` | Node | Abandoned attach cannot leak frames; frames stay bounded across switches |
| `soak-dom-ceiling` | Bun + Chrome | Message / log / session row counts match fixture; total element ceiling |

## Budgets

Timing uses best-of-3 (fastest round). Correctness failures are hard. Timing
ceilings are CI-tolerant multiples of quiet-machine baselines — see each file's
header comment for the measured numbers and why the gate is where it is.

Quiet-machine baselines (2026-09-04, arm64):

| Soak | Measured | Gate |
|------|----------|------|
| live-hitch | 2.5k ~14ms, 5k ~28ms, ~2.0× | ratio &lt; 3×; 5k commit &lt; 150ms; ingest &lt; 5s |
| catchup-reopen | connecting ~180ms; 1 publish under ceiling | connecting &lt; 3s; publishes ≪ frame count |
| session-switch | max frames ~81 / 20 switches | frames &lt; 200; attach growth bounded |
| dom-ceiling | 3211 elements @ 500/2000/200 rows | exact row counts; elements &lt; 6480 |

## Chrome requirement (DOM only)

`soak-dom-ceiling.mjs` uses `Bun.WebView({ backend: "chrome" })`. Bun resolves
Chrome, Chromium, Edge, Brave, or Playwright's cache (`chromium-*` / 
`chrome-headless-shell`, or `BUN_CHROME_PATH`). If spawn fails, that soak exits
loudly with an install hint; Node soaks still run and must pass.

Note: spawning Chrome needs a normal (non-sandbox) process environment — a
restricted sandbox can SEGV Chromium and surface as "Chrome process closed the pipe".
