"""Execution and trust-boundary checks for reusable Go CI entrypoints."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'


def document(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def steps(name):
    return document(name)['jobs']['check']['steps']


def named(name, title):
    return next(step for step in steps(name) if step.get('name') == title)


def run(script, **environment):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          env={**os.environ, **environment}, capture_output=True,
                          text=True, timeout=5)


class SharedGoWorkflowTests(unittest.TestCase):
    def test_source_policy_is_gated_by_the_standalone_self_workflow(self):
        source = document('ci.yml')
        trigger = source.get('on', source.get(True))
        self.assertIn('pull_request', trigger)
        self.assertEqual(trigger['push']['branches'], ['main'])
        self.assertEqual(source['permissions'], {'contents': 'read'})
        job = source['jobs']['shared-source']
        self.assertGreater(job['timeout-minutes'], 0)
        commands = [step.get('run') for step in job['steps']]
        for required in ['make test', 'make probe', 'make lint']:
            self.assertIn(required, commands)
        setup_go = next(step for step in job['steps']
                        if step.get('uses', '').startswith('actions/setup-go@'))
        self.assertIn('go-version', setup_go['with'])
        self.assertNotIn('go-version-file', setup_go['with'])
        for step in steps('shared-go.yml'):
            self.assertNotIn("github.repository == 'fin-assist/go-ci'", step.get('if', ''))

    def test_entrypoints_are_read_only_bounded_reusable_jobs(self):
        for name in ['shared-go.yml', 'shared-api.yml', 'shared-lint.yml', 'shared-migrations.yml']:
            with self.subTest(workflow=name):
                workflow = document(name)
                trigger = workflow.get('on', workflow.get(True))
                call = trigger['workflow_call']
                self.assertTrue(call['inputs']['revision']['required'])
                self.assertEqual(call['inputs']['revision']['type'], 'string')
                if name == 'shared-migrations.yml':
                    self.assertNotIn('secrets', call)
                else:
                    self.assertFalse(call['secrets']['GO_MODULES_TOKEN']['required'])
                self.assertEqual(workflow['permissions'], {'actions': 'read', 'contents': 'read'})
                self.assertEqual(workflow['jobs']['check'].get('name', 'check'), 'check')
                self.assertGreater(workflow['jobs']['check']['timeout-minutes'], 0)
                for step in steps(name):
                    self.assertNotIn('${{', step.get('run', ''))
                    if step.get('uses', '').startswith('actions/checkout@'):
                        self.assertIs(step['with']['persist-credentials'], False)
                    if 'TOKEN' in step.get('env', {}):
                        self.assertEqual(step['env']['TOKEN'],
                            "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}")

    def test_migration_consumer_checkout_keeps_history(self):
        workflow_steps = steps('shared-migrations.yml')
        checkout = next(step for step in workflow_steps if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        restore = named('shared-migrations.yml', 'Restore private CI inputs')
        self.assertNotIn('fetch-depth', restore['with'])
        self.assertLess(workflow_steps.index(checkout), workflow_steps.index(restore))

    def test_trusted_push_normalizes_checkout_for_the_existing_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / 'source'
            (repository / 'common').mkdir(parents=True)
            (repository / 'common/render_lint.py').write_text('# pinned source fixture\n')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repository), *args], text=True, timeout=5).strip()

            git('init', '--quiet')
            git('add', 'common/render_lint.py')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', 'commit', '-qm', 'fixture')
            revision = git('rev-parse', 'HEAD')
            git('remote', 'add', 'origin', 'https://github.com/fin-assist/go-ci')
            cache = root / '.finassist-ci' / revision
            cache.parent.mkdir()
            repository.rename(cache)
            validate = ['python3', '-B', '-c',
                        'import sys; from pathlib import Path; from common.bootstrap import validate_cache; '
                        'validate_cache(Path(sys.argv[1]), sys.argv[2])', str(cache), revision]
            bad = subprocess.run(validate, cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertIn('unexpected source repository', bad.stderr)
            for workflow in ('shared-go.yml', 'shared-lint.yml', 'shared-api.yml', 'shared-image.yml'):
                definition = document(workflow)
                job, = definition['jobs'].values()
                fix = next(step for step in job['steps'] if step.get('name') == 'Set the canonical go-ci cache origin')
                self.assertEqual(fix['if'], "github.event_name == 'push'")
                result = subprocess.run(['bash', '-e', '-c', fix['run']], cwd=root,
                                        env={**os.environ, 'REVISION': revision},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
            good = subprocess.run(validate, cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertEqual(good.returncode, 0, good.stderr)

    def test_revision_and_profile_validation_rejects_floating_or_hostile_inputs(self):
        validation = named('shared-go.yml', 'Validate shared profile and revision')['run']
        for profile in ['service', 'library']:
            self.assertEqual(run(validation, SHARED_REVISION='a'*40,
                                 SHARED_PROFILE=profile).returncode, 0)
        for revision in ['', 'main', 'v1', 'a'*39, 'a'*41, 'G'*40, 'a'*39+';', '$(exit 0)']:
            with self.subTest(revision=revision):
                self.assertNotEqual(run(validation, SHARED_REVISION=revision,
                                        SHARED_PROFILE='service').returncode, 0)
        for profile in ['', 'contracts', 'service; true', '$(exit 0)']:
            with self.subTest(profile=profile):
                self.assertNotEqual(run(validation, SHARED_REVISION='a'*40,
                                        SHARED_PROFILE=profile).returncode, 0)

    def test_api_also_refuses_non_sha_revision(self):
        validation = named('shared-api.yml', 'Validate shared revision')['run']
        self.assertEqual(run(validation, SHARED_REVISION='0'*40).returncode, 0)
        for revision in ['', 'main', 'a'*39, 'a'*41, '$(exit 0)']:
            self.assertNotEqual(run(validation, SHARED_REVISION=revision).returncode, 0)

    def test_api_checker_receives_its_required_pinned_analyzer(self):
        analyzer = document('shared-api.yml')['env']['GORELEASE']
        self.assertRegex(analyzer, r'^golang.org/x/exp/cmd/gorelease@v[0-9].+')

    def test_library_pr_api_check_scopes_git_and_uses_prepared_proxy(self):
        revision = 'a' * 40
        repository = 'fin-assist/go-identity'
        origin = f'https://github.com/{repository}'
        step = named('shared-api.yml', 'API changes')['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args],
                                               text=True, timeout=5).strip()

            git('init', '--quiet')
            git('remote', 'add', 'origin', origin + '.git')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org',
                'commit', '--allow-empty', '-qm', 'fixture')
            git('tag', 'v1.2.3')
            prepared = root / 'finassist-private-ci'
            prepared.mkdir()
            baselines = prepared / 'library-baselines.txt'
            baselines.write_text(f'v1.2.3 {git("rev-parse", "HEAD")}\n')
            scripts = root / '.finassist-ci' / revision / 'common' / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'private_module_proxy.py').write_text(
                'import signal, sys\nfrom pathlib import Path\n'
                'Path(sys.argv[-1]).write_text("http://127.0.0.1:1234\\n")\n'
                'signal.pause()\n')
            (root / 'scripts').mkdir()
            (root / 'scripts' / 'api_check.py').write_text('''\
import os
from pathlib import Path
import subprocess

def git_header(url):
    result = subprocess.run(['git', 'config', '--get-urlmatch', 'http.extraHeader', url],
                            capture_output=True, text=True, timeout=2)
    return result.stdout.strip()

origin = 'https://github.com/fin-assist/go-identity'
for url in (origin + '/info/refs', origin + '.git/info/refs'):
    assert git_header(url) == os.environ['GIT_CONFIG_VALUE_0']
for url in ('https://github.com/fin-assist/contracts.git/info/refs',
            'https://github.com/fin-assist/go-identity-other.git/info/refs',
            'https://example.org/fin-assist/go-identity.git/info/refs'):
    assert not git_header(url)
assert 'GITHUB_READ_TOKEN' not in os.environ and 'MODULES_TOKEN' not in os.environ
assert os.environ['GOPROXY'] == 'http://127.0.0.1:1234,https://proxy.golang.org'
assert os.environ['GONOPROXY'] == 'github.com/fin-assist/go-identity'
assert os.environ['GONOSUMDB'] == 'github.com/fin-assist/*'
Path('gorelease.txt').write_text('# summary\\n')
''')
            env = {**os.environ, 'GITHUB_EVENT_NAME': 'pull_request',
                   'GITHUB_REPOSITORY': repository, 'GITHUB_READ_TOKEN': 'fixture',
                   'GOPRIVATE': 'github.com/fin-assist/*', 'MODULES_TOKEN': '',
                   'RUNNER_TEMP': directory, 'SHARED_REVISION': revision}

            def check():
                return subprocess.run(['bash', '-euo', 'pipefail', '-c', step], cwd=root,
                                      env=env, capture_output=True, text=True, timeout=8)

            accepted = check()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            baselines.write_text(f'v1.2.3 {"b" * 40}\n')
            self.assertIn('no longer matches', check().stderr)
            baselines.unlink()
            self.assertIn('were not prepared', check().stderr)

    def test_lint_and_migrations_execute_pin_and_manifest_validation(self):
        for name in ['shared-lint.yml', 'shared-migrations.yml']:
            validation = named(name, 'Validate pin')['run']
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / '.finassist-ci.json'
                manifest.write_text(json.dumps({'revision': 'a' * 40}))
                for revision in ['a' * 40, '', 'main', 'a' * 39, 'a' * 41,
                                 'G' * 40, 'a' * 39 + ';', '$(exit 0)', 'b' * 40]:
                    with self.subTest(workflow=name, revision=revision):
                        result = subprocess.run(
                            ['bash', '-euo', 'pipefail', '-c', validation], cwd=directory,
                            env={**os.environ, 'SHARED_REVISION': revision},
                            capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode == 0, revision == 'a' * 40,
                                         result.stderr)
                manifest.write_text('{}')
                result = subprocess.run(
                    ['bash', '-euo', 'pipefail', '-c', validation], cwd=directory,
                    env={**os.environ, 'SHARED_REVISION': 'a' * 40},
                    capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)

    def test_analyzers_can_select_their_toolchain_without_changing_test_compiler(self):
        script_tests = named('shared-go.yml', 'Test the scripts')
        self.assertEqual(script_tests['env']['GOTOOLCHAIN'], 'auto')
        for suite in ['unit', 'integration', 'api']:
            check = named('shared-go.yml', f'Require 80 percent {suite} statements coverage')
            self.assertEqual(check['env']['GOTOOLCHAIN'], 'auto')
            collector = named('shared-go.yml', f'Collect {suite} statements coverage')
            self.assertNotIn('GOTOOLCHAIN', collector.get('env', {}))
        api = named('shared-api.yml', 'API changes')
        self.assertEqual(api['env']['GOTOOLCHAIN'], 'auto')

    def test_manifest_and_call_cannot_drift_apart(self):
        validation = named('shared-go.yml', 'Match the local source and profile')['run']
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / '.finassist-ci.json'
            for profile, revision, accepted in [('service', 'a'*40, True),
                                                ('library', 'a'*40, False),
                                                ('service', 'b'*40, False)]:
                manifest.write_text(json.dumps({'revision': revision, 'profile': profile}))
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', validation],
                    cwd=directory, env={**os.environ, 'SHARED_REVISION': 'a'*40,
                                       'SHARED_PROFILE': 'service'},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_every_collector_and_independent_gate_is_required(self):
        gate = named('shared-go.yml', 'Require all three suites and thresholds')
        self.assertEqual(gate['if'], 'always()')
        outcomes = {key: 'success' for key in gate['env']}
        self.assertEqual(set(outcomes), {'COLLECT_UNIT', 'CHECK_UNIT', 'COLLECT_INTEGRATION',
                                        'CHECK_INTEGRATION', 'COLLECT_API', 'CHECK_API'})
        self.assertEqual(run(gate['run'], **outcomes).returncode, 0)
        for key in outcomes:
            for rejected in ['', 'failure', 'skipped', 'cancelled']:
                with self.subTest(outcome=key, rejected=rejected):
                    self.assertNotEqual(run(gate['run'], **{**outcomes, key: rejected}).returncode, 0)
        indexed = {step['id']: step for step in steps('shared-go.yml') if 'id' in step}
        for suite in ['unit', 'integration', 'api']:
            collect, check = indexed['collect_'+suite], indexed['check_'+suite]
            self.assertIn('make collect-'+suite, collect['run'])
            self.assertIn('--root "$GITHUB_WORKSPACE" collect '+suite, collect['run'])
            self.assertIs(collect['continue-on-error'], True)
            self.assertIn('make check-'+suite, check['run'])
            self.assertIn('--root "$GITHUB_WORKSPACE" check '+suite, check['run'])
            self.assertIs(check['continue-on-error'], True)
            self.assertEqual(check['if'], f"always() && steps.collect_{suite}.outcome == 'success'")

    def test_quality_runner_is_explicit_and_legacy_profiles_do_not_migrate_implicitly(self):
        workflow = document('shared-go.yml')
        call = workflow.get('on', workflow.get(True))['workflow_call']
        self.assertEqual(call['inputs']['quality_runner'], {
            'description': 'Explicitly use the shared test/coverage runner (FIN-457 service migration)',
            'required': False, 'default': False, 'type': 'boolean'})
        self.assertEqual(workflow['env']['SHARED_QUALITY'], '${{ inputs.quality_runner }}')
        checkout = named('shared-go.yml', 'Checkout private CI source on trusted pushes')
        self.assertEqual(checkout['if'], "github.event_name == 'push'")
        self.assertEqual(checkout['uses'], 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1')
        self.assertEqual(checkout['with'], {
            'repository': 'fin-assist/go-ci', 'ref': '${{ inputs.revision }}',
            'path': '.finassist-ci/${{ inputs.revision }}', 'persist-credentials': False,
            'token': "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}"})
        self.assertLess(steps('shared-go.yml').index(checkout),
                        steps('shared-go.yml').index(named('shared-go.yml', 'Access to private modules')))

    def test_quality_and_legacy_branches_execute_only_the_selected_runner_and_propagate_failure(self):
        routes = [('All tests belong to a suite', ['test-names'], ['test-names']),
                  ('Combined statements coverage (informational union)', ['combined'], ['cover-combined'])]
        for level in ['unit', 'integration', 'api']:
            routes += [(f'Collect {level} statements coverage', ['collect', level], ['collect-' + level]),
                       (f'Require 80 percent {level} statements coverage', ['check', level], ['check-' + level])]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for command in ['python3', 'make']:
                path = root / command
                path.write_text('#!/bin/sh\nprintf "%s\\n" "' + command + '" "$@"\nexit "${FIXTURE_EXIT:-0}"\n')
                path.chmod(0o755)
            for profile in ['service', 'library']:
                for title, shared_args, legacy_args in routes:
                    for optin in ['true', 'false']:
                        for status in ['0', '7']:
                            with self.subTest(profile=profile, step=title, optin=optin, status=status):
                                result = run(named('shared-go.yml', title)['run'],
                                             PATH=str(root) + os.pathsep + os.environ['PATH'],
                                             SHARED_QUALITY=optin, SHARED_PROFILE=profile, SHARED_REVISION='a'*40,
                                             GITHUB_WORKSPACE=str(root / 'consumer with spaces'), FIXTURE_EXIT=status)
                                expected = (['python3', '-B', f'.finassist-ci/{"a"*40}/common/scripts/quality.py',
                                             '--root', str(root / 'consumer with spaces')] + shared_args
                                            if optin == 'true' else ['make'] + legacy_args)
                                self.assertEqual(result.stdout.splitlines(), expected, result.stderr)
                                self.assertEqual(result.returncode, int(status))

    def test_coverage_reports_and_library_na_evidence_are_preserved(self):
        summary = named('shared-go.yml', 'Coverage summary')
        self.assertEqual(summary['if'], 'always()')
        self.assertIn('Only the original empty go-library-template reports N/A after successful suites',
                      summary['run'])
        upload = next(step for step in steps('shared-go.yml')
                      if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        for evidence in ['unit.out', 'integration.out', 'api.out', '*.jsonl',
                         '*.coverage.txt', '*.report.txt']:
            self.assertIn(evidence, upload['with']['path'].splitlines())

    def test_changelog_uses_pinned_parser_only_for_opted_in_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'python3'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit "${FIXTURE_EXIT:-0}"\n')
            python.chmod(0o755)
            for profile in ('service', 'library'):
                for optin in ('true', 'false'):
                    for status in ('0', '7'):
                        with self.subTest(profile=profile, optin=optin, status=status):
                            consumer = str(root / 'consumer with spaces')
                            result = run(named('shared-go.yml', 'CHANGELOG.md format')['run'],
                                         PATH=str(root) + os.pathsep + os.environ['PATH'],
                                         SHARED_PROFILE=profile, SHARED_QUALITY=optin, SHARED_REVISION='a'*40,
                                         GITHUB_WORKSPACE=consumer, FIXTURE_EXIT=status)
                            expected = (['-B', f'.finassist-ci/{"a"*40}/common/scripts/changelog.py',
                                         '--root', consumer, 'check'] if optin == 'true'
                                        else ['scripts/changelog.py', 'check'])
                            self.assertEqual(result.stdout.splitlines(), expected, result.stderr)
                            self.assertEqual(result.returncode, int(status))

    def test_caller_script_dependencies_precede_tests_and_api_keeps_release_history(self):
        go_steps = steps('shared-go.yml')
        dependency = named('shared-go.yml', 'Script dependencies')
        tests = named('shared-go.yml', 'Test the scripts')
        self.assertLess(go_steps.index(dependency), go_steps.index(tests))
        self.assertIn('import yaml', dependency['run'])
        self.assertTrue(any(step.get('uses', '').startswith('actions/setup-node@')
                            for step in go_steps))
        checkout = next(step for step in steps('shared-api.yml')
                        if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertIn('python3 scripts/api_check.py', named('shared-api.yml', 'API changes')['run'])


if __name__ == '__main__':
    unittest.main()
