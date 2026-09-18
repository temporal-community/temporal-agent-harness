# Milestone 3: execution workspace and interactive review

This is the testing handoff. GitHub delivery and CI feedback are deliberately deferred.

## Start or update

Restart the app with `./apps/sdlc/dev` from the repository root (or retain `--local-harness` if that is your current development setup). Open the launch URL printed by the terminal. Keep your API key exported in that same terminal, or use your existing keyring profile. No new keys, environment variables, services, or dependencies are needed. The launcher starts Temporal automatically.

Existing tasks, workflows, profiles, and workspaces are preserved. SQLite adds a separate review table automatically. No workflow history migration is needed; agent orchestration and approvals are unchanged.

## Try the execution workspace

1. Start a small change or continue an existing task. New tasks use V2; V1 tasks retain their original agent and gain the file review UI.
2. In **Execution**, watch the lifecycle rail and the coordinator → implementer → reviewer assignments. The map follows the current role. Click an assignment to pin its inspector; **Follow live** resumes following.
3. Inspect the coordinator's blueprint, acceptance requirements, assumptions, and write scope. Inspect the implementer's latest operation and changed files, and the reviewer's findings, checks, and source revision.
4. **Remaining** lists unfinished lifecycle steps and missing evidence. Approvals and questions still appear below the workspace. Pause, stop, and resume retain their existing operation-boundary behavior.
5. **Trace** opens the embedded harness's session trace, including all child agents, in native focus mode. **Activity** below the composer keeps the newest entries first. Session details contain the complete plan, budgets, model, state version, and workspace path.

State comes from authoritative harness snapshots refreshed every two seconds. Disconnection is labelled as saved state. Stage completion records controller progress, not a claim that every requirement has been verified.

## Try an interactive review

1. In **Review**, select a changed file. Read the diff with old/new line numbers; click a line, write a comment, and **Save comment**. You can also add a whole-file comment. Saving does not run the agent.
2. **Mark reviewed** records the exact contents and file mode you inspected. Refresh or restart: comments and checkpoints remain. Review marks and task acceptance are separate decisions; a reviewed file can still have open comments.
3. Change a file with **Files**, or directly in the displayed isolated workspace. Return to Review and refresh. A changed file loses its reviewed indicator; **Since last review** compares against its last checkpoint. New/unreviewed files compare against the original task base. The list also retains reviewed files reverted to the base or deleted after review.
4. Leave the review open while another edit happens. The five-second refresh flags an outdated diff without replacing the code under your cursor. **Load latest diff** updates it. The server rejects comments and review marks against changed snapshots. A draft made on an older diff must be re-anchored before saving; saved comments keep their original side, line, and excerpt and are labelled when the file changes.
5. In **All comments**, select the open comments to address. **Request fixes** prepares a visible follow-up in the existing task conversation. Review the text, model, and budget, then **Send message**. The existing durable message delivery prevents duplicate execution after retry/restart. The next change starts with a new blueprint approval.
6. After correction, inspect the new diff and evidence. Resolve comments yourself or reopen them. Agent completion never automatically resolves human comments. Accept when satisfied, recording an acceptance note if verification is incomplete. Then choose **Merge into project** to submit a durable workflow update. Its journaled activity preflights and writes the accepted changes into the configured source checkout, and its receipt is retained in workflow state. The merge leaves the changes uncommitted and stops without writing when project edits overlap.

Comment drafts are retained in this browser and survive switching tabs/tasks. Saved reviews live in the app's SQLite database and are removed with the task. Comments remain available while the agent is running; sending a follow-up waits until its current message finishes or is stopped.

## Review boundaries

- Text files up to 100 KB, at most 6,000 displayed diff lines, and the first 500 review paths. Truncation and unsupported files are explicit. Binary files, links, and large files need local inspection; they cannot be marked reviewed in the app. Renames appear as a deletion and an addition.
- Secret, ignored-file, generated-directory, and path exclusions match the existing workspace tools. Review does not execute commands or contact a provider.
- Up to 200 saved comments per task, 2,000 characters per comment, and 20 MB of stored checkpoint contents. Fix requests must fit the existing message budget; select fewer comments if the request is too long.
- File marks bind to exact contents/existence/mode, not approximate relocated line numbers. Source changes do not erase prior comments or silently move their anchors.
- Review, patch export, and project merge use the task's original base commit, including changes committed locally later. Patch export retains its existing text/size exclusions. Project merge refuses a partial result when any changed path is unsupported; inspect those changes with local Git instead. Temporal may retry an interrupted merge activity with the same operation ID; its journal and reverse-apply check prevent a completed patch from being applied twice.
- This milestone does not add a terminal, preview server, editor language services, GitHub writes, CI polling, or deployment controls.

## Validation

The review tests cover persistent checkpoints, stale-file conflicts, concurrent duplicate comments, line anchors, resolution/reopening, targeted follow-up delivery, additions/deletions, Unicode paths, permission changes, local commits, unsupported/secret files, and task cleanup. Project merge tests cover non-overlapping local edits, add/delete changes, repeat requests, stale acceptance, unsupported files, and conflict rejection without source writes. The live Temporal test sends a review-driven correction in the same task, restarts at its approval gate, and verifies completion without auto-resolving comments. Provider traffic in that test uses a local SDK fixture.
