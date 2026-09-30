import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { finish, prepare, releaseDetails, releasePrUrl, summarize, validateInputs } from './npm-release.mjs';

const repository = 'temporal-community/temporal-agent-harness';
const sha = 'a'.repeat(40);
const event = () => ({ action: 'closed', pull_request: {
  merged: true, merge_commit_sha: sha, user: { login: 'maintainer' },
  base: { ref: 'main', repo: { full_name: repository } },
  head: { ref: 'release/npm-react-v0.1.1', repo: { full_name: repository } },
} });

function fixture(fn, version = '0.1.1') {
  const original = process.cwd();
  const root = mkdtempSync(join(tmpdir(), 'npm-release-test-'));
  mkdirSync(join(root, 'packages/react'), { recursive: true });
  writeFileSync(join(root, 'packages/react/package.json'), JSON.stringify({
    name: '@temporalio/agent-harness-react', version,
    dependencies: { '@temporalio/agent-harness-client': '^0.1.0' },
  }));
  writeFileSync(join(root, 'packages/react/package-lock.json'), JSON.stringify({
    version, packages: { '': { version } },
  }));
  process.chdir(root);
  try { fn(root); } finally { process.chdir(original); rmSync(root, { recursive: true }); }
}

function mockGitHub(existing = []) {
  const calls = [];
  return { calls, run(command, args) {
    calls.push([command, ...args]);
    if (command === 'git') return sha;
    if (args[0] === 'api' && args[1].includes('/matching-refs/')) return JSON.stringify(existing);
    return '';
  } };
}

test('accepts supported packages and increments with an optional exact client version', () => {
  for (const pkg of ['client', 'react', 'svelte']) {
    for (const bump of ['patch', 'minor', 'major']) validateInputs(pkg, bump);
    assert.equal(releaseDetails(`release/npm-${pkg}-v1.2.3`).tag, `npm-${pkg}-v1.2.3`);
  }
  validateInputs('svelte', 'minor', '0.2.0');
});

test('rejects unsafe or unsupported release input', () => {
  for (const args of [['../client', 'patch'], ['client', '--help'],
    ['client', 'patch', '0.1.1'], ['react', 'patch', '^0.1.1'],
    ['react', 'patch', '0.1.1-beta.1'], ['react', 'patch', '01.1.1']]) {
    assert.throws(() => validateInputs(...args));
  }
  for (const branch of ['main', 'release/npm-codegen-v1.0.0', 'release/npm-client-v0.1.1-beta.1']) {
    assert.throws(() => releaseDetails(branch));
  }
});

test('tags a human-authored release PR at its merged commit before dispatching publishing', () => fixture(() => {
  const mock = mockGitHub();
  assert.equal(finish(event(), repository, mock.run), 'npm-react-v0.1.1');
  assert.deepEqual(mock.calls.at(-2), ['gh', 'api', '--method', 'POST',
    `repos/${repository}/git/refs`, '-f', 'ref=refs/tags/npm-react-v0.1.1', '-f', `sha=${sha}`]);
  assert.deepEqual(mock.calls.at(-1), ['gh', 'workflow', 'run', 'publish-npm.yml',
    '--repo', repository, '--ref', 'npm-react-v0.1.1']);
}));

test('retry reuses a matching tag, but never moves a conflicting tag', () => fixture(() => {
  const ref = { ref: 'refs/tags/npm-react-v0.1.1', object: { type: 'commit', sha } };
  const mock = mockGitHub([ref]);
  finish(event(), repository, mock.run);
  assert(!mock.calls.some((call) => call.includes('POST')));
  const conflict = mockGitHub([{ ...ref, object: { type: 'commit', sha: 'b'.repeat(40) } }]);
  assert.throws(() => finish(event(), repository, conflict.run), /will not be moved/);
  assert(!conflict.calls.some((call) => call.includes('workflow')));
}));

test('rejects unmerged, foreign, or incorrectly named PRs before any external calls', () => {
  const variants = [
    (pr) => { pr.merged = false; }, (pr) => { pr.base.ref = 'develop'; },
    (pr) => { pr.head.repo.full_name = 'someone/fork'; },
    (pr) => { pr.head.ref = 'feature'; },
  ];
  for (const mutate of variants) {
    const input = event(); mutate(input.pull_request);
    assert.throws(() => finish(input, repository, () => assert.fail('External call')));
  }
});

test('PR creation link prefills the correct base, branch, title, and multiline body', () => {
  const body = 'Release notes\n\nDependency: ^0.2.0 & checks passed.';
  const url = new URL(releasePrUrl(repository, 'release/npm-react-v0.1.1', body));
  assert.equal(decodeURIComponent(url.pathname), `/${repository}/compare/main...release/npm-react-v0.1.1`);
  assert.equal(url.searchParams.get('quick_pull'), '1');
  assert.equal(url.searchParams.get('title'), 'Release npm-react-v0.1.1');
  assert.equal(url.searchParams.get('body'), body);
  assert.throws(() => releasePrUrl(repository, 'feature', body));
});

