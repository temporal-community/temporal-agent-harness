# typescript/apps/ui/

The console: a Svelte 5 app that replays and drives harness sessions. The workspace and SDK notes
are in [typescript/AGENTS.md](../../AGENTS.md); serving and embedding the build are in
[README.md](README.md). The rules below are each enforced by the test beside it.

## Commands

From the repository root:

```sh
just app-check # svelte-check and every console test under Bazel, as CI runs them
# svelte-check and every console test outside Bazel, including the manual docs-drift test
bazel run //typescript:pnpm -- --dir "$PWD/typescript/apps/ui" run check
scripts/check-ts-test-targets.sh # every test file has a test target
just ui-dev # Vite on http://127.0.0.1:5173, proxying /api to the server on :8000
```

One test under Bazel takes its target's label, for example
`bazel test //typescript/apps/ui/src/lib/components/agent:FilesPanel_test`.

`?mock=<fixture>` runs the console on fixture data with no API, in a development build only
(`just ui-dev`, or the development `vite build` in [README.md](README.md#embedding-the-console)). The
fixtures are in [jsonReplyScenarios.ts](src/lib/mock/jsonReplyScenarios.ts) (`worst`, `empty`,
`one`, `huge`, `longcontent` and the rest) and [scaleScenarios.ts](src/lib/mock/scaleScenarios.ts)
(`run100k`, `sessions10k`, `transcript5k`, `agents300`, `panesworst`, `panes3k` and the rest);
[main.ts](src/main.ts) lists most of them in a fixture bar.

Before calling a UI change done, try it with `?mock=worst` and the scale fixtures at narrow and
wide widths and at the pane's `minSize`.

## Add a pane

A pane is a kind in the registry, a component that takes props, and a branch in
[App.svelte](src/App.svelte). Copy the closest existing pane: the Files pane
([FilesPanel.svelte](src/lib/components/agent/FilesPanel.svelte),
[fileMounts.ts](src/lib/state/fileMounts.ts)) is a full one.

1. **Register the kind** in [registry.ts](src/lib/panes/registry.ts): add it to `PaneKind` and give
   it a `PANE_META` entry. Key order is the launcher's order. A kind only some agents have data for
   gets `optIn: true` and goes with the other opt-in kinds at the end, below the launcher's divider.
   Set `defaultSize` and `minSize`, `content` (`"viewport"` or `"document"`), `flexible` if a first
   visit should widen its column to fill the rail, `titleNames`, and an `accent` from the
   existing hue tokens. List it in `SINGLETON_KINDS` unless it can be opened once per workflow,
   as `logs:wf-123` is.
2. **Project its data** in a pure module under `src/lib/state/` with its own test, as
   [fileMounts.ts](src/lib/state/fileMounts.ts) does, and derive it on the run controller in
   [agentRun.svelte.ts](src/lib/state/agentRun.svelte.ts). Fold to the playhead, so scrubbing
   rewinds the pane, and fold incrementally, as the `*Builder` folds in `src/lib/state/` do, so a
   live frame doesn't rebuild the projection from every frame.
3. **Build the component** in `src/lib/components/agent/` (session data: chat, state, files,
   decisions) or `src/lib/components/flow/` (time and graph views: timeline, latency, tokens). It
   takes props, never the controller. Draw "nothing yet" with `EmptyState`, window any list that
   grows with the run with `WindowedRows` (as [MountTrees.svelte](src/lib/components/agent/MountTrees.svelte)
   does) or `@tanstack/svelte-virtual`, and draw buttons and labels with the other
   [primitives](src/lib/components/primitives); take colour, spacing and motion from the tokens in
   [app.css](src/app.css). Don't repeat the header's kind label inside the pane. A card pane pads
   with `--pane-inset` and is listed in `INSET_PANES` in
   [conventions.test.mjs](tests/conventions.test.mjs).
4. **Render it** in [App.svelte](src/App.svelte): a `describePane` case (title, and any count or
   status for the header) and an `{:else if pane.kind === "<kind>"}` branch in `paneContent`.
   `describePane`'s `never` default fails svelte-check until the case exists.
5. **Wire BUILD.** Give the component and the projection a `js_library` in their folder's
   `BUILD.bazel`, depending on what each imports at runtime (not `import type`). Add the component
   to the deps of `App` in [src/BUILD.bazel](src/BUILD.bazel) and the projection to the deps of
   `agentRun.svelte` in [src/lib/state/BUILD.bazel](src/lib/state/BUILD.bazel). Add both labels to
   `:sources` in [BUILD.bazel](BUILD.bazel); conventions.test.mjs fails on a source missing there.
   Run `just buildifier`.
6. **Test it.** Add a server-render test beside the component (as
   [FilesPanel.test.mjs](src/lib/components/agent/FilesPanel.test.mjs) does) and one for the
   projection, each with a `vitest_test` target. Add the pane to
   [empty-states.test.mjs](tests/empty-states.test.mjs), update the kind order in
   [launcher.test.mjs](src/lib/panes/launcher.test.mjs), and add `scales(...)` cases for its live
   frame and cursor step to [paneCost.test.mjs](tests/paneCost.test.mjs), adding each new import to
   that test target's deps. Run `scripts/check-ts-test-targets.sh`. Don't pin script statements
   by regex: render the component, or move the logic into a pure function under `src/lib/state/`
   and test that. Regex over `<style>` is fine where a server render can't see layout.
7. **Stress it.** Give the pane data in a scale fixture (`panesScenario` in
   [scaleScenarios.ts](src/lib/mock/scaleScenarios.ts) feeds the State, Files and Decisions panes
   through `?mock=panes`, `panesworst` and `panes3k`), then open it with those, `?mock=worst` and
   `?mock=run100k`, at narrow and wide widths and at its `minSize`, and do the stress step above.
8. Run the commands above.

## Rules

| Rule | Enforced by |
| --- | --- |
| Panes draw empty states with `EmptyState` | [empty-states.test.mjs](tests/empty-states.test.mjs) |
| Model and tool output can't run script in the console | [reply-xss.test.mjs](tests/reply-xss.test.mjs) |
| `{@html}` only in MarkdownMessage, JsonReply and AgentStateFlow (through `highlight()` or the markdown renderer), data hrefs via `safeHref`, TanStack packages allowlisted | [conventions.test.mjs](tests/conventions.test.mjs) |
| Windowed lists keep focus and keyboard reach for rows out of view | [windowedFocus.test.mjs](src/lib/components/primitives/windowedFocus.test.mjs) |
| A hairline box means "press me": a quiet `Chip` with nothing to press draws as a plain label, and one option is a label, not a tab | [primitiveProps.test.mjs](src/lib/components/primitives/primitiveProps.test.mjs), [AgentStatePanel.test.mjs](src/lib/components/agent/AgentStatePanel.test.mjs) |
| Card panes inset by `--pane-inset`; warning boxes use `.callout-warning` and jump buttons use `Chip`, not copies | [conventions.test.mjs](tests/conventions.test.mjs) |
| Pane content doesn't repeat the pane header's title; counts and filters go in the pane's toolbar | [conventions.test.mjs](tests/conventions.test.mjs) |
| Selection is a tint, never a side stroke: selected, open, current and changed rows use `--selected-bg`, focus rings only on `:focus-visible` | [conventions.test.mjs](tests/conventions.test.mjs) |
| Motion timing and easing come from the `app.css` tokens (spinner and loader cycles allowlisted); a transition lists its properties, never `transition: all` | [conventions.test.mjs](tests/conventions.test.mjs) |
| Every file that animates has a `prefers-reduced-motion` path; blur only on the attention queue's cards, never a pane-sized area | [conventions.test.mjs](tests/conventions.test.mjs) |
| A close is no slower than its open (`--duration-press` or `--duration-pane` out), and nothing delays a close | [PendingBannerStack.test.mjs](src/lib/components/agent/PendingBannerStack.test.mjs), [Copyable.test.mjs](src/lib/components/primitives/Copyable.test.mjs) |
| Rows in streamed or windowed lists appear without motion; following the tail jumps, never animates | [followScroll.test.mjs](src/lib/state/followScroll.test.mjs) |
| Components use Svelte 5 syntax only | [svelte5-syntax.test.mjs](tests/svelte5-syntax.test.mjs) |
| A pane's work per live frame and per cursor step stays O(run) or better, never quadratic | [paneCost.test.mjs](tests/paneCost.test.mjs) |
