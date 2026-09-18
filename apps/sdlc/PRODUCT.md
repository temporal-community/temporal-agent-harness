# A daily development application

Status: product requirements and proposed priorities, September 15, 2026. A first usable local milestone is implemented and ready for user testing; [README.md](README.md) identifies what works today and what remains deferred. Implementation architecture lives in [PLAN.md](PLAN.md); the required internal-state progress experience is specified in [STATE_UI.md](STATE_UI.md). The wider requirements below are not all implemented.

The goal is to become the place Alex opens to start, understand, change, review, and ship software every day. Success means finishing real work with less repeated explanation, less supervision, and confidence about what changed. A complete feature lifecycle is one important path through the application; small fixes, investigation, manual edits, existing PRs, and maintenance need equally direct paths.

Confirmed constraints remain: local use by one developer, GitHub, configurable deployment adapters, provider/model choice, secrets management, and an unchanged harness dependency with an editable development mode. Support existing repositories as well as new applications. The initial prioritization assumes context repetition, agent supervision, review effort, and switching tools are the main opportunities; validate that weighting during daily use.

## 1. The reason to switch

Build around five promises and measure whether they hold:

| Promise | Concrete experience | Proof |
| --- | --- | --- |
| Pick up where you left off | Reopen a project to its working files, preview, pending decisions, and a concise explanation of what happened while you were away. | Recover from an app/worker restart without repeating the brief or losing edits; distinguish sleeping/offline workers from active progress. |
| Delegate a bounded outcome | Give a task its scope, checks, budget, and allowed actions; the app works until completion or a meaningful blocker. | Routine permitted edits/checks proceed without repeated prompts; scope changes and release actions remain explicit. |
| Review the result efficiently | Inspect changes by purpose, comment on a hunk, see supporting tests and preview evidence, and review only what changed since your last pass. | Reduce active review time while still catching seeded defects and unmet requirements. |
| Carry decisions into future work | Accepted conventions, architectural decisions, and previous fixes are searchable and included when relevant. | A related task follows the recorded decision; stale memory is identified when source changes. |
| Finish the delivery loop | A request, its code, tests, PR, deployed version, and acceptance record remain connected. | From a regression, find the deployed artifact and original decision, produce a regression test, repair, and verify the next release. |

These are proposed advantages to earn through implementation and daily testing. They are not claims that competitors lack the underlying features.

