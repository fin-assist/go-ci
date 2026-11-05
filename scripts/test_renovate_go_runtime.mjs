/** Exercise the actual Renovate extractor and baseline matcher for manual Go. */
import assert from 'node:assert/strict';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

assert.ok(process.env.RENOVATE_RUNTIME);
const pkg = realpathSync(resolve(process.env.RENOVATE_RUNTIME, 'node_modules/renovate'));
const pinned = JSON.parse(readFileSync(new URL('./renovate-runtime/package.json', import.meta.url))).dependencies.renovate;
assert.equal(JSON.parse(readFileSync(resolve(pkg, 'package.json'))).version, pinned);
const mod = name => import(pathToFileURL(resolve(pkg, 'dist', name)).href);
const { init } = await mod('logger/index.js');
await init();
const { GlobalConfig } = await mod('config/global.js');
GlobalConfig.set({ localDir: process.cwd() });
const { extractPackageFile } = await mod('modules/manager/github-actions/extract.js');
const { applyPackageRules } = await mod('util/package-rules/index.js');
const baseline = JSON.parse(readFileSync(new URL('../renovate-base.json', import.meta.url)));
const root = fileURLToPath(new URL('..', import.meta.url));
const workflow = readFileSync(resolve(root, '.github/workflows/ci.yml'), 'utf8');
const extracted = await extractPackageFile(workflow, '.github/workflows/ci.yml');
const go = extracted.deps.find(dep => dep.depType === 'uses-with' && dep.depName === 'go');
assert.ok(go, 'The real source workflow must expose its Go version');
assert.equal(go.packageName, 'actions/go-versions');
assert.equal(go.datasource, 'github-releases');
const configured = await applyPackageRules({ ...go, manager: 'github-actions',
  packageRules: baseline.packageRules });
assert.equal(configured.enabled, false, 'Go uses-with updates must remain manual');
for (const depType of ['golang', 'toolchain']) {
  const value = await applyPackageRules({ manager: 'gomod', depType, depName: 'go',
    datasource: 'golang-version', packageRules: baseline.packageRules });
  assert.equal(value.enabled, false);
}
const action = extracted.deps.find(dep => dep.depName === 'actions/setup-go');
assert.ok(action);
const configuredAction = await applyPackageRules({ ...action, manager: 'github-actions',
  packageRules: baseline.packageRules });
assert.notEqual(configuredAction.enabled, false, 'Updating the setup action itself stays permitted');
const controller = resolve(root, 'scripts/renovate_policy.py');
const code = "import importlib.util,json,sys; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1]).parent)); spec=importlib.util.spec_from_file_location('policy',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); import base64; metadata=''.join(m.COMPACT_UPDATE+base64.b64encode(json.dumps(item).encode()).decode()+' -->\\n' for item in json.loads(sys.stdin.read())); print(json.dumps(m.manual_go_update(m.updates(metadata))))";
const mixed = [{ ...go, newValue: '1.27.3' }, { depName: 'actions/checkout',
  datasource: 'github-tags', newValue: 'v7' }];
assert.equal(JSON.parse(execFileSync('python3', ['-c', code, controller], {
  input: JSON.stringify(mixed), encoding: 'utf8', timeout: 5000 })), true);
console.log('PASS: real uses-with Go extraction is disabled by baseline and guarded in mixed signed metadata');
