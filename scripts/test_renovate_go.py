"""Manual Go toolchains remain outside merge, retry and close automation."""
import base64
import copy
import json
from pathlib import Path
import unittest

from renovate_policy import COMPACT_UPDATE, manual_go_update, reconcile, updates
from test_renovate_controller import Fake


class ManualGoTests(unittest.TestCase):
    def test_uses_with_metadata_is_normalized_to_the_package_identity(self):
        item = {'depName': 'go', 'packageName': 'actions/go-versions',
                'datasource': 'github-releases', 'newValue': '1.27.3'}
        message = COMPACT_UPDATE + base64.b64encode(json.dumps(item).encode()).decode() + ' -->'
        parsed = updates(message)
        self.assertEqual(parsed[0]['depName'], 'actions/go-versions')
        self.assertTrue(manual_go_update(parsed))

    def test_only_go_toolchain_identities_are_manual(self):
        for source, name in [('github-releases', 'actions/go-versions'),
                             ('github-tags', 'actions/go-versions'),
                             ('docker', 'golang'), ('docker', 'library/golang'),
                             ('docker', 'docker.io/library/golang'),
                             ('golang-version', 'go'), ('golang-version', 'golang/go')]:
            with self.subTest(source=source, name=name):
                self.assertTrue(manual_go_update([{'datasource': source, 'depName': name}]))
        for source, name in [('go', 'github.com/fin-assist/go-grpc'),
                             ('github-tags', 'actions/setup-go'), ('npm', 'go'),
                             ('docker', 'python')]:
            with self.subTest(source=source, name=name):
                self.assertFalse(manual_go_update([{'datasource': source, 'depName': name}]))

    def test_single_and_mixed_go_prs_never_merge_retry_close_or_write_receipts(self):
        go = {'depName': 'go', 'packageName': 'actions/go-versions',
              'datasource': 'github-releases', 'newValue': '1.27.3'}
        other = {'depName': 'python', 'newValue': '3.15', 'datasource': 'github-tags'}
        for items in ([go], [other, go]):
            for conclusion in ('success', 'failure'):
                for attempt in (1, 2):
                    with self.subTest(items=items, conclusion=conclusion, attempt=attempt):
                        api = Fake()
                        api.message = ''.join(COMPACT_UPDATE + base64.b64encode(
                            json.dumps(item).encode()).decode() + ' -->\n' for item in items)
                        # Editable body deliberately omits Go and cannot grant permission.
                        api.pr['body'] = api.message.replace('fin211-update', 'ignored')
                        api.run.update(conclusion=conclusion, run_attempt=attempt)
                        api.jobs[0]['conclusion'] = conclusion
                        api.checks[0]['conclusion'] = conclusion
                        if attempt == 2:
                            api.receipts = {'1:abc': {'state': {'sha': 'abc', 'runs': {
                                'ci.yml': {'run_id': 1, 'attempt': 1, 'retried': True}}},
                                'comment_id': 10}}
                        receipts = copy.deepcopy(api.receipts)
                        reconcile(api, api.config)
                        self.assertEqual(api.actions, [])
                        self.assertEqual(api.comments, [])
                        self.assertEqual(api.receipts, receipts)
                        self.assertEqual(api.pr['state'], 'open')

    def test_source_ci_configuration_and_workflow_names_match(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / '.github/renovate-policy.json').read_text())
        self.assertEqual(config['ciChecks'], ['shared-source'])
        self.assertEqual(config['requiredChecks'], [
            'shared-source', 'Renovate policy tests', 'Renovate runtime contract'])
        workflow = (root / '.github/workflows/renovate-policy.yml').read_text()
        self.assertIn('workflows: [Shared CI self checks, Renovate policy tests]', workflow)
        self.assertIn('name: Shared CI self checks', (root / '.github/workflows/ci.yml').read_text())


if __name__ == '__main__':
    unittest.main()
