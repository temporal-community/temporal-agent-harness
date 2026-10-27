// The release automation: one flow for the Python package and the npm packages.
//
//   prepare-release.yml  node scripts/release.mjs prepare   bumps versions on main, pushes a
//                                                           release/<unit>-v<version> branch
//                        node scripts/release.mjs summary   writes the PR-creation link
//   finish-release.yml   node scripts/release.mjs finish    on merge: tags the merged commit,
//                                                           creates the GitHub Release, dispatches
//                                                           the unit's publish workflow
//   publish-npm.yml      node scripts/release.mjs npm-plan  validates the tag against every
//                                                           publishable package, prints them in
//                                                           dependency order
//
// Units:
//   python  python/pyproject.toml; branch release/python-v<version>; tag <version> (bare, as the
//           `pypi` GitHub environment's tag policy requires); publish.yml.
//   npm     every typescript/packages/* manifest named @temporalio/agent-harness[-*], at one
//           shared version (lockstep); branch release/npm-v<version>; tag npm-v<version>;
//           publish-npm.yml publishes those that are not private, in dependency order.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { appendFileSync, existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export const stableVersion = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const units = ['python', 'npm'];
const bumps = ['patch', 'minor', 'major'];
export const PACKAGES_DIR = 'typescript/packages';
const WORKSPACE_DIRS = ['typescript/packages', 'typescript/apps', 'typescript/examples'];
const SCOPE = '@temporalio/agent-harness';
const PYPROJECT = 'python/pyproject.toml';
const README = 'README.md';

const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'));
const writeJson = (path, value) => writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
const execute = (command, args, options = {}) =>
  execFileSync(command, args, { encoding: 'utf8', ...options }).trim();
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// ---------------------------------------------------------------------------------------------
// Versions, branches and tags

export function bumpVersion(version, bump) {
  assert(stableVersion.test(version), `Current version must be stable, not ${version}`);
  assert(bumps.includes(bump), 'Select patch, minor, or major');
  const [major, minor, patch] = version.split('.').map(Number);
  if (bump === 'major') return `${major + 1}.0.0`;
  if (bump === 'minor') return `${major}.${minor + 1}.0`;
  return `${major}.${minor}.${patch + 1}`;
}

export function validateInputs(unit, bump) {
  assert(units.includes(unit), 'Select python or npm');
  assert(bumps.includes(bump), 'Select patch, minor, or major');
}

export function unitDetails(unit, version) {
  assert(units.includes(unit), `Unknown release unit ${unit}`);
  assert(stableVersion.test(version), `Release version must be stable, not ${version}`);
  return {
    unit,
    version,
    branch: `release/${unit}-v${version}`,
    tag: unit === 'python' ? version : `npm-v${version}`,
    publishWorkflow: unit === 'python' ? 'publish.yml' : 'publish-npm.yml',
    testTargets: unit === 'python'
      ? '//python/tests:test_packaged_distributions'
      : `//${PACKAGES_DIR}/...`,
  };
}

export function releaseDetails(branch) {
  const match = /^release\/(python|npm)-v(.+)$/.exec(branch);
  assert(match && stableVersion.test(match[2]), `Invalid release branch ${branch}`);
  return unitDetails(match[1], match[2]);
}

export function tagDetails(tag) {
  const npm = /^npm-v(.+)$/.exec(tag);
  if (npm) return unitDetails('npm', npm[1]);
  return unitDetails('python', tag);
}

// ---------------------------------------------------------------------------------------------
// Python: pyproject.toml and the README's pins

export function pythonVersion(pyproject) {
  const match = /^version = "([^"]+)"$/m.exec(pyproject);
  assert(match, `${PYPROJECT} has no version line`);
  return match[1];
}

export function setPythonVersion(pyproject, version) {
  const current = pythonVersion(pyproject);
  return pyproject.replace(`version = "${current}"`, `version = "${version}"`);
}

// Every pin of the previous version becomes the new version, and the "what changed since the
// previous release" diff command moves forward one release.
export function updateReadmePins(readme, previous, next) {
  const bare = new RegExp(`(?<![\\d.])${escapeRegExp(previous)}(?![\\d.])`, 'g');
  let count = 0;
  let text = readme.replace(bare, () => { count += 1; return next; });
  text = text.replace(new RegExp(`git diff \\S+ ${escapeRegExp(next)}`, 'g'),
    `git diff ${previous} ${next}`);
  assert(count > 0, `${README} pins nothing at ${previous}`);
  return text;
}

