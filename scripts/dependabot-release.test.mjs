import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import {
  assertDependencyChange, assertSecurityPr, assertVersionChange, checksPassed,
  applySecurityFixes, parsePlan, publicationState, publishRelease, securityHistory, securityPullRequests,
  verifyBump, verifyIsolatedRelease,
} from './dependabot-release.mjs';
import { releaseDetails } from './release.mjs';

const repository = 'owner/repo';
const alert = (number = 1, extra = {}) => ({
  state: 'OPEN', vulnerableManifestPath: 'typescript/pnpm-lock.yaml',
  securityVulnerability: { package: { ecosystem: 'NPM', name: 'undici' } },
  dependabotUpdate: { pullRequest: { number } }, ...extra,
});
const pr = (extra = {}) => ({
  number: 1, user: { login: 'dependabot[bot]' }, draft: false,
  base: { ref: 'main', repo: { full_name: repository } },
  head: { ref: 'dependabot/npm_and_yarn/undici', repo: { full_name: repository } }, ...extra,
});

test('only open/fixed TypeScript npm alerts with an actual PR link qualify', () => {
  const links = securityPullRequests([
    alert(), alert(1, { state: 'FIXED' }), alert(2, { state: 'DISMISSED' }),
    alert(3, { vulnerableManifestPath: 'python/uv.lock' }),
    alert(4, { securityVulnerability: { package: { ecosystem: 'PIP' } } }),
    alert(5, { dependabotUpdate: null }),
  ]);
  assert.deepEqual([...links.keys()], [1]);
  assert.equal(links.get(1).length, 2);
});

test('labels and branch names cannot substitute for security provenance', () => {
  assertSecurityPr(pr(), [alert()], repository);
  assert.throws(() => assertSecurityPr(pr(), undefined, repository), /not linked/);
  assert.throws(() => assertSecurityPr(pr({ user: { login: 'human' } }), [alert()], repository), /Dependabot-authored/);
  assert.throws(() => assertSecurityPr(pr({ head: { repo: { full_name: 'fork/repo' } } }), [alert()], repository), /Fork/);
  assert.throws(() => assertSecurityPr(pr({ draft: true }), [alert()], repository), /Draft/);
  assert.throws(() => assertSecurityPr(pr({ base: { ref: 'other' } }), [alert()], repository));
});

test('dependency PRs cannot change scripts, versions, source, or dependency membership', () => {
  const path = 'typescript/packages/worker/package.json';
  const before = { name: 'worker', version: '0.3.0', dependencies: { undici: '^6.28.1' }, scripts: { test: 'node --test' } };
  const after = { ...before, dependencies: { undici: '^6.29.1' } };
  const check = (next) => assertDependencyChange(path, JSON.stringify(before), JSON.stringify(next));
  check(after);
  assert.throws(() => check({ ...after, scripts: { test: 'echo success' } }), /Non-dependency/);
  assert.throws(() => check({ ...after, version: '0.4.0' }), /Non-dependency/);
  assert.throws(() => check({ ...after, dependencies: { undici: 'https://example.com/package.tgz' } }), /Non-version/);
  assert.throws(() => check({ ...after, dependencies: {} }), /Dependency set/);
  assert.throws(() => assertDependencyChange('typescript/packages/worker/src/index.ts', '', ''), /Not a dependency file/);
  assert.throws(() => assertDependencyChange('.github/workflows/tests.yml', '', ''), /Not a dependency file/);
});

test('missing, stale, pending and failed CI never count as passing', () => {
  const success = { status: 'completed', conclusion: 'success' };
  const pass = [{ bucket: 'pass' }];
  assert(checksPassed([success], pass));
  assert(!checksPassed([], pass));
  assert(!checksPassed([success], []));
  assert(!checksPassed([{ status: 'in_progress' }, success], pass));
  assert(!checksPassed([{ status: 'completed', conclusion: 'failure' }], pass));
  assert(!checksPassed([success], [{ bucket: 'pending' }]));
  assert(!checksPassed([success], [{ bucket: 'fail' }]));
});

