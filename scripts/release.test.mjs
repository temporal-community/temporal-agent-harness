import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import {
  bumpVersion, finish, lockstepPackages, lockstepVersion, npmPlan, prepare, publishablePackages,
  publishOrder, releaseDetails, releasePrUrl, setLockstepVersion, setPythonVersion, summarize,
  tagDetails, updateReadmePins, validateInputs, validateNpmRelease,
} from './release.mjs';

const repository = 'temporal-community/temporal-agent-harness';
const sha = 'a'.repeat(40);
const repoUrl = `git+https://github.com/${repository}.git`;
const publishConfig = { access: 'public', registry: 'https://registry.npmjs.org/' };

const event = (branch = 'release/npm-v0.1.1') => ({ action: 'closed', pull_request: {
  merged: true, merge_commit_sha: sha, body: 'What changed.',
  base: { ref: 'main', repo: { full_name: repository } },
  head: { ref: branch, repo: { full_name: repository } },
} });

const manifest = (name, version, extra = {}) => ({
  name, version, publishConfig, repository: { type: 'git', url: repoUrl }, ...extra,
});

// A repository with the lockstep packages at `version`: the three public packages, the two
// private SDK packages, codegen (another scope, untouched), an app with a workspace: range and an
// example with a caret range.
function fixture(fn, version = '0.1.0') {
  const original = process.cwd();
  const root = mkdtempSync(join(tmpdir(), 'release-test-'));
  const write = (path, value) => {
    mkdirSync(join(root, path, '..'), { recursive: true });
    writeFileSync(join(root, path), typeof value === 'string' ? value : `${JSON.stringify(value, null, 2)}\n`);
  };
  const client = '@temporalio/agent-harness-client';
  write('typescript/packages/client/package.json', manifest(client, version));
  write('typescript/packages/react/package.json',
    manifest('@temporalio/agent-harness-react', version, { dependencies: { [client]: `^${version}` } }));
  write('typescript/packages/svelte/package.json',
    manifest('@temporalio/agent-harness-svelte', version, { dependencies: { [client]: `^${version}` } }));
  write('typescript/packages/core/package.json',
    manifest('@temporalio/agent-harness', version, { private: true, dependencies: { [client]: `^${version}` } }));
  write('typescript/packages/worker/package.json',
    manifest('@temporalio/agent-harness-worker', version, { private: true,
      dependencies: { '@temporalio/agent-harness': `^${version}`, [client]: `^${version}` } }));
  write('typescript/packages/codegen/package.json',
    { name: '@temporal-agent-harness/codegen', version: '0.1.0', private: true });
  write('typescript/apps/ui/package.json', { name: 'ui', private: true, dependencies: { [client]: 'workspace:^' } });
  write('typescript/examples/monty/package.json', { name: 'monty', private: true,
    dependencies: { '@temporalio/agent-harness': `^${version}`, '@temporalio/agent-harness-worker': `^${version}` } });
  write('python/pyproject.toml', `[project]\nname = "temporal-agent-harness"\nversion = "${version}"\n\n[tool.other]\nversion = "9.9.9"\n`);
  write('README.md', [
    `git clone --branch ${version} https://github.com/x/y.git`,
    '`git diff 0.0.9 ' + version + '`.',
    `uv add 'temporal-agent-harness[ui]==${version}'`,
    `pin: temporal-agent-harness==${version}; unrelated 1${version}0 stays`,
  ].join('\n'));
  process.chdir(root);
  try { fn(root); } finally { process.chdir(original); rmSync(root, { recursive: true }); }
}

function mockGitHub({ tags = [], releases = [] } = {}) {
  const calls = [];
  return { calls, run(command, args, options) {
    calls.push([command, ...args, ...(options?.input !== undefined ? [`<stdin:${options.input}>`] : [])]);
    if (command === 'git') return sha;
    if (args[0] === 'api' && args[1].includes('/matching-refs/')) return JSON.stringify(tags);
    if (args[0] === 'release' && args[1] === 'list') return JSON.stringify(releases);
    if (args[0] === 'pr') return '[]';
    return '';
  } };
}

