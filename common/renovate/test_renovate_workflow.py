from paths import consumer_root, consumer_repository
"""Execute the workflow's real activation gate before privileged job admission."""
import ast
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


class WorkflowGateTests(unittest.TestCase):
    def gate_script(self):
        workflow = (consumer_root()
                    / '.github/workflows/renovate-policy.yml').read_text()
        gate = re.search(r'^  gate:\n(.*?)(?=^  reconcile:)', workflow, re.M | re.S)
        self.assertIsNotNone(gate, 'No independent gate protects Environment admission for TRUE')
        # The gate must not acquire privileges before it decides whether to run.
        self.assertNotIn('secrets.', gate[1])
        self.assertNotRegex(gate[1], re.compile(r'^    environment:', re.M))
        self.assertIn('    permissions: {}\n', gate[1])
        self.assertIn('RENOVATE_POLICY_ENABLED: ${{ vars.RENOVATE_POLICY_ENABLED }}', gate[1])
        self.assertIn('    needs: gate\n', workflow)
        self.assertIn("needs.gate.outputs.enabled == 'true'", workflow)
        run = re.search(r'^        run: \|\n((?:          .*\n|\n)+)', gate[1], re.M)
        self.assertIsNotNone(run, 'Workflow gate must contain an executable shell block')
        return textwrap.dedent(run[1])

    def assert_gate(self, value, expected, *, event="workflow_dispatch", now=None):
        script = self.gate_script()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = {'GITHUB_OUTPUT': str(output), 'EVENT_NAME': event}
            if now is not None:
                date = Path(directory) / 'date'
                date.write_text(f'#!/bin/sh\nprintf "%s\\n" {now}\n')
                date.chmod(0o700)
                env['PATH'] = directory
            if value is not None:
                env['RENOVATE_POLICY_ENABLED'] = value
            result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', script],
                                    env=env, cwd=directory, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), f'enabled={expected}\n')

    def test_uppercase_true_does_not_admit_privileged_job(self):
        self.assert_gate('TRUE', 'false')

    def test_only_exact_lowercase_true_admits_privileged_job(self):
        for value in (None, '', 'false', 'TRUE', 'True', 'true', '1'):
            with self.subTest(value=value):
                self.assert_gate(value, 'true' if value == 'true' else 'false')

    def test_value_is_data_and_cannot_inject_shell_commands_or_outputs(self):
        self.assert_gate('true\nenabled=true\n$(exit 1)', 'false')

    def test_event_gate_filters_renovate_pull_requests_and_keeps_pending_events(self):
        workflow = (consumer_root() / '.github/workflows/renovate-policy.yml').read_text()
        if consumer_repository() != 'fin-assist/go-ci' and "branches: ['renovate/**']" not in workflow:
            self.skipTest('Consumer has not yet migrated to the FIN-504 workflow')
        self.assertIn("    types: [completed]\n    branches: ['renovate/**']\n  schedule:", workflow)
        self.assertIn("    - cron: '17 6 * * 1'\n  workflow_dispatch:", workflow)
        self.assertIn('  group: renovate-policy-controller\n  queue: max', workflow)
        self.assertNotIn('cancel-in-progress: true', workflow)
        self.assertIn("github.event_name != 'workflow_run' ||", workflow)
        self.assertIn("github.event.workflow_run.event == 'pull_request' &&", workflow)
        self.assertIn('github.event.workflow_run.actor.id == 29139614 &&', workflow)
        self.assertIn('github.event.workflow_run.head_repository.full_name == github.repository', workflow)

    def test_gate_expression_evaluates_ci_and_reserve_events(self):
        workflow = (consumer_root() / '.github/workflows/renovate-policy.yml').read_text()
        if consumer_repository() != 'fin-assist/go-ci' and "branches: ['renovate/**']" not in workflow:
            self.skipTest('Consumer has not yet migrated to the FIN-504 workflow')
        expression = re.search(r'^  gate:\n    if: >-\n((?:      .+\n)+)', workflow, re.M)
        self.assertIsNotNone(expression)
        source = ' '.join(expression[1].splitlines()).strip()
        source = source.replace('&&', ' and ').replace('||', ' or ')
        condition = ast.parse(source, mode='eval').body

        def evaluate(node, context):
            # Consumer workflows are untrusted data. Interpret only the
            # boolean expression subset used by this gate.
            match node:
                case ast.Name(id='github'):
                    return context
                case ast.Name(id='true'):
                    return True
                case ast.Name(id='false'):
                    return False
                case ast.Attribute(value=value, attr=name):
                    return evaluate(value, context)[name]
                case ast.Constant(value=value) if type(value) in (int, str):
                    return value
                case ast.BoolOp(op=ast.And(), values=values):
                    return all(evaluate(value, context) for value in values)
                case ast.BoolOp(op=ast.Or(), values=values):
                    return any(evaluate(value, context) for value in values)
                case ast.Compare(left=left, ops=[ast.Eq()], comparators=[right]):
                    return evaluate(left, context) == evaluate(right, context)
                case ast.Compare(left=left, ops=[ast.NotEq()], comparators=[right]):
                    return evaluate(left, context) != evaluate(right, context)
                case _:
                    self.fail(f'Unsupported gate expression: {ast.dump(node)}')

        repo = 'fin-assist/go-ci'
        valid = {'event': 'pull_request', 'actor': {'id': 29139614},
                 'triggering_actor': {'id': 42}, 'head_repository': {'full_name': repo}}
        cases = [
            ('renovate retry', 'workflow_run', valid, 'refs/heads/main', True),
            ('other actor', 'workflow_run', dict(valid, actor={'id': 42}), 'refs/heads/main', False),
            ('fork', 'workflow_run', dict(valid, head_repository={'full_name': 'fork/go-ci'}), 'refs/heads/main', False),
            ('push', 'workflow_run', dict(valid, event='push'), 'refs/heads/main', False),
            ('schedule', 'schedule', None, 'refs/heads/main', True),
            ('manual', 'workflow_dispatch', None, 'refs/heads/main', True),
            ('other ref', 'workflow_dispatch', None, 'refs/heads/feature', False),
        ]
        for label, event, run, ref, expected in cases:
            with self.subTest(case=label):
                context = {'ref': ref, 'event_name': event, 'repository': repo, 'event': {'workflow_run': run}}
                self.assertIs(evaluate(condition, context), expected)

    def test_scheduled_reconcile_alternates_across_year_boundary(self):
        workflow = (consumer_root() / '.github/workflows/renovate-policy.yml').read_text()
        if "branches: ['renovate/**']" not in workflow:
            self.skipTest('Consumer has not yet migrated to the FIN-504 workflow')
        from datetime import datetime, timezone
        for year, month, day, expected in ((2026, 12, 21, 'true'),
                                            (2026, 12, 28, 'false'),
                                            (2027, 1, 4, 'true'),
                                            (2027, 1, 11, 'false')):
            now = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
            with self.subTest(day=day):
                self.assert_gate('true', expected, event='schedule', now=now)