test('release version changes cannot hide dependency changes or unrelated docs', () => {
  const path = 'typescript/packages/core/package.json';
  const before = { name: '@temporalio/agent-harness', version: '0.3.0',
    dependencies: { '@temporalio/agent-harness-client': '^0.3.0', undici: '^6.29.1' } };
  const after = { ...before, version: '0.3.1', dependencies: { ...before.dependencies, '@temporalio/agent-harness-client': '^0.3.1' } };
  const check = (next) => assertVersionChange(path, JSON.stringify(before), JSON.stringify(next), '0.3.0', '0.3.1');
  check(after);
  assert.throws(() => check({ ...after, dependencies: { ...after.dependencies, undici: '^7.0.0' } }));
  const lock = "importers:\n  packages/core:\n    dependencies:\n      '@temporalio/agent-harness-client':\n        specifier: ^0.3.0\n        version: link:../client\npackages:\n  undici@6.29.1: {}\n";
  assertVersionChange('typescript/pnpm-lock.yaml', lock, lock.replace('^0.3.0', '^0.3.1'), '0.3.0', '0.3.1');
  assert.throws(() => assertVersionChange('typescript/pnpm-lock.yaml', lock,
    lock.replace('6.29.1', '7.0.0'), '0.3.0', '0.3.1'), /beyond the version bump/);
  assertVersionChange('README.md', '@temporalio/agent-harness@0.3.0 npm-v0.3.0',
    '@temporalio/agent-harness@0.3.1 npm-v0.3.1', '0.3.0', '0.3.1');
  assert.throws(() => assertVersionChange('README.md', 'Old feature', 'New feature', '0.3.0', '0.3.1'));
  assert.throws(() => assertVersionChange('.github/workflows/publish-npm.yml', '', '', '0.3.0', '0.3.1'));
});

test('security integration branches cannot accidentally trigger the normal merge publisher', () => {
  assert.throws(() => releaseDetails('security/npm-v0.3.1'));
});

test('publication only succeeds on a completed successful run', () => {
  assert.equal(publicationState([]), 'absent');
  assert.equal(publicationState([{ status: 'in_progress', conclusion: null }]), 'pending');
  assert.equal(publicationState([{ status: 'completed', conclusion: 'failure' }]), 'failed');
  assert.equal(publicationState([{ status: 'completed', conclusion: 'success' }]), 'success');
  assert.equal(publicationState([{ status: 'completed', conclusion: 'failure' },
    { status: 'completed', conclusion: 'success' }]), 'failed');
});

test('publication tags the isolated head, resumes partial work, and never moves an existing tag', () => {
  const head = 'a'.repeat(40);
  const releasePr = pr({ head: { sha: head }, merge_commit_sha: 'b'.repeat(40) });
  const plan = { tag: 'npm-v0.3.0', fixes: [{ number: 1, sha: 'c'.repeat(40) }] };
  const calls = [];
  let refs = [];
  let releases = [];
  let runs = [];
  const dependencies = {
    request: (path) => path.startsWith('git/') ? refs : { workflow_runs: runs },
    list: () => releases, latest: () => plan.tag,
    publish: (args) => { calls.push(args); }, report: () => {},
  };
  assert.equal(publishRelease(releasePr, plan, dependencies), 'absent');
  assert(calls[0].includes(`sha=${head}`));
  assert(!calls[0].includes(`sha=${releasePr.merge_commit_sha}`));
  assert(calls.some((call) => call[0] === 'workflow' && call.includes('npm-v0.3.1')));
  refs = [{ ref: 'refs/tags/npm-v0.3.1', object: { type: 'commit', sha: head } }];
  releases = [{ tag_name: 'npm-v0.3.1', draft: false }];
  runs = [{ head_branch: 'npm-v0.3.1', status: 'in_progress', conclusion: null }];
  calls.length = 0;
  assert.equal(publishRelease(releasePr, plan, dependencies), 'pending');
  assert.deepEqual(calls, []);
  runs[0] = { ...runs[0], status: 'completed', conclusion: 'failure' };
  assert.equal(publishRelease(releasePr, plan, dependencies), 'failed');
  assert.deepEqual(calls, []);
  runs[0].conclusion = 'success';
  assert.equal(publishRelease(releasePr, plan, dependencies), 'success');
  assert.deepEqual(calls, []);
  refs[0].object.sha = 'd'.repeat(40);
  assert.throws(() => publishRelease(releasePr, plan, dependencies), /never move/);
  assert.deepEqual(calls, []);
});

