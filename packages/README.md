# Publishing the JavaScript packages

The client, React binding, and Svelte binding release independently through
[Prepare npm release](../.github/workflows/prepare-npm-release.yml). Run the action,
review its release PR, and merge it to publish using npm trusted publishing.
[Publish to npm](../.github/workflows/publish-npm.yml) performs the actual build and publication.

| Package | Tag format |
| --- | --- |
| `@temporalio/agent-harness-client` | `npm-client-v<version>` |
| `@temporalio/agent-harness-react` | `npm-react-v<version>` |
| `@temporalio/agent-harness-svelte` | `npm-svelte-v<version>` |

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
   builds the package, then opens a release PR. Find the PR link in the run summary.
5. Review the version and dependency changes. If GitHub shows **Approve workflows to run**,
   approve the pending CI runs. Wait for the required checks and merge the PR through GitHub.
6. **Release merged npm PR** tags the exact merged commit and explicitly dispatches
   **Publish to npm**, which tests and builds again before publishing.

Only one open release PR per package is allowed. Merge or close it before preparing
another release for that package. Closing without merging does not publish anything.
If preparing a binding with a new client dependency, release the client first and
wait for its version to become available on npm.

In **Settings → Actions → General → Workflow permissions**, enable **Allow GitHub
Actions to create and approve pull requests**. Organization policy may control this
setting. The workflows request their own scoped write permissions, so no additional
GitHub token or npm secret is needed. Existing npm trusted publisher settings remain
`publish-npm.yml` with environment `npm`.

The preparation workflow must be run from `main`; other refs are skipped. Release PRs
use branches named `release/npm-<package>-v<version>`. Keep these names unchanged.
Tagging only happens for merged release PRs created by `github-actions[bot]` in this
repository. The workflow does not create GitHub Releases or trigger PyPI publishing.

GitHub does not trigger push workflows for tags created with `GITHUB_TOKEN`, so the
merge workflow dispatches publishing explicitly. PRs created with that token may
require a maintainer to approve CI runs. See [GitHub's workflow triggering rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

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

- If preparation fails before opening a PR, fix the error and rerun it. If the branch
  was pushed but PR creation failed (for example, Actions lacked permission to open
  PRs), open a PR from that existing branch to review the changes. Because automated
  tagging requires a bot-authored PR, use the manual tag fallback after merging a
  manually opened PR, or delete the unused branch and rerun preparation.
- If **Release merged npm PR** fails, rerun that job. It reuses a tag only when it
  already points to the same merged commit and never moves an existing tag.
- If **Publish to npm** fails, inspect that run and retry it against the same tag.
  Check the registry first if publication may have succeeded. A published version
  cannot be overwritten; prepare another release for subsequent changes.
