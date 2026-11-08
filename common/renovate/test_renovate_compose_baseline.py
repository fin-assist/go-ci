from paths import consumer_root, consumer_repository
"""Android FIN-184 policy must follow the reviewed baseline, not copied constants."""
import json
from pathlib import Path
import unittest
from renovate_policy import compose_stack_rule
from renovate_policy_data import COMPOSE_PACKAGES, validate_policy

@unittest.skipUnless(consumer_repository() == 'fin-assist/android', 'Android baseline only')
class ComposeBaselineTests(unittest.TestCase):
    def test_generated_stack_covers_the_reviewed_baseline_matchers(self):
        baseline=json.loads((consumer_root()/'renovate-base.json').read_text())
        groups=[rule for rule in baseline['packageRules'] if rule.get('groupName')=='androidx compose stack']
        self.assertEqual(len(groups),1)
        self.assertCountEqual(groups[0]['matchPackageNames'],COMPOSE_PACKAGES)
        rule=compose_stack_rule(123)
        self.assertCountEqual(rule['matchPackageNames'],groups[0]['matchPackageNames'])
        self.assertEqual(rule['separateMajorMinor'],groups[0]['separateMajorMinor'])
        validate_policy({'baseBranchPatterns':['main'],'packageRules':[rule]})


@unittest.skipUnless(consumer_repository() == 'fin-assist/android', 'Android baseline only')
class ActualComposeBaselineSyncTests(unittest.TestCase):
    def test_reviewed_android_file_is_accepted_and_copied_to_generated_base(self):
        import base64
        from renovate_policy import GitHub
        baseline=(consumer_root()/'renovate-base.json').read_bytes()
        api=GitHub('credential','fin-assist/android');writes=[]
        def get(path):
            if path.startswith('branches/'):return {'commit':{'sha':'selected-sha'}}
            if path=='contents/renovate-base.json?ref=selected-sha':return {'content':base64.b64encode(baseline).decode()}
            if path=='contents/base.json?ref=renovate-policy':return {'sha':'old-sha','content':base64.b64encode(b'{}').decode()}
            raise AssertionError(path)
        api.get=get;api.request=lambda method,path,data:writes.append((method,path,data))
        api.sync_baseline('release/1.0.0')
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][:2],('PUT','contents/base.json'))
        self.assertEqual(base64.b64decode(writes[0][2]['content']),baseline)

if __name__ == '__main__':
    unittest.main()