test('malformed or duplicate source plans are rejected', () => {
  const fix = { number: 1, sha: 'a'.repeat(40) };
  assert.throws(() => parsePlan(JSON.stringify({ tag: 'main', fixes: [fix] })));
  assert.throws(() => parsePlan(JSON.stringify({ tag: 'npm-v0.3.0', fixes: [] })));
  assert.throws(() => parsePlan(JSON.stringify({ tag: 'npm-v0.3.0', fixes: [fix, fix] })));
  assert.throws(() => parsePlan(JSON.stringify({ tag: 'npm-v0.3.0', fixes: [{ ...fix, sha: '--all' }] })));
});

function fixture(fn) {
  const original = process.cwd();
  const root = mkdtempSync(join(tmpdir(), 'dependabot-release-test-'));
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const write = (path, text) => {
    mkdirSync(join(root, path, '..'), { recursive: true });
    writeFileSync(join(root, path), text);
  };
  const commit = (message) => { git('add', '.'); git('commit', '-m', message); return git('rev-parse', 'HEAD'); };
  try {
    git('init', '-b', 'main');
    git('config', 'user.name', 'Test');
    git('config', 'user.email', 'test@example.com');
    write('typescript/pnpm-lock.yaml', 'undici@6.28.1: {}\n');
    write('typescript/packages/core/package.json', JSON.stringify({ name: '@temporalio/agent-harness', version: '0.3.0' }));
    commit('Baseline');
    git('tag', 'npm-v0.3.0');
    process.chdir(root);
    fn({ git, write, commit });
  } finally {
    process.chdir(original);
    rmSync(root, { recursive: true, force: true });
  }
}

test('release selection ignores unrelated work on main and requires actual security provenance', () => fixture(({ write, commit }) => {
  write('typescript/pnpm-lock.yaml', 'undici@6.29.1: {}\n');
  const fix = commit('Security update');
  const securityPr = pr({ merged_at: '2026-11-05', merge_commit_sha: fix });
  const linked = securityPullRequests([alert(1, { state: 'FIXED' })]);
  const lookup = (path) => path.includes(fix) ? [securityPr] : [];
  assert.equal(securityHistory('npm-v0.3.0', fix, linked, lookup, repository).length, 1);
  assert.deepEqual(securityHistory('npm-v0.3.0', fix, new Map(), lookup, repository), []);
  write('feature.txt', 'unreleased feature');
  const unrelated = commit('Feature lands during release CI');
  assert.equal(securityHistory('npm-v0.3.0', unrelated, linked, lookup, repository).length, 1);
}));

test('real git release diff must contain only a patch version bump', () => fixture(({ git, write, commit }) => {
  const base = git('rev-parse', 'HEAD');
  const path = 'typescript/packages/core/package.json';
  const before = readFileSync(path, 'utf8');
  write(path, before.replace('0.3.0', '0.3.1'));
  const head = commit('Patch version');
  verifyBump(base, head, '0.3.0', '0.3.1');
  assert.throws(() => verifyBump(base, head, '0.3.0', '0.4.0'), /Only patch/);
  write('typescript/pnpm-lock.yaml', 'undici@7.0.0: {}\n');
  const bad = commit('Sneak in another update');
  assert.throws(() => verifyBump(base, bad, '0.3.0', '0.3.1'), /beyond the version bump/);
}));