Cursor already documents planning with clarifying questions and editable plans, including a direct path for quick changes. Treat planning as a baseline. [Cursor Plan Mode](https://cursor.com/docs/agent/plan-mode)

Its agent also supports code changes, terminal tools, checkpoints, and queued/steering messages. Merely adding an agent chat and a stop button will not establish a compelling daily workflow. [Cursor Agent](https://cursor.com/docs/agent/overview)

The Agents Window includes multiple projects, diffs and PR management, parallel agents, worktrees, and local/cloud handoffs. Those are relevant usability benchmarks. [Cursor Agents Window](https://cursor.com/docs/agent/agents-window)

Dedicated code review and browser testing are also established expectations. The opportunity here is the quality and continuity of the complete experience, plus ownership of provider, execution, and deployment configuration. [Cursor Agent Review](https://cursor.com/docs/agent/agent-review), [Cursor Browser](https://cursor.com/docs/agent/tools/browser)

## 2. Daily work must have several entry points

Use a **work item** as the common object, with a kind such as question, fix, feature, investigation, review, maintenance, or incident. A work item can begin with a prompt, selected code, terminal error, screenshot, GitHub issue/PR, or existing branch. It carries context, attempts, evidence, and outcomes without requiring the developer to fill out a ticket first.

| Entry point | Default path | What gets recorded |
| --- | --- | --- |
| Ask | Read-only investigation → answer with file/symbol references | Question, relevant source snapshot, answer and citations; optionally promote to a task. |
| Quick change | Scope summary → edit → relevant checks → diff review | A compact change brief, allowed scope, patch, evidence, and acceptance. No mandatory architecture document. |
| Debug | Reproduce → investigate hypotheses → regression test → repair → verify | Reproduction command, logs, hypotheses/evidence, exact fix and test. An unreproduced bug stays explicitly unverified. |
| Feature | Discovery → blueprint approval → implementation → verification → release | The full lifecycle from PLAN.md. |
| Review | Import branch/PR → inspect diff and checks → findings → optional repair | Findings tied to source positions and SHA; local comments stay local until publishing is authorized. |
| Continue existing work | Import the current branch and optionally selected dirty changes → choose the next action | A checkpoint and provenance of imported work; preserve the original checkout. |
| Ship | Select an existing verified candidate → release plan → approve → deploy → accept | Reuse valid evidence; run missing checks rather than forcing a new build conversation. |

Mode is always visible and changeable. Ask never silently gains write access. A quick change that touches a sensitive boundary or expands beyond its agreed scope offers a concise replan. A task may finish as an answer, accepted local patch, or PR; deployment is required only when it is part of that task's intended outcome. Show skipped/not-applicable stages as such.

For a small authorized fix, the submit action can approve a visible compact scope and the project's established sandbox policy. Do not add another approval merely to repeat the same instruction. Significant blueprint changes, merge, and deployment retain their own recorded decisions.

## 3. The main workspace

Recommended navigation: **Today**, **Projects**, **Work**, **Changes**, **Releases**, and **Settings**. A project workbench combines conversation, files/diffs, preview, and terminal/checks. The harness debugger is an expandable view available from any attempt or event.

```text
┌ Project / branch / work item ── worker status ── model / budget ── command menu ┐
│ Projects & work │ Conversation / plan │ Files / diff / preview                │
│                │                     │                                      │
│ Needs you      │ Decisions and        │ Review comments, test evidence,      │
│ In progress    │ concise progress     │ manual edits, visual inspection      │
│ Ready to review│                     │                                      │
├────────────────┴─────────────────────┴──────────────────────────────────────┤
│ Terminal | Checks | Services | Activity | Harness debugger                  │
└────────────────────────────────────────────────────────────────────────────┘
```

This is an information-layout proposal, not a fixed four-pane requirement. Support a focused single-pane view and resizable splits. Restore the layout, open files, scroll positions, selected attempt, and unsent draft per project. A new token, log line, or task must not steal keyboard focus or move the diff currently being reviewed.

**Today** should answer: what needs my decision, what is running, what can I review, and what changed since I last looked? Group by project and urgency; offer resume, answer, inspect, or dismiss. It should work from saved state while workers reconnect. Avoid an empty dashboard that requires creating a new feature before anything is useful.

## 4. Required for the daily-use pilot

These capabilities form the first daily loop. Their IDs are acceptance references for the implementation plan.

| ID | Capability | Required behavior |
| --- | --- | --- |
| D01 | Open and resume | Add a local repository or GitHub URL, infer commands/toolchain, validate prerequisites, resume recent work in one action. GitHub/deploy setup is deferred until needed. |
| D02 | Persistent work queue | Pin/reorder tasks across projects; distinguish running, queued, waiting for you, offline, failed, and ready to review. Concurrency and resource use are visible. |
| D03 | Fast task input | Ask, quick change, debug, plan, and review entry points; paste images/logs; attach `@file`, symbol, diff, issue, or previous task. Autosave drafts and upload state. |
| D04 | Useful code navigation | File tree, quick open, full-text search, source-linked answers, syntax highlighting, go-to-line, find/replace, basic manual edits, and inline edit/explain on a selection. |
| D05 | Human takeover | Pause new agent writes, settle in-flight writes, checkpoint, and hand control to manual editing or an external editor. Detect subsequent changes and refresh context/evidence. |
| D06 | Review workspace | Side-by-side/unified diffs, review by file or logical task, hunk comments, request changes, stage/unstage, selective restore, and comparison since last review. |
| D07 | Terminal and services | Interactive terminal scoped to the workspace, reconnectable output, named dev/test commands, managed dev servers, port discovery, readiness, stop/restart, and resource status. |
| D08 | Preview and feedback | Open the correct running app, select viewport, capture a screenshot, annotate a problem, and attach it to a corrective task. Support a separate browser if embedding fails. |
| D09 | Actual verification | Run project checks, display per-check results and logs, click a failure to inspect source or start repair, and invalidate evidence after relevant edits. |
| D10 | Durable history | Recover messages, decisions, artifacts, event transcript, and checkpoints after a restart; provide a short catch-up summary with links to evidence. |
| D11 | Inspectable context | Show attached sources and applicable rules, pin/exclude context, remember accepted project decisions, search previous work, and flag outdated records. |
| D12 | Bounded autonomy | Scope and budget presets, next-turn queue, pause/cancel, visible blockers, and notifications for decisions/completion. Avoid a prompt for every permitted edit. |
| D13 | Model and secret setup | Validate credentials, choose project/task profiles, view actual provider/model used, handle quota/auth failures clearly, and make changes without re-explaining the task. |
| D14 | Local reliability | One launcher, understandable health/status, safe restart/update, recovery when a dependency is missing, disk/retention controls, and offline access to saved work. |
| D15 | Keyboard and accessibility | Command palette, quick project/task switch, discoverable shortcuts, keyboard review/approval, screen-reader labels, sufficient contrast, reduced motion, and scalable type. |
| D16 | Custom agent-state progress | Render the root SDLC agent's actual observable internal state: current phase/action, completed and remaining tasks, blockers, questions, evidence, and next action. Restore the same state after reconnect and link to the harness debugger. |

For D04, use an existing embeddable editor. Standard copy/paste, undo/redo, file encoding/line endings, save state, and detection of external edits are acceptance requirements. Add diagnostics and symbol navigation through language services for the first supported stacks before claiming broad IDE replacement. A full extension ecosystem, advanced debugger, and competitive inline autocomplete are substantial separate investments. Keep a reliable external-editor handoff while measuring which missing editing functions actually cause Alex to leave the app.

For D16, the SDLC agent owns a typed `SdlcState` registered with `AgentWorkflowRunner.state("sdlc", ...)`. App components render the harness's snapshot/patch stream as a phase rail, current-work card, task board, needs-you panel, and verification/release view. Chat text is not a progress database. Counts describe planned tasks, not an invented estimate of total effort; blockers and unmet gates stay visible. The generic harness state pane remains available for inspection, while the custom UI provides the everyday view. See [STATE_UI.md](STATE_UI.md) for the schema, command path, reconnect protocol, and layout.

## 5. Required for the complete lifecycle release

These extend the daily loop into dependable shipping and preserve the original project goal.

| ID | Capability | Required behavior |
| --- | --- | --- |
| L01 | Living blueprint | Requirements, assumptions, risks, task dependencies, architecture, test and deployment plans are editable and versioned; show what changed since approval. |
| L02 | GitHub round trip | Import issues/PRs, create/update draft PRs, display exact-head checks and review threads, prepare replies, and apply feedback. Publishing, push, and merge follow explicit policy. |
| L03 | Checkpoints and alternatives | Name checkpoints; compare attempts or model variants; restore selected changes without losing later manual edits; preserve the old attempt and explanation. |
| L04 | Reproducible environments | Save toolchain/runner versions, install/check commands, ports, service dependencies, env-variable bindings, and test fixtures. Diagnose environment failures separately from code failures. |
| L05 | Browser verification | Run repeatable UI journeys with console/network errors, screenshots and traces as artifacts; tie results to the candidate and environment. |
| L06 | Release workspace | Review candidate, changes, evidence, target, runtime secret bindings, migration plan, and recovery options in one place. Promote the approved artifact where supported. |
| L07 | Production follow-through | Health checks, smoke tests, deployment receipt, human acceptance, defect capture, and an actionable restore/repair path. Keep unsupported recovery actions explicit. |
| L08 | Portable ownership | Export plans, memory/rules, configuration without secret values, transcripts, evidence, and Git changes. Support the packaged and local harness modes without application source edits. |
| L09 | Measured daily quality | Complete a real two-week pilot with a record of failures, forced tool switches, review effort, task success, and cost. Reliability and usability defects block release. |

Git operations must handle conflicts, rebases, stale bases, detached HEADs, large/binary diffs, and repository hooks with clear explanations. Detect submodules, Git LFS, and monorepo layouts; either support the detected configuration or explain the limitation before mutating it. For monorepos, select the working package and affected commands instead of running everything at the repository root.

## 6. Review should be one of the strongest experiences

Start a review with a short account of behavior changed, requirements covered, unresolved risks, and evidence. Every claim should link to the relevant diff, check, screenshot, or decision. Label agent-generated explanations separately from observed results.

Let Alex review changes by task or concern while preserving access to the full raw diff. Show generated files and lockfile changes in collapsible groups, never silently hide them. Comments can target a hunk, an acceptance criterion, or a preview screenshot. “Fix this and rerun the affected checks” creates a bounded repair with those references attached.

Track which revision was reviewed. When the agent changes a reviewed file, show only the new changes by default and mark the earlier review as stale where appropriate. Agent output must not scroll the review away from the user's position. Show the current base/head alongside the reviewed revision.

Hunk actions need precise meanings: **mark reviewed** changes review state; **stage** changes the Git index; **restore** changes files; **request change** creates work. A partially accepted patch requires checks against the resulting tree. Undoing a patch does not undo a pushed commit, deployment, migration, or external operation; those have separate recovery actions.

Include an independent reviewer role that reads the candidate and acceptance criteria. Allow a different model profile for a second opinion on a difficult change. Avoid presenting agreement among models as proof of correctness.

## 7. Context and memory should save explanation without becoming mysterious

Maintain four visibly separate sources of context:

| Source | Examples | Update policy |
| --- | --- | --- |
| Repository facts | Files/symbols, build graph, commands, dependencies, base/head | Refresh from the current source and invalidate by content/revision. |
| Project decisions | Architecture choices, conventions, compatibility requirements, accepted tradeoffs | Store provenance, scope and approval; user can inspect, edit, supersede, or delete. |
| Task context | Brief, attached issue, selected files, blueprint, attempts, failure evidence | Snapshot per attempt with explicit revisions. |
| Personal preferences | Preferred explanation style, editor, model defaults, notifications | Opt-in profile, project overrides, and no automatic sharing between unrelated repositories. |

Start retrieval with fast text/file search and a small local repository map, then add symbol-aware retrieval. Evaluate semantic indexing on representative large repositories before making it a dependency. Indexing must be incremental, cancellable, and excluded from the critical path for a simple question. Include unsaved or working-tree edits only when their provenance is clear.

Support `AGENTS.md` and offer a previewed import of relevant existing project instructions, including Cursor rules where translation is meaningful. Show scope/precedence conflicts and unsupported rule semantics. Do not silently rewrite existing files. Repository content is project guidance and data; it cannot grant itself new credentials, tool authority, or a release approval.

A context inspector should explain why a source was selected, its source revision, and whether it was summarized or truncated. Show excluded paths and the source types sent to the selected provider. Missing context should lead to retrieval or a focused question rather than an invented answer. Secret exclusions apply to search, indexing, attachments, and logs.

When an attempt needs a fresh context window or another provider, create a handoff containing the task, approved decisions, repository state, completed work, unresolved issues, and artifact references. Keep it editable and traceable to sources. Never present a model's unreviewed inference as an accepted long-term project decision.

## 8. Control agents without supervising every action

Use explicit task policies with understandable scopes:

| Preset | Allowed by the preset | Still needs a separate decision |
| --- | --- | --- |
| Inspect | Approved project reads, analysis, local context search | File writes, arbitrary command execution, publishing, deployment. |
| Work in sandbox | Edits and approved build/test commands in the task workspace, within budget | Expanded scope, access outside that workspace, new credentials/network grants, remote publication unless already granted. |
| Prepare a PR | Sandbox work plus branch push and draft PR operations granted for this project/task | Merge, production deployment, destructive remote operations. |

Allow project defaults and a task-specific narrowing of them. Show the active scope in plain language and permit revocation. A grouped permission is bounded by purpose, workspace, tool capability, target, and duration; it is not a permanent blanket approval for arbitrary shell commands. Enforcement belongs in execution/integration services, independent of what a model or debugger requests.

Separate **queue for next step**, **steer at the next safe boundary**, **pause**, and **stop**. Persist both the user's message and its delivery/acknowledgement state. The current harness queues turns sequentially; that alone is not mid-turn steering. Start with honest next-turn delivery and task-boundary pauses. Implement app-owned steering through workflow-safe control messages and explicit checks in the app's agent loop, and validate the pinned SDK boundary before advertising it. Never say an in-flight external action has stopped until its state is known.

Reduce unhelpful interruptions: batch related planning questions, suggest evidence-backed defaults, preserve previously granted scope, and continue independent permitted work while waiting. Detect repeated failing commands, unchanged patches, and provider retry loops. Report what was attempted and what decision would unblock it; stop at the budget instead of repeating indefinitely.

Provide per-project queues and a global concurrency/resource limit. Reserve capacity for an interactive question while builds run. Start parallel tasks in separate workspaces with dependency ordering and an integration queue; surface conflicts before merging results. Parallelism must improve elapsed time without making review unmanageable.

Local execution continues when the browser closes if its workers remain running. A sleeping or powered-off laptop cannot run local workers. Show this plainly and resume after wake; offer an optional remote worker later for work that must continue while the laptop is off.

## 9. Human editing, terminals, and previews need real engineering

Manual takeover is a protocol: request the workspace write lease, stop scheduling agent mutations, wait for active writes to settle, create a checkpoint, then grant the lease to the user. Resume by rereading changed files and invalidating affected tests and decisions. External file changes that bypass the lease are detected by watchers and expected-content hashes; a stale agent edit conflicts instead of overwriting the user.

The terminal needs an authenticated PTY broker with reconnectable, bounded output and explicit process ownership. Keep interactive terminals, finite test jobs, and persistent services distinct. Do not tie a dev server's lifetime to a browser HTTP request or repeat its start command whenever a panel reconnects. Manual commands are attributed to the user and still update workspace/evidence state.

Project environment profiles declare named processes, working packages, prerequisites, health checks, ports, and runtime secret bindings. Reuse a healthy existing service or explain port conflicts. Show which worktree and revision a preview is actually serving. Test/database fixtures use disposable local environments, with reset actions separate from production operations.

Serve generated previews on an origin separate from the application's credential/control UI. Apply appropriate frame restrictions; never host arbitrary generated JavaScript on the control application's origin. Support apps that prohibit framing through a separate browser window and automation connection. Browser automation uses an isolated profile with intentional test credentials, not unrestricted access to the user's personal logged-in browser.

Capture console/network failures and screenshot annotations into a reproducible bug report with route, viewport, source revision and relevant logs. Desktop, API-only, and CLI projects should get an appropriate run/test surface instead of an empty web-preview tab.

## 10. Small conveniences that make the app pleasant

Some of these belong in the first implementation of a surface; others can follow after the daily loop is dependable.

| Convenience | Priority | User value |
| --- | --- | --- |
| Command palette and recent commands | Daily pilot | Open a project, start a task, run checks, switch model, or inspect a decision without navigating settings. |
| Draft recovery and attachment autosave | Daily pilot | Switching tasks or reloading never loses an unfinished instruction. |
| Stable scroll and focus; separate follow-live toggle | Daily pilot | Read a diff or older event while new work arrives. |
| Light/dark/system themes, adjustable density/font size | Daily pilot | Comfortable use for long sessions. |
| Project switcher, pins, recent files, remembered pane layout | Daily pilot | Change contexts quickly and return to the same place. |
| Copy path/command/error, open file at line, reveal workspace | Daily pilot | Reduce small repeated manual steps. |
| Actionable completion/blocker notifications | Daily pilot | Return only when useful; opening a notification lands on the relevant decision or diff. |
| Notification batching, quiet hours, optional sound | Complete release | Avoid a notification for every tool or successful check. |
| “What changed while I was away?” | Daily pilot | Show completed work, failed checks, decisions needed, and the next useful action, with source links. |
| Saved prompts and task recipes | Complete release | Repeat “review PR,” “add regression test,” or “update dependency” with project context already supplied. |
| Named checkpoints and bookmarks in history | Complete release | Return to a known result without searching a long conversation. |
| Before/after preview and screenshot comparison | Complete release | Judge visual changes quickly; do not imply screenshot comparison proves behavior. |
| Optional desktop window / launch at login | Later | Make the app feel native and keep a visible background-worker status after browser tabs close. |
| Voice input and narrated handoff | Later | Capture a thought when typing is inconvenient; text remains editable before execution. |
| Mobile review/approval | Later, with remote service | Make a narrow decision away from the desk with authenticated access and full subject context. |
| Agent-generated project tour | Later | Explain a new repository with file links and a short runnable walkthrough. |
| Compare two approaches in separate workspaces | Later | Explore a meaningful alternative while preserving the accepted baseline and spend limits. |

Nice details should be integrated into real workflows. Do not create a settings screen for every preference before a reasonable default exists.

## 11. Setup, operating cost, privacy, and maintenance

First run should require only a repository and an available model profile for an ask or quick task. Discover languages, lockfiles and candidate commands; show the proposed setup and any command that must run. Add GitHub write access when publishing, and deployment credentials when adding a target. Explain container/toolchain requirements before downloading them, and retain setup progress across interruption.

A service health panel should identify whether a failure is in the app, Temporal, an agent worker, the model provider, a workspace service, GitHub, or deployment. Include retry/reconnect actions, last successful operation, and a redacted diagnostic export. A stopped worker should not look like a thinking model.

Expose token usage, estimated spend, elapsed active time, and retry count by task/project/day. Let Alex set soft warnings and hard call/token/concurrency limits. Present estimated currency separately from authoritative provider billing; unavailable pricing stays unknown. Make provider fallback opt-in and visible, and preserve profile/endpoint/privacy constraints. Switching models at a checkpoint retains context but does not guarantee identical behavior or cost.

Keep analytics local by default. Provide controls for artifacts, source indexes, trace retention, external telemetry, and provider-bound context. Export/import project configuration without secret values and export credentials separately only through a deliberate supported mechanism. Memory deletion and local retention settings must explain that externally submitted model data follows the provider's own retention behavior.

Back up the database and referenced artifacts consistently; show disk growth and preview cleanup actions. Before an app or harness upgrade, checkpoint active work, apply migration/versioning policy, and verify worker compatibility. A filesystem snapshot alone is not a safe downgrade of already-executed workflows or migrated data. Show pending upgrades and an explicit recovery procedure.

## 12. Extensibility after the core is useful

Reusable project recipes should combine a prompt, expected inputs/outputs, applicable rules, tools, checks, and bounded permissions. Keep a versioned local manifest and let Alex review the effective capabilities before enabling one. A shared marketplace can wait.

Support connector/tool adapters for documentation search, issue systems, observability, and deployment as concrete needs arise. For MCP or other external tools, expose identity, schema, credential references, network/resource scope, and approval behavior. A connected tool must not bypass the same lifecycle and execution controls as a built-in tool. Preserve the initial published-package constraint: no dependency on this repository's unpublished Nexus MCP package.

Generated agents/tools can become versioned, tested artifacts after the fixed development agents work well. A capability generator should produce code, schemas, tests, a permission manifest, and installation instructions. Install into a separately versioned worker after review, with a disabled/revoke path; never hot-load generated code into the control service.

Later opportunities include scheduled maintenance tasks, dependency/security updates, release monitoring that drafts repair work, remote execution, multi-repository changes, and richer debugging/language services. Recurring work is explicitly enabled, budgeted, and routed to review. Multi-repository releases need a new coordination model rather than assuming several Git commits are one transaction.

## 13. Delivery order and scope discipline

| Release | User-visible result | Scope |
| --- | --- | --- |
| Daily pilot | Open a real repo, ask a question, make a small change, inspect/edit it, run checks, preview, and resume it tomorrow. | D01–D16; start guided planning and observable-state progress in the same workbench. Support Python and TypeScript/JavaScript repositories first, with configurable commands for other stacks. |
| Complete lifecycle | Plan a feature, build/review/test it, publish a PR, deploy an approved candidate, accept it, and repair a defect. | L01–L09 and the original PLAN.md lifecycle. Include browser evidence, checkpoint recovery, environment profiles, and refined review. |
| Daily refinements | Remove the biggest friction revealed by actual use. | Prioritize remaining conveniences by measured frequency/impact; add language services, inline completion, deeper debugging, remote work, or integrations when they address repeated exits. |

The daily pilot is an intermediate milestone, not a reduction of the requested full lifecycle. Also avoid making the initial release depend on a full VS Code replacement, a vector database, a desktop shell, dozens of deployment integrations, or an agent marketplace. Broad feature counts do not compensate for slow editing, lost work, or unreliable recovery.

## 14. How to decide whether it is good enough to become the daily app

Use it on this repository and at least one real application repository. Record where Alex returns to another tool, why, and how often. Compare normal preferred workflows on a representative task set; when comparing model behavior, also run a controlled comparison with the same model and similar context where available. This is a product evaluation, not a claim of superiority before measurements.

| Scenario | Passing experience |
| --- | --- |
| Start the day | See pending decisions and recent results; resume the right branch, files, and preview without reading yesterday's entire chat. |
| Five-minute fix | Submit a concise change, review its diff, run the relevant check, and finish without a mandatory full blueprint/release wizard. |
| Understand unfamiliar code | Ask a question and receive navigable source references with a visible indication of missing/stale context. |
| Debug a failing check | Reproduce, inspect the failure, get a meaningful regression test/fix, and retain the evidence. |
| Take over manually | Edit in-app or in an external editor, resume the agent, and lose no manual changes. |
| Review a large change | Find requirements, important changes, test evidence and unresolved risks; request a correction and see only the subsequent delta. |
| Work on two tasks | Both retain context; writes remain isolated; interactive work stays responsive; integration conflicts are explicit. |
| Understand progress at a glance | Without reading chat, identify the phase, active tasks/agents, blockers, completed and remaining work, and next action; the same state survives reconnect and matches the harness state document. |
| Interrupt the system | Kill a worker/API process or restart the app; recover without duplicate external effects or missing decisions. |
| Run out of provider quota | See a clear cause and budget state; choose a permitted fallback or pause; preserve the work. |
| Ship and discover a defect | Identify the live version, capture a reproduction, repair or restore appropriately, and verify the outcome. |

Proposed targets to measure on declared reference hardware/repository sizes, excluding model/network/toolchain installation time:

- Warm task/project switch to useful saved content: p95 under one second; local interaction acknowledgement under 100 ms.
- Typing, scrolling, and diff review stay responsive while streaming large logs; virtualize and backpressure long streams.
- A configured repository accepts a quick task from launch within 30 seconds; first useful task setup targets under ten minutes when prerequisites are installed.
- Across crash/retry scenarios: zero lost acknowledged user commands, accepted decisions, or saved edits; no silent duplicate external mutation.
- Across the pilot: at least 80% of the agreed representative workflows finish without an unplanned tool switch. External-editor handoffs intentionally chosen for advanced editing are recorded separately.
- For bounded local tasks: fewer repeated permission prompts and repeated context explanations than Alex's baseline; measure these rather than assert them.
- Review quality must remain at least as strong on seeded defects and missed requirements while seeking lower active review time.

Record task outcome, active user time, wall time, review corrections, interruption count, recovery failures, forced exits, and estimated usage cost. Keep the dataset local. Two weeks of willing daily use and resolved critical recovery/editing defects are the adoption gate; a scripted demo is insufficient.

## 15. Architectural consequences

The following additions belong to the app, preserving the harness package boundary:

- Implement `SdlcAgentWorkflow` as the harness root agent and deterministic work-item lifecycle controller, superseding the earlier plain `FeatureLifecycleWorkflow` / `WorkItemLifecycleWorkflow` proposals. Its canonical `SdlcState(HarnessState)` drives the custom UI through harness snapshots/patches. Guided features retain blueprint/release gates; small tasks use a compact brief and finish at their intended outcome.
- Add workspace/file/PTY/service APIs and a human/agent write lease. The current harness debugger supplies observation; it does not provide the daily code editor, preview manager, or terminal.
- Add context retrieval, approved memory, repository watchers, and immutable context manifests per attempt.
- Add review sessions/comments, named checkpoints, imported work provenance, and evidence invalidation on both human and agent edits.
- Add a persistent task queue, notification inbox/preferences, workbench layouts/drafts, and local usage accounting.
- Add a versioned snapshot/patch projector and state bootstrap/resume API with atomic watermarks; progress is a rebuildable view of agent state, not a second lifecycle authority. Add app-owned typed state components without depending on the unimplemented generic display-annotation design.
- Add a separate-origin preview gateway/browser runner and environment/service profiles.
- Keep all task mutation/steering routed through lifecycle-aware commands. Do not create a second independent agent driver in the product chat beside the lifecycle worker.

See [PLAN.md](PLAN.md) for the revised implementation sequence, storage records, and integration constraints. Full harness archived replay and generic embed controls remain optional upstream improvements; a daily app must still work against the packaged harness available today.
