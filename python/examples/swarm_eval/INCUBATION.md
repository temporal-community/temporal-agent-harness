# From an idea to a product

The first candidate is **Agent Regression Lab**, a regression-testing tool for teams building
agents with the harness. `candidate.json` records its target user, problem, current workaround,
Temporal and harness advantages, adoption hypothesis, and acceptance thresholds. It is a hypothesis,
not a declaration of product fit. Its initial experiment uses Monty as a pilot target.

The lifecycle is: candidate → evaluated → assessed → declared ready → promotion patch.
The CLI writes immutable JSON artifacts under `.incubation/` (gitignored). Keep these files in
reliable storage if the checkout is temporary. They contain full conversations and tool outputs;
review their contents before sharing a promotion patch.

## Run the first small experiment

Start Temporal and the eval worker as described in [README.md](README.md). Use the same checkout
for the workers and CLI; the source snapshot records the local checkout, not a remote worker's
build attestation. Restart workers after code changes. Then, from the repository root:

```bash
bazel run //python/examples/swarm_eval:incubate -- evaluate \
  --candidate python/examples/swarm_eval/candidate.json \
  --output .incubation/evaluation.json
```

This starts a fresh dispatcher, prints its session ID immediately, runs eight cases with at most
two conversations and one judge, and exports the full evidence. The source fingerprint covers
tracked and nonignored files, including uncommitted changes. Editing code during the experiment
invalidates export; editing it afterward requires another evaluation before readiness.

For a no-key smoke run, copy the candidate to `.incubation/candidate.json`, set
`evaluation.simulate` to `true`, and pass that path. Simulated evidence is always ineligible for
readiness. No product is declared ready by the smoke run.

An interrupted CLI leaves the durable session running. Its `export` handler accepts `run_id`
and returns the full evaluation contract, cases, results and report. The CLI's timeout stops
waiting; it does not cancel the remote experiment. Local files are never overwritten with
different contents: choose a new output name for each experiment.

## Evaluate a different agent

Set both `evaluation.target` and `evaluation.profile` in the candidate. A target runs on an
already-running worker; this command does not deploy it. For example:

```json
{
  "target": {
    "workflow_type": "WorkflowIncidentAssistant",
    "task_queue": "incident-assistant",
    "handler": "investigate",
    "input_field": "question",
    "output_field": "answer",
    "model_handler": null,
    "init_data": {"environment": "sandbox"}
  },
  "profile": {
    "description": "A read-only assistant that diagnoses failed Temporal workflows from tool evidence.",
    "categories": [{
      "key": "diagnosis",
      "title": "Failure diagnosis",
      "brief": "Ask why a known workflow failed, supplying its workflow ID.",
      "rubric": "Identifies the failed activity and cites its actual failure; makes no mutations."
    }]
  }
}
```

Update `categories` and `required_categories` to match the profile. Custom agents use their
normal safe-tool approval policy; the evaluator does not automatically approve arbitrary side
effects. Custom targets cannot use the built-in travel simulation. The evaluation worker needs
OpenAI credentials; run custom target workers separately. Target model selection is optional
through `model_handler`; the default target model setting is intended for Monty.

## Record fit evidence and inspect readiness

Copy `fit.template.json` to `.incubation/fit.json`. Bind it to the evaluation hash printed by the
CLI. Record observations, references and a named assessor for all five dimensions:

- **Problem:** intended users encountered the problem, with concrete examples.
- **Temporal advantage:** durable execution, recovery, or orchestration materially helps.
- **Harness advantage:** the harness reduces the work needed to build/adopt the solution.
- **Adoption:** an intended user tried the integration and found the outcome useful.
- **Operability:** tests and a recovery exercise support running and maintaining the product.

Record actual provider cost and its usage-record reference. Change `kind` to `observed` only
when these are real observations. Unsupported or unknown dimensions block readiness, as do
synthetic evidence, missing cases, invalid verdicts, execution errors, failed categories,
threshold violations, or a mismatched source/configuration. References are assessor-supplied;
the CLI does not independently verify interviews or billing records. Archive their underlying
records alongside the assessment.

```bash
bazel run //python/examples/swarm_eval:incubate -- assess \
  --evaluation .incubation/evaluation.json --fit .incubation/fit.json
```

Assessments return a list of blockers and a nonzero status while anything is missing. Passing
this check does not itself declare readiness. An operator makes that decision explicitly:

```bash
bazel run //python/examples/swarm_eval:incubate -- declare-ready \
  --evaluation .incubation/evaluation.json --fit .incubation/fit.json \
  --by 'Your name' --rationale 'Why this evidence justifies product ownership' \
  --output .incubation/readiness.json
```

This declaration binds hashes of the complete candidate, evaluation, fit assessment and source.
It is not exposed as a model-callable tool. This is a local trusted-operator workflow, not an
authenticated multi-user approval service.

## Prepare the move for review

```bash
bazel run //python/examples/swarm_eval:incubate -- prepare \
  --evaluation .incubation/evaluation.json --fit .incubation/fit.json \
  --declaration .incubation/readiness.json --output .incubation/promotion.patch
git apply --check .incubation/promotion.patch
```

The patch moves the example into `products/<slug>/examples/<module>`, updates repository path
references and Bazel access, removes it from the example dev list, and adds a product dev target,
README and readiness/evidence bundle. Keeping the Python namespace stable avoids renaming saved
workflow code. The command creates a patch; it does not apply it, commit, publish, or deploy.

Review and apply it on a branch, then run `just gazelle`, `just buildifier`, the promoted
`:agents_update`, its registry tests and affected Python tests. Smoke-test
`bazel run //products/<slug>/examples:dev` against the configured Temporal server before merging.
The first milestone supports text-only Bazel Python examples; symlink and binary assets require
an explicit migration instead of being silently dropped.

## Limits of this milestone

Candidate experiments are bounded to 32 cases, four active conversations and two judges.
When reusing a saved suite, set `evaluation.max_cases` to 32 or less. Cost and duration thresholds
are readiness checks, not live spending caps; the CLI timeout does not stop remote work. Each
lane runs sequentially; concurrency is a ceiling rather than a throughput promise. Suite names
are immutable, and comparisons require matching suite contents, target/profile and evaluator
configuration. Full evidence is retained; exports above 1.5 MB fail explicitly. Large experiments
need artifact storage and pagination before scaling up.

A local source fingerprint does not prove which build a remote worker executed. This is why the
operability assessment must verify worker provenance. Similarly, product-fit judgments are
explicit human assessments, not market validation generated by simulated users. General swarm
claim reclamation and deduplication across board Continue-As-New remain separate runtime work.
