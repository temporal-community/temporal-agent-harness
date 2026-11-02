# Publishing the JavaScript packages

The release flow, for npm and for the Python package alike, is in the repository
[release guide](../../RELEASE.md): **Prepare release** bumps and pushes a branch, merging its PR
tags, creates the GitHub Release and publishes. This page covers what is specific to these
packages: how they version, how to test them locally, and the one-time registry setup.

Every `@temporalio/agent-harness*` package here shares one version. A release moves all of them
and every workspace dependency on one of them (`^<version>`), tags `npm-v<version>`, and
[Publish to npm](../../.github/workflows/publish-npm.yml) publishes the public ones in dependency
order with npm trusted publishing. `@temporalio/agent-harness` (core) and
`@temporalio/agent-harness-worker` publish from the first SDK release (see [RELEASE.md](../../RELEASE.md)).

| Package | Publishes today |
| --- | --- |
| `@temporalio/agent-harness-client` | yes |
| `@temporalio/agent-harness-react` | yes |
| `@temporalio/agent-harness-svelte` | yes |
| `@temporalio/agent-harness` | from the first SDK release |
| `@temporalio/agent-harness-worker` | from the first SDK release |

## Testing local changes without publishing

The packages are one pnpm workspace in `typescript/`, with one lockfile. A binding's
`^<version>` client dependency links the client in the working tree (`linkWorkspacePackages`), so
they build and test against it in the same commit; the published range is that same minimum.
`bazel test //typescript/...` builds the client first. For an IDE,
`bazel run //typescript:node_modules_link` links each package's `node_modules` to the one Bazel
builds, where workspace packages are already built; rerun it after a dependency or a workspace
package changes. It leaves a `node_modules` from `pnpm install` in place. Outside Bazel, build the
client before the bindings, which resolve it through `packages/client/dist`, and again after
editing it. Run these commands from `typescript/`. They need the pnpm `package.json` pins; without
it, run each `pnpm <args>` as `bazel run //typescript:pnpm -- --dir "$PWD" <args>`, which brings
that pnpm and its Node:

```sh
pnpm install
(cd packages/client && pnpm run build)
(cd packages/react && pnpm run check && pnpm test && pnpm run build)
(cd packages/svelte && pnpm run check && pnpm test && pnpm run build)
```

### Testing in a separate application

CI does this on every change: [check-installed-npm.sh](../../scripts/check-installed-npm.sh)
installs the tarballs Bazel builds into an empty project and type-checks a consumer. To do it by
hand, from the repository root:

```sh
bazel build //typescript/packages/client:pkg_tgz //typescript/packages/react:pkg_tgz //typescript/packages/svelte:pkg_tgz
mkdir -p /tmp/tarballs
for p in client react svelte; do
  cp "$(bazel info execution_root)/$(bazel cquery //typescript/packages/$p:pkg_tgz --output=files)" /tmp/tarballs/$p.tgz
done
scripts/check-installed-npm.sh /tmp/tarballs
```

Or install those tarballs into your own application with `npm install --no-save
/tmp/tarballs/client.tgz /tmp/tarballs/react.tgz` (the client tarball must be installed alongside a
binding so the binding's dependency resolves to it). Tarballs are snapshots: rebuild and reinstall
after further edits, and keep `.tgz` files out of commits.

## One-time setup

1. In the GitHub repository settings, the `npm` environment exists with a deployment tag policy
   that admits `npm-v*` (it was created with one policy per package; add the new one with
   `gh api --method POST repos/temporal-community/temporal-agent-harness/environments/npm/deployment-branch-policies -f name='npm-v*' -f type=tag`).
2. For **each** public npm package, open Settings → Trusted Publisher → GitHub Actions and enter:

   | Field | Value |
   | --- | --- |
   | Organization or user | `temporal-community` |
   | Repository | `temporal-agent-harness` |
   | Workflow filename | `publish-npm.yml` |
   | Environment name | `npm` |
   | Allowed actions, if shown | Allow direct `npm publish` |

The GitHub owner is `temporal-community`, even though the npm scope is `@temporalio`.
All fields must match exactly. No `NPM_TOKEN` secret is needed. See the
[npm trusted publishing documentation](https://docs.npmjs.com/trusted-publishers/).

## Release

Follow the [release guide](../../RELEASE.md#release) with unit `npm`. In short: **Actions →
Prepare release → Run workflow** on `main`, choose `npm` and the bump, open the PR from the run
summary, write the notes, merge. The merge tags `npm-v<version>`, creates the GitHub Release and
dispatches **Publish to npm**, which publishes client, then the bindings. Only one open release PR
per unit is allowed.

Stable `X.Y.Z` versions only, published to npm's `latest` tag. A prerelease channel needs tooling
changes first.

## Manual fallback

From an up-to-date `main` with passing CI, bump every lockstep package by hand (or run
`node scripts/release.mjs prepare` locally with `RELEASE_UNIT=npm RELEASE_BUMP=patch
GITHUB_REF=refs/heads/main GITHUB_REPOSITORY=temporal-community/temporal-agent-harness
GITHUB_OUTPUT=/dev/null RUNNER_TEMP=/tmp`), merge the change through the usual review, and from
the merged commit:

```sh
git tag npm-v0.1.1
git push origin npm-v0.1.1
```

The tag push triggers **Publish to npm**, which requires every public package's version to equal
the tag's and its dependencies on sibling packages to be `^<that version>`.

## Recovering a failed release

- **Prepare** failed before pushing: fix and rerun. The branch was pushed: open a PR from it. The
  workflow never force-pushes; delete an unused release branch before preparing that version again.
- **Release merged PR** failed: rerun it. It reuses a tag that already points at the merged commit,
  skips an existing GitHub Release, and never moves a tag.
- **Publish to npm** failed: check the registry, then rerun against the same tag
  (`gh workflow run publish-npm.yml --ref npm-v0.1.1`). Packages already at that version are
  skipped, so the rerun publishes only what is missing. A published version can never be
  overwritten; prepare another release for subsequent changes.
