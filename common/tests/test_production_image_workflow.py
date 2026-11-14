"""Exercise the production image workflow's trust boundary and tag handling."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
SHA = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class ProductionImageTests(unittest.TestCase):
    def test_read_only_smoke_job_exports_only_on_opted_in_main_push(self):
        source = workflow("shared-image.yml")
        self.assertEqual(source["permissions"], {"actions": "read", "contents": "read"})
        job = source["jobs"]["image"]
        export, upload = job["steps"][-2:]
        gate = "inputs.prepare_production && github.event_name == 'push' && github.ref == 'refs/heads/main'"
        self.assertEqual(export["if"], gate)
        self.assertEqual(upload["if"], gate)
        self.assertIn('docker image save --output "$archive" "$IMAGE_ID"', export["run"])
        self.assertNotIn("secrets.", repr(export))
        self.assertIn("HIGH,CRITICAL --exit-code 1", export["run"])
        self.assertIn("cyclonedx", export["run"])

    def test_only_protected_main_pushes_from_service_repositories_are_admitted(self):
        publish = workflow("shared-production-image.yml")["jobs"]["publish"]
        self.assertEqual(publish["if"], "github.event_name == 'push' && github.ref == 'refs/heads/main'")
        self.assertEqual(publish["permissions"], {"actions": "read", "contents": "read", "packages": "write"})
        self.assertEqual(publish["concurrency"]["queue"], "max")
        admission = publish["steps"][0]["run"]
        for repo, protected, artifact_id, passed in [
            ("fin-assist/user", "true", "12", True),
            ("fin-assist/go-service-template", "true", "12", False),
            ("another-org/user", "true", "12", False),
            ("fin-assist/user", "false", "12", False),
            ("fin-assist/user", "true", "12,13", False),
        ]:
            with self.subTest(repo=repo, protected=protected, artifact_id=artifact_id):
                env = {**os.environ, "GITHUB_REPOSITORY": repo, "GITHUB_REF_PROTECTED": protected,
                       "ARTIFACT_ID": artifact_id, "GITHUB_SHA": SHA}
                result = subprocess.run(["bash", "-e", "-c", admission], env=env, timeout=5)
                self.assertEqual(result.returncode == 0, passed)

    def test_publish_reuses_matching_tags_and_refuses_conflicts_and_registry_errors(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        preflight = next(step["run"] for step in steps if step["name"] == "Check the caller and existing package are private")
        publish = next(step["run"] for step in steps if step.get("id") == "publish")
        cases = [
            ("matching", "private", "matching", "matching", [], True),
            ("new", "private", "missing", "missing", ["sha-" + SHA, "v1.2.3"], True),
            ("version conflict", "private", "missing", "conflict", [], False),
            ("registry denied", "private", "denied", "missing", [], False),
            ("public package", "public", "missing", "missing", [], False),
        ]
        for name, visibility, source_tag, version_tag, pushed, passed in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "service-image"
                artifact.mkdir()
                (artifact / "build.json").write_text("{}")
                self.stub(root, "gh", 'printf "%s\\n" "$VISIBILITY"\n')
                self.stub(root, "git", 'printf "v1.2.3\\nv1.2.4-rc1\\n"\n')
                self.stub(root, "crane", """
case "$1" in
    auth) cat >/dev/null ;;
    manifest) printf '{"config":{"digest":"%s"}}\\n' "$IMAGE_ID" ;;
    push) printf '%s\\n' "${3##*:}" >> "$CALLS" ;;
    digest)
        if [[ "$2" == --tarball ]]; then
            printf '%s\\n' "$DIGEST"
            exit 0
        fi
        tag="${2##*:}"
        state="$SOURCE_TAG"
        [[ "$tag" != v1.2.3 ]] || state="$VERSION_TAG"
        if [[ "$state" == matching ]] || grep -qxF "$tag" "$CALLS"; then
            printf '%s\\n' "$DIGEST"
        elif [[ "$state" == missing ]]; then
            printf 'MANIFEST_UNKNOWN\\n' >&2
            exit 1
        elif [[ "$state" == conflict ]]; then
            printf '%s\\n' "$IMAGE_ID"
        else
            printf 'DENIED\\n' >&2
            exit 1
        fi ;;
esac
""")
                call_log = root / "calls"
                call_log.touch()
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "RUNNER_TEMP": directory,
                       "GITHUB_REPOSITORY": "fin-assist/analytics", "GITHUB_SHA": SHA, "GH_TOKEN": "test",
                       "GITHUB_ACTOR": "test", "GITHUB_WORKFLOW_REF": "main",
                       "GITHUB_OUTPUT": str(root / "output"), "GITHUB_STEP_SUMMARY": str(root / "summary"),
                       "PRIVATE_REPOSITORY": "true", "VISIBILITY": visibility, "IMAGE_ID": IMAGE_ID, "DIGEST": DIGEST,
                       "SOURCE_TAG": source_tag, "VERSION_TAG": version_tag, "CALLS": str(call_log)}
                result = subprocess.run(["bash", "-e", "-c", preflight + publish], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, passed, result.stderr)
                self.assertEqual(call_log.read_text().splitlines(), pushed)
                if passed:
                    self.assertIn(f"image=ghcr.io/fin-assist/analytics@{DIGEST}",
                                  (root / "output").read_text())
                else:
                    self.assertFalse((root / "output").exists())

    @staticmethod
    def stub(directory, name, script):
        path = directory / name
        path.write_text("#!/bin/bash\n" + script)
        path.chmod(0o755)
