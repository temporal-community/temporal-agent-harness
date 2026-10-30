# Releasing

One flow releases either unit: run **Prepare release**, open the PR it pushes, merge it. Merging
tags the merged commit, creates the GitHub Release from the PR description, and publishes.
Everything that can be checked by a machine runs in CI on every pull request; this page is the
rest: what to decide, what to write down, and what to do when something fails.

## What ships

| Unit | Packages | Version source | Tag |
| --- | --- | --- | --- |
| `python` | `temporal-agent-harness` on PyPI: the SDK, agent client, CLI, server, session manager and the compiled console | [python/pyproject.toml](python/pyproject.toml) | `<version>` (bare: the `pypi` GitHub environment only admits tags matching `[0-9]*.[0-9]*.[0-9]*`) |
| `npm` | Every `@temporalio/agent-harness*` package under [typescript/packages](typescript/packages), at **one shared version**: `-client`, `-react` and `-svelte` publish today; `@temporalio/agent-harness` (core) and `-worker` publish from [their first release](#first-typescript-sdk-release) | each package's `package.json`, kept equal by the tooling | `npm-v<version>` |

`@temporal-agent-harness/codegen` is private build tooling with no release. Examples ship in the
repository, not in any package: release notes name the tag to check out.

The TypeScript runtime uses the Python server, session manager and console, so a server or console
change needs a `python` release even when the motivating feature is TypeScript. The two units
release independently otherwise; nothing requires their version numbers to match.

### Versions

Pre-1.0, a **patch** is compatible fixes, security updates and packaging corrections; a **minor** is
new capabilities or breaking changes, with the breaking changes and migrations spelled out in the
notes. **1.0** is an explicit stability decision, never a way to align the two units' numbers.
Package versions say nothing about wire or workflow-history compatibility.

The npm packages move together: a release bumps every `@temporalio/agent-harness*` manifest and
every workspace dependency on one of them to `^<version>`, so a binding always requires the client
it was built and tested with, and publishes them in dependency order. One version number, one tag,
one PR. (`workspace:` ranges, which pnpm resolves itself, are left alone.)

### Parity

Shared agent behavior (handlers, tools, approvals, state, streams, subagents) lands in both SDKs in
the same change, or the [parity guide](typescript/docs/python-parity.md) records the gap: feature,
language, user impact, workaround, tracking issue, owner. The release reviewer accepts each new gap
explicitly and the notes call it out. Language-specific APIs and provider integrations may ship in
one SDK with the reason documented. A gap never excuses a broken shared contract.

## What CI already checks

Every pull request, including a release PR, runs [tests.yml](.github/workflows/tests.yml):

- `bazel test //...` on Python 3.11–3.14: the harness, both SDKs' conformance runs against the
  shared scenarios, the replay tests, the examples, and the checks of the built wheel, sdist and
  npm tarballs against what their manifests declare.
- Generated Bazel files, Python package entry points, TypeScript test targets, and the pytest
  groups Bazel does not run yet.
- **Installed artifacts**: [check-installed-python.sh](scripts/check-installed-python.sh) installs
  the built wheel into an empty environment on the oldest and newest supported interpreters, runs
  the CLI, imports every integration, and runs the conformance scenarios against the install;
  [check-installed-npm.sh](scripts/check-installed-npm.sh) installs the built tarballs into an
  empty project, checks the bindings resolve that client, and type-checks a consumer of every
  package. Both run from the repository root against Bazel's outputs; the Python one holds
  third-party versions to `uv.lock`, so a dependency the lock has not caught up with is a change to
  make in a PR, not a release-day surprise.

A green release PR therefore already means the artifacts build, pass, install and run. The
publish workflows rerun the unit's own tests before uploading, but they are not the gate; the PR is.

## Before a release

Judgment calls the pipeline cannot make. Put the answers in the release PR description.

- [ ] **Scope and bump.** What changed since the unit's last tag, and which bump that is. Check
  the version is unused on the registry and in git (the tooling bumps, so it will be).
- [ ] **Parity.** New or continuing gaps are recorded and accepted; the notes mention them.
- [ ] **Protocol, registry or schema changes.** Current producers were tried with the previous
  supported clients and current clients with the previous producers. If a pair cannot work, the
  notes give the upgrade order rather than claiming compatibility.
- [ ] **Workflow changes.** Replay against retained histories passed (CI runs the replay tests;
  add coverage for the changed behavior). Worker deployment or migration steps are written down.
  Never regenerate old histories to make a failure go away.
- [ ] **Changed provider integrations.** A live smoke test against the provider, in both
  languages where both have it; scripted model tests do not prove provider access. Record what was
  skipped and why.
- [ ] **Release notes.** Features by package, fixes, breaking changes and migrations, parity gaps,
  the tested combination (Python package, npm packages, Temporal server), and the example checkout
  tag. GitHub's generated notes are a fine starting point; the PR description becomes the Release.

## Release

1. **Actions → Prepare release → Run workflow** on `main`. Choose the unit and the bump.
   - `python`: bumps `pyproject.toml`, runs `uv lock`, moves the README's install pins forward,
     then runs `bazel test //python/tests:test_packaged_distributions` at the new version.
   - `npm`: bumps every lockstep manifest and dependency range, refreshes `pnpm-lock.yaml`, then
     runs `bazel test //typescript/packages/...`.

   It pushes `release/<unit>-v<version>` and refuses to run beside an open release PR for the unit.
2. Open the PR from the link in the run summary. Replace the placeholder release notes in the
   description. Review the diff: for `python`, the lockfile should change only the project's own
   version unless a dependency moved; for `npm`, every manifest and `.aspect/` record.
3. Wait for CI, get a review, **merge**. [Release merged PR](.github/workflows/finish-release.yml)
   then tags the merged commit, creates a GitHub Release from the description (marked pre-release
   while the version is `0.x`), and dispatches the publish workflow. It is idempotent: rerun it
   after a failure and it reuses a tag that already points at the merged commit, skips an existing
   Release, and never moves a tag.
4. Watch the publish run.
   - [Publish to PyPI](.github/workflows/publish.yml) tests `//python/...`, builds `//python:dists`,
     checks the archives' version against the tag, and uploads through the `pypi` environment with
     OIDC. **That environment has a required reviewer**: the job waits until a reviewer listed in
     the environment's settings approves it under *Review deployments* on the run page. Make sure
     one is around before merging.
   - [Publish to npm](.github/workflows/publish-npm.yml) validates every public package against
     the tag, tests `//typescript/packages/...`, builds each `:pkg_tgz`, and publishes them in
     dependency order through the `npm` environment with provenance. A package already at that
     version on the registry is skipped, so a rerun finishes a partial publish.
5. Verify from the registries and note it in the PR:

   ```sh
   uv pip install 'temporal-agent-harness[ui]==<version>' && temporal-agent-harness --help
   npm view @temporalio/agent-harness-client@<version> version
   npm view @temporalio/agent-harness-react@<version> dependencies
   ```

   The npm clients had one hand-made release, `0.1.0`, before this automation existed; its first
   run is the first automated npm release, so watch each stage rather than assuming the next fired.
   Before it, the `npm` GitHub environment must admit the new tag pattern (it was created with
   one policy per package):

   ```sh
   gh api --method POST repos/temporal-community/temporal-agent-harness/environments/npm/deployment-branch-policies -f name='npm-v*' -f type=tag
   ```

Do not tag or create GitHub Releases by hand in the normal flow; the merge does both. Pushing a
matching tag yourself (`0.9.0` or `npm-v0.9.0`) is the manual fallback and also publishes.

## When something fails

- **Prepare** fails before pushing: fix and rerun. It pushed but you cannot find the link: open a
  PR from the `release/...` branch yourself. It never force-pushes; delete an abandoned release
  branch before preparing that version again.
- **Release merged PR** fails: rerun it. See step 3 above.
- **A publish** fails: look at the registry first; an upload can succeed and a later step fail.
  Then rerun the publish workflow against the same tag (`gh workflow run publish.yml --ref 0.9.0`,
  `gh workflow run publish-npm.yml --ref npm-v0.9.0`). Never against a branch.
- **A bad release**: never move a published tag or reuse a version. Stop dependent releases,
  write down the affected versions, release a corrected version, and consider yanking or
  deprecating the bad one. Reinstalling an older worker may not safely undo a deployment; assess
  workflow-history compatibility before recommending it.
- **Coordinated changes** (a new server and a new client): publication is not atomic across
  PyPI and npm. Publish in the tested upgrade order and announce when every artifact is live.

## First TypeScript SDK release

`@temporalio/agent-harness` and `@temporalio/agent-harness-worker` are already in the lockstep set
(their versions and ranges move with every npm release) and have tarball targets and tests. To
publish them, in a reviewed PR:

- [x] Drop `private: true` from both manifests and add the `publishConfig` the other packages
  have. The tooling then includes them in the publish order and in
  [check-installed-npm.sh](scripts/check-installed-npm.sh), which installs both next to the Temporal
  SDK version the worker's `@temporalio/openai-agents` peer pins and loads their entry points.
- [ ] Configure npm trusted publishing for each new package: repository
  `temporal-community/temporal-agent-harness`, workflow `publish-npm.yml`, environment `npm`.
  Check whether the registry needs a bootstrap for a new package before its first OIDC publish.
- [ ] Confirm supported Node, Temporal and provider versions, the optional peers, and that a clean
  install with the native Monty dependency works.
- [ ] Update the SDK quickstart to the published packages and the Python server version they
  were tested with.

Then release `npm` as usual. The bindings and the SDK share the version from that point on.

## Release record

The PR description, once the notes are in, is the record. Make sure it answers:

```text
Unit, previous → new version, merged commit, tag
What changed and why this bump
Tested combination (Python package, npm packages, Temporal server) and upgrade order
Breaking changes and migrations; workflow-history notes
Parity gaps accepted (feature, language, impact, issue, owner)
Live provider smoke tests run or skipped, and why
Publish run, registry verification, follow-up issues
```