test('bumps stable versions and rejects anything else', () => {
  assert.equal(bumpVersion('0.1.9', 'patch'), '0.1.10');
  assert.equal(bumpVersion('0.1.9', 'minor'), '0.2.0');
  assert.equal(bumpVersion('0.1.9', 'major'), '1.0.0');
  for (const version of ['0.1', '01.1.1', '0.1.1-beta.1', 'v0.1.1']) {
    assert.throws(() => bumpVersion(version, 'patch'));
  }
  assert.throws(() => bumpVersion('0.1.0', 'premajor'));
});

test('release units map to branches, tags and publish workflows', () => {
  for (const [unit, bump] of [['python', 'patch'], ['npm', 'major']]) validateInputs(unit, bump);
  for (const args of [['client', 'patch'], ['../npm', 'patch'], ['npm', '--help']]) {
    assert.throws(() => validateInputs(...args));
  }
  const python = releaseDetails('release/python-v0.9.0');
  assert.equal(python.tag, '0.9.0');
  assert.equal(python.publishWorkflow, 'publish.yml');
  const npm = releaseDetails('release/npm-v0.2.0');
  assert.equal(npm.tag, 'npm-v0.2.0');
  assert.equal(npm.publishWorkflow, 'publish-npm.yml');
  assert.deepEqual(tagDetails('npm-v0.2.0'), npm);
  assert.deepEqual(tagDetails('0.9.0'), python);
  for (const branch of ['main', 'release/npm-client-v0.1.1', 'release/python-v0.9', 'release/npm-v0.2.0-rc.1']) {
    assert.throws(() => releaseDetails(branch));
  }
  for (const tag of ['v0.9.0', 'npm-client-v0.1.1', 'npm-v0.2']) assert.throws(() => tagDetails(tag));
});

test('pyproject version is the project version, not another table\'s', () => fixture(() => {
  const pyproject = readFileSync('python/pyproject.toml', 'utf8');
  const updated = setPythonVersion(pyproject, '0.2.0');
  assert.match(updated, /^version = "0\.2\.0"$/m);
  assert.match(updated, /^version = "9\.9\.9"$/m);
  assert.doesNotMatch(updated, /0\.1\.0/);
}));

test('README pins move to the new version and the diff command advances one release', () => {
  fixture(() => {
    const updated = updateReadmePins(readFileSync('README.md', 'utf8'), '0.1.0', '0.2.0');
    assert.match(updated, /git clone --branch 0\.2\.0 /);
    assert.match(updated, /`git diff 0\.1\.0 0\.2\.0`/);
    assert.match(updated, /\[ui\]==0\.2\.0'/);
    assert.match(updated, /harness==0\.2\.0;/);
    assert.match(updated, /10\.1\.00 stays/);
    assert.throws(() => updateReadmePins(updated, '0.0.1', '0.3.0'), /pins nothing/);
  });
});

// Under Bazel only the script is beside this test, so the repository's own files are checked by
// `node --test scripts/release.test.mjs` (and by `prepare`, which refuses a README without pins).
const repoReadme = new URL('../README.md', import.meta.url);
const repoPyproject = new URL('../python/pyproject.toml', import.meta.url);
test('the real README pins exactly the pyproject version',
  { skip: !existsSync(repoReadme) || !existsSync(repoPyproject) }, () => {
  const readme = readFileSync(repoReadme, 'utf8');
  const version = /^version = "([^"]+)"$/m.exec(readFileSync(repoPyproject, 'utf8'))[1];
  const updated = updateReadmePins(readme, version, '99.0.0');
  assert.match(updated, /temporal-agent-harness\[ui\]==99\.0\.0/);
  assert.match(updated, /--branch 99\.0\.0 /);
  assert.match(updated, new RegExp(`git diff ${version.replaceAll('.', '\\.')} 99\\.0\\.0`));
});

test('lockstep set is every @temporalio/agent-harness package, published in dependency order', () => fixture(() => {
  const packages = lockstepPackages();
  assert.deepEqual(packages.map((pkg) => pkg.name), ['client', 'core', 'react', 'svelte', 'worker']);
  assert.equal(lockstepVersion(packages), '0.1.0');
  assert.deepEqual(publishOrder(packages).map((pkg) => pkg.name), ['client', 'core', 'react', 'svelte', 'worker']);
  assert.deepEqual(publishablePackages(packages).map((pkg) => pkg.name), ['client', 'react', 'svelte']);
  const worker = packages.find((pkg) => pkg.name === 'worker');
  worker.manifest.private = false;
  const core = packages.find((pkg) => pkg.name === 'core');
  core.manifest.private = false;
  assert.deepEqual(publishablePackages(packages).map((pkg) => pkg.name), ['client', 'core', 'react', 'svelte', 'worker']);
}));

