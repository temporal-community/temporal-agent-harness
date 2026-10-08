# Cedar auto mode example

The [auto mode travel agent](../auto_mode/README.md), with its gated tool calls judged by a
[Cedar](https://www.cedarpolicy.com) policy instead of by Jev. The agent asks the policy with
`agent.cedar_evaluator(...)`, which calls a `VerifyToolCalls` Nexus operation. The Cedar verifier
that serves that operation lives in another repo; you run it next to the agent worker (see
[The policy service](#the-policy-service-lives-in-another-repo)).

Nothing in `workflow.py` says which tool may do what. That is all in
[`policies/travel.cedar`](policies/travel.cedar).

## What the policy sees

Each gated call is one Cedar request for the action `travel-agent/<tool>`:

- **A script** (`run_travel_code`). Cedar can't read a script, so the evaluator sends facts
  about it instead:
  - what `analyze_script` reads off the source: which host functions it calls, how many call
    sites each has, whether any can run in a loop or concurrently, and whether it uses
    `eval` / `exec`;
  - Jev's answers to yes/no questions about it (`jev_script_classifier()`), as 0–100, such as
    whether it could make an unbounded number of calls.
- **Each host call the script makes**, with its concrete arguments, e.g.
  `{"request": {"flight_id": "F1", "passenger_name": "Ada"}}`.

[`policies/schema.cedarschema`](policies/schema.cedarschema) declares all of it. It is
generated from the agent's tools (`just schema`), so the policy can't drift from what the agent
sends.

What [`policies/travel.cedar`](policies/travel.cedar) decides:

- **Scripts run unless a `forbid` matches**: booking in a loop, concurrently or twice in one
  script, using `eval`, or rated by Jev as likely to run away.
- **Searches run without asking** (`lookups`).
- **Every booking comes to you.** No policy permits `book_flight` or `book_hotel`, and a call
  nothing permits escalates. Only a `forbid` refuses: a flight booking with no passenger name.

## The policy service lives in another repo

The Cedar verifier is in
[02strich/temporal-untrusted-workers](https://github.com/02strich/temporal-untrusted-workers),
on the `nexus-command-verifier` branch, under `verifiers/cedar`. It's a Rust program that polls a
Temporal task queue for `VerifyToolCalls` Nexus operations and answers each with the decision of
the Cedar policy it was started with. This repo only supplies that policy, in
[`policies/`](policies).

If cloning it fails with "repository not found", you don't have access yet: ask its owner
([@02strich](https://github.com/02strich)) to add you.

## Setup

You need the [`temporal` CLI](https://docs.temporal.io/cli) and a Rust toolchain
([rustup](https://rustup.rs)) to build the verifier.

1. Copy the shared env template at the **repo root** and fill it in:

   ```bash
   cp .env.example .env.local     # run from the repo root
   ```

   Set `GEMINI_API_KEY` (the conversation) and `TYPESAFE_API_KEY` (the script classifier).
2. Clone the verifier anywhere outside this repo, and tell the recipes where it is:

   ```bash
   git clone -b nexus-command-verifier https://github.com/02strich/temporal-untrusted-workers
   export CEDAR_VERIFIER_DIR=$PWD/temporal-untrusted-workers/verifiers/cedar
   ```

   To keep it, put `CEDAR_VERIFIER_DIR=...` in the repo-root `.env.local` instead.

The verifier connects to `TEMPORAL_ADDRESS` (default `http://127.0.0.1:7233`) in
`TEMPORAL_NAMESPACE` (default `default`) and polls the `tool-verifier` task queue as the
`tool-policy` service, which is what `just endpoint` routes to and what the agent calls. If
`.env.local` points the agent at another server, set those, and `TEMPORAL_API_KEY`, to match, and
create the endpoint on that server.

## Run

Each command in its own terminal, all from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just endpoint          # 2. once per server: the tool-verifier Nexus endpoint
just verifier          # 3. the Cedar verifier on policies/travel.cedar (the first build takes minutes)
just session-manager   # 4. session-manager worker
just server            # 5. FastAPI API + built Svelte UI  ->  http://localhost:8000
just worker            # 6. the agent
```

`just verifier` also passes `policies/schema.cedarschema`, so the verifier refuses to start if the
policy names a tool or parameter the agent doesn't have. After changing the agent's tools, run
`just schema` and restart the verifier.

## Trying it

Open <http://localhost:8000> and start a **Travel agent (Cedar Auto mode)** session. Each gated
call shows an evaluation card with the action, the exact parameter Cedar saw, and Cedar's reason.

1. **"Find me flights from SFO to JFK on November 1st."** The script and its searches run
   without a prompt: `scripts` permits the script, `lookups` the searches.
2. **"Book the first one for Ada Lovelace."** The script runs, and the `book_flight` call comes
   to you: no policy permits it, so it escalates ("not permitted by any Cedar policy").
3. **"Book both flights for Ada."** If the model writes the bookings as a loop, the script is
   refused before anything runs, and the model is told why ("denied by Cedar policy
   no-bulk-flight-booking").
4. **Stop the verifier** and ask for anything gated. Each call escalates to you, with the failure
   on its `auto_approval_evaluation_error` event.

Edit `policies/travel.cedar` and restart the verifier to change what runs unattended.
