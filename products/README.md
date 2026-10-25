# Products

Products graduate from `python/examples` through an explicit readiness declaration. A high model
score alone does not establish product fit. The first candidate is
[Agent Regression Lab](../python/examples/swarm_eval/candidate.json); it remains an example.

Use the [incubation workflow](../python/examples/swarm_eval/INCUBATION.md) to run a small experiment,
record actual product-fit observations, declare an evaluated revision ready, and prepare a
promotion patch. Review the patch and run its build and tests before merging it.

A promoted Python product owns `products/<slug>/examples/<module>`, its README, and the exact
readiness/evidence bundle. The `examples.<module>` import namespace remains stable for workflow
compatibility; the files and development entry point belong to the product. Products still depend
on the harness and any explicitly declared example dependencies within this monorepo.
