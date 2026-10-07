import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const stableVersion = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const packages = ['client', 'react', 'svelte'];
const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'));
const execute = (command, args, options = {}) =>
  execFileSync(command, args, { encoding: 'utf8', ...options }).trim();

export function validateInputs(pkg, bump, clientVersion = '') {
  assert(packages.includes(pkg), 'Select client, react, or svelte');
  assert(['patch', 'minor', 'major'].includes(bump), 'Select patch, minor, or major');
  assert(!clientVersion || stableVersion.test(clientVersion),
    'Client version must be an exact stable version, such as 0.1.1');
  assert(pkg !== 'client' || !clientVersion,
    'The optional client version applies only to React and Svelte');
}

export function releaseDetails(branch) {
  const match = /^release\/npm-(client|react|svelte)-v(.+)$/.exec(branch);
  assert(match && stableVersion.test(match[2]), 'Invalid npm release branch');
  const [, pkg, version] = match;
  return { pkg, version, tag: `npm-${pkg}-v${version}`, directory: `packages/${pkg}` };
}

export function validateRelease(manifest, lock, pkg, version) {
  assert.equal(manifest.name, `@temporalio/agent-harness-${pkg}`);
  assert.equal(manifest.version, version, 'Release branch must match package version');
  assert.equal(lock.version, version, 'Update the lockfile version');
  assert.equal(lock.packages[''].version, version, 'Update the lockfile root version');
}

export function prepare(env = process.env, run = execute) {
  const pkg = env.RELEASE_PACKAGE;
  const bump = env.RELEASE_BUMP;
  const clientVersion = (env.CLIENT_VERSION ?? '').trim();
  validateInputs(pkg, bump, clientVersion);
  assert.equal(env.GITHUB_REF, 'refs/heads/main', 'Run this workflow from main');
  const directory = `packages/${pkg}`;
  const manifest = readJson(`${directory}/package.json`);
  assert.equal(manifest.name, `@temporalio/agent-harness-${pkg}`);
  assert(stableVersion.test(manifest.version), 'Current version must be stable');

  const prs = JSON.parse(run('gh', ['pr', 'list', '--repo', env.GITHUB_REPOSITORY,
    '--base', 'main', '--state', 'open', '--limit', '1000', '--json', 'headRefName,url']));
  const existing = prs.find((pr) => pr.headRefName.startsWith(`release/npm-${pkg}-v`));
  assert(!existing, `An open ${pkg} release PR already exists: ${existing?.url}`);

  if (clientVersion) {
    // Resolve the requested version before changing either manifest or lockfile.
    const available = JSON.parse(run('npm', ['view',
      `@temporalio/agent-harness-client@${clientVersion}`, 'version', '--json',
      '--registry=https://registry.npmjs.org/', '--prefer-online']));
    assert.equal(available, clientVersion, 'Client version is not available on npm yet');
    run('npm', ['install', `@temporalio/agent-harness-client@^${clientVersion}`,
      '--package-lock-only', '--ignore-scripts'], { cwd: directory });
  }
  run('npm', ['version', bump, '--no-git-tag-version', '--ignore-scripts'], { cwd: directory });
  const updated = readJson(`${directory}/package.json`);
  const version = updated.version;
  const tag = `npm-${pkg}-v${version}`;
  validateRelease(updated, readJson(`${directory}/package-lock.json`), pkg, version);
  appendFileSync(env.GITHUB_OUTPUT,
    `directory=${directory}\nbranch=release/${tag}\ntag=${tag}\n`);
  const dependency = updated.dependencies?.['@temporalio/agent-harness-client'];
  writeFileSync(`${env.RUNNER_TEMP}/npm-release-body.md`, [
    `Release \`${updated.name}\` from **${manifest.version}** to **${version}**.`,
    '',
    dependency ? `Shared client dependency: \`${dependency}\`.` : '',
    '',
    'This PR was prepared by **Prepare npm release**. The package checks, tests, and build',
    'ran before the release branch was pushed. Wait for the required PR checks before merging.',
    '',
    `Merging creates \`${tag}\` on the merged commit and dispatches **Publish to npm**,`,
    'which tests and builds that commit again before publishing with OIDC.',
    'Closing without merging does not publish anything.',
    '',
    `See [the release guide](https://github.com/${env.GITHUB_REPOSITORY}/blob/main/packages/README.md) for setup and retries.`,
    '',
  ].join('\n'));
}

