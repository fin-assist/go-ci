"""Exercise the credential-free restoration against actual Git bundles and files."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from common import bootstrap

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github/actions/restore-private-ci"
SHA = "a" * 40
BASE = "b" * 40


def execute(script, **env):
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                          cwd=env.get("GITHUB_WORKSPACE", ROOT),
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=15)


class PrivateInputRestoreTests(unittest.TestCase):
    def setUp(self):
        self.steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]

    def test_context_is_bound_to_same_repo_and_exact_pr_shas(self):
        valid = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "fin-assist/assistant",
                 "HEAD_REPO": "fin-assist/assistant", "REVISION": SHA, "RUN_ID": "123",
                 "ARTIFACT": f"private-ci-{SHA}-{BASE}", "HEAD_SHA": SHA, "BASE_SHA": BASE}
        self.assertEqual(execute(self.steps[0]["run"], **valid).returncode, 0)
        for overrides in ({"HEAD_REPO": "fork/assistant"}, {"GITHUB_EVENT_NAME": "push"},
                          {"GITHUB_EVENT_NAME": "pull_request_target"}, {"REVISION": "a" * 12},
                          {"RUN_ID": "no-run"}, {"ARTIFACT": f"private-ci-{BASE}-{SHA}"}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(execute(self.steps[0]["run"], **(valid | overrides)).returncode, 0)

    def test_only_exact_bundle_and_manifests_restore_the_cache(self):
        for fault in ("", "bundle", "go.sum", "pin"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, consumer = root / "source", root / "consumer"
                source.mkdir()
                consumer.mkdir()
                (source / "common").mkdir()
                (source / "common/render_lint.py").write_text("# go-ci fixture\n")
                def git(*args):
                    return subprocess.check_output(["git", "-C", str(source), *args],
                                                   text=True, timeout=5).strip()
                git("init", "--quiet")
                git("add", "common/render_lint.py")
                git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture")
                revision = git("rev-parse", "HEAD")
                prepared = root / "finassist-private-ci"
                prepared.mkdir()
                git("bundle", "create", str(prepared / "source.bundle"), "HEAD")
                (consumer / "go.mod").write_text("module github.com/fin-assist/assistant\n")
                (consumer / "go.sum").write_text("verified fixture\n")
                config = consumer / ".finassist-ci.json"
                pin = SHA if fault == "bundle" else revision
                config.write_text(json.dumps({"revision": pin, "profile": "service"}))
                manifest = prepared / "manifest.json"
                subprocess.run(["python3", "-B", str(ROOT / "common/scripts/private_ci_manifest.py"), "write",
                                "--directory", str(consumer), "--revision", pin, "--manifest", str(manifest)],
                               timeout=5, check=True)
                if fault == "go.sum":
                    (consumer / "go.sum").write_text("changed fixture\n")
                if fault == "pin":
                    config.write_text(json.dumps({"revision": SHA}))
                cache_input = prepared / "proxy/github.com/fin-assist/module/@v"
                cache_input.mkdir(parents=True)
                (cache_input / "v1.0.0.ziphash").write_text("fixture checksum")
                github_env = root / "github-env"
                result = execute(self.steps[2]["run"], REVISION=pin, RUNNER_TEMP=str(root),
                                 GITHUB_ENV=str(github_env), GITHUB_WORKSPACE=str(consumer),
                                 GITHUB_ACTION_PATH=str(ACTION))
                if fault:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(github_env.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    restored = consumer / ".finassist-ci" / pin
                    self.assertEqual(bootstrap.validate_cache(restored, pin), restored / "common")
                    self.assertIn(f"FINASSIST_GO_MODULE_CACHE={prepared / 'proxy'}", github_env.read_text())
                    self.assertTrue((root / "finassist-go-mod/cache/download/github.com/fin-assist/module/@v/v1.0.0.ziphash").is_file())

    def test_trusted_preparation_checks_out_only_the_pinned_go_ci_repository(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        job = workflow["jobs"]["prepare"]
        for required in ("github.event_name == 'pull_request_target'",
                         "github.event.pull_request.base.ref == 'main'",
                         "github.event.pull_request.head.repo.full_name == github.repository",
                         "github.event_name == 'workflow_dispatch'",
                         "github.ref == 'refs/heads/fin491-ci-bootstrap'"):
            self.assertIn(required, job["if"])
        checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 2)
        for checkout in checkouts:
            self.assertEqual(checkout["with"]["repository"], "fin-assist/go-ci")
            self.assertFalse(checkout["with"]["persist-credentials"])
            self.assertNotIn("head.sha", checkout["with"]["ref"])
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^actions/[^@]+@[0-9a-f]{40}$")
