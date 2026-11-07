"""Boundary tests for immutable action pins, explicit data and privileged execution."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import Mock, patch

import renovate_policy
from paths import consumer_root

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
spec = importlib.util.spec_from_file_location('shared_runner', HERE / 'test.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ConsumerPinTests(unittest.TestCase):
    def fixture(self, root, go=False):
        (root / '.github/workflows').mkdir(parents=True)
        pin = 'a' * 40
        manifest = root / ('.finassist-ci.json' if go else '.github/renovate-source.json')
        manifest.write_text(json.dumps({'revision': pin, **({'profile': 'library'} if go else {})}))
        (root / 'scripts').mkdir()
        (root / 'scripts/common_ci.py').write_bytes((SOURCE / 'common/bootstrap.py').read_bytes())
        (root / '.github/renovate-policy.json').write_text(json.dumps({
            'controllerLogin': 'controller', 'requiredChecks': ['go', 'Renovate policy tests',
            'Renovate runtime contract'], 'ciChecks': ['go']}))
        (root / 'renovate.json').write_text('{"automerge":false}')
        for file, action in (('renovate-policy.yml', 'renovate-policy'),
                             ('renovate-policy-tests.yml', 'renovate-policy-tests')):
            (root / '.github/workflows' / file).write_text(
                'steps:\n  - name: Shared\n    uses: fin-assist/go-ci/.github/actions/' + action + '@' + pin + '\n')
        with (root / '.github/workflows/renovate-policy-tests.yml').open('a') as workflow:
            workflow.write('    with:\n      source-revision: ' + pin + '\n')
        return pin

    def test_go_and_special_consumers_require_matching_full_action_and_manifest_pin(self):
        for go in (False, True):
            with self.subTest(go=go), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pin = self.fixture(root, go)
                runner.contract(root, pin)
                for supplied in (None, 'b' * 40, 'main', 'a' * 12):
                    with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                        runner.contract(root, supplied)
                for filename in ('renovate-policy.yml', 'renovate-policy-tests.yml'):
                    path = root / '.github/workflows' / filename
                    original = path.read_text()
                    for bad in ('main', 'b' * 40, 'a' * 12):
                        path.write_text(original.replace(pin, bad))
                        with self.subTest(filename=filename, bad=bad), self.assertRaises(ValueError):
                            runner.contract(root, pin)
                    path.write_text(original)
                path = root / '.github/workflows/renovate-policy-tests.yml'
                path.write_text(path.read_text().replace('source-revision: ' + pin, 'source-revision: ' + 'b' * 40))
                with self.assertRaisesRegex(ValueError, 'source-revision'):
                    runner.contract(root, pin)

    def test_readonly_runner_refuses_mutation_credentials_before_reading_consumer_data(self):
        with patch.dict(os.environ, {'RENOVATE_POLICY_TOKEN': 'placeholder'}), \
                patch.object(Path, 'read_text') as read:
            with self.assertRaisesRegex(ValueError, 'Read-only'):
                runner.contract(Path('/missing'), 'a' * 40)
        read.assert_not_called()

    def test_duplicate_manifest_keys_extra_special_fields_and_changed_bootstrap_fail_closed(self):
        for go in (False, True):
            with self.subTest(go=go), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pin = self.fixture(root, go)
                manifest = root / ('.finassist-ci.json' if go else '.github/renovate-source.json')
                original = manifest.read_text()
                manifest.write_text(original.replace('{', '{"revision":"' + pin + '",', 1))
                with self.assertRaises(ValueError):
                    runner.contract(root, pin)
                manifest.write_text(original)
                if not go:
                    manifest.write_text(json.dumps({'revision': pin, 'command': 'untrusted'}))
                    with self.assertRaises(ValueError):
                        runner.contract(root, pin)
                    manifest.write_text(original)
                (root / 'scripts/common_ci.py').write_text('raise AssertionError("consumer code")')
                with self.assertRaisesRegex(ValueError, 'bootstrap differs'):
                    runner.contract(root, pin)


class ActionBoundaryTests(unittest.TestCase):
    def script(self):
        action = (SOURCE / '.github/actions/renovate-policy/action.yml').read_text()
        match = re.search(r'^      run: \|\n(.*)', action, re.M | re.S)
        self.assertIsNotNone(match)
        self.assertNotIn('working-directory:', action)
        self.assertIn('POLICY_ACTION_PATH: ${{ github.action_path }}', action)
        return textwrap.dedent(match[1])

    def test_disabled_and_wrong_trigger_never_execute_python_or_consumer_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'executed'
            python = root / 'python3'
            python.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
            python.chmod(0o755)
            for enabled, ref, event, expected in [
                    ('TRUE', 'refs/heads/main', 'schedule', 0),
                    ('false', 'refs/heads/main', 'workflow_run', 0),
                    ('true', 'refs/heads/renovate/x', 'workflow_run', 1),
                    ('true', 'refs/heads/main', 'pull_request', 1)]:
                env = {'PATH': str(root) + ':/usr/bin:/bin', 'RENOVATE_POLICY_ENABLED': enabled,
                       'GITHUB_REF': ref, 'GITHUB_EVENT_NAME': event}
                result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', self.script()],
                                        env=env, cwd=root, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(marker.exists())

    def test_enabled_action_invokes_only_isolated_reviewed_source_with_explicit_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'consumer-executed'
            (root / 'scripts').mkdir()
            (root / 'scripts/renovate_policy.py').write_text('raise AssertionError("consumer code")')
            log = root / 'arguments'
            for name, content in {
                'timeout': '#!/bin/sh\nshift\nexec "$@"\n',
                'python3': '#!/bin/sh\nprintf "%s\\n" "$@" > "' + str(log) + '"\n',
            }.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            env = {'PATH': str(root) + ':/usr/bin:/bin', 'RENOVATE_POLICY_ENABLED': 'true',
                   'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_run',
                   'POLICY_ACTION_PATH': str(SOURCE / '.github/actions/renovate-policy'),
                   'POLICY_ROOT': str(root)}
            result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', self.script()],
                                    env=env, cwd=root, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = log.read_text().splitlines()
            self.assertEqual(args[:2], ['-I', '-B'])
            self.assertEqual(Path(args[2]).resolve(), HERE / 'renovate_policy.py')
            self.assertEqual(args[3:], ['--root', str(root)])
            self.assertFalse(marker.exists())

    def test_enabled_cli_rejects_non_main_events_before_inputs_or_credentials(self):
        for ref, event in [('refs/heads/feature/x', 'workflow_run'), ('refs/heads/main', 'pull_request')]:
            with self.subTest(ref=ref, event=event), patch.dict(os.environ, {
                    'RENOVATE_POLICY_ENABLED': 'true', 'GITHUB_ACTIONS': 'true',
                    'GITHUB_REF': ref, 'GITHUB_EVENT_NAME': event}, clear=True), \
                    patch.object(sys, 'argv', ['controller']), patch.object(Path, 'read_text') as read:
                with self.assertRaisesRegex(ValueError, 'main-only'):
                    renovate_policy.main()
                read.assert_not_called()

    def test_json_symlinks_are_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outside').write_text('{"private":"fixture"}')
            (root / 'renovate-base.json').symlink_to(root / 'outside')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                renovate_policy.read_caller_data(root, 'renovate-base.json')


class LocalShimBoundaryTests(unittest.TestCase):
    @unittest.skipIf(consumer_root() == SOURCE, 'Source host invokes its local reviewed action without a shim')
    def test_local_shim_rejects_invalid_manifests_before_source_transport(self):
        # Exercise the actual read-only consumer shim in an isolated copy. Only
        # canonical JSON validation is real; transport and test dispatch are mocks.
        shim = (consumer_root() / 'scripts/test_renovate_shared.py').read_bytes()
        pin = 'a' * 40
        for go in (False, True):
            valid = {'revision': pin, **({'profile': 'library'} if go else {})}
            invalid = [json.dumps(dict(valid, revision=value)) for value in (
                '../outside-cache', '--upload-pack=untrusted', 'main', 'a' * 12, 'A' * 40,
                '', None, 123, True)]
            invalid += [json.dumps(valid).replace('{', '{"revision":"' + pin + '",', 1),
                        json.dumps(dict(valid, command='untrusted')), '[]']
            if not go:
                invalid.append(json.dumps(valid) + ' ' * 4097)
            for value in [json.dumps(valid), *invalid]:
                with self.subTest(go=go, manifest=value[:150]), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'scripts').mkdir()
                    (root / '.github').mkdir()
                    path = root / 'scripts/test_renovate_shared.py'
                    path.write_bytes(shim)
                    manifest = root / ('.finassist-ci.json' if go else '.github/renovate-source.json')
                    manifest.write_text(value)
                    helper = types.ModuleType('common_ci')
                    helper.read_config = runner.bootstrap.read_config
                    helper.unique_object = runner.bootstrap.unique_object
                    helper.source_dir = Mock(return_value=Path('/canonical/common'))
                    copied_spec = importlib.util.spec_from_file_location('copied_consumer_shim', path)
                    copied = importlib.util.module_from_spec(copied_spec)
                    with patch.dict(sys.modules, {'common_ci': helper}):
                        copied_spec.loader.exec_module(copied)
                    case = copied.SharedRenovate('test_pinned_policy_and_consumer_contract')
                    with patch.object(copied.subprocess, 'run') as dispatch:
                        if value == json.dumps(valid):
                            case.test_pinned_policy_and_consumer_contract()
                            helper.source_dir.assert_called_once()
                            self.assertEqual(helper.source_dir.call_args.args[1]['revision'], pin)
                            dispatch.assert_called_once()
                        else:
                            with self.assertRaises(ValueError):
                                case.test_pinned_policy_and_consumer_contract()
                            helper.source_dir.assert_not_called()
                            dispatch.assert_not_called()