// ---------------------------------------------------------------------------------------------
// npm: the lockstep package set

const inScope = (name) => name === SCOPE || name.startsWith(`${SCOPE}-`);

export function lockstepPackages(root = '.') {
  const dir = join(root, PACKAGES_DIR);
  const packages = readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && existsSync(join(dir, entry.name, 'package.json')))
    .map((entry) => ({
      name: entry.name,
      directory: `${PACKAGES_DIR}/${entry.name}`,
      path: join(dir, entry.name, 'package.json'),
      manifest: readJson(join(dir, entry.name, 'package.json')),
    }))
    .filter((pkg) => inScope(pkg.manifest.name))
    .sort((a, b) => a.name.localeCompare(b.name));
  assert(packages.length > 0, `No ${SCOPE} packages under ${PACKAGES_DIR}`);
  return packages;
}

export function lockstepVersion(packages) {
  const versions = new Set(packages.map((pkg) => pkg.manifest.version));
  assert.equal(versions.size, 1,
    `Lockstep packages disagree on their version: ${[...versions].join(', ')}`);
  const [version] = versions;
  assert(stableVersion.test(version), `Current version must be stable, not ${version}`);
  return version;
}

// Dependency order among the lockstep packages: a package comes after every package it depends on.
export function publishOrder(packages) {
  const byName = new Map(packages.map((pkg) => [pkg.manifest.name, pkg]));
  const ordered = [];
  const visiting = new Set();
  const visit = (pkg) => {
    if (ordered.includes(pkg)) return;
    assert(!visiting.has(pkg), `Dependency cycle at ${pkg.manifest.name}`);
    visiting.add(pkg);
    for (const name of Object.keys(pkg.manifest.dependencies ?? {})) {
      if (byName.has(name)) visit(byName.get(name));
    }
    visiting.delete(pkg);
    ordered.push(pkg);
  };
  packages.forEach(visit);
  return ordered;
}

export const publishablePackages = (packages) =>
  publishOrder(packages).filter((pkg) => !pkg.manifest.private);

// Writes `version` into every lockstep manifest, and moves every workspace manifest's caret
// range on a lockstep package (`^0.1.0`, the published minimum) to `^<version>`. `workspace:`
// ranges, which pnpm resolves itself, are left alone. Returns the files written.
export function setLockstepVersion(version, root = '.') {
  assert(stableVersion.test(version), `Release version must be stable, not ${version}`);
  const written = [];
  for (const pkg of lockstepPackages(root)) {
    pkg.manifest.version = version;
    writeJson(pkg.path, pkg.manifest);
    written.push(pkg.path);
  }
  for (const dir of WORKSPACE_DIRS) {
    if (!existsSync(join(root, dir))) continue;
    for (const entry of readdirSync(join(root, dir), { withFileTypes: true })) {
      const path = join(root, dir, entry.name, 'package.json');
      if (!entry.isDirectory() || !existsSync(path)) continue;
      const manifest = readJson(path);
      let changed = false;
      for (const field of ['dependencies', 'peerDependencies', 'devDependencies']) {
        for (const [name, range] of Object.entries(manifest[field] ?? {})) {
          if (inScope(name) && /^\^\d/.test(range) && range !== `^${version}`) {
            manifest[field][name] = `^${version}`;
            changed = true;
          }
        }
      }
      if (changed) {
        writeJson(path, manifest);
        if (!written.includes(path)) written.push(path);
      }
    }
  }
  return written;
}

// What publish-npm.yml checks before publishing: every publishable package is at the release
// version, with the metadata trusted publishing needs. Returns them in publish order.
export function validateNpmRelease(version, repository, root = '.') {
  const packages = lockstepPackages(root);
  assert.equal(lockstepVersion(packages), version, 'Tag must match the packages\' version');
  const publishable = publishablePackages(packages);
  assert(publishable.length > 0, 'No publishable package');
  for (const { manifest } of publishable) {
    assert.equal(manifest.publishConfig?.access, 'public', `${manifest.name} must publish publicly`);
    assert.equal(manifest.publishConfig?.registry, 'https://registry.npmjs.org/',
      `${manifest.name} must publish to the public registry`);
    assert.equal(manifest.repository?.url, `git+https://github.com/${repository}.git`,
      `${manifest.name} must name this repository for provenance`);
    for (const [name, range] of Object.entries(manifest.dependencies ?? {})) {
      assert(!/^(file:|link:|workspace:)/.test(range),
        `${manifest.name} depends on ${name} at ${range}; publish registry dependencies only`);
      if (inScope(name)) {
        assert.equal(range, `^${version}`,
          `${manifest.name} must depend on ${name} at ^${version} for a lockstep release`);
      }
    }
  }
  return publishable;
}

