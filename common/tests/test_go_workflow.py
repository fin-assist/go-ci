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
    return [step for job in document(name)['jobs'].values() for step in job['steps']]


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
                self.assertFalse(call['inputs']['revision']['required'])
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
                fix = next(step for step in steps(workflow) if step.get('name') == 'Set the canonical go-ci cache origin')
                self.assertEqual(fix['if'], "github.event_name == 'push'")
                result = subprocess.run(['bash', '-e', '-c', fix['run']], cwd=root,
                                        env={**os.environ, 'SHARED_REVISION': revision, 'CI_REVISION': revision},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
            good = subprocess.run(validate, cwd=ROOT, capture_output=True, text=True, timeout=5)
            self.assertEqual(good.returncode, 0, good.stderr)

    def test_revision_and_profile_validation_rejects_floating_or_hostile_inputs(self):
        profile_check = named('shared-go.yml', 'Validate shared profile')['run']
        for profile in ['service', 'library']:
            self.assertEqual(run(profile_check, SHARED_PROFILE=profile).returncode, 0)
        for profile in ['', 'contracts', 'service; true', '$(exit 0)']:
            self.assertNotEqual(run(profile_check, SHARED_PROFILE=profile).returncode, 0)
        for workflow in ('shared-go.yml', 'shared-lint.yml', 'shared-api.yml', 'shared-migrations.yml'):
            step = named(workflow, 'Resolve the shared workflow revision')
            self.assertEqual(step['env']['RESOLVED_SHA'], '${{ inputs.revision || job.workflow_sha }}')
            self.assertEqual(named(workflow, 'Restore private CI inputs')['uses'], '$/.github/actions/restore-private-ci')
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'evidence'
                env = {'GITHUB_ENV': str(target), 'GITHUB_STEP_SUMMARY': str(Path(directory) / 'summary')}
                for revision in ('', 'main', 'a' * 39, '$(exit 0)'):
                    self.assertNotEqual(run(step['run'], RESOLVED_SHA=revision, **env).returncode, 0)
                self.assertFalse(target.exists())
                revision = 'a' * 40
                self.assertEqual(run(step['run'], RESOLVED_SHA=revision, **env).returncode, 0)
                self.assertIn('FINASSIST_CI_REVISION=' + revision, target.read_text())

    def test_api_checker_receives_its_required_pinned_analyzer(self):
        analyzer = document('shared-api.yml')['env']['GORELEASE']
        self.assertRegex(analyzer, r'^golang.org/x/exp/cmd/gorelease@v[0-9].+')

    def test_library_api_check_scopes_git_and_requires_prepared_inputs_and_report(self):
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
import base64
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
assert not git_header('https://example.org/fin-assist/go-identity.git/info/refs')
assert 'GITHUB_READ_TOKEN' not in os.environ and 'MODULES_TOKEN' not in os.environ
contracts = 'https://github.com/fin-assist/contracts.git/info/refs'
neighbor = 'https://github.com/fin-assist/go-identity-other.git/info/refs'
if os.environ['GITHUB_EVENT_NAME'] == 'pull_request':
    assert not git_header(contracts) and not git_header(neighbor)
    assert os.environ['GOPROXY'] == 'http://127.0.0.1:1234,https://proxy.golang.org'
    assert os.environ['GONOPROXY'] == 'github.com/fin-assist/go-identity'
    assert os.environ['GONOSUMDB'] == 'github.com/fin-assist/*'
else:
    expected = 'Authorization: Basic ' + base64.b64encode(b'x-access-token:trusted-fixture').decode()
    keys = [os.environ[f'GIT_CONFIG_KEY_{i}'] for i in range(int(os.environ['GIT_CONFIG_COUNT']))]
    assert keys == [f'http.{origin}.extraHeader', f'http.{origin}.git.extraHeader',
                    'http.followRedirects', 'http.https://github.com/fin-assist/.extraHeader']
    assert git_header(contracts) == expected
    assert git_header(neighbor) == expected
report = os.environ.get('FIXTURE_REPORT')
if report is not None:
    Path('gorelease.txt').write_text(report)
''')
            env = {**os.environ, 'GITHUB_EVENT_NAME': 'pull_request',
                   'GITHUB_REPOSITORY': repository, 'GITHUB_READ_TOKEN': 'fixture',
                   'GOPRIVATE': 'github.com/fin-assist/*', 'MODULES_TOKEN': '',
                   'RUNNER_TEMP': directory, 'SHARED_REVISION': revision}

            def check(report='# summary\n', *, stale_report=False, **options):
                (root / 'gorelease.txt').unlink(missing_ok=True)
                if stale_report:
                    (root / 'gorelease.txt').write_text('# summary\nold report\n')
                test_env = env | options
                if report is not None:
                    test_env['FIXTURE_REPORT'] = report
                return subprocess.run(['bash', '-euo', 'pipefail', '-c', step], cwd=root,
                                      env=test_env, capture_output=True, text=True, timeout=8)

            accepted = check()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            push = check(GITHUB_EVENT_NAME='push', MODULES_TOKEN='trusted-fixture')
            self.assertEqual(push.returncode, 0, push.stderr)
            for report in (None, 'incomplete report\n', '## errors in library\n# summary\n'):
                with self.subTest(report=report):
                    result = check(report)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('did not produce a complete compatibility report', result.stderr)
            self.assertIn('did not produce a complete compatibility report',
                          check(None, stale_report=True).stderr)
            git('tag', 'v1.3.0')
            self.assertIn('Release tags changed after preparation', check().stderr)
            git('tag', '-d', 'v1.3.0')
            baselines.write_text(f'v1.2.3 {"b" * 40}\n')
            self.assertIn('Release tags changed after preparation', check().stderr)
            baselines.unlink()
            self.assertIn('were not prepared', check().stderr)
            git('remote', 'set-url', 'origin', 'https://github.com/fin-assist/go-identity-other.git')
            self.assertIn('Unexpected repository origin', check().stderr)

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
                manifest.write_text('{"profile": "service"}')
                result = subprocess.run(
                    ['bash', '-euo', 'pipefail', '-c', validation], cwd=directory,
                    env={**os.environ, 'SHARED_REVISION': 'a' * 40},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_analyzers_can_select_their_toolchain_without_changing_test_compiler(self):
        script_tests = named('shared-go.yml', 'Test the scripts')
        self.assertEqual(script_tests['env']['GOTOOLCHAIN'], 'auto')
        check = named('shared-go.yml', 'Require 80 percent suite statements coverage')
        self.assertEqual(check['env']['GOTOOLCHAIN'], 'auto')
        collector = named('shared-go.yml', 'Collect suite statements coverage')
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

    def test_parallel_suites_are_isolated_and_cannot_cancel_each_other(self):
        jobs = document('shared-go.yml')['jobs']
        suite = jobs['suite']
        self.assertEqual(suite['strategy'], {'fail-fast': False,
                                            'matrix': {'suite': ['unit', 'integration', 'api']}})
        self.assertNotIn('needs', suite)
        self.assertNotIn('needs', jobs['checks'])
        self.assertEqual(jobs['check']['needs'], ['checks', 'suite'])
        self.assertEqual(suite['if'], "github.repository != 'fin-assist/bank-bff'")
        self.assertEqual(jobs['check']['if'], "always() && github.repository != 'fin-assist/bank-bff'")
        for job in jobs.values():
            self.assertEqual(job['runs-on'], 'ubuntu-latest')
            self.assertGreater(job['timeout-minutes'], 0)
            restore = next(step for step in job['steps'] if step.get('name') == 'Restore private CI inputs')
            self.assertEqual(restore['with']['revision'], '${{ env.SHARED_REVISION }}')
            self.assertEqual(restore['with']['run-id'], '${{ inputs.private_input_run_id }}')
            self.assertEqual(restore['with']['artifact-name'], '${{ inputs.private_input_artifact }}')
            self.assertNotIn('TEST_DATABASE_URL', job.get('env', {}))
            self.assertEqual(restore['if'], "github.event_name == 'pull_request'")
            resolve = next(step for step in job['steps'] if step.get('name') == 'Resolve the shared workflow revision')
            self.assertEqual(resolve['env']['RESOLVED_SHA'], '${{ inputs.revision || job.workflow_sha }}')
            source = next(step for step in job['steps'] if step.get('name') == 'Checkout private CI source on trusted pushes')
            self.assertEqual(source['if'], "github.event_name == 'push'")
            self.assertEqual(source['with']['ref'], '${{ env.SHARED_REVISION }}')
            self.assertEqual(source['with']['token'], "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}")
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(step['with']['persist-credentials'], False)
        self.assertNotIn('Test the scripts', [step.get('name') for step in suite['steps']])
        clean = named('shared-go.yml', 'Remove stale suite evidence')
        self.assertLess(suite['steps'].index(clean), next(index for index, step in enumerate(suite['steps'])
                                                        if step.get('name') == 'Restore private CI inputs'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in ('out', 'jsonl', 'coverage.txt', 'report.txt'):
                (root / ('unit.' + suffix)).write_text('stale evidence')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', clean['run']], cwd=root,
                                    env={**os.environ, 'SUITE': 'unit'}, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(list(root.iterdir()))

    def test_only_bank_bff_collects_three_suites_sequentially_in_the_existing_checks_job(self):
        jobs = document('shared-go.yml')['jobs']
        checks = jobs['checks']['steps']
        guard = "github.repository == 'fin-assist/bank-bff'"
        collect_ids = []
        previous = checks.index(named('shared-go.yml', 'CHANGELOG.md format'))
        for level in ('unit', 'integration', 'api'):
            collect = named('shared-go.yml', f'Collect {level} statements coverage')
            threshold = named('shared-go.yml', f'Require 80 percent {level} statements coverage')
            self.assertEqual(collect['if'], guard)
            self.assertEqual(threshold['if'], f"always() && {guard} && steps.collect_{level}.outcome == 'success'")
            self.assertIs(collect['continue-on-error'], True)
            self.assertIs(threshold['continue-on-error'], True)
            self.assertNotIn('GOTOOLCHAIN', collect.get('env', {}))
            self.assertEqual(threshold['env']['GOTOOLCHAIN'], 'auto')
            self.assertLess(previous, checks.index(collect))
            self.assertLess(checks.index(collect), checks.index(threshold))
            previous = checks.index(threshold)
            collect_ids.append(collect['id'])
        combined = named('shared-go.yml', 'bank-bff combined statements coverage (informational union)')
        self.assertEqual(combined['if'], ' && '.join(['always()', guard] +
                         [f"steps.{identifier}.outcome == 'success'" for identifier in collect_ids]))
        self.assertLess(previous, checks.index(combined))
        self.assertNotIn('needs', jobs['checks'])
        self.assertFalse(any(step.get('uses', '').startswith('actions/download-artifact@') for step in checks))
        summary = named('shared-go.yml', 'bank-bff coverage summary')
        upload = next(step for step in checks if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertEqual(summary['if'], 'always() && ' + guard)
        self.assertEqual(upload['if'], 'always() && ' + guard)
        parallel_upload = next(step for step in jobs['check']['steps']
                               if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertEqual(upload['with'], parallel_upload['with'])
        self.assertEqual(summary['run'], named('shared-go.yml', 'Coverage summary')['run'])

    def test_bank_bff_cleans_old_evidence_before_any_validation_or_collection(self):
        checks = document('shared-go.yml')['jobs']['checks']['steps']
        clean = named('shared-go.yml', 'Remove stale bank-bff coverage evidence')
        self.assertEqual(clean['if'], "github.repository == 'fin-assist/bank-bff'")
        checkout = next(step for step in checks if step.get('uses', '').startswith('actions/checkout@'))
        self.assertLess(checks.index(checkout), checks.index(clean))
        self.assertLess(checks.index(clean), checks.index(named('shared-go.yml', 'Match the local source and profile')))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for level in ('unit', 'integration', 'api', 'combined'):
                for suffix in ('out', 'jsonl', 'coverage.txt', 'report.txt'):
                    (root / f'{level}.{suffix}').write_text('stale evidence')
            (root / 'keep.go').write_text('package fixture\n')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', clean['run']], cwd=root,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([path.name for path in root.iterdir()], ['keep.go'])

    def test_every_collector_and_independent_gate_is_required(self):
        mappings = {
            'Require all three suites and checks': {
                'CHECKS': '${{ needs.checks.result }}',
                'CHECKS_ATTEMPT': '${{ needs.checks.outputs.run_attempt }}',
                'SUITES': '${{ needs.suite.result }}',
                'INPUTS': '${{ steps.coverage_inputs.outcome }}'},
            'Require the suite and its threshold': {
                'COLLECT': '${{ steps.collect.outcome }}',
                'THRESHOLD': '${{ steps.threshold.outcome }}'},
            'Require bank-bff suites and thresholds': {
                'COLLECT_UNIT': '${{ steps.collect_unit.outcome }}',
                'CHECK_UNIT': '${{ steps.check_unit.outcome }}',
                'COLLECT_INTEGRATION': '${{ steps.collect_integration.outcome }}',
                'CHECK_INTEGRATION': '${{ steps.check_integration.outcome }}',
                'COLLECT_API': '${{ steps.collect_api.outcome }}',
                'CHECK_API': '${{ steps.check_api.outcome }}'},
        }
        for title, mapping in mappings.items():
            gate = named('shared-go.yml', title)
            condition = "always() && github.repository == 'fin-assist/bank-bff'" if 'bank-bff' in title else 'always()'
            self.assertEqual(gate['if'], condition)
            self.assertEqual(gate['env'], mapping)
            outcomes = {key: 'success' for key in mapping if key != 'CHECKS_ATTEMPT'}
            attempt = {'CHECKS_ATTEMPT': '2', 'GITHUB_RUN_ATTEMPT': '2'}
            self.assertEqual(run(gate['run'], **outcomes, **attempt).returncode, 0)
            for key in outcomes:
                for rejected in ['', 'failure', 'skipped', 'cancelled']:
                    with self.subTest(gate=title, outcome=key, rejected=rejected):
                        self.assertNotEqual(run(gate['run'], **{**outcomes, key: rejected},
                                                **attempt).returncode, 0)
        collect = named('shared-go.yml', 'Collect suite statements coverage')
        check = named('shared-go.yml', 'Require 80 percent suite statements coverage')
        self.assertIs(collect['continue-on-error'], True)
        self.assertIs(check['continue-on-error'], True)
        self.assertEqual(check['if'], "always() && steps.collect.outcome == 'success'")
        for step in (collect, check):
            self.assertEqual(step['env']['SUITE'], '${{ matrix.suite }}')
        combined = named('shared-go.yml', 'Combined statements coverage (informational union)')
        self.assertEqual(combined['if'], "always() && steps.coverage_inputs.outcome == 'success'")

    def test_successful_checks_from_another_attempt_cannot_satisfy_the_gate(self):
        checks = document('shared-go.yml')['jobs']['checks']
        self.assertEqual(checks['outputs'], {'run_attempt': '${{ github.run_attempt }}'})
        gate = named('shared-go.yml', 'Require all three suites and checks')['run']
        outcomes = {'CHECKS': 'success', 'SUITES': 'success', 'INPUTS': 'success',
                    'GITHUB_RUN_ATTEMPT': '2'}
        for checks_attempt in ('', '1', '3'):
            with self.subTest(checks_attempt=checks_attempt):
                result = run(gate, **outcomes, CHECKS_ATTEMPT=checks_attempt)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Checks must run in this CI attempt', result.stderr)
        self.assertEqual(run(gate, **outcomes, CHECKS_ATTEMPT='2').returncode, 0)

    def test_current_attempt_artifacts_are_required_and_cannot_overwrite_code(self):
        upload = named('shared-go.yml', 'Upload suite coverage evidence')
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['name'],
                         'coverage-suite-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.suite }}')
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        self.assertEqual(upload['with']['path'].splitlines(),
                         ['${{ matrix.suite }}.' + suffix for suffix in ('out', 'jsonl', 'coverage.txt', 'report.txt')])
        download = named('shared-go.yml', "Download this attempt's suite evidence")
        self.assertEqual(download['if'], 'always()')
        self.assertEqual(download['with']['pattern'],
                         'coverage-suite-${{ github.run_id }}-${{ github.run_attempt }}-*')
        self.assertNotIn('run-id', download['with'])
        self.assertNotIn('merge-multiple', download['with'])
        command = named('shared-go.yml', 'Require and stage all three suite profiles')['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / 'temp' / 'finassist-coverage'
            artifacts.mkdir(parents=True)
            code = root / 'Makefile'
            code.write_text('original code')
            for level in ('unit', 'integration', 'api'):
                source = artifacts / ('coverage-suite-123-2-' + level)
                source.mkdir()
                for suffix in ('out', 'jsonl', 'coverage.txt', 'report.txt'):
                    (source / (level + '.' + suffix)).write_text(level + '.' + suffix)
                (source / 'Makefile').write_text('untrusted artifact code')
            env = {**os.environ, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                   'RUNNER_TEMP': str(root / 'temp')}
            def stage():
                return subprocess.run(['bash', '-euo', 'pipefail', '-c', command], cwd=root,
                                      env=env, capture_output=True, text=True, timeout=5)
            good = stage()
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(code.read_text(), 'original code')
            for level in ('unit', 'integration', 'api'):
                self.assertEqual((root / (level + '.out')).read_text(), level + '.out')
            missing = artifacts / 'coverage-suite-123-2-api'
            missing.rename(artifacts / 'coverage-suite-123-1-api')
            (root / 'combined.report.txt').write_text('stale combined')
            absent = stage()
            self.assertNotEqual(absent.returncode, 0)
            self.assertIn('Missing api evidence', absent.stderr)
            self.assertFalse((root / 'api.out').exists())
            self.assertFalse((root / 'combined.report.txt').exists())
            # Existing profiles from the checkout cannot replace missing current-attempt inputs.
            (root / 'api.out').write_text('stale profile')
            self.assertNotEqual(stage().returncode, 0)
            self.assertFalse((root / 'api.out').exists())
            (artifacts / 'coverage-suite-123-1-api').rename(missing)
            (missing / 'api.jsonl').write_text('')
            self.assertNotEqual(stage().returncode, 0)

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
            'repository': 'fin-assist/go-ci', 'ref': '${{ env.SHARED_REVISION }}',
            'path': '.finassist-ci/${{ env.SHARED_REVISION }}', 'persist-credentials': False,
            'token': "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}"})
        self.assertLess(steps('shared-go.yml').index(checkout),
                        steps('shared-go.yml').index(named('shared-go.yml', 'Access to private modules')))

    def test_quality_and_legacy_branches_execute_only_the_selected_runner_and_propagate_failure(self):
        self.assertNotIn('All tests belong to a suite', [step.get('name') for step in steps('shared-go.yml')])
        routes = [('Combined statements coverage (informational union)', ['combined'], ['cover-combined'], '')]
        routes.append(('bank-bff combined statements coverage (informational union)', ['combined'], ['cover-combined'], ''))
        for level in ['unit', 'integration', 'api']:
            routes += [('Collect suite statements coverage', ['collect', level], ['collect-' + level], level),
                       ('Require 80 percent suite statements coverage', ['check', level], ['check-' + level], level),
                       (f'Collect {level} statements coverage', ['collect', level], ['collect-' + level], ''),
                       (f'Require 80 percent {level} statements coverage', ['check', level], ['check-' + level], '')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for command in ['python3', 'make']:
                path = root / command
                path.write_text('#!/bin/sh\nprintf "%s\\n" "' + command + '" "$@"\nexit "${FIXTURE_EXIT:-0}"\n')
                path.chmod(0o755)
            for profile in ['service', 'library']:
                for title, shared_args, legacy_args, level in routes:
                    for optin in ['true', 'false']:
                        for status in ['0', '7']:
                            with self.subTest(profile=profile, step=title, optin=optin, status=status):
                                result = run(named('shared-go.yml', title)['run'],
                                             PATH=str(root) + os.pathsep + os.environ['PATH'],
                                             SHARED_QUALITY=optin, SHARED_PROFILE=profile, SHARED_REVISION='a'*40,
                                             GITHUB_WORKSPACE=str(root / 'consumer with spaces'), FIXTURE_EXIT=status, SUITE=level)
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
        upload = next(step for step in document('shared-go.yml')['jobs']['check']['steps']
                      if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        for evidence in ['unit.out', 'integration.out', 'api.out', '*.jsonl',
                         '*.coverage.txt', '*.report.txt']:
            self.assertIn(evidence, upload['with']['path'].splitlines())

    def test_summary_retains_available_reports_and_marks_each_missing_suite(self):
        command = named('shared-go.yml', 'Coverage summary')['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'unit.report.txt').write_text('Total test coverage: 81.0%')
            summary = root / 'summary.md'
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', command], cwd=root,
                                    env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary),
                                         'GITHUB_SERVER_URL': 'https://github.com',
                                         'GITHUB_REPOSITORY': 'fin-assist/fixture', 'GITHUB_RUN_ID': '123'},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = summary.read_text()
            for level in ('unit', 'integration', 'api', 'combined'):
                self.assertIn('## ' + level + ' coverage', report)
            self.assertIn('Total test coverage: 81.0%', report)
            self.assertEqual(report.count('Coverage unavailable:'), 3)

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
        checkout = next(step for step in steps('shared-api.yml')
                        if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertIn('python3 scripts/api_check.py', named('shared-api.yml', 'API changes')['run'])

    def test_node_is_opt_in_and_precedes_script_tests(self):
        workflow = document('shared-go.yml')
        call = workflow.get('on', workflow.get(True))['workflow_call']
        self.assertEqual(call['inputs']['setup_node'], {
            'description': "Install Node.js for the caller's script tests",
            'required': False, 'default': False, 'type': 'boolean'})
        go_steps = steps('shared-go.yml')
        node, = [step for step in go_steps if step.get('uses', '').startswith('actions/setup-node@')]
        self.assertEqual(node['if'], 'inputs.setup_node')
        self.assertEqual(node['with']['node-version'], 26)
        self.assertLess(go_steps.index(node), go_steps.index(named('shared-go.yml', 'Test the scripts')))

    def test_script_discovery_excludes_only_the_root_policy_shim_and_preserves_failures(self):
        command = named('shared-go.yml', 'Test the scripts')['run']
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / 'scripts'
            nested = scripts / 'nested'
            nested.mkdir(parents=True)
            (nested / '__init__.py').touch()
            (scripts / 'test_renovate_shared.py').write_text('raise RuntimeError("policy shim imported")\n')
            (scripts / 'testlib.py').write_text('raise RuntimeError("non-test helper imported")\n')
            passing = 'import unittest\nclass Fixture(unittest.TestCase):\n    def test_fixture(self):\n        pass\n'
            (nested / 'test_renovate_shared.py').write_text(passing)
            bootstrap = scripts / 'test_common_ci.py'
            bootstrap.write_text(passing)
            for expected, output in [(0, 'OK'), (1, 'fixture failure')]:
                if expected == 1:
                    bootstrap.write_text(passing.replace('pass', 'self.fail("fixture failure")'))
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', command],
                                        cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn('Ran 2 tests', result.stderr)
                self.assertIn(output, result.stderr)


if __name__ == '__main__':
    unittest.main()
