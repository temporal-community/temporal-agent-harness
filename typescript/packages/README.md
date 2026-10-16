# Publishing the JavaScript packages

The client, React binding, and Svelte binding release independently through
[Prepare npm release](../.github/workflows/prepare-npm-release.yml). Run the action,
open a PR from its prepared branch, and merge it to publish using npm trusted publishing.
[Publish to npm](../.github/workflows/publish-npm.yml) performs the actual build and publication.

| Package | Tag format |
| --- | --- |
| `@temporalio/agent-harness-client` | `npm-client-v<version>` |
| `@temporalio/agent-harness-react` | `npm-react-v<version>` |
| `@temporalio/agent-harness-svelte` | `npm-svelte-v<version>` |

## Testing local changes without publishing

Run these commands from the repository root to build the shared client and link it
into both bindings:

```sh
(cd packages/client && npm install && npm run build)
(cd packages/react && npm install --no-save --package-lock=false ../client)
(cd packages/svelte && npm install --no-save --package-lock=false ../client)
```

The bindings' `.npmrc` files set `install-links=false`, so the local client is linked
instead of copied. The `--no-save --package-lock=false` flags keep the bindings'
published dependency declarations and lockfiles unchanged.

Check, test, and build the bindings against the local client:

```sh
(cd packages/react && npm run check && npm test && npm run build)
(cd packages/svelte && npm run check && npm test && npm run build)
```

After editing the shared client, rebuild it so the bindings see the changes:

```sh
(cd packages/client && npm run build)
```

To restore the published dependencies from the lockfiles:

```sh
(cd packages/react && npm ci)
(cd packages/svelte && npm ci)
```

### Testing in a separate application

After building the local client and bindings, create tarballs containing the files
that would be published:

```sh
(cd packages/client && npm pack)
(cd packages/react && npm pack)
(cd packages/svelte && npm pack)
```

Each command prints the generated `.tgz` filename in that package's directory. In
your test app, install the client tarball and the relevant binding tarball together,
using their absolute paths. For example:

```sh
npm install --no-save --package-lock=false \
  /path/to/temporal-agent-harness/packages/client/temporalio-agent-harness-client-0.1.0.tgz \
  /path/to/temporal-agent-harness/packages/react/temporalio-agent-harness-react-0.1.0.tgz
```

Use the filenames produced by `npm pack`, and substitute the Svelte tarball for a
Svelte app. The local client's version must satisfy the binding's declared dependency
range so npm uses it for the binding. Tarballs are snapshots: rebuild, repack, and
reinstall after further edits. Run `npm ci` in the test app to restore its locked
dependencies, and keep generated `.tgz` files out of commits.

## One-time setup

1. Merge the workflows, release scripts, and package metadata into `main`.
2. In the GitHub repository settings, create an environment named `npm`.
3. For **each** of the three npm packages, open Settings → Trusted Publisher →
   GitHub Actions and enter:

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

## Release a package from GitHub

1. Open **Actions → Prepare npm release → Run workflow** and select the `main` branch.
2. Choose `client`, `react`, or `svelte`, then `patch`, `minor`, or `major`.
3. For a binding, optionally enter an already published client version such as `0.1.1`.
   Leave it blank to keep the current dependency. The workflow records it as `^0.1.1`.
4. Run the workflow. It updates the package manifest and lockfile, checks, tests, and
   builds the package, then pushes a release branch.
5. In the run summary, click **Create the release pull request**. The link prefills
   the branch, title, and description. Click **Create pull request** on GitHub.
6. Review the version and dependency changes, wait for the required CI checks, and merge.
7. **Release merged npm PR** tags the exact merged commit and explicitly dispatches
   **Publish to npm**, which tests and builds again before publishing.

Only one open release PR per package is allowed. Merge or close it before preparing
another release for that package. Closing without merging does not publish anything.
If preparing a binding with a new client dependency, release the client first and
wait for its version to become available on npm.

The workflow pushes branches with `contents: write` and checks for existing release
PRs with `pull-requests: read`. You create the PR yourself, so **Allow GitHub Actions
to create and approve pull requests** can remain disabled. No additional GitHub token
or npm secret is needed. Existing npm trusted publisher settings remain
`publish-npm.yml` with environment `npm`.

The preparation workflow must be run from `main`; other refs are skipped. Release PRs
use branches named `release/npm-<package>-v<version>`. Keep these names unchanged.
Tagging only happens for release branches in this repository merged into `main`,
with the package and lockfile versions matching the release branch. The PR author
can be a maintainer. The workflow does not create GitHub Releases or trigger PyPI publishing.

GitHub does not trigger push workflows for tags created with `GITHUB_TOKEN`, so the
merge workflow dispatches publishing explicitly. Opening the PR yourself triggers
the normal PR checks. See [GitHub's workflow triggering rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Manual fallback

Start from an up-to-date `main` with passing CI. For example, to release the next
client patch, run from the repository root:

```sh
(cd packages/client && npm version patch --no-git-tag-version)
git add packages/client/package.json packages/client/package-lock.json
git commit -m "Release client 0.1.1"
```

Use the version actually written by `npm version` in the commit message and tag.
Merge the version change into `main` through the usual review process. Then, from
the merged release commit:

```sh
git tag npm-client-v0.1.1
git push origin npm-client-v0.1.1
```

Use `react` or `svelte` in the directory and tag for a binding release. The workflow
requires the tag version to match both the package manifest and lockfile. It supports
stable versions only and publishes to npm's `latest` tag.

If a binding needs a new client version, publish the client first and wait until
`npm view @temporalio/agent-harness-client@<version> version` succeeds. Then update
the binding with `npm install '@temporalio/agent-harness-client@^<version>'`, commit
its manifest and lockfile, and release the binding. Local `file:` dependencies
cannot be published by this workflow.

Monitor the **Publish to npm** run in GitHub Actions. For a failed run before publication,
rerun it, or use `gh workflow run publish-npm.yml --ref npm-client-v0.1.1` after the
workflow exists on the default branch. Already published versions cannot be reused;
check npm before retrying a run whose publish result is uncertain.

Pushing these tags does not trigger the PyPI workflow. Creating a GitHub Release
does trigger that workflow, so use tag pushes for npm releases.

## Recovering a failed automated release

- If preparation fails before pushing the branch, fix the error and rerun it. If the
  branch was already pushed, open a PR from that branch using the run summary link
  or GitHub's **Compare & pull request** button. The workflow never force-pushes an
  existing branch; delete an unused release branch before preparing that version again.
- If **Release merged npm PR** fails, rerun that job. It reuses a tag only when it
  already points to the same merged commit and never moves an existing tag.
- If **Publish to npm** fails, inspect that run and retry it against the same tag.
  Check the registry first if publication may have succeeded. A published version
  cannot be overwritten; prepare another release for subsequent changes.
