// Hourly security patches: select actual Dependabot fixes, cherry-pick them onto
// the last npm tag, publish that isolated commit, then integrate its PR into main.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  bumpVersion, lockstepPackages, lockstepVersion, setLockstepVersion, updateDevelopmentPins,
} from './release.mjs';

const branchPrefix = 'security/npm-v';
const planPrefix = 'Security-release-plan: ';
const dependencyFields = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'];
const manifestPath = /^typescript\/(?:(?:packages|apps|examples)\/[^/]+\/)?package\.json$/;
const hashPath = /^\.aspect\/rules\/external_repository_action_cache\/npm_translate_lock_[\w=]+$/;
const shaPattern = /^[a-f0-9]{40}$/;
const run = (command, args, options = {}) => (execFileSync(command, args, {
  encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, ...options,
}) ?? '').trim();
const git = (...args) => run('git', args);
const repo = () => process.env.GITHUB_REPOSITORY;
const api = (path, ...args) => JSON.parse(run('gh', ['api', `repos/${repo()}/${path}`, ...args]));
const pages = (path) => JSON.parse(run('gh', ['api', '--paginate', '--slurp', `repos/${repo()}/${path}`])).flat();
const note = (message) => {
  console.log(message);
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${message}\n\n`);
};
const output = (name, value) => appendFileSync(process.env.GITHUB_OUTPUT, `${name}=${value}\n`);

export function securityPullRequests(alerts) {
  const result = new Map();
  for (const alert of alerts) {
    const number = alert.dependabotUpdate?.pullRequest?.number;
    if (!number || !['OPEN', 'FIXED'].includes(alert.state)
      || alert.securityVulnerability?.package?.ecosystem !== 'NPM'
      || !alert.vulnerableManifestPath.startsWith('typescript/')) continue;
    const linked = result.get(number) ?? [];
    linked.push(alert);
    result.set(number, linked);
  }
  return result;
}

function alertsByPr() {
  const [owner, name] = repo().split('/');
  // Keep fixed alerts: after merging, the same relation proves release provenance.
  const query = `query($owner:String!, $name:String!, $endCursor:String) {
    repository(owner:$owner, name:$name) {
      vulnerabilityAlerts(first:100, after:$endCursor) {
        nodes { number state vulnerableManifestPath
          securityAdvisory { ghsaId summary }
          securityVulnerability { package { ecosystem name } }
          dependabotUpdate { pullRequest { number } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }`;
  const results = JSON.parse(run('gh', ['api', 'graphql', '--paginate', '--slurp',
    '-f', `query=${query}`, '-f', `owner=${owner}`, '-f', `name=${name}`]));
  return securityPullRequests(results.flatMap((page) => {
    assert(!page.errors, JSON.stringify(page.errors));
    return page.data.repository.vulnerabilityAlerts.nodes;
  }));
}

export function assertSecurityPr(pr, linked, repository) {
  assert(linked?.length, `#${pr.number} is not linked to a TypeScript Dependabot security alert`);
  assert.equal(pr.user.login, 'dependabot[bot]', 'Only Dependabot-authored PRs are eligible');
  assert.equal(pr.base.ref, 'main');
  assert.equal(pr.base.repo.full_name, repository);
  assert.equal(pr.head.repo?.full_name, repository, 'Fork PRs are not eligible');
  assert(!pr.draft, 'Draft PRs are not eligible');
}

export function assertDependencyChange(path, before, after) {
  if (path === 'typescript/pnpm-lock.yaml') return;
  assert(manifestPath.test(path), `Not a dependency file: ${path}`);
  const old = JSON.parse(before);
  const next = JSON.parse(after);
  for (const field of dependencyFields) {
    // Security updates must not add or remove dependencies or change their source.
    assert.deepEqual(Object.keys(old[field] ?? {}).sort(), Object.keys(next[field] ?? {}).sort(),
      `Dependency set changed in ${path}`);
    for (const [name, range] of Object.entries(next[field] ?? {})) {
      if (range !== old[field][name]) {
        assert(/^[~^]?\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(range),
          `Non-version dependency range in ${path}: ${name}`);
      }
    }
    delete old[field];
    delete next[field];
  }
  assert.deepEqual(next, old, `Non-dependency manifest change in ${path}`);
}

function changedFiles(base, head) {
  assert(shaPattern.test(base) && shaPattern.test(head), 'Expected commit IDs');
  return git('diff', '--name-status', '--no-renames', base, head).split('\n').filter(Boolean).map((line) => {
    const [status, path] = line.split('\t');
    assert.equal(status, 'M', `Only modifications to existing files are allowed: ${line}`);
    return path;
  });
}

function checkDependencyDiff(base, head) {
  const files = changedFiles(base, head);
  assert(files.length > 0, 'Empty dependency update');
  for (const path of files) {
    assertDependencyChange(path, git('show', `${base}:${path}`), git('show', `${head}:${path}`));
  }
}

export function checksPassed(runs, checks) {
  // The named workflow must actually have run, so missing or wholly skipped CI
  // cannot be mistaken for success. The API returns newest runs first.
  return runs.length > 0 && runs[0].status === 'completed' && runs[0].conclusion === 'success'
    && checks.length > 0 && checks.every((check) => ['pass', 'skipping'].includes(check.bucket));
}

function ready(pr) {
  if (pr.state !== 'open' || pr.mergeable !== true || pr.mergeable_state !== 'clean') return false;
  const runs = api(`actions/workflows/tests.yml/runs?head_sha=${pr.head.sha}&event=pull_request&per_page=100`).workflow_runs;
  let checks;
  try {
    checks = JSON.parse(run('gh', ['pr', 'checks', String(pr.number), '--repo', repo(), '--json', 'name,bucket']));
  } catch {
    return false; // failed, pending or unavailable checks; retry on the next hour
  }
  return checksPassed(runs, checks);
}

function merge(pr) {
  // Do not bypass rules or approve on a maintainer's behalf. The SHA guard also
  // rejects a Dependabot rebase that happened after we inspected the checks.
  run('gh', ['pr', 'merge', String(pr.number), '--repo', repo(), '--squash',
    '--match-head-commit', pr.head.sha]);
  note(`Merged #${pr.number}: ${pr.html_url}`);
}

function refreshMain() {
  git('fetch', 'origin', 'main', '--tags');
  return git('rev-parse', 'origin/main');
}

function latestRelease(excludeTag) {
  const releases = pages('releases?per_page=100').filter((release) => !release.draft
    && /^npm-v\d+\.\d+\.\d+$/.test(release.tag_name) && release.tag_name !== excludeTag);
  releases.sort((a, b) => {
    const av = a.tag_name.slice(5).split('.').map(Number);
    const bv = b.tag_name.slice(5).split('.').map(Number);
    return bv[0] - av[0] || bv[1] - av[1] || bv[2] - av[2];
  });
  assert(releases.length, 'Create an initial npm release before enabling automation');
  return releases[0].tag_name;
}

function assertPublished(tag) {
  const sha = git('rev-parse', `${tag}^{commit}`);
  const runs = api(`actions/workflows/publish-npm.yml/runs?head_sha=${sha}&per_page=100`).workflow_runs;
  assert(runs.some((entry) => entry.head_branch === tag && entry.conclusion === 'success'),
    `${tag} has no successful npm publish run; recover that release first`);
}

export function securityHistory(tag, head, linked, readPrs = pages, repository = repo()) {
  const base = git('rev-parse', `${tag}^{commit}`);
  // A previous automatic tag is on a maintenance branch, not necessarily an
  // ancestor of main (the integration PR is squash-merged). Remember original
  // source commits recorded in every maintenance release in the tag's ancestry.
  const delivered = new Set();
  for (const line of git('log', '--format=%B', base).split('\n')) {
    if (line.startsWith(planPrefix)) {
      for (const fix of parsePlan(line.slice(planPrefix.length)).fixes) delivered.add(fix.sha);
    }
  }
  const commits = git('rev-list', '--first-parent', '--reverse', `${base}..${head}`).split('\n').filter(Boolean);
  const prs = [];
  for (const sha of commits) {
    if (delivered.has(sha)) continue;
    const associated = readPrs(`commits/${sha}/pulls?per_page=100`);
    const pr = associated.find((entry) => entry.merged_at && entry.merge_commit_sha === sha
      && entry.base.ref === 'main' && linked.has(entry.number));
    if (!pr) continue; // Unrelated main commits never enter the maintenance branch.
    assertSecurityPr(pr, linked.get(pr.number), repository);
    const parents = git('show', '-s', '--format=%P', sha).split(' ');
    assert.equal(parents.length, 1, `Non-squash security commit ${sha}; review manually`);
    checkDependencyDiff(parents[0], sha);
    prs.push(pr);
  }
  return prs;
}

// Verify the release commit itself, including generated lock/cache files. A
// passing release PR must not introduce a second, unreviewed dependency update.
export function assertVersionChange(path, before, after, previous, next) {
  const escaped = previous.replaceAll('.', '\\.');
  if (manifestPath.test(path)) {
    const expected = JSON.parse(before);
    if (/^@temporalio\/agent-harness(?:-|$)/.test(expected.name)) expected.version = next;
    for (const field of ['dependencies', 'peerDependencies', 'devDependencies']) {
      for (const [name, range] of Object.entries(expected[field] ?? {})) {
        if (/^@temporalio\/agent-harness(?:-|$)/.test(name) && range === `^${previous}`) expected[field][name] = `^${next}`;
      }
    }
    assert.deepEqual(JSON.parse(after), expected, `Unexpected release manifest change: ${path}`);
  } else if (path === 'typescript/pnpm-lock.yaml') {
    const expected = before.replace(new RegExp(`('@temporalio/agent-harness(?:-[a-z-]+)?':\\n\\s+specifier: )\\^${escaped}(?=\\n)`, 'g'), `$1^${next}`);
    assert.equal(after, expected, 'Release lockfile changed dependencies beyond the version bump');
  } else if (hashPath.test(path)) {
    const stripHashes = (text) => text.replace(/=-?\d+$/gm, '=HASH');
    assert.equal(stripHashes(after), stripHashes(before), 'Unexpected generated cache change');
  } else {
    assert(/\.md$|\.tmpl$/.test(path), `Unexpected release file: ${path}`);
    const expected = before.replace(new RegExp(`(@temporalio/agent-harness(?:-[a-z-]+)?@)${escaped}(?![\\d.])`, 'g'), `$1${next}`)
      .replaceAll(`npm-v${previous}`, `npm-v${next}`);
    assert.equal(after, expected, `Non-version documentation change: ${path}`);
  }
}

export function verifyBump(base, head, previous, next) {
  assert.equal(next, bumpVersion(previous, 'patch'), 'Only patch releases are automatic');
  const files = changedFiles(base, head);
  assert(files.some((path) => path.startsWith('typescript/packages/') && manifestPath.test(path)), 'Missing version bump');
  for (const path of files) assertVersionChange(path, git('show', `${base}:${path}`), git('show', `${head}:${path}`), previous, next);
}

export function parsePlan(text) {
  const plan = JSON.parse(text);
  assert(/^npm-v\d+\.\d+\.\d+$/.test(plan.tag), 'Invalid base tag');
  assert(Array.isArray(plan.fixes) && plan.fixes.length > 0, 'No security fixes');
  const seen = new Set();
  for (const fix of plan.fixes) {
    assert(Number.isSafeInteger(fix.number) && fix.number > 0 && shaPattern.test(fix.sha), 'Invalid source PR');
    assert(!seen.has(fix.sha), 'Duplicate source commit');
    seen.add(fix.sha);
  }
  return plan;
}

function readPlan(head) {
  const lines = git('show', '-s', '--format=%B', head).split('\n').filter((line) => line.startsWith(planPrefix));
  assert.equal(lines.length, 1, 'Release commit must have exactly one provenance plan');
  return parsePlan(lines[0].slice(planPrefix.length));
}

function patchId(sha) {
  const patch = git('show', '--format=', '--binary', sha);
  const result = run('git', ['patch-id', '--stable'], { input: patch });
  assert(result, 'Empty security patch');
  return result.split(' ')[0];
}

// Prove the entire release tree is precisely: previous tag, selected security
// patches, then a version-only commit. Never infer scope from the PR description.
export function verifyIsolatedRelease(head, main, linked, readPr = (number) => api(`pulls/${number}`), repository = repo()) {
  const plan = readPlan(head);
  const base = git('rev-parse', `${plan.tag}^{commit}`);
  git('merge-base', '--is-ancestor', base, head);
  const commits = git('rev-list', '--reverse', `${base}..${head}`).split('\n').filter(Boolean);
  assert.equal(commits.length, plan.fixes.length + 1, 'Unexpected commits in security release');
  for (let i = 0; i < plan.fixes.length; i++) {
    const fix = plan.fixes[i];
    git('merge-base', '--is-ancestor', fix.sha, main);
    const pr = readPr(fix.number);
    assertSecurityPr(pr, linked.get(fix.number), repository);
    assert(pr.merged_at && pr.merge_commit_sha === fix.sha, 'Source PR does not match merged commit');
    const sourceParents = git('show', '-s', '--format=%P', fix.sha).split(' ');
    assert.equal(sourceParents.length, 1, 'Source fix must be squash-merged');
    checkDependencyDiff(sourceParents[0], fix.sha);
    assert.equal(git('show', '-s', '--format=%P', commits[i]), i ? commits[i - 1] : base,
      'Release must be a linear chain of cherry-picks');
    assert.equal(patchId(commits[i]), patchId(fix.sha), 'Cherry-pick differs from reviewed security fix');
  }
  const bumpBase = commits.at(-2);
  assert.equal(git('show', '-s', '--format=%P', head), bumpBase);
  verifyBump(bumpBase, head, plan.tag.slice(5), bumpVersion(plan.tag.slice(5), 'patch'));
  return plan;
}

export function applySecurityFixes(plan) {
  git('checkout', '--detach', plan.tag);
  for (const fix of plan.fixes) {
    // Conflicts stop the job. Never resolve a backport by copying main's files,
    // which could bring unrelated dependency updates into the release.
    git('cherry-pick', '-x', fix.sha);
  }
}

const versionAt = (sha) => JSON.parse(git('show', `${sha}:typescript/packages/core/package.json`)).version;

// Use GITHUB_TOKEN only for tags/releases and the explicit publish dispatch.
// The PAT is for PR operations, so those continue to trigger ordinary PR CI.
function publishCommand(args, options = {}) {
  assert(process.env.PUBLISH_TOKEN, 'PUBLISH_TOKEN is required for publication');
  return run('gh', args, { ...options, env: { ...process.env, GH_TOKEN: process.env.PUBLISH_TOKEN } });
}

export function publicationState(runs) {
  if (!runs.length) return 'absent';
  if (runs.some((entry) => entry.status !== 'completed')) return 'pending';
  return runs[0].conclusion === 'success' ? 'success' : 'failed';
}

export function publishRelease(pr, plan, {
  request = api, list = pages, latest = latestRelease, publish = publishCommand, report = note,
} = {}) {
  const version = bumpVersion(plan.tag.slice(5), 'patch');
  const tag = `npm-v${version}`;
  const refs = request(`git/matching-refs/tags/${tag}`);
  const existing = refs.find((ref) => ref.ref === `refs/tags/${tag}`);
  if (existing) {
    assert.equal(existing.object.type, 'commit');
    assert.equal(existing.object.sha, pr.head.sha, 'Published tag must never move');
  } else {
    assert.equal(latest(), plan.tag, 'A newer npm release exists; recreate this backport');
    publish(['api', '--method', 'POST', `repos/${repo()}/git/refs`,
      '-f', `ref=refs/tags/${tag}`, '-f', `sha=${pr.head.sha}`]);
  }
  const releases = list('releases?per_page=100');
  const release = releases.find((entry) => entry.tag_name === tag);
  if (!release) {
    const notes = [`Security patch based on ${plan.tag}. Only these Dependabot fixes and package version metadata are included:`, '',
      ...plan.fixes.map((fix) => `- #${fix.number} (${fix.sha})`), '',
      `Integration into main: ${pr.html_url}`, '',
    ].join('\n');
    const args = ['release', 'create', tag, '--repo', repo(), '--verify-tag', '--title', tag, '--notes-file', '-'];
    if (version.startsWith('0.')) args.push('--prerelease');
    publish(args, { input: notes });
  } else assert(!release.draft, 'Existing release is still a draft');
  const runs = request(`actions/workflows/publish-npm.yml/runs?head_sha=${pr.head.sha}&per_page=100`)
    .workflow_runs.filter((entry) => entry.head_branch === tag);
  const state = publicationState(runs);
  if (state === 'absent') {
    publish(['workflow', 'run', 'publish-npm.yml', '--repo', repo(), '--ref', tag]);
    report(`Tagged the isolated release ${tag} and dispatched npm publication. Main integration waits for publication to succeed.`);
  } else if (state === 'failed') {
    report(`${tag} publication failed. Rerun Publish to npm against that tag; the integration PR remains open.`);
  } else if (state === 'pending') report(`${tag} publication is still running.`);
  return state;
}

function reconcileRelease(pr, linked) {
  assert.equal(pr.base.ref, 'main');
  assert.equal(pr.head.repo?.full_name, repo());
  const main = refreshMain();
  git('fetch', 'origin', pr.head.sha);
  const plan = verifyIsolatedRelease(pr.head.sha, main, linked);
  const version = bumpVersion(plan.tag.slice(5), 'patch');
  assert.equal(pr.head.ref, `${branchPrefix}${version}`);
  assertPublished(plan.tag);
  // Do not overwrite a newer normal release's versions during integration.
  assert.equal(versionAt(main), plan.tag.slice(5), 'Main version advanced; integrate this security release manually');
  if (!ready(pr)) {
    note(`Security release #${pr.number} is waiting for CI, reviews or conflict resolution.`);
    return;
  }
  if (publishRelease(pr, plan) === 'success') merge(pr);
}

export function reconcile() {
  assert.equal(process.env.GITHUB_REF, 'refs/heads/main');
  const linked = alertsByPr(); // Authentication failures must fail, not look like zero alerts.
  const open = pages('pulls?state=open&base=main&per_page=100');
  const releases = open.filter((pr) => pr.head.ref.startsWith('release/npm-') || pr.head.ref.startsWith(branchPrefix));
  if (releases.length) {
    assert.equal(releases.length, 1, 'Multiple npm release PRs need manual review');
    const pr = api(`pulls/${releases[0].number}`);
    if (!pr.head.ref.startsWith(branchPrefix)) {
      note(`Manual npm release #${pr.number} is open; leaving it to its owner.`);
      return;
    }
    reconcileRelease(pr, linked);
    return;
  }

  // Merge at most one update per run. Dependabot manages its own rebases.
  for (const entry of open.filter((pr) => linked.has(pr.number))) {
    const pr = api(`pulls/${entry.number}`);
    try {
      assertSecurityPr(pr, linked.get(pr.number), repo());
      git('fetch', 'origin', pr.head.sha, pr.base.sha);
      const base = git('merge-base', pr.base.sha, pr.head.sha);
      checkDependencyDiff(base, pr.head.sha);
    } catch (error) {
      note(`Skipped #${pr.number}: ${error.message}`);
      continue;
    }
    if (!ready(pr)) {
      note(`Security update #${pr.number} is waiting for passing CI and repository merge rules.`);
      continue;
    }
    merge(pr);
    break;
  }

  const main = refreshMain();
  const tag = latestRelease();
  assertPublished(tag);
  const fixes = securityHistory(tag, main, linked);
  if (!fixes.length) {
    note('No unreleased TypeScript security fixes.');
    return;
  }
  assert.equal(versionAt(main), tag.slice(5), 'Main already has an unreleased version change');
  writeFileSync(`${process.env.RUNNER_TEMP}/security-release.json`, JSON.stringify({ main, tag,
    fixes: fixes.map((pr) => ({ number: pr.number, sha: pr.merge_commit_sha, url: pr.html_url,
      advisories: linked.get(pr.number).map((alert) => alert.securityAdvisory.ghsaId) })),
  }));
  output('prepare', 'true');
}

export function prepareSecurityRelease() {
  const plan = JSON.parse(readFileSync(`${process.env.RUNNER_TEMP}/security-release.json`, 'utf8'));
  parsePlan(JSON.stringify(plan));
  const previous = plan.tag.slice(5);
  const version = bumpVersion(previous, 'patch');
  const branch = `${branchPrefix}${version}`;
  const existing = api(`git/matching-refs/heads/${branch}`);
  assert(!existing.some((ref) => ref.ref === `refs/heads/${branch}`),
    `Branch ${branch} already exists; recover its PR or remove the unused branch manually`);
  applySecurityFixes(plan);
  assert.equal(lockstepVersion(lockstepPackages()), previous);
  const bumpBase = git('rev-parse', 'HEAD');
  setLockstepVersion(version);
  updateDevelopmentPins('npm', previous, version);
  run('bazel', ['run', '//typescript:pnpm', '--', '--dir', `${process.cwd()}/typescript`,
    'install', '--lockfile-only', '--ignore-scripts'], { stdio: 'inherit' });
  run('bazel', ['test', '//typescript/...', '--config=ci'], { stdio: 'inherit' });
  git('switch', '-c', branch);
  // Bazel may refresh platform-specific MODULE lock metadata on Linux. It is
  // build bookkeeping, not part of the patch; retain the released copy in git.
  const files = git('diff', '--name-only').split('\n').filter((path) => path && path !== 'MODULE.bazel.lock');
  git('add', '--', ...files);
  git('commit', '-m', `Release npm-v${version}: Dependabot security fixes`, '-m', `${planPrefix}${JSON.stringify(plan)}`);
  verifyBump(bumpBase, git('rev-parse', 'HEAD'), previous, version);
  verifyIsolatedRelease(git('rev-parse', 'HEAD'), refreshMain(), alertsByPr());
  assert.equal(latestRelease(), plan.tag, 'A newer npm release appeared during preparation');
  git('push', 'origin', `HEAD:refs/heads/${branch}`);
  const body = [
    `Patch release from ${plan.tag}; only the following Dependabot security fixes and version metadata are included.`, '',
    ...plan.fixes.map((pr) => `- #${pr.number}: ${pr.advisories.join(', ')} (${pr.url})`), '',
    '`bazel test //typescript/...` passed before this PR was opened. The hourly workflow waits for PR CI and repository merge rules.', '',
    'The hourly workflow publishes the isolated branch head, waits for npm publication to succeed, then merges this PR into main. Do not merge it before publication.', '',
  ].join('\n');
  const bodyPath = `${process.env.RUNNER_TEMP}/security-release-body.md`;
  writeFileSync(bodyPath, body);
  const url = run('gh', ['pr', 'create', '--repo', repo(), '--base', 'main', '--head', branch,
    '--title', `Release npm-v${version}: security fixes`, '--body-file', bodyPath]);
  note(`Prepared security patch release: ${url}`);
}

// Read-only preflight using already fetched refs. Useful before enabling a token
// or when diagnosing a scope blocker; it cannot merge, push, tag or publish.
export function inspectSecurityRelease() {
  const linked = alertsByPr();
  const tag = latestRelease();
  assertPublished(tag);
  const head = git('rev-parse', 'origin/main');
  const fixes = securityHistory(tag, head, linked);
  note(`${tag} -> ${head}: ${fixes.length} unreleased security PR(s): ${fixes.map((pr) => `#${pr.number}`).join(', ') || 'none'}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const command = process.argv[2];
  if (command === 'reconcile') reconcile();
  else if (command === 'prepare') prepareSecurityRelease();
  else if (command === 'inspect') inspectSecurityRelease();
  else throw new Error('Usage: node scripts/dependabot-release.mjs reconcile|prepare|inspect');
}