test('a version disagreement among lockstep packages is refused', () => fixture(() => {
  const path = 'typescript/packages/react/package.json';
  const react = JSON.parse(readFileSync(path, 'utf8'));
  react.version = '0.1.1';
  writeFileSync(path, JSON.stringify(react));
  assert.throws(() => lockstepVersion(lockstepPackages()), /disagree/);
}));

test('setting the lockstep version rewrites manifests and caret ranges, not workspace: or codegen', () => fixture(() => {
  const written = setLockstepVersion('0.2.0');
  const read = (path) => JSON.parse(readFileSync(path, 'utf8'));
  for (const name of ['client', 'core', 'react', 'svelte', 'worker']) {
    assert.equal(read(`typescript/packages/${name}/package.json`).version, '0.2.0');
  }
  assert.equal(read('typescript/packages/react/package.json').dependencies['@temporalio/agent-harness-client'], '^0.2.0');
  assert.equal(read('typescript/packages/worker/package.json').dependencies['@temporalio/agent-harness'], '^0.2.0');
  assert.equal(read('typescript/examples/monty/package.json').dependencies['@temporalio/agent-harness-worker'], '^0.2.0');
  assert.equal(read('typescript/apps/ui/package.json').dependencies['@temporalio/agent-harness-client'], 'workspace:^');
  assert.equal(read('typescript/packages/codegen/package.json').version, '0.1.0');
  assert(written.some((path) => path.endsWith('examples/monty/package.json')));
  assert(!written.some((path) => path.includes('apps/ui')));
  assert(!written.some((path) => path.includes('codegen')));
  // Files keep npm's two-space layout and trailing newline.
  assert.match(readFileSync('typescript/packages/client/package.json', 'utf8'), /^\{\n  "name"/);
  assert.match(readFileSync('typescript/packages/client/package.json', 'utf8'), /\n$/);
}));

test('npm release validation checks version, metadata and lockstep ranges', () => fixture(() => {
  const ordered = validateNpmRelease('0.1.0', repository);
  assert.deepEqual(ordered.map((pkg) => pkg.name), ['client', 'react', 'svelte']);
  assert.throws(() => validateNpmRelease('0.1.1', repository), /Tag must match/);
  assert.throws(() => validateNpmRelease('0.1.0', 'someone/fork'), /provenance/);
  const path = 'typescript/packages/react/package.json';
  const react = JSON.parse(readFileSync(path, 'utf8'));
  react.dependencies['@temporalio/agent-harness-client'] = '^0.0.9';
  writeFileSync(path, JSON.stringify(react));
  assert.throws(() => validateNpmRelease('0.1.0', repository), /lockstep/);
  react.dependencies['@temporalio/agent-harness-client'] = 'workspace:^';
  writeFileSync(path, JSON.stringify(react));
  assert.throws(() => validateNpmRelease('0.1.0', repository), /registry dependencies only/);
}));

test('prepare python bumps pyproject, locks, repins the README and describes the PR', () => fixture((root) => {
  const env = { RELEASE_UNIT: 'python', RELEASE_BUMP: 'minor', GITHUB_REF: 'refs/heads/main',
    GITHUB_REPOSITORY: repository, GITHUB_OUTPUT: join(root, 'outputs'), RUNNER_TEMP: root };
  const calls = [];
  prepare(env, (command, args, options) => {
    calls.push([command, ...args, options?.cwd]);
    if (command === 'gh') return '[]';
    return '';
  });
  assert.deepEqual(calls[1], ['uv', 'lock', 'python']);
  assert.match(readFileSync('python/pyproject.toml', 'utf8'), /^version = "0\.2\.0"$/m);
  assert.match(readFileSync('README.md', 'utf8'), /\[ui\]==0\.2\.0'/);
  const outputs = readFileSync(env.GITHUB_OUTPUT, 'utf8');
  assert.match(outputs, /^unit=python$/m);
  assert.match(outputs, /^branch=release\/python-v0\.2\.0$/m);
  assert.match(outputs, /^tag=0\.2\.0$/m);
  assert.match(outputs, /^test_targets=\/\/python\/tests:test_packaged_distributions$/m);
  const body = readFileSync(join(root, 'release-body.md'), 'utf8');
  assert.match(body, /0\.1\.0 → \*\*0\.2\.0\*\*/);
  assert.match(body, /publish\.yml/);
}));

test('prepare npm bumps every lockstep package, refreshes the lockfile and lists the publish order', () => fixture((root) => {
  const env = { RELEASE_UNIT: 'npm', RELEASE_BUMP: 'patch', GITHUB_REF: 'refs/heads/main',
    GITHUB_REPOSITORY: repository, GITHUB_OUTPUT: join(root, 'outputs'), RUNNER_TEMP: root };
  const calls = [];
  prepare(env, (command, args, options) => {
    calls.push([command, ...args, options?.cwd]);
    if (command === 'gh') return '[]';
    return '';
  });
  assert.deepEqual(calls[1], ['pnpm', 'install', '--lockfile-only', '--ignore-scripts', 'typescript']);
  assert.equal(lockstepVersion(lockstepPackages()), '0.1.1');
  const outputs = readFileSync(env.GITHUB_OUTPUT, 'utf8');
  assert.match(outputs, /^branch=release\/npm-v0\.1\.1$/m);
  assert.match(outputs, /^tag=npm-v0\.1\.1$/m);
  assert.match(outputs, /^test_targets=\/\/typescript\/packages\/\.\.\.$/m);
  const body = readFileSync(join(root, 'release-body.md'), 'utf8');
  assert.match(body, /1\. `@temporalio\/agent-harness-client`\n1\. `@temporalio\/agent-harness-react`\n1\. `@temporalio\/agent-harness-svelte`/);
  assert.match(body, /`private`, so not published: `@temporalio\/agent-harness`, `@temporalio\/agent-harness-worker`/);
}));

test('prepare refuses to run off main or beside an open release PR, changing nothing', () => fixture(() => {
  assert.throws(() => prepare({ RELEASE_UNIT: 'npm', RELEASE_BUMP: 'patch',
    GITHUB_REF: 'refs/heads/feature', GITHUB_REPOSITORY: repository }, () => assert.fail('External call')), /from main/);
  assert.throws(() => prepare({ RELEASE_UNIT: 'python', RELEASE_BUMP: 'patch',
    GITHUB_REF: 'refs/heads/main', GITHUB_REPOSITORY: repository }, () =>
    JSON.stringify([{ headRefName: 'release/python-v0.1.1', url: 'https://example.test/pr' }])),
  /already exists/);
  assert.match(readFileSync('python/pyproject.toml', 'utf8'), /^version = "0\.1\.0"$/m);
  assert.equal(lockstepVersion(lockstepPackages()), '0.1.0');
}));

test('PR creation link prefills base, branch, title and multiline body', () => {
  const body = 'Release notes\n\nDependency: ^0.2.0 & checks passed.';
  const url = new URL(releasePrUrl(repository, 'release/python-v0.9.0', body));
  assert.equal(decodeURIComponent(url.pathname), `/${repository}/compare/main...release/python-v0.9.0`);
  assert.equal(url.searchParams.get('quick_pull'), '1');
  assert.equal(url.searchParams.get('title'), 'Release 0.9.0');
  assert.equal(url.searchParams.get('body'), body);
  assert.throws(() => releasePrUrl(repository, 'feature', body));
});

test('summary provides a PR creation link without any GitHub API mutations', () => fixture((root) => {
  writeFileSync(join(root, 'release-body.md'), 'Release notes');
  const summary = join(root, 'summary');
  summarize({ GITHUB_REPOSITORY: repository, RELEASE_BRANCH: 'release/npm-v0.1.1',
    RUNNER_TEMP: root, GITHUB_STEP_SUMMARY: summary });
  const text = readFileSync(summary, 'utf8');
  assert.match(text, /Create the release pull request/);
  assert.match(text, /quick_pull=1/);
  assert.match(text, /Merging tags/);
}));

test('finish tags the merged commit, creates a pre-release from the PR body, then dispatches publishing', () => fixture(() => {
  const mock = mockGitHub();
  assert.equal(finish(event(), repository, mock.run), 'npm-v0.1.1');
  const [tagCall, , releaseCall, dispatchCall] = mock.calls.slice(-4);
  assert.deepEqual(tagCall, ['gh', 'api', '--method', 'POST', `repos/${repository}/git/refs`,
    '-f', 'ref=refs/tags/npm-v0.1.1', '-f', `sha=${sha}`]);
  assert.deepEqual(releaseCall, ['gh', 'release', 'create', 'npm-v0.1.1', '--repo', repository,
    '--verify-tag', '--title', 'npm-v0.1.1', '--notes-file', '-', '--prerelease', '<stdin:What changed.>']);
  assert.deepEqual(dispatchCall, ['gh', 'workflow', 'run', 'publish-npm.yml', '--repo', repository, '--ref', 'npm-v0.1.1']);
}, '0.1.1'));

test('finish releases python with a bare tag and publish.yml, and drops --prerelease from 1.0', () => fixture(() => {
  const mock = mockGitHub();
  assert.equal(finish(event('release/python-v1.0.0'), repository, mock.run), '1.0.0');
  const releaseCall = mock.calls.find((call) => call[1] === 'release' && call[2] === 'create');
  assert(!releaseCall.includes('--prerelease'));
  assert.deepEqual(mock.calls.at(-1), ['gh', 'workflow', 'run', 'publish.yml', '--repo', repository, '--ref', '1.0.0']);
}, '1.0.0'));

test('a rerun reuses a matching tag and an existing release, but never moves a conflicting tag', () => fixture(() => {
  const ref = { ref: 'refs/tags/npm-v0.1.1', object: { type: 'commit', sha } };
  const mock = mockGitHub({ tags: [ref], releases: [{ tagName: 'npm-v0.1.1' }] });
  finish(event(), repository, mock.run);
  assert(!mock.calls.some((call) => call.includes('POST')));
  assert(!mock.calls.some((call) => call[1] === 'release' && call[2] === 'create'));
  assert(mock.calls.some((call) => call[1] === 'workflow'));
  const conflict = mockGitHub({ tags: [{ ...ref, object: { type: 'commit', sha: 'b'.repeat(40) } }] });
  assert.throws(() => finish(event(), repository, conflict.run), /will not be moved/);
  assert(!conflict.calls.some((call) => call[1] === 'workflow' || call[1] === 'release'));
}, '0.1.1'));

test('finish rejects unmerged, foreign, misnamed or stale PRs before any external calls', () => fixture(() => {
  const variants = [
    (pr) => { pr.merged = false; }, (pr) => { pr.base.ref = 'develop'; },
    (pr) => { pr.head.repo.full_name = 'someone/fork'; }, (pr) => { pr.head.ref = 'feature'; },
    (pr) => { pr.head.ref = 'release/npm-client-v0.1.1'; },
  ];
  for (const mutate of variants) {
    const input = event(); mutate(input.pull_request);
    assert.throws(() => finish(input, repository, () => assert.fail('External call')));
  }
  // The manifests are at 0.1.1 but the branch says 0.1.2: only the checkout is consulted.
  const mock = mockGitHub();
  assert.throws(() => finish(event('release/npm-v0.1.2'), repository, mock.run), /Tag must match/);
  assert.equal(mock.calls.length, 1);
  assert.throws(() => finish(event('release/python-v0.1.2'), repository, mock.run), /pyproject/);
}, '0.1.1'));

test('npm-plan accepts only a matching npm release tag and prints the publish order', () => fixture((root) => {
  const env = { GITHUB_REF_TYPE: 'tag', GITHUB_REF_NAME: 'npm-v0.1.0', GITHUB_REPOSITORY: repository,
    GITHUB_OUTPUT: join(root, 'outputs') };
  npmPlan(env);
  const outputs = readFileSync(env.GITHUB_OUTPUT, 'utf8');
  assert.match(outputs, /^version=0\.1\.0$/m);
  assert.match(outputs, /^directories=typescript\/packages\/client typescript\/packages\/react typescript\/packages\/svelte$/m);
  assert.throws(() => npmPlan({ ...env, GITHUB_REF_TYPE: 'branch' }), /release tag/);
  assert.throws(() => npmPlan({ ...env, GITHUB_REF_NAME: '0.1.0' }), /npm release tag/);
  assert.throws(() => npmPlan({ ...env, GITHUB_REF_NAME: 'npm-v0.1.1' }), /Tag must match/);
}));
