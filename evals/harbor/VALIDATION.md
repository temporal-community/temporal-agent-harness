# Validation and boundaries

This experiment was moved from the standalone Harbor prototype onto the `evals`
branch, based on repository commit `9eaf026e3d07b77f7d5b9776bb1deb37af7f20a4`.
The Python SDK is an editable dependency on this checkout, not the original
prototype's pinned remote revision.

## Compatibility workaround

At the branch base, `OpenAIStreamObserver._emit_tool_requested` evaluates
`event.name` while constructing `dict.pop`'s default, even when the call metadata
already exists. OpenAI arguments-done events do not have that attribute.
`src/harness_evals/demo_entry.py` applies a narrow, process-local replacement
that uses the captured opening-call metadata. A regression test exercises that
case. Only the OpenAI example-worker launcher applies it; the generic adapter and
exporter do not patch the SDK. Remove the workaround when the observer is fixed.

## Saved evidence

`examples/saved-jobs/rewardkit-matrix/` contains the original four successful
Harbor trials: two example agents, each on weather and greeting. All have
behavior, quality, and overall reward of 1. These used Agent Harness 0.8.0,
Harbor 0.23.0, and Rewardkit 0.2.4. They predate this port; their model-judge scores
are saved evidence, not newly executed model calls during the move.

## Scope

The adapter supports a local Temporal connection, one text instruction, and one
agent per trial. Tests cover event conversion, lifecycle/timeout cleanup, missing
or corrupt evidence, real deterministic Rewardkit execution, and mocked judge
success/failure. They do not prove Cloud/TLS, subagents, approvals, callbacks,
or agent access to sandbox files. The host `rescore` command grades saved JSON;
it does not import a completed session by Temporal session ID.

## Checks on this checkout

Validated on 2026-11-06 with Python 3.12.14 and SDK 0.9.0:

- `uv sync --locked --offline`: passed with the SDK resolved from `../../python`.
- `uv run pytest -q`: **32 passed**. This includes running the real task scripts
  against all four saved recordings without model credentials.
- Ruff checks and formatting: passed for `src`, `tests`, `tasks`, and `run.py`.
- Both original example worker modules import successfully in this environment.
  The editable SDK import points to this checkout.
- `run.py` imports and constructs its user agent configuration successfully;
  CLI help loads and lists the run, rescore, and example-worker commands.
- Local documentation links and saved result JSON validate. Included raw/ATIF
  evidence and Rewardkit rewards/details match the standalone originals byte for byte.
- The added project files contain no original machine-specific absolute paths or
  API-key-shaped strings. Generated caches and job output are ignored.
- `git diff --cached --check`: passed.

No new live agent, Docker verifier, or model-judge run was performed during the
move. The live results bundled above came from the original prototype.
The broader SDK suite was not run. `just gazelle` and `just buildifier` could not
run because `just` and Bazel are not installed here; the only added Bazel file
excludes this standalone project from Gazelle generation.