@unittest.skipUnless(consumer_repository() == 'fin-assist/infra', 'Infra stage data only')
class InfraWorkflowContractTests(unittest.TestCase):
    def test_production_config_and_trigger_select_validation_not_deployment(self):
        import json
        root = consumer_root()
        config = json.loads((root / '.github/renovate-policy.json').read_text())
        self.assertEqual(config['ciWorkflow'], 'stage.yml')
        self.assertEqual(config['ciChecks'], ['stage'])
        self.assertEqual(config['requiredChecks'],
                         ['stage', 'Renovate policy tests', 'Renovate runtime contract'])
        workflow = (root / '.github/workflows/renovate-policy.yml').read_text()
        self.assertIn('workflows: [Stage configuration, Renovate policy tests]', workflow)
        self.assertNotIn('stage-deploy', workflow)
        self.assertNotRegex(workflow, r'(?m)^    environment: stage$')
        self.assertNotIn('STAGE_SSH', workflow)
        self.assertIn('          ref: main\n', workflow)
        self.assertIn('          persist-credentials: false\n', workflow)

    def test_baseline_preserves_existing_recommended_policy(self):
        import json
        root = consumer_root()
        baseline = json.loads((root / 'renovate-base.json').read_text())
        config = json.loads((root / 'renovate.json').read_text())
        expected_baseline = {'extends': ['config:recommended']}
        if 'schedule' in config or 'packageRules' in baseline:
            self.assertEqual(config.get('baseBranchPatterns'), ['main'])
            self.assertEqual(config.get('schedule'), ['* 0-8 * * 1'])
            self.assertEqual(config.get('timezone'), 'Europe/Moscow')
            self.assertIs(config.get('updateNotScheduled'), False)
            expected_baseline['packageRules'] = [{
                'description': 'FIN-504: batch ordinary updates in the weekly window',
                'matchPackageNames': ['*'],
                'matchUpdateTypes': ['minor', 'patch', 'digest', 'pin', 'pinDigest'],
                'groupName': 'weekly dependencies',
                'groupSlug': 'weekly-dependencies',
            }]
        self.assertEqual(baseline, expected_baseline)
        # FIN-456 keeps the source-pin updates in their own atomic group.
        self.assertEqual(len(config['customManagers']), 2)
        for manager in config['customManagers']:
            self.assertEqual(set(manager) - {'description'}, {
                'customType', 'managerFilePatterns', 'matchStrings', 'depNameTemplate',
                'packageNameTemplate', 'datasourceTemplate', 'currentValueTemplate'})
            self.assertEqual(manager['customType'], 'regex')
            self.assertEqual(manager['depNameTemplate'], 'fin-assist/go-ci')
            self.assertEqual(manager['packageNameTemplate'], 'fin-assist/go-ci')
            self.assertEqual(manager['datasourceTemplate'], 'github-digest')
            self.assertEqual(manager['currentValueTemplate'], 'main')
        self.assertCountEqual([manager['managerFilePatterns'] for manager in config['customManagers']], [
            ['.github/workflows/renovate-policy-tests.yml'], ['/\\.github\\/renovate-source\\.json$/']])
        self.assertEqual([{key: value for key, value in rule.items() if key != 'description'}
                          for rule in config['packageRules']], [{
            'matchPackageNames': ['fin-assist/go-ci', 'fin-assist/go-ci/**'],
            'groupName': 'FinAssist shared Renovate controller',
            'groupSlug': 'finassist-shared-renovate', 'pinDigests': True}])
        self.assertEqual(config['extends'],
                         ['local>fin-assist/infra:base#renovate-policy',
                          'local>fin-assist/infra:policy#renovate-policy'])
        self.assertFalse(config['automerge'])
        self.assertTrue(config['dependencyDashboard'])