// ---------------------------------------------------------------------------------------------
// The workflows' entry points

export function prepare(env = process.env, run = execute) {
  const unit = env.RELEASE_UNIT;
  const bump = env.RELEASE_BUMP;
  validateInputs(unit, bump);
  assert.equal(env.GITHUB_REF, 'refs/heads/main', 'Run this workflow from main');

  const prs = JSON.parse(run('gh', ['pr', 'list', '--repo', env.GITHUB_REPOSITORY,
    '--base', 'main', '--state', 'open', '--limit', '1000', '--json', 'headRefName,url']));
  const existing = prs.find((pr) => pr.headRefName.startsWith(`release/${unit}-v`));
  assert(!existing, `An open ${unit} release PR already exists: ${existing?.url}`);

  let previous;
  let version;
  const lines = [];
  if (unit === 'python') {
    const pyproject = readFileSync(PYPROJECT, 'utf8');
    previous = pythonVersion(pyproject);
    version = bumpVersion(previous, bump);
    writeFileSync(PYPROJECT, setPythonVersion(pyproject, version));
    run('uv', ['lock'], { cwd: 'python' });
    writeFileSync(README, updateReadmePins(readFileSync(README, 'utf8'), previous, version));
    lines.push(`Release \`temporal-agent-harness\` ${previous} → **${version}** to PyPI.`, '',
      `Changed: \`${PYPROJECT}\`, \`python/uv.lock\` (\`uv lock\`), the README's install pins.`,
      'Review the lockfile diff for dependency changes beyond the version.');
  } else {
    const packages = lockstepPackages();
    previous = lockstepVersion(packages);
    version = bumpVersion(previous, bump);
    setLockstepVersion(version);
    run('pnpm', ['install', '--lockfile-only', '--ignore-scripts'], { cwd: 'typescript' });
    const published = publishablePackages(lockstepPackages());
    const skipped = packages.filter((pkg) => pkg.manifest.private);
    lines.push(`Release the npm packages ${previous} → **${version}**, published in this order:`, '',
      ...published.map((pkg) => `1. \`${pkg.manifest.name}\``), '');
    if (skipped.length) {
      lines.push(`Also at ${version} but \`private\`, so not published: `
        + skipped.map((pkg) => `\`${pkg.manifest.name}\``).join(', ') + '.', '');
    }
    lines.push('Every workspace dependency on these packages now requires `^' + version + '`.');
  }

  const details = unitDetails(unit, version);
  appendFileSync(env.GITHUB_OUTPUT, [
    `unit=${unit}`, `version=${version}`, `branch=${details.branch}`, `tag=${details.tag}`,
    `test_targets=${details.testTargets}`, '',
  ].join('\n'));
  writeFileSync(`${env.RUNNER_TEMP}/release-body.md`, [
    ...lines, '',
    '## Release notes', '',
    '_Replace this section before merging: what changed, by package; breaking changes and',
    'migrations; parity exceptions; the tested version combination. It becomes the GitHub',
    'Release body._', '',
    '## Checklist', '',
    `See [RELEASE.md](https://github.com/${env.GITHUB_REPOSITORY}/blob/main/RELEASE.md).`, '',
    '---', '',
    `Prepared by **Prepare release**, which ran \`bazel test ${details.testTargets}\` before`,
    `pushing this branch. Wait for the required checks. **Merging publishes**: it tags the merged`,
    `commit \`${details.tag}\`, creates a GitHub Release from this description, and runs`,
    `**${details.publishWorkflow}**. Closing without merging publishes nothing.`, '',
  ].join('\n'));
}

export function releasePrUrl(repository, branch, body) {
  const { tag } = releaseDetails(branch);
  const query = new URLSearchParams({ quick_pull: '1', title: `Release ${tag}`, body });
  return `https://github.com/${repository}/compare/main...${encodeURIComponent(branch)}?${query}`;
}

