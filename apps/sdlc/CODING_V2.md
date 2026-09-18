# Coding agent V2 — testing milestone

New tasks run `SdlcAgentV2`. Existing tasks stay on `SdlcAgentV1`, including their same-task follow-ups. There is no in-place history migration. Fork an old task to try V2 with its current patch. The create-task API also accepts `engine: "v1"` for explicit comparison runs.

## Try it

1. Stop your current boltzmann with Ctrl-C, then run `./apps/sdlc/dev` from the repository root. This installs the newly locked Code Mode dependency and builds the UI. Use `--local-harness` to test this harness checkout.
2. Open the launch URL printed by the launcher. Your saved profiles and `OPENAI_API_KEY` setup are unchanged: export in the launching terminal before starting. The launcher starts Temporal automatically.
3. Start a **new** task, or run the demo without a key. Check for the **Coding agent V2** panel. Existing V1 tasks remain usable.
4. Ask for a focused change with a regression test. Review the proposed requirements, assumptions, file/directory scope and checks before approving the blueprint.
5. Watch the implementer child. Approve the exact verification commands after implementation. Approval includes the source revision; edits to scripts/configuration require fresh approval.
6. Inspect Checks, Changes and the independent review findings. One in-scope repair is allowed, followed by fresh check approval and another independent review. A task may be ready for human review while verification is incomplete. Accepting that requires a note.
7. Send a follow-up in the same task. Each message has a fresh shared model budget; approved blueprint history and the workspace persist. Try Ask mode, pause/stop, manual edits and restarting at an approval gate.

Please evaluate real model quality on a small repository first: requirement coverage, edit quality, usefulness of review findings, missing context, cost, and whether the extra roles save you time. The automated fixture validates execution and failure handling; it does not establish that V2 outperforms V1 or another coding product.

## Execution and evidence

The coordinator investigates with read tools, then returns a structured blueprint. A deterministic controller owns the lifecycle. The implementer and reviewer are actual harness child agents. The implementer gets only the approved scope; the reviewer gets fresh context and read tools. The controller reuses the implementer for one repair. Both children remain idle between messages so their detailed traces stay readable in harness 0.4.0; assignment permissions are revoked at each message boundary. Deleting/closing their parent terminates the owned children. Optional explorers, parallel writers and deployment are later work.

Both direct tools and `inspect_code`/`change_code` use the same typed host dispatcher. It checks role, mode, scope, pause/stop and budget on every operation. Child dispatch uses durable request/reply signals. Operations share one writer lock, including controller verification and settling calls from a timed-out script. Host operation IDs are independent, deterministic workflow UUIDs; results are journaled. Completed operations reuse receipts, and interrupted commands with unknown outcomes are not automatically repeated.

Edits support exact single-occurrence replacement and hash-guarded file replacements. Multi-file patches preflight all hashes before writing, then atomically replace each file. They recover after partial application, but are **not** a filesystem transaction; an external editor can still race a patch. Delete/rename and arbitrary shell access are not model tools in this milestone.

The controller runs up to eight proposed checks sequentially, after approval at workspace root. It hashes Git-visible source, including relevant new/deleted files, binary contents and file modes, before/after each check and review. Secret/generated exclusions remain in force. A command that changes source cannot leave green evidence for the resulting revision. Verification requires all proposed checks to pass for the current revision, actual reviewer inspection, and no high/medium findings or missing evidence. No checks, fabricated claims, stale evidence and waived checks never become verified automatically. Acceptance is a separate human decision.

The Activity panel reads `CodingState` directly: roles, current operations, controller stages, blueprint, revision IDs, verification and findings. Children also publish their own internal state and native tool/model traces in Harness. Blueprints and approval notes persist independently of the bounded recent conversation. Native SDK histories stay local to each role run; portable handoffs carry requirements, check receipts and findings.

## Bounds and compatibility

- The profile/message model-call limit (up to 200) is shared across the coordinator and children. Calls are counted before the SDK invokes a model, including failed attempts. Implementation reserves calls for review. Tokens come from actual SDK usage.
- Each message permits 400 model-requested host operations. Roles execute sequentially; all workspace operations serialize at the controller. Code Mode permits eight host calls per batch and eight scripts per role run, with a 20,000-character script limit and 120-second overall deadline.
- The packaged harness Code Mode driver/stepper runs in disposable, credential-free processes. Monty limits are 32 MB sandbox memory, 500,000 allocations, depth 200 and two seconds of execution time; each process also has a CPU limit and a ten-second supervisor deadline. Snapshots/output have an 8 MB transport cap. This is separate from repository commands, which still run on the host after approval with the existing 90-second/output bounds.
- Harness 0.4.0 does not expose sandbox resource configuration. boltzmann explicitly registers bounded replacements for its public batch activity names, and adapts Monty's public constructor **only inside the disposable process** to supply limits. The package's workflow driver and stepper remain in use. This compatibility adapter should be removed when a release exposes limits directly. No example code or private harness helper is imported.
- Default installation uses published `temporal-agent-harness[ui,openai-agents,code-mode]==0.4.0`, including `pydantic-monty==0.0.18`. `--local-harness` consumes the checkout as an editable dependency. OpenAI uses the existing harness SDK plugin and credential resolver; future integrations can implement `run_role` alongside the legacy `decide` contract.
- Provider testing now checks an actual harmless function-call round trip and the V2 role-output schema. It performs up to three model requests and never opens a repository.

External edits are rechecked at evidence/acceptance boundaries, not continuously watched. Durable request receipts, source snapshots and role handoffs add Temporal history overhead; automatic history rotation and long-running repository benchmarks are future work. Normal stop/launcher shutdown settles operations; forcibly killing the host process can leave command outcomes uncertain. boltzmann does not claim an OS sandbox for approved repository commands.

## Validation

`test_coding.py` covers scope/mode/role denial, aggregate budgets, controller serialization, exact edit and patch recovery, uncertain commands, stale source and sandbox limits. `test_coding_live.py` uses real Temporal and OpenAI Agents SDK Responses streams from a local fixture: a multi-file edit, blocked out-of-scope script, failed check, bounded repair, independent review, restart at approval, stale approval rejection, external-edit acceptance guard, and same-task follow-up. The existing V1 live and saved-history replay tests remain part of validation. No real provider key is used by these tests.

Handoff validation: 89 focused tests and both V1/V2 live tests passed against the published package. V2's live/guard tests also passed in a separate environment using the editable harness checkout (11 tests). The live test verifies child state/tool traces on late attachment and refuses a fabricated review claim even with passing checks. Svelte checks, production build and browser inspection of the demo/embedded debugger passed. Live-provider quality evaluation is the next user test; broader fault injection and long-session benchmarks remain follow-up work.
