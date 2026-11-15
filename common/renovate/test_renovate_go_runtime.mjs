/** Exercise the actual Renovate extractor and baseline matcher for manual Go. */
import assert from 'node:assert/strict';
import { readFileSync, realpathSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

assert.ok(process.env.RENOVATE_RUNTIME);
const pkg = realpathSync(resolve(process.env.RENOVATE_RUNTIME, 'node_modules/renovate'));
const pinned = JSON.parse(readFileSync(process.env.RENOVATE_RUNTIME_MANIFEST)).dependencies.renovate;
assert.equal(JSON.parse(readFileSync(resolve(pkg, 'package.json'))).version, pinned);
const mod = name => import(pathToFileURL(resolve(pkg, 'dist', name)).href);
const { init } = await mod('logger/index.js');
await init();
const { GlobalConfig } = await mod('config/global.js');
GlobalConfig.set({ localDir: process.cwd() });
const { extractPackageFile } = await mod('modules/manager/github-actions/extract.js');
const { applyPackageRules } = await mod('util/package-rules/index.js');
const root = process.env.RENOVATE_CONSUMER_ROOT;
const baseline = JSON.parse(readFileSync(resolve(root, 'renovate-base.json')));
let workflow;
try { workflow = readFileSync(resolve(root, '.github/workflows/ci.yml'), 'utf8'); } catch { workflow = ''; }
const source = JSON.parse(readFileSync(resolve(root, '.github/renovate-policy.json'))).ciChecks?.includes('shared-source');
if (!source) workflow = 'jobs:\n  sample:\n    steps:\n      - uses: actions/setup-go@v7\n        with:\n          go-version: 1.26.0\n';
const extracted = await extractPackageFile(workflow, '.github/workflows/ci.yml');
const go = extracted.deps.find(dep => dep.depType === 'uses-with' && dep.depName === 'go');
assert.ok(go, 'The real source workflow must expose its Go version');
assert.equal(go.packageName, 'actions/go-versions');
assert.equal(go.datasource, 'github-releases');
const configured = await applyPackageRules({ ...go, manager: 'github-actions',
  packageRules: baseline.packageRules });
if (source) assert.equal(configured.enabled, false, 'Go uses-with updates must remain manual');
for (const depType of ['golang', 'toolchain']) {
  const value = await applyPackageRules({ manager: 'gomod', depType, depName: 'go',
    datasource: 'golang-version', packageRules: baseline.packageRules });
  if (source) assert.equal(value.enabled, false);
}
const action = extracted.deps.find(dep => dep.depName === 'actions/setup-go');
assert.ok(action);
const configuredAction = await applyPackageRules({ ...action, manager: 'github-actions',
  packageRules: baseline.packageRules });
assert.notEqual(configuredAction.enabled, false, 'Updating the setup action itself stays permitted');
const controller = process.env.RENOVATE_POLICY_SCRIPT;
const code = "import importlib.util,json,sys; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1]).parent)); spec=importlib.util.spec_from_file_location('policy',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); import base64; metadata=''.join(m.COMPACT_UPDATE+base64.b64encode(json.dumps(item).encode()).decode()+' -->\\n' for item in json.loads(sys.stdin.read())); print(json.dumps(m.manual_go_update(m.updates(metadata))))";
const mixed = [{ ...go, newValue: '1.27.3' }, { depName: 'actions/checkout',
  datasource: 'github-tags', newValue: 'v7' }];
assert.equal(JSON.parse(execFileSync('python3', ['-B', '-c', code, controller], {
  input: JSON.stringify(mixed), encoding: 'utf8', timeout: 5000 })), true);
console.log(source ? 'PASS: source uses-with Go extraction is disabled by baseline and guarded in mixed signed metadata' : 'PASS: real sample uses-with Go extraction and mixed signed metadata remain manual');

// Extract every shared action/manifest/test-input pin with the real managers and
// apply the caller's reviewed package rules. This catches a selector that silently
// stops updating source-revision, as well as action pins split into another group.
const manifestPath = existsSync(resolve(root, '.finassist-ci.json'))
  ? resolve(root, '.finassist-ci.json') : resolve(root, '.github/renovate-source.json');
const revision = existsSync(manifestPath) ? JSON.parse(readFileSync(manifestPath)).revision : undefined;
if (existsSync(manifestPath) && revision === undefined) {
  const { mergeChildConfig } = await mod('config/utils.js');
  const active = JSON.parse(readFileSync(resolve(root, 'renovate.json')));
  const effective = mergeChildConfig(baseline, active);
  const file = '.github/workflows/ci.yml';
  const content = readFileSync(resolve(root, file), 'utf8');
  const extracted = (await extractPackageFile(content, file))?.deps ?? [];
  const shared = extracted.filter(dep => dep.packageName === 'fin-assist/go-ci' || dep.depName === 'fin-assist/go-ci');
  assert.ok(shared.length >= 3, 'All branch-backed shared workflows must be visible to the Actions manager');
  for (const dep of shared) {
    assert.equal(dep.currentValue, 'refs/heads/main');
    assert.equal(dep.currentDigest, undefined);
    const configured = await applyPackageRules({ ...dep, manager: 'github-actions', packageFile: file, packageRules: effective.packageRules });
    assert.equal(configured.enabled, false, 'Shared branch references must not receive digest bumps');
  }
  assert.ok((effective.customManagers ?? []).every(manager => manager.depNameTemplate !== 'fin-assist/go-ci'),
    'Obsolete SHA managers must be removed');
  console.log('PASS: actual Renovate Actions extraction disables bumps for the shared branch');
}
if (revision !== undefined) {
  const { mergeChildConfig } = await mod('config/utils.js');
  // FIN-260 keeps the shared CI managers/rules directly in renovate.json in
  // three libraries. Apply Renovate's real merge semantics to both data layers.
  const active = JSON.parse(readFileSync(resolve(root, 'renovate.json')));
  const effective = mergeChildConfig(baseline, active);
  const { extractPackageFile: extractRegex } = await mod('modules/manager/custom/regex/index.js');
  const { matchRegexOrGlob } = await mod('util/string-match.js');
  const { normalizeDepNames } = await mod('workers/repository/extract/manager-files.js');
  async function customDependencies(config, file) {
    const content = readFileSync(resolve(root, file), 'utf8');
    const result = [];
    for (const custom of config.customManagers ?? []) {
      if (!(custom.managerFilePatterns ?? []).some(pattern => matchRegexOrGlob(file, pattern))) continue;
      for (const dep of (await extractRegex(content, file, custom))?.deps ?? []) {
        if (dep.packageName === 'fin-assist/go-ci' || dep.depName === 'fin-assist/go-ci')
          result.push({ ...dep, manager: 'custom.regex', packageFile: file });
      }
    }
    return result;
  }
  // A disabled write-controller cannot publish a new base.json preset. New pin
  // locations must therefore be extracted directly from active renovate.json.
  const activeInputs = await customDependencies(active, '.github/workflows/renovate-policy-tests.yml');
  assert.equal(activeInputs.length, 2,
    'Both source-revision inputs must update from active renovate.json without baseline synchronization');
  if (manifestPath.endsWith('/.github/renovate-source.json')) {
    const manifestDeps = await customDependencies(active, '.github/renovate-source.json');
    assert.equal(manifestDeps.length, 1, 'New specialized manifest manager must be active while disabled');
    const grouped = await applyPackageRules({ ...manifestDeps[0], packageRules: active.packageRules });
    assert.equal(grouped.groupSlug, 'finassist-shared-renovate');
    assert.equal(grouped.pinDigests, true);
  }
  const releaseFile = '.github/workflows/release.yml';
  if (existsSync(resolve(root, releaseFile)) &&
      readFileSync(resolve(root, releaseFile), 'utf8').includes('fin-assist/go-ci/.github/workflows/shared-release.yml@')) {
    const releaseInputs = await customDependencies(active, releaseFile);
    assert.equal(releaseInputs.length, 1, 'Shared release revision input must update without activation');
  }
  const files = ['.github/workflows/renovate-policy.yml', '.github/workflows/renovate-policy-tests.yml',
    '.github/workflows/ci.yml', releaseFile, '.finassist-ci.json', '.github/renovate-source.json'];
  const dependencies = [];
  for (const file of files) {
    if (!existsSync(resolve(root, file))) continue;
    const content = readFileSync(resolve(root, file), 'utf8');
    if (file.endsWith('.yml')) {
      const result = await extractPackageFile(content, file);
      for (const dep of result?.deps ?? []) {
        if (dep.packageName === 'fin-assist/go-ci' || dep.depName === 'fin-assist/go-ci' || dep.depName?.startsWith('fin-assist/go-ci/'))
          dependencies.push({ ...dep, manager: 'github-actions', packageFile: file });
      }
    }
    dependencies.push(...await customDependencies(effective, file));
  }
  assert.ok(dependencies.some(dep => dep.packageFile === '.github/workflows/renovate-policy.yml'));
  assert.equal(dependencies.filter(dep => dep.packageFile === '.github/workflows/renovate-policy-tests.yml').length, 4,
    'Both action refs and both source-revision inputs must be extracted');
  assert.ok(dependencies.some(dep => dep.packageFile.endsWith('.json')), 'The local source manifest pin must be extracted');
  async function verifySourcePinGroup(packageRules) {
    const slugs = new Set();
    for (const dep of dependencies) {
      normalizeDepNames(dep);
      assert.equal(dep.currentDigest, revision, `${dep.packageFile}: source pin drift`);
      assert.equal(dep.currentValue, 'main', 'Full action SHA annotations must retain the lookup branch');
      assert.equal(dep.datasource, 'github-digest');
      const configured = await applyPackageRules({ ...dep, packageRules });
      assert.notEqual(configured.enabled, false, 'Shared action pins must not be skipped as unversioned refs');
      assert.ok(['finassist-shared-go-ci', 'finassist-shared-renovate'].includes(configured.groupSlug),
        `${dep.depName}: action, manifest and test-input pins must share the reviewed group`);
      assert.equal(configured.pinDigests, true);
      slugs.add(configured.groupSlug);
    }
    assert.equal(slugs.size, 1, `source pins split across groups: ${[...slugs]}`);
    return [...slugs][0];
  }
  const group = await verifySourcePinGroup(effective.packageRules);
  // Both slugs are individually valid: only a cross-pin check detects this split.
  const splitActions = { matchManagers: ['github-actions'],
    matchPackageNames: ['fin-assist/go-ci', 'fin-assist/go-ci/**'],
    groupSlug: group === 'finassist-shared-go-ci' ? 'finassist-shared-renovate' : 'finassist-shared-go-ci' };
  await assert.rejects(() => verifySourcePinGroup([...effective.packageRules, splitActions]),
    /source pins split across groups/, 'A real package-rule mutation must fail when action pins split');
  console.log(`PASS: actual regex/actions extraction keeps ${dependencies.length} immutable source pins grouped`);
}