export function releasePrUrl(repository, branch, body) {
  const { tag } = releaseDetails(branch);
  const query = new URLSearchParams({ quick_pull: '1', title: `Release ${tag}`, body });
  return `https://github.com/${repository}/compare/main...${encodeURIComponent(branch)}?${query}`;
}

export function summarize(env = process.env) {
  const body = readFileSync(`${env.RUNNER_TEMP}/npm-release-body.md`, 'utf8');
  const url = releasePrUrl(env.GITHUB_REPOSITORY, env.RELEASE_BRANCH, body);
  appendFileSync(env.GITHUB_STEP_SUMMARY, [
    `Release branch pushed: \`${env.RELEASE_BRANCH}\`.`,
    '',
    `[Create the release pull request](${url})`,
    '',
    'Click the link, review the prefilled title and description, and click **Create pull request**.',
    'Wait for CI, then review and merge. Merging automatically tags and publishes the release.',
    '',
  ].join('\n'));
}

export function finish(event, repository, run = execute) {
  const pr = event.pull_request;
  assert(event.action === 'closed' && pr?.merged, 'Only merged PRs can release');
  assert.equal(pr.base.ref, 'main');
  assert.equal(pr.base.repo.full_name, repository);
  assert.equal(pr.head.repo.full_name, repository, 'Release PR must belong to this repository');
  const { pkg, version, tag, directory } = releaseDetails(pr.head.ref);
  assert(/^[a-f0-9]{40}$/.test(pr.merge_commit_sha), 'Invalid merge commit');
  assert.equal(run('git', ['rev-parse', 'HEAD']), pr.merge_commit_sha,
    'Checkout must point to the merged release commit');
  validateRelease(readJson(`${directory}/package.json`),
    readJson(`${directory}/package-lock.json`), pkg, version);

  // Read matching refs so an absent tag is distinct from authentication/network errors.
  const refs = JSON.parse(run('gh', ['api',
    `repos/${repository}/git/matching-refs/tags/${tag}`]));
  const existing = refs.find((ref) => ref.ref === `refs/tags/${tag}`);
  if (existing) {
    assert.equal(existing.object.type, 'commit', 'Expected a lightweight release tag');
    assert.equal(existing.object.sha, pr.merge_commit_sha,
      'Existing release tag points to a different commit; it will not be moved');
  } else {
    run('gh', ['api', '--method', 'POST', `repos/${repository}/git/refs`,
      '-f', `ref=refs/tags/${tag}`, '-f', `sha=${pr.merge_commit_sha}`]);
  }
  // GITHUB_TOKEN-created tags do not trigger push workflows. Dispatch explicitly.
  run('gh', ['workflow', 'run', 'publish-npm.yml', '--repo', repository, '--ref', tag]);
  return tag;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv[2] === 'prepare') {
    prepare();
  } else if (process.argv[2] === 'summary') {
    summarize();
  } else if (process.argv[2] === 'finish') {
    const tag = finish(readJson(process.env.GITHUB_EVENT_PATH), process.env.GITHUB_REPOSITORY);
    appendFileSync(process.env.GITHUB_STEP_SUMMARY,
      `Dispatched **Publish to npm** for \`${tag}\`. Check that workflow for the publication result.\n`);
  } else {
    throw new Error('Usage: node scripts/npm-release.mjs prepare|summary|finish');
  }
}
