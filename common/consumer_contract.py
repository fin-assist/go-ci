"""Shared contract tests for the actual consumer workflow and copied bootstrap."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import yaml


SOURCE = 'fin-assist/go-ci/.github/workflows/'
QUALITY_MODULES = {'github.com/fin-assist/' + name for name in (
    'go-service-template', 'bank-bff', 'user', 'bank-statement-data', 'analytics', 'assistant')}
PUSH_TOKEN = "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}"

DISCOVERY_CONDITION = ("github.event_name == 'pull_request' && "
                       "github.event.pull_request.head.repo.full_name == github.repository")
DISCOVERY_ACTION = 'fin-assist/go-ci/.github/actions/private-ci-inputs@'
DISCOVERY_OUTPUTS = {'run_id': '${{ steps.inputs.outputs.run-id }}',
                     'artifact_name': '${{ steps.inputs.outputs.artifact-name }}'}
DISCOVERY_INPUTS = {'token': '${{ github.token }}',
                    'head-sha': '${{ github.event.pull_request.head.sha }}',
                    'base-sha': '${{ github.event.pull_request.base.sha }}'}
CHECK_CONDITION = "always() && (github.event_name == 'push' || needs.private-inputs.result == 'success')"
MIGRATIONS_CONDITION = "always() && github.event_name == 'pull_request' && needs.private-inputs.result == 'success'"

DELEGATE_PROBE = '''
import runpy, sys, types
from pathlib import Path
wrapper, root, shared = map(Path, sys.argv[1:])
config = {'fixture': 'selected immutable source'}
def read_config(path):
    if path != root:
        raise ValueError('Wrapper did not use its own consumer root')
    return config
def source_dir(path, selected):
    if path != root or selected is not config:
        raise ValueError('Wrapper bypassed the verified resolver')
    return shared
adapter = types.ModuleType('common_ci')
adapter.read_config, adapter.source_dir = read_config, source_dir
sys.modules['common_ci'] = adapter
sys.argv = [str(wrapper), 'probe', 'argument with spaces']
runpy.run_path(str(wrapper), run_name='__main__')
'''


def suite(root, common):
    """Build consumer-local cases without exposing an auto-discovered global TestCase."""
    root, common = Path(root), Path(common)

    class ConsumerContract(unittest.TestCase):
        def service_delegates(self):
            # Execute the real thin wrapper, but never resolve/download its source.
            with tempfile.TemporaryDirectory(prefix='consumer-delegate-') as directory:
                shared = Path(directory) / 'common'
                (shared / 'scripts').mkdir(parents=True)
                for name in ('changelog', 'stage_artifact'):
                    script = shared / 'scripts' / (name + '.py')
                    script.write_text("import json, sys\nif __name__ == '__main__':\n"
                                      "    print(json.dumps({'argv': sys.argv[1:], 'file': __file__, "
                                      "'bytecode': sys.dont_write_bytecode}))\n")
                    result = subprocess.run([sys.executable, '-I', '-c', DELEGATE_PROBE,
                                             str(root.resolve() / 'scripts' / (name + '.py')),
                                             str(root.resolve()), str(shared)], cwd=directory,
                                            env={'PATH': os.environ.get('PATH', '')},
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {
                        'argv': ['--root', str(root.resolve()), 'probe', 'argument with spaces'],
                        'file': str(script), 'bytecode': True})

        def context(self):
            manifest = json.loads((root / '.finassist-ci.json').read_text())
            self.assertRegex(manifest['revision'], r'^[0-9a-f]{40}$')
            self.assertIn(manifest['profile'], ('service', 'library'))
            modules = re.findall(r'(?m)^module\s+(\S+)\s*$', (root / 'go.mod').read_text())
            self.assertEqual(len(modules), 1)
            kinds = {'go', 'lint', 'api' if manifest['profile'] == 'library' else 'image'}
            if modules[0] != 'github.com/fin-assist/bank-bff':
                kinds.add('migrations')
            workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
            self.assertEqual(workflow['permissions'], {'actions': 'read', 'contents': 'read'})
            self.assertEqual(set(workflow['jobs']), kinds | {kind + '-check' for kind in kinds} | {'private-inputs'})
            discover = workflow['jobs']['private-inputs']
            self.assertEqual(discover['if'].strip(), DISCOVERY_CONDITION)
            self.assertEqual(discover['outputs'], DISCOVERY_OUTPUTS)
            self.assertNotIn('permissions', discover)
            self.assertEqual(len(discover['steps']), 1)
            step, = discover['steps']
            self.assertEqual(step['id'], 'inputs')
            self.assertEqual(step['uses'], DISCOVERY_ACTION + manifest['revision'])
            self.assertNotIn('if', step)
            discovery_inputs = dict(step['with'])
            bootstrap = discovery_inputs.pop('bootstrap-sha', None)
            if bootstrap is not None:
                self.assertRegex(bootstrap, r'^[0-9a-f]{40}$')
            self.assertEqual(discovery_inputs, DISCOVERY_INPUTS)
            prep = yaml.safe_load((root / '.github/workflows/private-ci-inputs.yml').read_text())
            self.assertEqual(prep['permissions'], {'contents': 'read', 'pull-requests': 'read'})
            self.assertEqual(prep['jobs'], {'prepare': {
                'uses': SOURCE + 'shared-private-inputs.yml@' + manifest['revision'],
                'with': {'revision': manifest['revision']},
                'secrets': {'GO_MODULES_TOKEN': '${{ secrets.GO_MODULES_TOKEN }}'},
            }})
            events = prep.get('on', prep.get(True))
            self.assertEqual(events, {'pull_request_target': {
                'branches': ['main'], 'types': ['opened', 'reopened', 'synchronize'],
            }})
            return manifest, workflow['jobs'], kinds

        def test_all_calls_use_the_manifest_source_and_profile(self):
            manifest, jobs, kinds = self.context()
            for kind in kinds:
                with self.subTest(call=kind):
                    call = jobs[kind + '-check']
                    self.assertEqual(call['uses'], SOURCE + 'shared-' + kind + '.yml@' + manifest['revision'])
                    self.assertEqual(call['with']['revision'], manifest['revision'])
                    self.assertNotIn('permissions', call)
                    self.assertEqual(call['needs'], 'private-inputs')
                    self.assertEqual(call['if'], MIGRATIONS_CONDITION if kind == 'migrations' else CHECK_CONDITION)
                    self.assertEqual(call['with']['private_input_run_id'], '${{ needs.private-inputs.outputs.run_id }}')
                    self.assertEqual(call['with']['private_input_artifact'], '${{ needs.private-inputs.outputs.artifact_name }}')
                    if kind == 'go':
                        self.assertEqual(call['with']['profile'], manifest['profile'])
                        quality = call['with'].get('quality_runner', False)
                        self.assertIs(type(quality), bool)
                        if quality:
                            self.assertEqual(manifest['profile'], 'service')
                            module = re.findall(r'(?m)^module\s+(\S+)\s*$', (root / 'go.mod').read_text())[0]
                            self.assertIn(module, QUALITY_MODULES)
                            for target, command in [('test-names', 'test-names'),
                                                    ('collect-unit', 'collect "$(@:collect-%=%)"'),
                                                    ('collect-integration', 'collect "$(@:collect-%=%)"'),
                                                    ('collect-api', 'collect "$(@:collect-%=%)"'),
                                                    ('check-unit', 'check "$(@:check-%=%)"'),
                                                    ('check-integration', 'check "$(@:check-%=%)"'),
                                                    ('check-api', 'check "$(@:check-%=%)"'),
                                                    ('cover-combined', 'combined')]:
                                recipe = []
                                selected = False
                                for line in (root / 'Makefile').read_text().splitlines():
                                    if line and not line[0].isspace() and not line.startswith('#'):
                                        selected = target in line.split(':', 1)[0].split()
                                    elif selected and line.startswith('\t') and line.strip():
                                        recipe.append(line.strip())
                                self.assertEqual(recipe, [
                                    '@common=$$(python3 scripts/common_ci.py) && python3 -B '
                                    '"$$common/scripts/quality.py" --root "$(CURDIR)" ' + command])
                            release = yaml.safe_load((root / '.github/workflows/release.yml').read_text())
                            events = release.get('on', release.get(True))
                            self.assertEqual(set(events), {'push', 'workflow_dispatch'})
                            self.assertEqual(events['push'], {'tags': ['v*']})
                            manual = events['workflow_dispatch']['inputs']
                            self.assertEqual(set(manual), {'tag'})
                            self.assertIs(manual['tag']['required'], True)
                            self.assertEqual(manual['tag']['type'], 'string')
                            self.assertEqual(release['permissions'], {'contents': 'write'})
                            self.assertEqual(set(release['jobs']), {'release'})
                            publisher = release['jobs']['release']
                            self.assertNotIn('permissions', publisher)
                            self.assertNotIn('secrets', publisher)
                            self.assertEqual(publisher['uses'], SOURCE + 'shared-release.yml@' + manifest['revision'])
                            self.assertEqual({key: value for key, value in publisher['with'].items()
                                              if key != 'checkout_version'},
                                             {'revision': manifest['revision'],
                                              'tag': '${{ inputs.tag || github.ref_name }}'})
                            self.assertEqual(publisher['with'].get('checkout_version', 'v4'),
                                             'v7' if module == 'github.com/fin-assist/bank-bff' else 'v4')
                            self.service_delegates()
                    expected_secrets = {} if kind == 'migrations' else {'GO_MODULES_TOKEN': PUSH_TOKEN}
                    self.assertEqual(call.get('secrets', {}), expected_secrets)

        def test_required_facades_reject_every_non_success_result(self):
            _, jobs, kinds = self.context()
            for kind in kinds:
                with self.subTest(gate=kind):
                    gate = jobs[kind]
                    self.assertEqual(gate.get('name', kind), kind)
                    self.assertEqual(gate['needs'], kind + '-check')
                    condition = "always() && github.event_name == 'pull_request'" if kind == 'migrations' else 'always()'
                    self.assertEqual(gate['if'], condition)
                    self.assertEqual(gate['env']['RESULT'], '${{ needs.' + kind + '-check.result }}')
                    self.assertFalse(gate.get('continue-on-error', False))
                    self.assertEqual(gate['runs-on'], 'ubuntu-latest')
                    self.assertEqual(gate['timeout-minutes'], 2)
                    self.assertEqual(len(gate['steps']), 1)
                    step = gate['steps'][0]
                    self.assertNotIn('if', step)
                    self.assertFalse(step.get('continue-on-error', False))
                    self.assertEqual(step['run'], 'test "$RESULT" = success')
                    for outcome in ['success', 'failure', 'skipped', 'cancelled', '']:
                        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', step['run']],
                            env={**os.environ, 'RESULT': outcome}, capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode == 0, outcome == 'success', outcome)

        def test_copied_bootstrap_matches_the_pinned_source(self):
            self.assertEqual((root / 'scripts/common_ci.py').read_bytes(), (common / 'bootstrap.py').read_bytes())

    return unittest.defaultTestLoader.loadTestsFromTestCase(ConsumerContract)