test('summary provides a PR creation link without any GitHub API mutations', () => fixture((root) => {
  writeFileSync(join(root, 'npm-release-body.md'), 'Release notes');
  const summary = join(root, 'summary');
  summarize({ GITHUB_REPOSITORY: repository, RELEASE_BRANCH: 'release/npm-react-v0.1.1',
    RUNNER_TEMP: root, GITHUB_STEP_SUMMARY: summary });
  const text = readFileSync(summary, 'utf8');
  assert.match(text, /Create the release pull request/);
  assert.match(text, /quick_pull=1/);
  assert.match(text, /click \*\*Create pull request\*\*/);
}));

test('rejects a stale release PR whose version no longer matches the merged manifest', () => fixture(() => {
  const input = event(); input.pull_request.head.ref = 'release/npm-react-v0.1.2';
  const mock = mockGitHub();
  assert.throws(() => finish(input, repository, mock.run), /must match/);
  assert.equal(mock.calls.length, 1);
}));

test('does not create a tag when GitHub ref lookup fails', () => fixture(() => {
  const calls = [];
  assert.throws(() => finish(event(), repository, (command, args) => {
    calls.push([command, ...args]);
    if (command === 'git') return sha;
    throw new Error('Network unavailable');
  }), /Network unavailable/);
  assert.equal(calls.length, 2);
}));

test('prepares version and lockfile using npm and retains the existing dependency by default', () => fixture((root) => {
  const env = { RELEASE_PACKAGE: 'react', RELEASE_BUMP: 'patch', GITHUB_REF: 'refs/heads/main',
    GITHUB_REPOSITORY: repository, GITHUB_OUTPUT: join(root, 'outputs'), RUNNER_TEMP: root };
  const calls = [];
  prepare(env, (command, args, options) => {
    calls.push([command, ...args]);
    if (command === 'gh') return '[]';
    // Exercise npm's actual version and lockfile update, without network access.
    assert.equal(options.cwd, 'packages/react');
    assert.deepEqual(args, ['version', 'patch', '--no-git-tag-version', '--ignore-scripts']);
    return execFileSync(command, args, { ...options, encoding: 'utf8' });
  });
  assert.equal(calls.length, 2);
  assert.match(readFileSync(env.GITHUB_OUTPUT, 'utf8'), /branch=release\/npm-react-v0.1.1/);
  assert.match(readFileSync(join(root, 'npm-release-body.md'), 'utf8'), /\^0.1.0/);
}, '0.1.0'));

test('rejects an existing open release PR without changing versions', () => fixture((root) => {
  assert.throws(() => prepare({ RELEASE_PACKAGE: 'react', RELEASE_BUMP: 'patch',
    GITHUB_REF: 'refs/heads/main', GITHUB_REPOSITORY: repository }, () =>
    JSON.stringify([{ headRefName: 'release/npm-react-v0.1.1', url: 'https://example.test/pr' }])),
  /already exists/);
  assert.equal(JSON.parse(readFileSync('packages/react/package.json')).version, '0.1.0');
}, '0.1.0'));

test('unavailable client version fails before dependency or version updates', () => fixture(() => {
  const calls = [];
  assert.throws(() => prepare({ RELEASE_PACKAGE: 'react', RELEASE_BUMP: 'patch',
    CLIENT_VERSION: '0.2.0', GITHUB_REF: 'refs/heads/main', GITHUB_REPOSITORY: repository },
  (command, args) => {
    calls.push([command, ...args]);
    if (command === 'gh') return '[]';
    throw new Error('404: client not available');
  }), /not available/);
  assert.equal(calls.length, 2);
  assert.equal(calls[1][1], 'view');
}));

test('checks client availability and updates the dependency before bumping a binding', () => fixture((root) => {
  const calls = [];
  prepare({ RELEASE_PACKAGE: 'react', RELEASE_BUMP: 'patch', CLIENT_VERSION: '0.2.0',
    GITHUB_REF: 'refs/heads/main', GITHUB_REPOSITORY: repository,
    GITHUB_OUTPUT: join(root, 'outputs'), RUNNER_TEMP: root }, (command, args, options) => {
    calls.push([command, ...args]);
    if (command === 'gh') return '[]';
    if (args[0] === 'view') return '"0.2.0"';
    if (args[0] === 'install') {
      assert.deepEqual(args, ['install', '@temporalio/agent-harness-client@^0.2.0',
        '--package-lock-only', '--ignore-scripts']);
      const manifest = JSON.parse(readFileSync('packages/react/package.json'));
      manifest.dependencies['@temporalio/agent-harness-client'] = '^0.2.0';
      writeFileSync('packages/react/package.json', JSON.stringify(manifest));
      return '';
    }
    return execFileSync(command, args, { ...options, encoding: 'utf8' });
  });
  assert.deepEqual(calls.map((call) => call[1]), ['pr', 'view', 'install', 'version']);
  assert.match(readFileSync(join(root, 'npm-release-body.md'), 'utf8'), /\^0.2.0/);
}, '0.1.0'));
