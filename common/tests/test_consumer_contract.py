"""Mutations of real caller files must fail the shared consumer contract."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

COMMON = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('consumer_contract', COMMON / 'consumer_contract.py')
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
PIN = 'a' * 40


class ConsumerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / '.github/workflows').mkdir(parents=True)
        shutil.copyfile(COMMON / 'bootstrap.py', self.root / 'scripts/common_ci.py')

    def fixture(self, profile='service', module='github.com/fin-assist/user'):
        (self.root / 'go.mod').write_text('module ' + module + '\n')
        (self.root / '.finassist-ci.json').write_text(json.dumps({'revision': PIN, 'profile': profile}))
        kinds = {'go', 'lint', 'api' if profile == 'library' else 'image'}
        if module != 'github.com/fin-assist/bank-bff':
            kinds.add('migrations')
        jobs = {}
        for kind in kinds:
            call = {'uses': CONTRACT.SOURCE + 'shared-' + kind + '.yml@' + PIN,
                    'with': {'revision': PIN}}
            if kind == 'go':
                call['with']['profile'] = profile
            if kind != 'migrations':
                call['secrets'] = {'GO_MODULES_TOKEN': CONTRACT.PUSH_TOKEN}
            jobs[kind + '-check'] = call
            jobs[kind] = {'needs': kind + '-check',
                'if': "always() && github.event_name == 'pull_request'" if kind == 'migrations' else 'always()',
                'env': {'RESULT': '${{ needs.' + kind + '-check.result }}'},
                'runs-on': 'ubuntu-latest', 'timeout-minutes': 2,
                'steps': [{'run': 'test "$RESULT" = success'}]}
        return {'permissions': {'contents': 'read'}, 'jobs': jobs}

    def result(self, document):
        (self.root / '.github/workflows/ci.yml').write_text(yaml.safe_dump(document))
        stream = io.StringIO()
        return unittest.TextTestRunner(stream=stream).run(CONTRACT.suite(self.root, COMMON))

    def test_service_library_template_and_exact_bff_profiles_pass(self):
        for profile, module in [('service', 'github.com/fin-assist/user'),
                                ('library', 'github.com/fin-assist/go-library-template'),
                                ('library', 'github.com/fin-assist/go-postgres'),
                                ('service', 'github.com/fin-assist/bank-bff')]:
            with self.subTest(profile=profile, module=module):
                result = self.result(self.fixture(profile, module))
                self.assertTrue(result.wasSuccessful())
                self.assertEqual(result.testsRun, 3)

    def quality_make(self):
        text = ''
        for targets, command in [('test-names', 'test-names'),
                                 ('collect-unit collect-integration collect-api', 'collect "$(@:collect-%=%)"'),
                                 ('check-unit check-integration check-api', 'check "$(@:check-%=%)"'),
                                 ('cover-combined', 'combined')]:
            text += targets + ':\n\t@common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" ' + command + '\n\n'
        (self.root / 'Makefile').write_text(text)
        self.service_tooling()

    def service_tooling(self):
        inputs = {'revision': PIN, 'tag': '${{ inputs.tag || github.ref_name }}'}
        if 'module github.com/fin-assist/bank-bff\n' == (self.root / 'go.mod').read_text():
            inputs['checkout_version'] = 'v7'
        (self.root / '.github/workflows/release.yml').write_text(yaml.safe_dump({
            'on': {'push': {'tags': ['v*']}, 'workflow_dispatch': {
                'inputs': {'tag': {'required': True, 'type': 'string'}}}},
            'permissions': {'contents': 'write'}, 'jobs': {'release': {
                'uses': CONTRACT.SOURCE + 'shared-release.yml@' + PIN,
                'with': inputs}}}))
        for name in ('changelog', 'stage_artifact'):
            (self.root / 'scripts' / (name + '.py')).write_text('''from pathlib import Path
import runpy
import sys
sys.dont_write_bytecode = True
from common_ci import read_config, source_dir
root = Path(__file__).resolve().parent.parent
common = source_dir(root, read_config(root))
script = common / 'scripts/''' + name + '''.py'
sys.argv = [str(script), '--root', str(root), *sys.argv[1:]]
runpy.run_path(str(script), run_name='__main__')
''')

    def test_quality_runner_optin_only_accepts_six_explicit_services_and_matching_local_wrappers(self):
        for module in CONTRACT.QUALITY_MODULES:
            with self.subTest(module=module):
                doc = self.fixture('service', module)
                doc['jobs']['go-check']['with']['quality_runner'] = True
                self.quality_make()
                self.assertTrue(self.result(doc).wasSuccessful())
        for profile, module, optin in [('library', 'github.com/fin-assist/go-library-template', True),
                                       ('library', 'github.com/fin-assist/go-worker', True),
                                       ('service', 'example.com/fork', True),
                                       ('service', 'github.com/fin-assist/user', 'true'),
                                       ('service', 'github.com/fin-assist/user', 1),
                                       ('service', 'github.com/fin-assist/user', None)]:
            with self.subTest(profile=profile, module=module, optin=optin):
                doc = self.fixture(profile, module)
                doc['jobs']['go-check']['with']['quality_runner'] = optin
                self.assertFalse(self.result(doc).wasSuccessful())
        for module in ('go-library-template', 'go-identity', 'go-idempotency', 'go-outbox',
                       'go-tombstone', 'go-postgres', 'go-grpc', 'go-migrate', 'go-worker'):
            doc = self.fixture('library', 'github.com/fin-assist/' + module)
            doc['jobs']['go-check']['with']['quality_runner'] = False
            self.assertTrue(self.result(doc).wasSuccessful())

    def test_quality_optin_rejects_local_make_bypass_or_wrong_source_path(self):
        for mutation in ('make collect-unit', 'python3 scripts/quality.py', 'common/scripts/quality.py', 'true'):
            with self.subTest(mutation=mutation):
                doc = self.fixture()
                doc['jobs']['go-check']['with']['quality_runner'] = True
                self.quality_make()
                path = self.root / 'Makefile'
                text = path.read_text().replace('"$$common/scripts/quality.py"', mutation)
                path.write_text(text)
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_quality_optin_rejects_release_source_or_input_drift(self):
        for mutation in ('source-pin', 'source-path', 'revision', 'tag'):
            with self.subTest(mutation=mutation):
                doc = self.fixture()
                doc['jobs']['go-check']['with']['quality_runner'] = True
                self.quality_make()
                path = self.root / '.github/workflows/release.yml'
                release = yaml.safe_load(path.read_text())
                call = release['jobs']['release']
                if mutation == 'source-pin':
                    call['uses'] = call['uses'].replace(PIN, 'b' * 40)
                elif mutation == 'source-path':
                    call['uses'] = call['uses'].replace('shared-release', 'shared-go')
                elif mutation == 'revision':
                    call['with']['revision'] = 'b' * 40
                else:
                    call['with']['tag'] = '${{ github.sha }}'
                path.write_text(yaml.safe_dump(release))
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_quality_optin_rejects_release_trigger_permissions_or_secret_drift(self):
        for mutation in ('tag-trigger', 'manual-tag-input', 'permissions', 'secrets'):
            with self.subTest(mutation=mutation):
                doc = self.fixture()
                doc['jobs']['go-check']['with']['quality_runner'] = True
                self.quality_make()
                path = self.root / '.github/workflows/release.yml'
                release = yaml.safe_load(path.read_text())
                if mutation == 'tag-trigger':
                    release['on']['push'] = {'branches': ['main']}
                elif mutation == 'manual-tag-input':
                    release['on']['workflow_dispatch']['inputs'] = {}
                elif mutation == 'permissions':
                    release['permissions']['issues'] = 'write'
                else:
                    release['jobs']['release']['secrets'] = 'inherit'
                path.write_text(yaml.safe_dump(release))
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_quality_optin_rejects_wrong_root_missing_delegation_and_bytecode_writes(self):
        for name in ('changelog', 'stage_artifact'):
            for mutation in ('root', 'missing-root', 'source-path', 'no-main', 'bytecode'):
                with self.subTest(wrapper=name, mutation=mutation):
                    doc = self.fixture()
                    doc['jobs']['go-check']['with']['quality_runner'] = True
                    self.quality_make()
                    path = self.root / 'scripts' / (name + '.py')
                    text = path.read_text()
                    if mutation == 'root':
                        text = text.replace('Path(__file__).resolve().parent.parent', 'Path.cwd()')
                    elif mutation == 'missing-root':
                        text = text.replace("'--root', str(root), ", '')
                    elif mutation == 'source-path':
                        text = text.replace("/ 'scripts/" + name + ".py'", "/ 'scripts/other.py'")
                    elif mutation == 'no-main':
                        text = text.replace(", run_name='__main__'", '')
                    else:
                        text = text.replace('sys.dont_write_bytecode = True', 'sys.dont_write_bytecode = False')
                    path.write_text(text)
                    self.assertFalse(self.result(doc).wasSuccessful())

    def test_quality_optin_preserves_checkout_major_and_rejects_unknown_selector(self):
        for module, version in [('user', 'v7'), ('bank-bff', 'v4'), ('user', 'v5')]:
            with self.subTest(module=module, version=version):
                doc = self.fixture('service', 'github.com/fin-assist/' + module)
                doc['jobs']['go-check']['with']['quality_runner'] = True
                self.quality_make()
                path = self.root / '.github/workflows/release.yml'
                release = yaml.safe_load(path.read_text())
                release['jobs']['release']['with']['checkout_version'] = version
                path.write_text(yaml.safe_dump(release))
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_missing_call_wrong_uses_input_or_profile_fail(self):
        for change in ('missing', 'uses-pin', 'uses-path', 'input-pin', 'go-profile', 'secrets'):
            with self.subTest(change=change):
                doc = self.fixture()
                call = doc['jobs']['go-check']
                if change == 'missing': del doc['jobs']['lint-check']
                elif change == 'uses-pin': call['uses'] = call['uses'].replace(PIN, 'b' * 40)
                elif change == 'uses-path': call['uses'] = call['uses'].replace('shared-go', 'shared-api')
                elif change == 'input-pin': call['with']['revision'] = 'b' * 40
                elif change == 'go-profile': call['with']['profile'] = 'library'
                else: call['secrets'] = 'inherit'
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_facade_name_needs_expression_condition_and_bypass_mutations_fail(self):
        for change in ('name', 'needs', 'result', 'if', 'run', 'skip-step', 'ignore-failure'):
            with self.subTest(change=change):
                doc = self.fixture()
                gate = doc['jobs']['go']
                if change == 'name': gate['name'] = 'unrequired-check'
                elif change == 'needs': gate['needs'] = 'lint-check'
                elif change == 'result': gate['env']['RESULT'] = '${{ needs.lint-check.result }}'
                elif change == 'if': gate['if'] = 'success()'
                elif change == 'run': gate['steps'][0]['run'] = 'true'
                elif change == 'skip-step': gate['steps'][0]['if'] = 'false'
                else: gate['continue-on-error'] = True
                self.assertFalse(self.result(doc).wasSuccessful())

    def test_bff_exemption_does_not_apply_to_other_module(self):
        doc = self.fixture('service', 'github.com/fin-assist/user')
        del doc['jobs']['migrations']
        del doc['jobs']['migrations-check']
        self.assertFalse(self.result(doc).wasSuccessful())

    def test_modified_local_bootstrap_fails(self):
        doc = self.fixture('library', 'github.com/fin-assist/go-postgres')
        (self.root / 'scripts/common_ci.py').write_text('# changed local entrypoint\n')
        self.assertFalse(self.result(doc).wasSuccessful())

    def test_gate_really_executes_rejection_cases(self):
        doc = self.fixture()
        # A faulty shell that returns success for every outcome must be rejected too.
        with mock.patch.object(CONTRACT.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            self.assertFalse(self.result(doc).wasSuccessful())


if __name__ == '__main__':
    unittest.main()
