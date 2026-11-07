"""Exercise the actual release workflow shell with a stateful GitHub CLI fake."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / 'common'
WORKFLOW = ROOT / '.github/workflows/shared-release.yml'


def workflow_script(name):
    workflow = WORKFLOW.read_text()
    step = re.search(r'^      - name: ' + re.escape(name)
                     + r'\n(.*?)(?=^      - |\Z)', workflow, re.M | re.S)
    if step is None:
        raise AssertionError(f'Missing workflow step: {name}')
    block = re.search(r'^        run: \|\n((?:          .*\n|\n)+)', step[1], re.M)
    if block:
        return textwrap.dedent(block[1])
    return re.search(r'^        run: (.+)$', step[1], re.M)[1]


FAKE_GH_COMMAND = 'gh() { "$FAKE_PYTHON" "$FAKE_GH_SCRIPT" "$@"; }\n'


FAKE_GH = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ['FAKE_GITHUB'])
state = json.loads((root / 'state.json').read_text())
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[1:3]
if command == ['release', 'view']:
    if state.get('read_error'):
        print(state['read_error'], file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if state.get('exists') else 1)
if command == ['release', 'create']:
    if state.get('concurrent'):
        state['exists'] = True
        state['notes'] = 'Published by another actor'
        (root / 'state.json').write_text(json.dumps(state))
    if state.get('exists'):
        print('HTTP 422: Release.tag_name already exists', file=sys.stderr)
        sys.exit(1)
    if state.get('create_error'):
        print(state['create_error'], file=sys.stderr)
        sys.exit(1)
    state['exists'] = True
    state['notes'] = Path(sys.argv[sys.argv.index('--notes-file') + 1]).read_text()
    (root / 'state.json').write_text(json.dumps(state))
    sys.exit(0)
print('Unexpected mutation: ' + ' '.join(sys.argv[1:]), file=sys.stderr)
sys.exit(1)
'''


class ReleasePublicationTests(unittest.TestCase):
    def publish(self, initial, attempts=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state.json').write_text(json.dumps(initial))
            (root / 'notes.md').write_text('Notes from the tagged CHANGELOG\n')
            gh = root / 'gh'
            gh.write_text(FAKE_GH)
            env = dict(os.environ, TAG='v0.1.1', FAKE_GITHUB=directory, FAKE_PYTHON=sys.executable, FAKE_GH_SCRIPT=str(gh),
                       PATH=directory + os.pathsep + os.environ['PATH'])
            results = [subprocess.run(
                ['/bin/bash', '-e', '-o', 'pipefail', '-c', FAKE_GH_COMMAND + workflow_script('GitHub release')],
                cwd=root, env=env, capture_output=True, text=True, timeout=10)
                for _ in range(attempts)]
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            return results, calls, json.loads((root / 'state.json').read_text())

    def test_existing_release_is_preserved_without_creation(self):
        initial = {'exists': True, 'notes': 'Owner notes', 'assets': ['archive.zip']}
        results, calls, state = self.publish(initial)
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        self.assertEqual([call[:2] for call in calls], [['release', 'view']])
        self.assertEqual(state, initial)

    def test_new_release_can_be_published_twice(self):
        results, calls, state = self.publish({'exists': False}, attempts=2)
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        self.assertEqual([call[:2] for call in calls],
                         [['release', 'view'], ['release', 'create'], ['release', 'view']])
        self.assertIn('--verify-tag', calls[1])
        self.assertEqual(state['notes'], 'Notes from the tagged CHANGELOG\n')

    def test_concurrent_publication_is_success_and_preserves_other_release(self):
        results, _, state = self.publish({'exists': False, 'concurrent': True})
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        self.assertEqual(state['notes'], 'Published by another actor')

    def test_api_errors_without_a_confirmed_release_still_fail(self):
        for error in ('HTTP 403: Resource not accessible', 'HTTP 500: Server error',
                      'Network connection failed'):
            with self.subTest(error=error):
                results, _, state = self.publish({'exists': False, 'read_error': error,
                                                  'create_error': error})
                self.assertNotEqual(results[0].returncode, 0)
                self.assertFalse(state['exists'])


class ReleaseValidationTests(unittest.TestCase):
    def validate_and_publish(self, *, off_main=False, changelog_tag='v0.1.1', tag='v0.1.1', with_module=True,
                             bad_format=None, missing_main=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True,
                                      capture_output=True, text=True, timeout=10).stdout.strip()

            git('init', '-b', 'main')
            git('config', 'user.name', 'Release Test')
            git('config', 'user.email', 'release-test@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            if with_module:
                (root / 'go.mod').write_text('module example.invalid/service\n')
            (root / 'CHANGELOG.md').write_text('# Changelog\n\n## [Unreleased]\n')
            git('add', '.')
            git('commit', '-m', 'Initial main')
            if off_main:
                git('checkout', '-b', 'unmerged')
            changelog = (
                '# Changelog\n\n## [Unreleased]\n\n'
                f'## [{changelog_tag}] — 2026-10-24\n\n### Исправлено\n\n- Tagged notes.\n')
            if bad_format == 'date':
                changelog = changelog.replace('2026-10-24', 'unknown')
            elif bad_format == 'empty':
                changelog = changelog.replace('### Исправлено\n\n- Tagged notes.\n', '')
            elif bad_format == 'duplicate':
                changelog += f'\n## [{changelog_tag}] — 2026-10-24\n\n- Duplicate.\n'
            (root / 'CHANGELOG.md').write_text(changelog)
            git('add', '.')
            git('commit', '-m', 'Prepare tagged release')
            git('tag', tag)
            if off_main:
                git('checkout', 'main')
            # The workflow's main SHA differs from the checked-out historical tag.
            (root / 'CHANGELOG.md').write_text(
                '# Changelog\n\n## [Unreleased]\n\n## [v0.1.2] — 2026-10-25\n')
            git('add', '.')
            git('commit', '-m', 'Advance main')
            main_sha = git('rev-parse', 'HEAD')
            git('remote', 'add', 'origin', directory)
            if not missing_main:
                # Model checkout fetch-depth:0 while its own credential is present.
                git('fetch', 'origin', '+refs/heads/main:refs/remotes/origin/main')
            git('checkout', '--detach', tag)
            source = root / '.finassist-ci/source/common/scripts'
            source.mkdir(parents=True)
            (source / 'changelog.py').write_text((COMMON / 'scripts/changelog.py').read_text())
            (root / 'state.json').write_text(json.dumps({'exists': True, 'notes': 'Owner notes'}))
            gh = root / 'gh'
            gh.write_text(FAKE_GH)
            env = dict(os.environ, TAG=tag, CI_REVISION='a'*40, GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main', GITHUB_WORKSPACE=str(root), GITHUB_SHA=main_sha, FAKE_GITHUB=directory, FAKE_PYTHON=sys.executable, FAKE_GH_SCRIPT=str(gh),
                       PATH=directory + os.pathsep + os.environ['PATH'])
            script = (FAKE_GH_COMMAND + workflow_script('Validate release invocation and immutable source') + '\n' + workflow_script('The tag is a release of this service') + '\n'
                      + workflow_script('GitHub release'))
            result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', script],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=15)
            calls = root / 'calls.jsonl'
            return result, calls.exists(), (root / 'notes.md').read_text() if (root / 'notes.md').exists() else ''

    def test_manual_run_uses_tagged_metadata_after_main_has_advanced(self):
        result, published, notes = self.validate_and_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(published)
        self.assertIn('Tagged notes.', notes)

    def test_missing_fetched_main_ref_never_reaches_publication(self):
        result, published, _ = self.validate_and_publish(missing_main=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(published)

    def test_existing_release_does_not_bypass_tag_validation(self):
        for case, expected in (
                ({'off_main': True}, 'does not point to a commit on main'),
                ({'changelog_tag': 'v0.1.0'}, 'The newest section of CHANGELOG.md is not'),
                ({'tag': 'v01.1.1'}, 'is not vX.Y.Z')):
            with self.subTest(case=case):
                result, published, _ = self.validate_and_publish(**case)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertFalse(published, 'Invalid tags must never reach the GitHub release step')

    def test_matching_version_with_invalid_changelog_never_reaches_publication(self):
        for malformed in ('date', 'empty', 'duplicate'):
            with self.subTest(malformed=malformed):
                result, published, _ = self.validate_and_publish(bad_format=malformed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('CHANGELOG.md:', result.stderr)
                self.assertFalse(published)

    def test_service_major_does_not_require_a_go_module_suffix(self):
        result, published, _ = self.validate_and_publish(tag='v2.1.1', changelog_tag='v2.1.1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(published)

    def test_service_scaffold_without_go_module_can_validate_release_metadata(self):
        result, published, _ = self.validate_and_publish(with_module=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(published)

class ReleaseContractTests(unittest.TestCase):
    def workflow(self):
        return yaml.safe_load(WORKFLOW.read_text())

    def test_only_typed_reusable_release_interface_and_separate_write_job(self):
        workflow = self.workflow()
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(set(trigger), {"workflow_call"})
        inputs = trigger["workflow_call"]["inputs"]
        self.assertEqual(set(inputs), {"revision", "tag"})
        self.assertTrue(all(value["type"] == "string" and value["required"] is True for value in inputs.values()))
        self.assertNotIn("secrets", trigger["workflow_call"])
        self.assertEqual(workflow["permissions"], {"contents": "write"})
        self.assertEqual(set(workflow["jobs"]), {"release"})
        job = workflow["jobs"]["release"]
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertIn("github.event_name == 'push'", job["if"])
        self.assertIn("github.event_name == 'workflow_dispatch'", job["if"])
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        steps = job["steps"]
        checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 2)
        for step in checkouts:
            self.assertRegex(step["uses"], r"^actions/checkout@[0-9a-f]{40}$")
            self.assertIs(step["with"]["persist-credentials"], False)
        self.assertEqual(checkouts[0]["with"]["ref"], "refs/tags/${{ inputs.tag }}")
        source = checkouts[1]["with"]
        self.assertEqual(source["repository"], "fin-assist/go-ci")
        self.assertEqual(source["ref"], "${{ inputs.revision }}")
        self.assertEqual(source["path"], ".finassist-ci/source")
        for step in steps:
            self.assertNotIn("${{", step.get("run", ""))
            if "GH_TOKEN" in step.get("env", {}):
                self.assertEqual(step["name"], "GitHub release")
        validation = workflow_script("The tag is a release of this service")
        self.assertNotIn("python3 scripts/", validation)
        self.assertIn('--root "$GITHUB_WORKSPACE"', validation)
        self.assertIn('git show-ref --verify --quiet refs/remotes/origin/main', validation)
        self.assertNotIn('git fetch', validation)
        self.assertNotIn('gh release edit', workflow_script("GitHub release"))

    def test_release_invocation_rejects_pr_wrong_ref_tag_or_revision(self):
        valid = {"TAG": "v0.1.1", "CI_REVISION": "a" * 40,
                 "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}
        changes = [{}, {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/tags/v0.1.1"},
                   {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_REF": "refs/heads/MAIN"},
                   {"TAG": "v01.1.1"}, {"TAG": "v1.2.3;echo injected"}, {"CI_REVISION": "main"},
                   {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"},
                   {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/tags/v0.1.2"}]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", workflow_script("Validate release invocation and immutable source")],
                                        env=os.environ | valid | change, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, index < 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