test('isolated release excludes main features, integrates without losing them, and never rereleases the same fix', () => fixture(({ git, write, commit }) => {
  write('feature.txt', 'unreleased feature');
  commit('Feature before security update');
  write('typescript/pnpm-lock.yaml', 'undici@6.29.1: {}\n');
  const source = commit('Security update');
  write('another-feature.txt', 'another unreleased feature');
  const main = commit('Feature after security update');
  const securityPr = pr({ merged_at: '2026-11-05', merge_commit_sha: source });
  const linked = securityPullRequests([alert(1, { state: 'FIXED' })]);
  const plan = { tag: 'npm-v0.3.0', fixes: [{ number: 1, sha: source }] };
  applySecurityFixes(plan);
  assert.throws(() => git('show', 'HEAD:feature.txt'));
  assert.throws(() => git('show', 'HEAD:another-feature.txt'));
  assert.equal(git('show', 'HEAD:typescript/pnpm-lock.yaml'), 'undici@6.29.1: {}');
  write('typescript/packages/core/package.json', JSON.stringify({ name: '@temporalio/agent-harness', version: '0.3.1' }));
  const head = commit(`Release npm-v0.3.1\n\nSecurity-release-plan: ${JSON.stringify(plan)}`);
  assert.deepEqual(verifyIsolatedRelease(head, main, linked, () => securityPr, repository), plan);
  git('tag', 'npm-v0.3.1');
  git('switch', 'main');
  git('merge', '--squash', head);
  const integrated = commit('Integrate published security release');
  assert.equal(readFileSync('feature.txt', 'utf8'), 'unreleased feature');
  assert.equal(readFileSync('another-feature.txt', 'utf8'), 'another unreleased feature');
  const lookup = (path) => path.includes(source) ? [securityPr] : [];
  assert.deepEqual(securityHistory('npm-v0.3.1', integrated, linked, lookup, repository), []);
  write('typescript/pnpm-lock.yaml', 'undici@6.30.0: {}\n');
  const second = commit('Next security fix');
  const secondPr = pr({ number: 2, merged_at: '2026-11-06', merge_commit_sha: second });
  linked.set(2, [alert(2)]);
  const nextLookup = (path) => path.includes(second) ? [secondPr] : lookup(path);
  assert.deepEqual(securityHistory('npm-v0.3.1', second, linked, nextLookup, repository).map((entry) => entry.number), [2]);
}));

test('a release branch cannot smuggle extra commits or change a cherry-picked patch', () => fixture(({ git, write, commit }) => {
  write('typescript/pnpm-lock.yaml', 'undici@6.29.1: {}\n');
  const source = commit('Security update');
  const securityPr = pr({ merged_at: '2026-11-05', merge_commit_sha: source });
  const linked = securityPullRequests([alert()]);
  const plan = { tag: 'npm-v0.3.0', fixes: [{ number: 1, sha: source }] };
  git('checkout', '--detach', 'npm-v0.3.0');
  write('typescript/pnpm-lock.yaml', 'undici@7.0.0: {}\n');
  commit('Not the reviewed patch');
  write('typescript/packages/core/package.json', JSON.stringify({ name: '@temporalio/agent-harness', version: '0.3.1' }));
  const bad = commit(`Release\n\nSecurity-release-plan: ${JSON.stringify(plan)}`);
  assert.throws(() => verifyIsolatedRelease(bad, source, linked, () => securityPr, repository), /differs from reviewed/);
  write('feature.txt', 'extra feature');
  const extra = commit(`Extra commit\n\nSecurity-release-plan: ${JSON.stringify(plan)}`);
  assert.throws(() => verifyIsolatedRelease(extra, source, linked, () => securityPr, repository), /Unexpected commits/);
}));

test('a backport conflict stops rather than copying main into the release', () => fixture(({ git, write, commit }) => {
  write('typescript/pnpm-lock.yaml', 'undici@7.0.0: {}\n');
  commit('Unrelated major update');
  write('typescript/pnpm-lock.yaml', 'undici@7.1.0: {}\n');
  const source = commit('Security update on new major');
  assert.throws(() => applySecurityFixes({ tag: 'npm-v0.3.0', fixes: [{ number: 1, sha: source }] }));
  assert(git('status', '--porcelain').includes('UU typescript/pnpm-lock.yaml'));
}));
