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
MAIN = 'main'
RELAY_CONDITION = (
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository) || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/main')"
)
RELAY_RUN = '''test "$RELAY_RESULT" = success
test "$ACCEPTED" = true
test "$SOURCE_RUN_ATTEMPT" = "$EXPECTED_RUN_ATTEMPT"
test "$RESULT" = success'''

PREPARE_EVENTS = {'pull_request_target': {
    'branches': ['main', 'release/**'], 'types': ['opened', 'reopened', 'synchronize'],
}}

QUALITY_MODULES = {'github.com/fin-assist/' + name for name in (
    'go-service-template', 'bank-bff', 'user', 'bank-statement-data', 'analytics', 'assistant')}
DEPLOYMENT_MODULES = QUALITY_MODULES - {'github.com/fin-assist/go-service-template'}
PRODUCTION_TRIGGER = {'workflow_run': {'workflows': ['CI'], 'branches': ['main'], 'types': ['completed']}}
PREPARE_PRODUCTION = "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
PUBLISH_CONDITION = ("github.event.workflow_run.conclusion == 'success' && "
                     "github.event.workflow_run.event == 'push' && "
                     "github.event.workflow_run.head_branch == 'main' && "
                     "github.event.workflow_run.head_repository.full_name == github.repository")
PUSH_TOKEN = "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}"

SAME_REPO = 'github.event.pull_request.head.repo.full_name == github.repository'
BRANCH_PREPARE_CONDITION = (
    "github.event_name == 'workflow_run' && github.event.workflow_run.event == 'pull_request' && "
    "github.event.workflow_run.head_repository.full_name == github.repository"
)
BRIDGE_PREPARE_CONDITION = (
    "(github.event_name == 'pull_request_target' && " + SAME_REPO + ") || (" + BRANCH_PREPARE_CONDITION + ")"
)
DISCOVERY_CONDITION = ("github.event_name == 'pull_request' && "
                       "github.event.pull_request.head.repo.full_name == github.repository")
DISCOVERY_ACTION = 'fin-assist/go-ci/.github/actions/private-ci-inputs@'
DISCOVERY_OUTPUTS = {'run_id': '${{ steps.inputs.outputs.run-id }}',
                     'artifact_name': '${{ steps.inputs.outputs.artifact-name }}'}
DISCOVERY_INPUTS = {'token': '${{ github.token }}',
                    'head-sha': '${{ github.event.pull_request.head.sha }}',
                    'base-sha': '${{ github.event.pull_request.base.sha }}'}
