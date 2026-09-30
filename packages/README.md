# Publishing the JavaScript packages

The client, React binding, and Svelte binding release independently through
[Publish to npm](../.github/workflows/publish-npm.yml). Pushing a stable release tag
tests, builds, and publishes the corresponding package using npm trusted publishing.

| Package | Tag format |
| --- | --- |
| `@temporalio/agent-harness-client` | `npm-client-v<version>` |
| `@temporalio/agent-harness-react` | `npm-react-v<version>` |
| `@temporalio/agent-harness-svelte` | `npm-svelte-v<version>` |

## One-time setup

1. Merge the workflow and package metadata into `main`.
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

## Release a package

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
