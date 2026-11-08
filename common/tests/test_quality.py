"""Positive and negative Go suite/coverage contracts, with actual compiler/analyzer fixtures."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('shared_quality', Path(__file__).parents[1] / 'scripts/quality.py')
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)
PASS = {'Action': 'pass', 'Test': 'TestUnitRule'}
PROFILE = 'mode: atomic\nexample.com/quality/rule.go:3.1,3.20 1 0\n'


class QualityFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest('service')

    def manifest(self, profile):
        (self.root / '.finassist-ci.json').write_text(json.dumps({'revision': 'a' * 40, 'profile': profile}))

    def evidence(self, events=None, profile=PROFILE, level='unit'):
        events = [PASS] if events is None else events
        (self.root / (level + '.jsonl')).write_text(''.join(json.dumps(e) + '\n' for e in events))
        path = self.root / (level + '.out')
        path.unlink(missing_ok=True)
        if profile is not None:
            path.write_text(profile)


class QualityEvidenceTests(QualityFixture):
    def test_real_test_pass_with_package_skip_and_zero_hit_statements_is_valid(self):
        self.evidence([PASS, {'Action': 'skip', 'Package': 'example.com/no-tests'}])
        for profile in ('service', 'library'):
            self.assertEqual(QUALITY.validate(self.root, profile, 'unit'), 'statements')

    def test_missing_execution_or_failed_skipped_test_never_reaches_analyzer(self):
        streams = [[], [{'Action': 'pass', 'Package': 'example.com/quality'}],
                   [PASS, {'Action': 'fail', 'Package': 'example.com/quality'}],
                   [PASS, {'Action': 'skip', 'Test': 'TestUnitDatabase'}],
                   [PASS, {'Action': 'fail', 'Test': 'TestUnitRule'}],
                   [{'Action': 'pass', 'Test': ''}], [None], [[]]]
        for profile in ('service', 'library'):
            for events in streams:
                with self.subTest(profile=profile, events=events):
                    self.evidence(events)
                    with patch.object(QUALITY, 'analyze') as analyzer:
                        with self.assertRaises((QUALITY.Failed, ValueError)):
                            QUALITY.check(self.root, profile, 'unit')
                        analyzer.assert_not_called()

    def test_malformed_json_and_corrupt_profiles_fail_closed_for_both_profiles(self):
        for profile in ('service', 'library'):
            for data in [None, '', 'mode: atomic\n', 'mode: count\n',
                         'mode: atomic\nexample.com/quality/rule.go:3.1,3.20 0 1\n',
                         'mode: atomic\ninvalid\n', PROFILE + 'invalid\n']:
                with self.subTest(profile=profile, data=data):
                    self.evidence(profile=data)
                    with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'fin-assist/fork'}):
                        with self.assertRaises((QUALITY.Failed, OSError)):
                            QUALITY.validate(self.root, profile, 'unit')
            for malformed in ('not JSON\n', '{"Action":"fail","Action":"pass","Test":"TestUnitRule"}\n',
                              '{"Action":"pass","Test":"TestUnitRule","bad":NaN}\n'):
                self.evidence()
                (self.root / 'unit.jsonl').write_text(malformed)
                with self.assertRaises(ValueError):
                    QUALITY.validate(self.root, profile, 'unit')

    def test_original_template_na_requires_exact_identity_profile_and_successful_evidence(self):
        for profile in ('service', 'library'):
            for repo in ('fin-assist/go-library-template', 'fork/go-library-template', 'fin-assist/go-worker', ''):
                with self.subTest(profile=profile, repo=repo):
                    self.evidence(profile='mode: atomic\n')
                    with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': repo}):
                        if profile == 'library' and repo == 'fin-assist/go-library-template':
                            self.assertEqual(QUALITY.validate(self.root, profile, 'unit'), 'N/A')
                            self.evidence([PASS, {'Action': 'skip', 'Test': 'TestUnitSkipped'}], 'mode: atomic\n')
                            with self.assertRaises(QUALITY.Failed):
                                QUALITY.validate(self.root, profile, 'unit')
                        else:
                            with self.assertRaises(QUALITY.Failed):
                                QUALITY.validate(self.root, profile, 'unit')

    def test_original_template_na_does_not_accept_near_empty_profile_bytes(self):
        for data in (b'mode: atomic\r\n', b'mode: atomic\n\n', b'mode: atomic\n '):
            self.evidence()
            (self.root / 'unit.out').write_bytes(data)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': QUALITY.ORIGINAL_TEMPLATE}):
                with self.assertRaises(QUALITY.Failed):
                    QUALITY.validate(self.root, 'library', 'unit')

    def test_local_template_na_checks_actual_git_origin_not_module_name(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True, timeout=10)
        subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/fin-assist/fork.git'],
                       cwd=self.root, check=True, timeout=10)
        self.evidence(profile='mode: atomic\n')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaises(QUALITY.Failed):
                QUALITY.validate(self.root, 'library', 'unit')
            for origin in QUALITY.ORIGINAL_ORIGINS:
                subprocess.run(['git', 'remote', 'set-url', 'origin', origin], cwd=self.root, check=True, timeout=10)
                self.assertEqual(QUALITY.validate(self.root, 'library', 'unit'), 'N/A')

    def test_combined_validates_all_inputs_without_compensating_for_invalid_suite(self):
        for level in QUALITY.LEVELS:
            self.evidence(level=level)
        for profile in ('service', 'library'):
            with patch.object(QUALITY, 'analyze') as analyzer:
                QUALITY.combined(self.root, profile)
                self.assertEqual(analyzer.call_args.args[1], ['unit.out', 'integration.out', 'api.out'])
                self.assertIs(analyzer.call_args.kwargs['threshold'], False)
            self.evidence([PASS, {'Action': 'fail'}], level='api')
            with patch.object(QUALITY, 'analyze') as analyzer:
                with self.assertRaises(QUALITY.Failed):
                    QUALITY.combined(self.root, profile)
                analyzer.assert_not_called()
            self.evidence(level='api')

    def test_template_combined_na_is_only_for_three_valid_empty_profiles(self):
        for level in QUALITY.LEVELS:
            self.evidence(profile='mode: atomic\n', level=level)
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': QUALITY.ORIGINAL_TEMPLATE}):
            with patch.object(QUALITY, 'analyze') as analyzer:
                QUALITY.combined(self.root, 'library')
                analyzer.assert_not_called()
            self.assertIn('N/A:', (self.root / 'combined.report.txt').read_text())
            self.evidence(level='api')
            with self.assertRaisesRegex(QUALITY.Failed, 'disagree'):
                QUALITY.combined(self.root, 'library')
            self.assertFalse((self.root / 'combined.report.txt').exists())


class QualityCommandsTests(QualityFixture):
    def test_collectors_keep_race_count_timeout_ci_denominator_and_suite_selectors(self):
        for profile in ('service', 'library'):
            for level in QUALITY.LEVELS:
                with self.subTest(profile=profile, level=level):
                    def invoke(root, command, **kwargs):
                        self.command, self.kwargs = command, kwargs
                        self.evidence(level=level)
                    packages = 'example.com/quality\nexample.com/quality/internal/pgtest\nexample.com/quality/nested/internal/pgtest'
                    with patch.object(QUALITY, 'test_names') as inventory, patch.object(QUALITY, 'run', side_effect=invoke), patch.object(QUALITY, 'capture', side_effect=['example.com/quality', packages]):
                        QUALITY.collect(self.root, profile, level)
                    inventory.assert_called_once_with(self.root, profile)
                    for flag in ('-race', '-count=1', '-timeout=8m', '-coverprofile=' + level + '.out'):
                        self.assertIn(flag, self.command)
                    self.assertEqual(self.command[-1], './...')
                    self.assertEqual(self.kwargs['env']['CI'], 'true')
                    self.assertEqual(self.kwargs['timeout'], 45 * 60)
                    expected = './...' if profile == 'service' else 'example.com/quality,example.com/quality/nested/internal/pgtest'
                    self.assertIn('-coverpkg=' + expected, self.command)
                    selector = self.command[self.command.index('-run') + 1]
                    expected = '^(Test|Fuzz)' + {'unit': 'Unit', 'integration': 'Integration', 'api': 'API'}[level]
                    if profile == 'library' and level == 'api':
                        expected = '^((Test|Fuzz)API|Example)'
                    self.assertEqual(selector, expected)

    def test_failed_inventory_removes_stale_collect_artifacts_before_any_run(self):
        for suffix in ('.out', '.jsonl', '.report.txt', '.coverage.txt'):
            (self.root / ('unit' + suffix)).write_text('stale')
        with patch.object(QUALITY, 'test_names', side_effect=QUALITY.Failed('inventory')), patch.object(QUALITY, 'run') as run:
            with self.assertRaises(QUALITY.Failed):
                QUALITY.collect(self.root, 'service', 'unit')
            run.assert_not_called()
        self.assertFalse(list(self.root.glob('unit.*')))

    def test_analyzer_is_official_pinned_separate_toolchain_and_propagates_failure(self):
        self.evidence()
        with patch.dict(os.environ, {'GOTOOLCHAIN': 'local'}), patch.object(QUALITY, 'run') as invoke:
            QUALITY.check(self.root, 'service', 'unit')
        command = invoke.call_args.args[1]
        self.assertEqual(command[:3], ['go', 'run', 'github.com/vladopajic/go-test-coverage/v2@v2.20.1'])
        self.assertEqual(command[command.index('--config') + 1], '.testcoverage.yml')
        self.assertEqual(invoke.call_args.kwargs['env']['GOTOOLCHAIN'], 'auto')
        with patch.object(QUALITY, 'run', side_effect=QUALITY.Failed('threshold', 9)):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(QUALITY.main(['--root', str(self.root), 'check', 'unit']), 9)

    def test_cli_requires_explicit_root_valid_profile_level_and_safe_timeout(self):
        for args in ([], ['collect', 'unit'], ['--root', str(self.root), 'collect'],
                     ['--root', str(self.root), 'combined', 'unit'],
                     ['--root', str(self.root), 'test', '--test-timeout', '0m'],
                     ['--root', str(self.root), 'collect', 'unit', '--test-timeout', '2m'],
                     ['--root', str(self.root), 'test', '--test-timeout', '8m;true']):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    QUALITY.main(args)
        self.manifest('unknown')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(QUALITY.main(['--root', str(self.root), 'test-names']), 1)
        self.manifest('service')
        with patch.object(QUALITY, 'test_names'), patch.object(QUALITY, 'run') as invoke:
            self.assertEqual(QUALITY.main(['--root', str(self.root), 'test', '--test-timeout', '1m30s']), 0)
            self.assertIn('-timeout=1m30s', invoke.call_args.args[1])
            self.assertEqual(invoke.call_args.kwargs['timeout'], 45 * 60)
        with patch.object(QUALITY, 'test_names'), patch.object(QUALITY, 'run') as invoke:
            self.assertEqual(QUALITY.main(['--root', str(self.root), 'test', '--test-timeout', '1h']), 0)
            self.assertEqual(invoke.call_args.kwargs['timeout'], 3600 + 120)

    def test_inventory_allows_the_existing_job_budget_including_race_compilation(self):
        def invoke(root, command, **kwargs):
            kwargs['output'].write_text('TestUnitRule\n')
        with patch.object(QUALITY, 'run', side_effect=invoke) as process:
            QUALITY.test_names(self.root, 'service')
        self.assertEqual(process.call_args.kwargs['timeout'], 45 * 60)

    def test_sigterm_cancellation_terminates_descendant_which_ignores_term(self):
        lock, group = self.root / 'cancel.lock', self.root / 'group.pid'
        child = ('import fcntl,signal,time,sys;f=open(sys.argv[1],"w");'
                 'fcntl.flock(f,fcntl.LOCK_EX);signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                 'f.write("locked");f.flush();time.sleep(60)')
        parent = ('import subprocess,sys,time,os;open(sys.argv[3],"w").write(str(os.getpid()));'
                  'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(60)')
        launcher = ('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import quality;'
                    'quality.run(Path(sys.argv[2]),[sys.executable,"-c",*sys.argv[3:]],timeout=30)')
        runner = subprocess.Popen([sys.executable, '-B', '-c', launcher,
                                   str(Path(QUALITY.__file__).parent), str(self.root),
                                   parent, child, str(lock), str(group)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not lock.exists() or lock.read_text() != 'locked':
                if runner.poll() is not None or time.monotonic() >= deadline:
                    self.fail('Runner did not start the lock-holding descendant.')
                time.sleep(0.02)
            runner.send_signal(signal.SIGTERM)
            self.assertNotEqual(runner.wait(timeout=10), 0)
            # The descendant ignores TERM: only runner cleanup can release this lock.
            subprocess.run([sys.executable, '-c',
                            'import fcntl,sys;f=open(sys.argv[1]);fcntl.flock(f,fcntl.LOCK_EX)', str(lock)],
                           check=True, timeout=5)
        finally:
            if group.exists():
                try:
                    os.killpg(int(group.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=5)

    def test_process_timeout_terminates_descendant_which_ignores_term(self):
        lock = self.root / 'child.lock'
        child = 'import fcntl,signal,time,sys;f=open(sys.argv[1],"w");fcntl.flock(f,fcntl.LOCK_EX);signal.signal(signal.SIGTERM,signal.SIG_IGN);f.write("locked");f.flush();time.sleep(60)'
        parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(60)'
        with self.assertRaises(QUALITY.Failed) as error:
            QUALITY.run(self.root, [sys.executable, '-c', parent, child, str(lock)], timeout=1)
        self.assertEqual(error.exception.code, 124)
        self.assertEqual(lock.read_text(), 'locked')
        # Wait on the actual lock release, rather than scheduler timing after SIGKILL.
        subprocess.run([sys.executable, '-c',
                        'import fcntl,sys;f=open(sys.argv[1]);fcntl.flock(f,fcntl.LOCK_EX)', str(lock)],
                       check=True, timeout=5)


class RealGoQualityTests(QualityFixture):
    def module(self, profile):
        self.manifest(profile)
        (self.root / 'go.mod').write_text('module example.com/quality\n\ngo 1.26.0\n')
        (self.root / '.testcoverage.yml').write_text('threshold:\n  total: 80\n')
        (self.root / 'rule.go').write_text('package quality\nfunc Rule() int { return 1 }\n')
        (self.root / 'rule_test.go').write_text('''package quality
import "testing"
func TestUnitRule(t *testing.T) { if Rule()!=1 { t.Fatal("rule") } }
func TestIntegrationRule(t *testing.T) { if Rule()!=1 { t.Fatal("rule") } }
func TestAPIRule(t *testing.T) { if Rule()!=1 { t.Fatal("rule") } }
func FuzzUnitRule(f *testing.F) { f.Add("seed"); f.Fuzz(func(t *testing.T, _ string) { Rule() }) }
''')

    def test_actual_service_and_library_collect_check_and_combined(self):
        for profile in ('service', 'library'):
            with self.subTest(profile=profile):
                self.module(profile)
                for level in QUALITY.LEVELS:
                    with contextlib.redirect_stdout(io.StringIO()):
                        QUALITY.collect(self.root, profile, level)
                        QUALITY.check(self.root, profile, level)
                    self.assertIn('PASS', (self.root / (level + '.report.txt')).read_text())
                with contextlib.redirect_stdout(io.StringIO()):
                    QUALITY.combined(self.root, profile)
                self.assertRegex((self.root / 'combined.report.txt').read_text(), r'Total test coverage: 100(?:\.0)?%')
                self.assertIn('FuzzUnitRule', (self.root / 'unit.jsonl').read_text())
                self.assertNotIn('FuzzUnitRule', (self.root / 'api.jsonl').read_text())

    def test_race_only_unclassified_test_fuzz_and_service_example_block_collect(self):
        self.module('service')
        invalid = {
            'race_test.go': '//go:build race\n\npackage quality\nimport "testing"\nfunc TestRaceRule(t *testing.T) {}\n',
            'fuzz_test.go': 'package quality\nimport "testing"\nfunc FuzzUnknown(f *testing.F) { f.Add("x"); f.Fuzz(func(t *testing.T,_ string){}) }\n',
            'example_test.go': 'package quality\nimport "fmt"\nfunc Example() { fmt.Println(1)\n// Output: 1\n}\n',
        }
        for name, text in invalid.items():
            with self.subTest(name=name):
                (self.root / name).write_text(text)
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(QUALITY.Failed):
                        QUALITY.collect(self.root, 'service', 'unit')
                self.assertFalse((self.root / 'unit.jsonl').exists())
                (self.root / name).unlink()

    def test_actual_library_examples_run_in_api_while_service_rejects_them(self):
        self.module('library')
        (self.root / 'example_test.go').write_text('package quality\nimport "fmt"\nfunc ExampleRule() { fmt.Println(Rule())\n// Output: 1\n}\n')
        with contextlib.redirect_stdout(io.StringIO()):
            QUALITY.collect(self.root, 'library', 'api')
        self.assertIn('"Test":"ExampleRule"', (self.root / 'api.jsonl').read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(QUALITY.Failed, 'Examples'):
                QUALITY.test_names(self.root, 'service')

    def test_actual_missing_skipped_or_failed_suite_cannot_publish_coverage(self):
        self.module('service')
        for body in ('', 'func TestIntegrationSkip(t *testing.T) { t.Skip("database absent") }',
                     'func TestIntegrationFail(t *testing.T) { t.Fatal("failed") }'):
            with self.subTest(body=body):
                (self.root / 'rule_test.go').write_text('package quality\nimport "testing"\nfunc TestUnitRule(t *testing.T) { Rule() }\n' + body + '\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(QUALITY.Failed):
                        QUALITY.collect(self.root, 'service', 'integration')

    def test_actual_production_denominator_only_excludes_exact_library_pgtest(self):
        for profile in ('service', 'library'):
            with self.subTest(profile=profile):
                self.module(profile)
                for relative, package in [('internal/pgtest', 'pgtest'), ('internal/pgtest/child', 'child')]:
                    path = self.root / relative
                    path.mkdir(parents=True, exist_ok=True)
                    (path / 'helper.go').write_text('package ' + package + '\nfunc Helper() int { return 2 }\n')
                test = self.root / 'rule_test.go'
                test.write_text(test.read_text().replace('import "testing"',
                    'import ("testing"; _ "example.com/quality/internal/pgtest"; _ "example.com/quality/internal/pgtest/child")'))
                with contextlib.redirect_stdout(io.StringIO()):
                    QUALITY.collect(self.root, profile, 'unit')
                text = (self.root / 'unit.out').read_text()
                self.assertEqual('example.com/quality/internal/pgtest/helper.go' in text, profile == 'service')
                self.assertIn('example.com/quality/internal/pgtest/child/helper.go', text)

    def test_local_make_wrappers_execute_the_same_cli_with_an_explicit_consumer_root(self):
        self.module('service')
        (self.root / 'scripts').mkdir()
        common = Path(__file__).resolve().parents[1]
        (self.root / 'scripts/common_ci.py').write_text('print(' + repr(str(common)) + ')\n')
        recipe = 'common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" '
        text = 'collect-unit:\n\t@' + recipe + 'collect unit\ncheck-unit:\n\t@' + recipe + 'check unit\n'
        (self.root / 'Makefile').write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            QUALITY.run(self.root, ['make', 'collect-unit'], output=self.root / 'make-collect.log', timeout=120)
            QUALITY.run(self.root, ['make', 'check-unit'], output=self.root / 'make-check.log', timeout=180)
        self.assertEqual(QUALITY.validate(self.root, 'service', 'unit'), 'statements')
        self.assertIn('PASS', (self.root / 'unit.report.txt').read_text())

    def test_official_analyzer_agreed_80_percent_rounding_boundaries(self):
        self.module('service')
        (self.root / 'rule.go').write_text('package quality\nfunc Covered() { _ = 1 }\nfunc Uncovered() { _ = 2 }\n')
        for covered, missed, success in ((1600, 400, True), (1599, 401, True), (1598, 402, False), (0, 2000, False)):
            with self.subTest(covered=covered):
                self.evidence(profile='mode: atomic\n' + f'example.com/quality/rule.go:2.16,2.25 {covered} 1\n' + f'example.com/quality/rule.go:3.18,3.27 {missed} 0\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    if success:
                        QUALITY.check(self.root, 'service', 'unit')
                    else:
                        with self.assertRaises(QUALITY.Failed):
                            QUALITY.check(self.root, 'service', 'unit')


if __name__ == '__main__':
    unittest.main()