CHECK_CONDITION = "${{ !cancelled() && (github.event_name == 'push' || needs.private-inputs.result == 'success') }}"
MIGRATIONS_CONDITION = "${{ !cancelled() && github.event_name == 'pull_request' && needs.private-inputs.result == 'success' }}"

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

        def service_quality(self, ref, pin, module):
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
            self.assertEqual(publisher['uses'], SOURCE + 'shared-release.yml@' + ref)
            self.assertEqual({key: value for key, value in publisher['with'].items()
                              if key != 'checkout_version'},
                             ({'revision': pin} if pin else {}) |
                             {'tag': '${{ inputs.tag || github.ref_name }}'})
            self.assertEqual(publisher['with'].get('checkout_version', 'v4'),
                             'v7' if module == 'github.com/fin-assist/bank-bff' else 'v4')
            self.service_delegates()

        def context(self):
            manifest = json.loads((root / '.finassist-ci.json').read_text())
            pin = manifest.get('revision')
            if pin is not None:
                self.assertRegex(pin, r'^[0-9a-f]{40}$')
            ref = pin or MAIN
            revision_input = {'revision': pin} if pin else {}
            self.assertIn(manifest['profile'], ('service', 'library'))
            modules = re.findall(r'(?m)^module\s+(\S+)\s*$', (root / 'go.mod').read_text())
            self.assertEqual(len(modules), 1)
            kinds = {'go', 'lint', 'api' if manifest['profile'] == 'library' else 'image'}
            if modules[0] != 'github.com/fin-assist/bank-bff':
                kinds.add('migrations')
            workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
            if 'relay' in workflow['jobs']:
                self.assertEqual(manifest['profile'], 'service')
                self.assertIn(modules[0], QUALITY_MODULES)
                self.assertNotIn('revision', manifest)
                self.assertEqual(set(workflow) - {'on', True}, {'name', 'permissions', 'jobs'})
                self.assertEqual(workflow.get('name'), 'CI')
                self.assertEqual(workflow.get('on', workflow.get(True)), {
                    'pull_request': None, 'push': {'branches': ['main']}})
                self.assertEqual(workflow['permissions'], {'contents': 'read'})
                self.assertEqual(set(workflow['jobs']), kinds | {'relay'})
                self.assertFalse((root / '.github/workflows/private-ci-inputs.yml').exists())
                self.assertFalse((root / '.github/workflows/production-image.yml').exists())
                relay = dict(workflow['jobs']['relay'])
                condition = ' '.join(relay.pop('if').split())
                self.assertEqual(condition, RELAY_CONDITION)
                self.assertEqual(relay, {
                    'uses': SOURCE + 'shared-remote-ci.yml@main',
                    'secrets': {
                        'CI_BROKER_TOKEN': '${{ secrets.RENOVATE_POLICY_TOKEN }}',
                        'GO_MODULES_TOKEN': '${{ secrets.GO_MODULES_TOKEN }}',
                    },
                })
                if modules[0] == 'github.com/fin-assist/bank-bff':
                    self.qase90()
                else:
                    self.assertFalse((root / '.github/workflows/qase90.yml').exists())
                # A second source-side artifact producer defeats private broker storage.
                allowed_workflows = {
                    SOURCE + 'shared-remote-ci.yml@main', SOURCE + 'shared-release.yml@main',
                }
                for path in (root / '.github/workflows').glob('*.y*ml'):
                    document = yaml.safe_load(path.read_text())
                    if path.name in ('deploy-stage.yml', 'rollback-stage.yml') and modules[0] in DEPLOYMENT_MODULES:
                        operation = path.stem.removesuffix('-stage')
                        dispatch = dict(document)
                        events = dispatch.pop(True if True in dispatch else 'on', None)
                        self.assertEqual(events, {'workflow_dispatch': None})
                        self.assertEqual(dispatch, {
                            'name': operation.capitalize() + ' stage', 'permissions': {},
                            'jobs': {operation: {
                                'uses': SOURCE + 'shared-stage.yml@main',
                                'with': {'operation': operation},
                                'secrets': {'STAGE_DISPATCH_TOKEN': '${{ secrets.STAGE_DISPATCH_TOKEN }}'},
                            }},
                        })
                        continue
                    if path.name in ('deploy.yml', 'rollback.yml') and modules[0] in DEPLOYMENT_MODULES:
                        operation = path.stem
                        dispatch = dict(document)
                        # PyYAML's default loader parses an unquoted "on" as True.
                        events = dispatch.pop(True if True in dispatch else 'on', None)
                        self.assertEqual(events, {'workflow_dispatch': None})
                        self.assertEqual(dispatch, {
                            'name': operation.capitalize() + ' production', 'permissions': {},
                            'jobs': {operation: {
                                'uses': SOURCE + f'shared-production-{operation}.yml@main',
                                'secrets': {'DEPLOY_DISPATCH_TOKEN': '${{ secrets.DEPLOY_DISPATCH_TOKEN }}'},
                            }},
                        })
                        continue
                    pending = [document]
                    while pending:
                        item = pending.pop()
                        if isinstance(item, dict):
                            if 'uses' in item:
                                reference = item['uses']
                                self.assertIsInstance(reference, str, str(path))
                                if reference.lower().startswith(SOURCE):
                                    self.assertIn(reference, allowed_workflows, str(path))
                                self.assertFalse(reference.lower().startswith('actions/upload-artifact'), str(path))
                            pending.extend(item.values())
                        elif isinstance(item, list):
                            pending.extend(item)
                return manifest, workflow['jobs'], kinds
            self.assertEqual(workflow['permissions'], {'actions': 'read', 'contents': 'read'})
            self.assertEqual(set(workflow['jobs']), kinds | {kind + '-check' for kind in kinds} | {'private-inputs'})
            production_file = root / '.github/workflows/production-image.yml'
            enabled = workflow['jobs'].get('image-check', {}).get('with', {}).get('prepare_production', False)
            if production_file.is_file():
                self.assertEqual(enabled, PREPARE_PRODUCTION)
                self.assertEqual(manifest['profile'], 'service')
                self.assertIn(modules[0], DEPLOYMENT_MODULES)
                self.assertEqual(workflow.get('name'), PRODUCTION_TRIGGER['workflow_run']['workflows'][0])
                self.assertEqual(workflow['jobs']['image-check']['with']['service'], modules[0].rsplit('/', 1)[1])
                production = yaml.safe_load(production_file.read_text())
                self.assertEqual(production.get('on', production.get(True)), PRODUCTION_TRIGGER)
                self.assertEqual(production['permissions'], {'actions': 'read', 'contents': 'read'})
                self.assertEqual(production['jobs'], {'publish': {
                    'if': PUBLISH_CONDITION,
                    'uses': SOURCE + 'shared-production-image.yml@' + ref,
                    'permissions': {'actions': 'read', 'contents': 'read', 'packages': 'write'},
                    **({'with': revision_input} if pin else {}),
                }})
            else:
                self.assertIs(enabled, False)
            discover = workflow['jobs']['private-inputs']
            self.assertEqual(discover['if'].strip(), DISCOVERY_CONDITION)
            if pin:
                self.assertEqual(discover['outputs'], DISCOVERY_OUTPUTS)
                self.assertNotIn('permissions', discover)
                step, = discover['steps']
                self.assertEqual(step['id'], 'inputs')
                self.assertEqual(step['uses'], DISCOVERY_ACTION + ref)
                self.assertNotIn('if', step)
                discovery_inputs = dict(step['with'])
                bootstrap = discovery_inputs.pop('bootstrap-sha', None)
                if bootstrap is not None:
                    self.assertRegex(bootstrap, r'^[0-9a-f]{40}$')
                self.assertEqual(discovery_inputs, DISCOVERY_INPUTS)
            else:
                self.assertEqual(discover, {
                    'if': DISCOVERY_CONDITION,
                    'uses': SOURCE + 'shared-private-discovery.yml@' + MAIN,
                })
            prep = yaml.safe_load((root / '.github/workflows/private-ci-inputs.yml').read_text())
            prepare = prep['jobs']['prepare']
            prep_ref = prepare['uses'].rsplit('@', 1)[-1]
            # Accept the transitional producer spelling until all consumers migrate.
            self.assertIn(prep_ref, {ref, MAIN, 'refs/heads/main'} if pin else {MAIN})
            branch_prep = prep_ref != pin
            expected_permissions = {'contents': 'read', 'pull-requests': 'read'}
            if branch_prep:
                expected_permissions['actions'] = 'read'
            self.assertEqual(prep['permissions'], expected_permissions)
            inputs = prepare.get('with', {})
            prepare_inputs = {}
            if pin or 'allowed_module_repositories' in inputs:
                allowed = inputs['allowed_module_repositories']
                self.assertIsInstance(allowed, str)
                self.assertTrue(all(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', repo) for repo in allowed.split()))
                prepare_inputs['allowed_module_repositories'] = allowed
            if not branch_prep:
                prepare_inputs.update(revision_input)
            if manifest['profile'] == 'library':
                prepare_inputs['profile'] = 'library'
            qase90_inputs = ('qase90_bsd_revision', 'qase90_analytics_revision')
            if any(name in inputs for name in qase90_inputs):
                self.assertEqual(modules[0], 'github.com/fin-assist/bank-bff')
                for name in qase90_inputs:
                    revision = inputs.get(name, '')
                    self.assertRegex(revision, r'^[0-9a-f]{40}$')
                    prepare_inputs[name] = revision
            expected_prepare = {
                'if': (SAME_REPO if not branch_prep else
                           BRIDGE_PREPARE_CONDITION if pin else BRANCH_PREPARE_CONDITION),
                'uses': SOURCE + 'shared-private-inputs.yml@' + prep_ref,
                'secrets': {'GO_MODULES_TOKEN': '${{ secrets.GO_MODULES_TOKEN }}'},
            }
            if prepare_inputs:
                expected_prepare['with'] = prepare_inputs
            self.assertEqual(prep['jobs'], {'prepare': expected_prepare})
            events = prep.get('on', prep.get(True))
            expected_events = dict(PREPARE_EVENTS) if pin else {}
            if branch_prep:
                expected_events['workflow_run'] = {
                    'workflows': ['CI', 'Qase 90 cross-service'] if modules[0] == 'github.com/fin-assist/bank-bff' else ['CI'],
                    'types': ['in_progress'],
                }
            self.assertEqual(events, expected_events)
            return manifest, workflow['jobs'], kinds

        def test_all_calls_use_the_manifest_source_and_profile(self):
            manifest, jobs, kinds = self.context()
            pin = manifest.get('revision')
            ref = pin or MAIN
            if 'relay' in jobs:
                module = re.findall(r'(?m)^module\s+(\S+)\s*$', (root / 'go.mod').read_text())[0]
                self.service_quality(MAIN, None, module)
                return
            for kind in kinds:
                with self.subTest(call=kind):
                    call = jobs[kind + '-check']
                    self.assertEqual(call['uses'], SOURCE + 'shared-' + kind + '.yml@' + ref)
                    if pin:
                        self.assertEqual(call['with']['revision'], pin)
                    else:
                        self.assertNotIn('revision', call['with'])
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
                            self.service_quality(ref, pin, module)
                    expected_secrets = {} if kind == 'migrations' else {'GO_MODULES_TOKEN': PUSH_TOKEN}
                    self.assertEqual(call.get('secrets', {}), expected_secrets)

        def remote_facade(self, gate, kind, dependency='relay', name=None, condition='always()'):
            self.assertEqual(set(gate) - {'name'}, {
                'needs', 'if', 'permissions', 'runs-on', 'timeout-minutes', 'env', 'steps'})
            self.assertEqual(gate.get('name', kind), name or kind)
            self.assertEqual(gate['needs'], dependency)
            self.assertEqual(gate['if'], condition)
            self.assertEqual(gate['permissions'], {})
            self.assertEqual(gate['runs-on'], 'ubuntu-latest')
            self.assertEqual(gate['timeout-minutes'], 2)
            self.assertEqual(gate['env'], {
                'RELAY_RESULT': '${{ needs.' + dependency + '.result }}',
                'ACCEPTED': '${{ needs.' + dependency + '.outputs.accepted }}',
                'SOURCE_RUN_ATTEMPT': '${{ needs.' + dependency + '.outputs.source_run_attempt }}',
                'EXPECTED_RUN_ATTEMPT': '${{ github.run_attempt }}',
                'RESULT': '${{ needs.' + dependency + '.outputs.' + kind + ' }}',
            })
            self.assertEqual(len(gate['steps']), 1)
            step = gate['steps'][0]
            self.assertEqual(set(step) - {'name'}, {'run'})
            self.assertEqual(step['run'].strip(), RELAY_RUN)
            # Execute the actual shell gate; stale successful receipts must stay red.
            accepted_env = {'RELAY_RESULT': 'success', 'ACCEPTED': 'true',
                            'SOURCE_RUN_ATTEMPT': '2', 'EXPECTED_RUN_ATTEMPT': '2', 'RESULT': 'success'}
            cases = [({}, True), ({'SOURCE_RUN_ATTEMPT': '1', 'EXPECTED_RUN_ATTEMPT': '1'}, True)]
            for key, values in {
                'RELAY_RESULT': ('failure', 'skipped', 'cancelled', ''),
                'ACCEPTED': ('false', '', 'True', 'success'),
                'SOURCE_RUN_ATTEMPT': ('1', '3', '', '02'),
                'RESULT': ('failure', 'skipped', 'cancelled', ''),
            }.items():
                cases.extend(({key: value}, False) for value in values)
            for changed, allowed in cases:
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', step['run']],
                    env={**os.environ, **accepted_env, **changed}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, allowed, changed)

        def qase90(self):
            workflow = yaml.safe_load((root / '.github/workflows/qase90.yml').read_text())
            self.assertEqual(set(workflow) - {'on', True}, {'name', 'permissions', 'jobs'})
            self.assertEqual(workflow['name'], 'Qase 90 cross-service')
            self.assertEqual(workflow.get('on', workflow.get(True)), {
                'pull_request': None, 'push': {'branches': ['main']}})
            self.assertEqual(workflow['permissions'], {'contents': 'read'})
            self.assertEqual(set(workflow['jobs']), {'qase90-check', 'qase90'})
            call = dict(workflow['jobs']['qase90-check'])
            self.assertEqual(' '.join(call.pop('if').split()), RELAY_CONDITION)
            inputs = call.pop('with')
            self.assertEqual(set(inputs), {'mode', 'qase90_revisions'})
            self.assertEqual(inputs['mode'], 'qase90')
            revisions = json.loads(inputs['qase90_revisions'])
            self.assertEqual(set(revisions), {'bsd', 'analytics'})
            for revision in revisions.values():
                self.assertRegex(revision, r'^[0-9a-f]{40}$')
            self.assertEqual(call, {
                'uses': SOURCE + 'shared-remote-ci.yml@main',
                'secrets': {'CI_BROKER_TOKEN': '${{ secrets.RENOVATE_POLICY_TOKEN }}',
                            'GO_MODULES_TOKEN': '${{ secrets.GO_MODULES_TOKEN }}'},
            })
            self.remote_facade(workflow['jobs']['qase90'], 'qase90', dependency='qase90-check',
                               name='Qase 90 cross-service')

        def test_required_facades_reject_every_non_success_result(self):
            _, jobs, kinds = self.context()
            for kind in kinds:
                with self.subTest(gate=kind):
                    gate = jobs[kind]
                    condition = "always() && github.event_name == 'pull_request'" if kind == 'migrations' else 'always()'
                    if 'relay' in jobs:
                        self.remote_facade(gate, kind, condition=condition)
                        continue
                    self.assertEqual(gate.get('name', kind), kind)
                    self.assertEqual(gate['needs'], kind + '-check')
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

        def test_legacy_pinned_bootstrap_matches_its_source(self):
            if 'revision' in json.loads((root / '.finassist-ci.json').read_text()):
                self.assertEqual((root / 'scripts/common_ci.py').read_bytes(), (common / 'bootstrap.py').read_bytes())

    return unittest.defaultTestLoader.loadTestsFromTestCase(ConsumerContract)
