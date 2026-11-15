"""Exercise the credential-free restoration against actual Git bundles and files."""

import json
import re
import sys
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from common import bootstrap
from common.scripts.find_private_ci_inputs import artifact_name

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
                 "ARTIFACT": f"private-ci-{SHA}-{BASE}", "HEAD_SHA": SHA, "BASE_SHA": BASE, "BRANCH_MODE": "false"}
        self.assertEqual(execute(self.steps[0]["run"], **valid).returncode, 0)
        updated = valid | {"BRANCH_MODE": "true", "ARTIFACT": f"private-ci-source-{SHA}-{SHA}"}
        self.assertEqual(execute(self.steps[0]["run"], **updated).returncode, 0)
        self.assertEqual(execute(self.steps[0]["run"], **(updated | {"BASE_SHA": "c" * 40})).returncode, 0)
        stale = updated | {"ARTIFACT": f"private-ci-source-{SHA}-{BASE}"}
        self.assertNotEqual(execute(self.steps[0]["run"], **stale).returncode, 0)
        for overrides in ({"HEAD_REPO": "fork/assistant"}, {"GITHUB_EVENT_NAME": "push"},
                          {"GITHUB_EVENT_NAME": "pull_request_target"}, {"REVISION": "a" * 12},
                          {"RUN_ID": "no-run"}, {"ARTIFACT": f"private-ci-{BASE}-{SHA}"}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(execute(self.steps[0]["run"], **(valid | overrides)).returncode, 0)

    def test_only_exact_bundle_and_manifests_restore_the_cache(self):
        for fault in ("", "no-pin", "no-sum", "bundle", "go.sum", "pin"):
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
                git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "--allow-empty", "-qm", "second")
                revision = git("rev-parse", "HEAD")
                prepared = root / "finassist-private-ci"
                prepared.mkdir()
                shallow = root / "shallow"
                subprocess.run(["git", "clone", "-q", "--depth=1", source.as_uri(), str(shallow)],
                               check=True, timeout=5)
                self.assertTrue((shallow / ".git/shallow").is_file())
                subprocess.run(["git", "-C", str(shallow), "bundle", "create",
                                str(prepared / "source.bundle"), "HEAD"], check=True, timeout=5)
                (consumer / "go.mod").write_text("module github.com/fin-assist/assistant\n")
                if fault != "no-sum":
                    (consumer / "go.sum").write_text("verified fixture\n")
                config = consumer / ".finassist-ci.json"
                pin = SHA if fault == "bundle" else revision
                config.write_text(json.dumps({"profile": "service"} | ({} if fault == "no-pin" else {"revision": pin})))
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
                if fault in {"bundle", "go.sum", "pin"}:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(github_env.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    restored = consumer / ".finassist-ci" / pin
                    self.assertEqual(bootstrap.validate_cache(restored, pin), restored / "common")
                    self.assertIn(f"FINASSIST_GO_MODULE_CACHE={prepared / 'proxy'}", github_env.read_text())
                    self.assertTrue((root / "finassist-go-mod/cache/download/github.com/fin-assist/module/@v/v1.0.0.ziphash").is_file())

    def test_workflow_run_prefetch_uses_the_exact_resolved_source(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        step = next(step for step in workflow["jobs"]["prepare"]["steps"] if step.get("id") == "attempt")
        repository = "fin-assist/assistant"
        allowed = {"full_name": repository}
        shared = {"path": "fin-assist/go-ci/.github/workflows/shared-go.yml@refs/heads/main", "sha": BASE}
        details = {"event": "pull_request", "path": ".github/workflows/ci.yml",
                   "repository": allowed, "head_repository": allowed, "head_sha": SHA,
                   "head_branch": "tooling/update", "referenced_workflows": [shared]}
        pull = {"number": 42, "head": {"sha": SHA, "repo": allowed}, "base": {"repo": allowed}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeout = root / "timeout"
            timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n')
            timeout.chmod(0o755)
            gh = root / "gh"
            gh.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                '"api repos/fin-assist/assistant/actions/runs/123/attempts/2") printf "%s" "$DETAILS" ;;\n'
                '"api --method GET repos/fin-assist/assistant/pulls -f state=open -f head=fin-assist:tooling/update -F per_page=100") '
                'printf "%s" "$PULLS" ;;\n*) exit 2 ;;\nesac\n')
            gh.chmod(0o755)
            output = root / "output"
            env = {"PATH": directory + os.pathsep + os.environ["PATH"], "GITHUB_REPOSITORY": repository,
                   "GITHUB_REF": "refs/heads/main", "GITHUB_REF_PROTECTED": "true",
                   "SOURCE_RUN": "123", "SOURCE_ATTEMPT": "2", "GITHUB_OUTPUT": str(output)}
            good = execute(step["run"], **env, DETAILS=json.dumps(details), PULLS=json.dumps([pull]))
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(output.read_text(), f"number=42\nhead={SHA}\nrevision={BASE}\n")
            output.unlink()
            pinned = shared | {"path": shared["path"].replace("refs/heads/main", BASE)}
            legacy = execute(step["run"], **env,
                             DETAILS=json.dumps(details | {"referenced_workflows": [pinned]}), PULLS="[]")
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertFalse(output.exists())
            for update, pulls in (
                ({"event": "push"}, [pull]),
                ({"head_repository": {"full_name": "fork/assistant"}}, [pull]),
                ({"path": ".github/workflows/other.yml"}, [pull]),
                ({"referenced_workflows": [shared, shared | {"sha": SHA}]}, [pull]),
                ({"referenced_workflows": [shared | {"path": shared["path"].replace("refs/heads/", "refs/tags/")}]}, [pull]),
                ({}, []), ({}, [pull, pull]),
            ):
                with self.subTest(detail=update, pull_count=len(pulls)):
                    output.unlink(missing_ok=True)
                    result = execute(step["run"], **env, DETAILS=json.dumps(details | update),
                                     PULLS=json.dumps(pulls))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())

    def test_producer_discovery_and_restore_share_exact_artifact_names(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        upload = next(step for step in workflow["jobs"]["prepare"]["steps"]
                      if step.get("uses", "").startswith("actions/upload-artifact@"))
        formats = re.findall(r"format\('([^']+)', steps\.pull\.outputs\.head, steps\.(?:attempt\.outputs\.revision|pull\.outputs\.base)\)", upload["with"]["name"])
        self.assertEqual(len(formats), 2)
        for branch_mode, identity, template in ((True, SHA, formats[0]), (False, BASE, formats[1])):
            expected = template.format(SHA, identity)
            discovered = artifact_name(SHA, BASE, SHA if branch_mode else None)
            self.assertEqual(expected, discovered)
            env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "fin-assist/assistant",
                   "HEAD_REPO": "fin-assist/assistant", "REVISION": SHA, "RUN_ID": "123",
                   "ARTIFACT": expected, "HEAD_SHA": SHA, "BASE_SHA": "c" * 40 if branch_mode else BASE,
                   "BRANCH_MODE": str(branch_mode).lower()}
            result = execute(self.steps[0]["run"], **env)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_only_packages_the_correct_manifest_mode(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        action = next(step for step in workflow["jobs"]["prepare"]["steps"] if step.get("id") == "manifests")
        validator = action["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-ci-input"
            source.mkdir()
            output = root / "output"
            for event, attempt, pin, accepted, revision in (
                ("workflow_run", SHA, None, True, SHA),
                ("workflow_run", SHA, BASE, False, None),
                ("pull_request_target", "", BASE, True, BASE),
                ("pull_request_target", "", None, True, None),
                ("workflow_dispatch", "", None, False, None),
                ("pull_request_target", "", "main", False, None),
            ):
                with self.subTest(event=event, attempt=attempt, pin=pin):
                    output.unlink(missing_ok=True)
                    config = {"profile": "library"} | ({"revision": pin} if pin else {})
                    (source / ".finassist-ci.json").write_text(json.dumps(config))
                    env = {**os.environ, "GITHUB_EVENT_NAME": event, "ATTEMPT_REVISION": attempt, "PROFILE": "library",
                           "RUNNER_TEMP": directory, "GITHUB_OUTPUT": str(output)}
                    result = subprocess.run([sys.executable, "-c", validator], env=env,
                                            timeout=10, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
                    if revision:
                        self.assertEqual(output.read_text(), f"revision={revision}\n")
                    else:
                        self.assertFalse(output.exists())

    def test_new_reusable_callers_bind_source_and_keep_pr_jobs_credential_free(self):
        cases = [
            ("shared-private-discovery.yml", "find"),
            ("shared-qase90.yml", "qase90"),
        ]
        for name, job_name in cases:
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            job = workflow["jobs"][job_name]
            discover = next(step for step in job["steps"]
                            if step.get("uses") == "$/.github/actions/private-ci-inputs")
            self.assertEqual(discover["with"]["revision"], "${{ job.workflow_sha }}")
            self.assertNotIn("base-sha", discover["with"])
            self.assertEqual(workflow["permissions"]["actions"], "read")
        self.assertIn("github.repository == 'fin-assist/bank-bff'", job["if"])
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", job["if"])
        for step in job["steps"]:
            for value in list(step.get("env", {}).values()) + list(step.get("with", {}).values()):
                if "secrets.GO_MODULES_TOKEN" in str(value):
                    self.assertEqual(step["if"], "github.event_name == 'push'")
                    self.assertEqual(value, "${{ github.event_name == 'push' && secrets.GO_MODULES_TOKEN || '' }}")
        restore = next(step for step in job["steps"] if step.get("uses") == "$/.github/actions/restore-private-ci")
        self.assertEqual(restore["with"]["revision"], "${{ job.workflow_sha }}")
        self.assertEqual(restore["with"]["branch-mode"], "true")

    def test_release_preparation_requires_semver_branch_and_protection(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        gate = next(step for step in workflow["jobs"]["prepare"]["steps"]
                    if step.get("name") == "Require a protected release target")
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text('#!/bin/sh\n[ "$2" = "repos/fin-assist/assistant/branches/release%2F1.2.3" ] || exit 1\nprintf "%s\\n" "$PROTECTED"\n')
            gh.chmod(0o755)
            env = {"PATH": directory + os.pathsep + os.environ["PATH"], "GITHUB_REPOSITORY": "fin-assist/assistant"}
            for ref, protected, accepted in (("release/1.2.3", "true", True),
                                              ("release/1.2.3", "false", False),
                                              ("release/test", "true", False),
                                              ("release/01.2.3", "true", False)):
                with self.subTest(ref=ref, protected=protected):
                    result = execute(gate["run"], **env, TARGET_REF=ref, PROTECTED=protected)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_trusted_preparation_checks_out_only_the_pinned_go_ci_repository(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-private-inputs.yml").read_text())
        job = workflow["jobs"]["prepare"]
        for required in ("github.event_name == 'pull_request_target'",
                         "github.event.pull_request.base.ref == 'main'",
                         "startsWith(github.event.pull_request.base.ref, 'release/')",
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
                action = step["uses"]
                if action.startswith("./"):
                    self.assertEqual(action, "./.finassist-ci/trusted-preparation/.github/actions/prepare-qase90")
                    self.assertEqual(step["id"], "qase90")
                    self.assertLess(job["steps"].index(checkouts[0]), job["steps"].index(step))
                else:
                    self.assertRegex(action, r"^actions/[^@]+@[0-9a-f]{40}$")