export function summarize(env = process.env) {
  const body = readFileSync(`${env.RUNNER_TEMP}/release-body.md`, 'utf8');
  const url = releasePrUrl(env.GITHUB_REPOSITORY, env.RELEASE_BRANCH, body);
  appendFileSync(env.GITHUB_STEP_SUMMARY, [
    `Release branch pushed: \`${env.RELEASE_BRANCH}\`.`, '',
    `[Create the release pull request](${url})`, '',
    'Click the link, review the prefilled title and description, and click **Create pull request**.',
    'Write the release notes into the description, wait for CI, then review and merge.',
    'Merging tags the merged commit, creates the GitHub Release, and publishes.', '',
  ].join('\n'));
}

// On merge of a release PR: tag the merged commit, create the GitHub Release from the PR body
// and dispatch publishing. Tags and releases created with GITHUB_TOKEN trigger no workflows, so
// the dispatch is explicit. Every step is idempotent so a failed run can be rerun.
export function finish(event, repository, run = execute) {
  const pr = event.pull_request;
  assert(event.action === 'closed' && pr?.merged, 'Only merged PRs can release');
  assert.equal(pr.base.ref, 'main');
  assert.equal(pr.base.repo.full_name, repository);
  assert.equal(pr.head.repo.full_name, repository, 'Release PR must belong to this repository');
  const { unit, version, tag, publishWorkflow } = releaseDetails(pr.head.ref);
  assert(/^[a-f0-9]{40}$/.test(pr.merge_commit_sha), 'Invalid merge commit');
  assert.equal(run('git', ['rev-parse', 'HEAD']), pr.merge_commit_sha,
    'Checkout must point to the merged release commit');
  if (unit === 'python') {
    assert.equal(pythonVersion(readFileSync(PYPROJECT, 'utf8')), version,
      'Release branch must match the pyproject.toml version');
  } else {
    validateNpmRelease(version, repository);
  }

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

  const releases = JSON.parse(run('gh', ['release', 'list', '--repo', repository,
    '--limit', '1000', '--json', 'tagName']));
  if (!releases.some((release) => release.tagName === tag)) {
    const args = ['release', 'create', tag, '--repo', repository, '--verify-tag',
      '--title', tag, '--notes-file', '-'];
    if (version.startsWith('0.')) args.push('--prerelease');
    run('gh', args, { input: pr.body ?? '' });
  }

  run('gh', ['workflow', 'run', publishWorkflow, '--repo', repository, '--ref', tag]);
  return tag;
}

// publish-npm.yml: the tag names a lockstep release, every publishable package is at that
// version, and here they are in the order to publish them.
export function npmPlan(env = process.env) {
  assert.equal(env.GITHUB_REF_TYPE, 'tag', 'Select a release tag such as npm-v0.2.0');
  const { unit, version } = tagDetails(env.GITHUB_REF_NAME);
  assert.equal(unit, 'npm', 'Select an npm release tag such as npm-v0.2.0');
  const packages = validateNpmRelease(version, env.GITHUB_REPOSITORY);
  appendFileSync(env.GITHUB_OUTPUT, [
    `version=${version}`,
    `directories=${packages.map((pkg) => pkg.directory).join(' ')}`,
    `names=${packages.map((pkg) => pkg.manifest.name).join(' ')}`, '',
  ].join('\n'));
  return packages;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const command = process.argv[2];
  if (command === 'prepare') {
    prepare();
  } else if (command === 'summary') {
    summarize();
  } else if (command === 'finish') {
    const tag = finish(readJson(process.env.GITHUB_EVENT_PATH), process.env.GITHUB_REPOSITORY);
    appendFileSync(process.env.GITHUB_STEP_SUMMARY,
      `Tagged and released \`${tag}\` and dispatched its publish workflow. Check that workflow for the publication result.\n`);
  } else if (command === 'npm-plan') {
    npmPlan();
  } else if (command === 'npm-packages') {
    // Publishable package directories in publish order, for anything that builds their tarballs.
    console.log(publishablePackages(lockstepPackages()).map((pkg) => pkg.directory).join(' '));
  } else {
    throw new Error('Usage: node scripts/release.mjs prepare|summary|finish|npm-plan|npm-packages');
  }
}
