"""Exercise the production image workflow's trust boundary and tag handling."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
SHA = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64
SAVED_CONFIG = b'{"architecture":"amd64","os":"linux"}'
CONFIG_DIGEST = "sha256:" + hashlib.sha256(SAVED_CONFIG).hexdigest()


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
        self.assertIn("sha256sum image.tar build.json > SHA256SUMS", export["run"])
        publisher = workflow("shared-production-image.yml")["jobs"]["publish"]
        scan = next(step["run"] for step in publisher["steps"] if step.get("name") == "Scan image and generate SBOM")
        self.assertIn("HIGH,CRITICAL --exit-code 1", scan)
        self.assertIn("cyclonedx", scan)

    def test_only_protected_main_pushes_from_service_repositories_are_admitted(self):
        publish = workflow("shared-production-image.yml")["jobs"]["publish"]
        self.assertIn("github.event.workflow_run.event == 'push'", publish["if"])
        self.assertEqual(publish["permissions"], {"actions": "read", "contents": "read", "packages": "write"})
        self.assertEqual(publish["concurrency"]["queue"], "max")
        self.assertFalse(any("actions/checkout@" in step.get("uses", "") for step in publish["steps"]))
        admission = publish["steps"][0]["run"]
        upstream = {"path": ".github/workflows/ci.yml", "event": "push", "head_branch": "main",
                    "status": "completed", "conclusion": "success", "head_sha": SHA, "id": 12,
                    "run_attempt": 3, "repository": {"full_name": "fin-assist/user"},
                    "head_repository": {"full_name": "fin-assist/user"}}
        for repo, protected, changes, passed in [
            ("fin-assist/user", "true", {}, True),
            ("fin-assist/go-service-template", "true", {}, False),
            ("fin-assist/user", "false", {}, False),
            ("fin-assist/user", "true", {"id": "12,13"}, False),
            ("fin-assist/user", "true", {"event": "pull_request"}, False),
            ("fin-assist/user", "true", {"head_branch": "dev"}, False),
            ("fin-assist/user", "true", {"head_repository": {"full_name": "example/user"}}, False),
            ("fin-assist/user", "true", {"repository": {"full_name": "example/user"}}, False),
            ("fin-assist/user", "true", {"path": ".github/workflows/other.yml"}, False),
            ("fin-assist/user", "true", {"conclusion": "failure"}, False),
            ("fin-assist/user", "true", {"status": "in_progress"}, False),
            ("fin-assist/user", "true", {"GITHUB_EVENT_NAME": "push"}, False),
            ("fin-assist/user", "true", {"GITHUB_REF": "refs/heads/dev"}, False),
        ]:
            with self.subTest(repo=repo, protected=protected, changes=changes), tempfile.TemporaryDirectory() as directory:
                event = Path(directory) / "event.json"
                event.write_text(json.dumps({"workflow_run": upstream | {
                    key: value for key, value in changes.items() if not key.startswith("GITHUB_")
                }}))
                output = event.with_suffix(".out")
                env = {**os.environ, "GITHUB_REPOSITORY": repo, "GITHUB_REF_PROTECTED": protected,
                       "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_EVENT_PATH": str(event), "PUBLISHER_SHA": SHA, "GITHUB_OUTPUT": str(output),
                       "GITHUB_SHA": "d" * 40,
                       **{key: value for key, value in changes.items() if key.startswith("GITHUB_")}}
                result = subprocess.run(["bash", "-e", "-c", admission], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, passed)
                if passed:
                    self.assertEqual(output.read_text(), f"run=12\nattempt=3\nsha={SHA}\n")
                else:
                    self.assertFalse(output.exists())

    def test_legacy_rerun_checks_the_exact_published_tag_and_creates_no_new_receipt(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        index = next(i for i, step in enumerate(steps) if step.get("id") == "legacy")
        legacy = steps[index]
        self.assertEqual(legacy["if"], "steps.source.outputs.attempt != '1'")
        self.assertEqual(steps[index - 1]["name"], "Check the caller and existing package are private")
        self.assertTrue(all(step["if"] == "steps.legacy.outputs.retained != 'true'" for step in steps[index + 1:]))
        old = "2026-11-15T15:42:21Z"
        cutoff = "2026-11-15T15:42:22Z"
        tag = {"created_at": old, "name": DIGEST, "metadata": {"container": {"tags": ["sha-" + SHA]}}}
        wrong_tag = tag | {"metadata": {"container": {"tags": ["sha-" + "e" * 40]}}}
        cases = [
            ("legacy on later page", [[wrong_tag], [tag]], "", True, True),
            ("tag created after the transition", [[tag | {"created_at": cutoff}]], "", True, False),
            ("another tag", [[wrong_tag]], "", True, False),
            ("package absent", [[]], "HTTP 404", True, False),
            ("registry API denied", [[]], "HTTP 403", False, False),
            ("duplicate tag", [[tag], [tag]], "", False, False),
            ("malformed legacy digest", [[tag | {"name": "latest"}]], "", False, False),
            ("malformed legacy date", [[tag | {"created_at": "invalid"}]], "", False, False),
        ]
        for name, pages, api_error, accepted, retained in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture, output, summary, calls = [root / item for item in ("pages", "output", "summary", "calls")]
                fixture.write_text(json.dumps(pages))
                self.stub(root, "gh", """
printf '%s\n' "$*" > "$CALLS"
if [[ -n "$API_ERROR" ]]; then
    printf '%s\n' "$API_ERROR" >&2
    exit 1
fi
cat "$FIXTURE"
""")
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "RUNNER_TEMP": directory,
                       "GITHUB_REPOSITORY": "fin-assist/analytics", "SOURCE_SHA": SHA,
                       "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary), "FIXTURE": str(fixture),
                       "CALLS": str(calls), "API_ERROR": api_error}
                result = subprocess.run(["bash", "-e", "-c", legacy["run"]], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                self.assertEqual(calls.read_text(),
                                 "api --paginate --slurp orgs/fin-assist/packages/container/analytics/versions"
                                 "?state=active&per_page=100\n")
                self.assertEqual(output.exists(), retained)
                self.assertEqual(summary.exists(), retained)
                if retained:
                    self.assertEqual(output.read_text(), "retained=true\n")
                    self.assertIn(f"ghcr.io/fin-assist/analytics@{DIGEST}", summary.read_text())

    def test_scan_refuses_vulnerable_image_or_scanner_failure(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        scan = next(step["run"] for step in steps if step.get("name") == "Scan image and generate SBOM")
        for outcome in ("success", "vulnerable", "unavailable"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "service-image"
                artifact.mkdir()
                self.stub(root, "curl", "exit 0\n")
                self.stub(root, "sha256sum", "cat >/dev/null\n")
                self.stub(root, "tar", 'cp "$STUB_TRIVY" "$4/trivy"\n')
                self.stub(root, "trivy", """
case "$*" in
  *--exit-code*)
    [[ "$OUTCOME" == success ]] || {
      if [[ "$OUTCOME" == vulnerable ]]; then
        printf '%s\\n' '{"Results":[{"Vulnerabilities":[{"VulnerabilityID":"CVE-fixture","PkgName":"fixture","Severity":"HIGH"}]}]}' > vulnerabilities.json
      fi
      exit 1
    }
    printf '%s\\n' '{}' > vulnerabilities.json ;;
  *) printf '%s\\n' '{}' > sbom.cdx.json ;;
esac
""")
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "RUNNER_TEMP": directory,
                       "STUB_TRIVY": str(root / "trivy"), "OUTCOME": outcome}
                result = subprocess.run(["bash", "-e", "-c", scan], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, outcome == "success", result.stderr)
                self.assertEqual((artifact / "sbom.cdx.json").exists(), outcome == "success")
                if outcome == "vulnerable":
                    self.assertIn("CVE-fixture\tfixture\tHIGH", result.stderr)

    def test_downloaded_tool_checksum_blocks_tampered_trivy_and_crane(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        tool_steps = ("Scan image and generate SBOM", "Install pinned registry client")
        for title in tool_steps:
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.stub(root, "curl", """
while [[ "$1" != -o ]]; do shift; done
printf 'tampered data' > "$2"
""")
                self.stub(root, "tar", 'touch "$TAR_CALLED"\n')
                invoked = root / "extract-called"
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "RUNNER_TEMP": directory,
                       "TAR_CALLED": str(invoked), "GITHUB_PATH": str(root / "path")}
                step = next(step["run"] for step in steps if step.get("name") == title)
                result = subprocess.run(["bash", "-e", "-c", step], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("FAILED", result.stdout)
                self.assertFalse(invoked.exists())

    def test_source_gates_are_required_exactly_once(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        gate = next(step["run"] for step in steps if step["name"] == "Verify completed source CI gates")
        successful = [{"name": name, "status": "completed", "conclusion": "success"}
                      for name in ("go", "lint", "image")]
        for rows, valid in [(successful, True), (successful[1:], False), (successful + successful[:1], False),
                            ([successful[0] | {"conclusion": "skipped"}, *successful[1:]], False)]:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "jobs.json").write_text(json.dumps([{"jobs": rows}]))
                self.stub(root, "gh", 'cat "$FIXTURE"\n')
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "fin-assist/user",
                       "SOURCE_RUN": "12", "SOURCE_ATTEMPT": "1", "FIXTURE": str(root / "jobs.json")}
                result = subprocess.run(["bash", "-e", "-c", gate], env=env, timeout=5, capture_output=True)
                self.assertEqual(result.returncode == 0, valid, result.stderr)

    def test_validation_rejects_changed_archive_evidence_and_build_identity(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        validate = next(step["run"] for step in steps if step.get("id") == "validated")
        cases = (
            ("valid", {}, None, IMAGE_ID, True),
            ("wrong source", {"source_sha": "e" * 40}, None, IMAGE_ID, False),
            ("wrong repository", {"repository": "fin-assist/user"}, None, IMAGE_ID, False),
            ("wrong run", {"run_id": 14}, None, IMAGE_ID, False),
            ("wrong attempt", {"run_attempt": 2}, None, IMAGE_ID, False),
            ("wrong event", {"event": "pull_request"}, None, IMAGE_ID, False),
            ("wrong ref", {"ref": "refs/heads/dev"}, None, IMAGE_ID, False),
            ("wrong platform", {"platform": "linux/arm64"}, None, IMAGE_ID, False),
            ("wrong service", {"service": "user"}, None, IMAGE_ID, False),
            ("tag instead of ID", {"image_id": "analytics:latest"}, None, "analytics:latest", False),
            ("multiple images", {}, None, IMAGE_ID, False),
            ("missing config blob", {}, None, IMAGE_ID, False),
            ("changed image", {}, "image.tar", IMAGE_ID, False),
            ("changed metadata", {}, "build.json", IMAGE_ID, False),
            ("wrong loaded ID", {}, None, DIGEST, False),
        )
        for name, changes, tampered, loaded, accepted in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "service-image"
                artifact.mkdir()
                archive = artifact / "image.tar"
                config_path = "blobs/sha256/" + CONFIG_DIGEST.removeprefix("sha256:")
                with tarfile.open(archive, "w") as image:
                    entry = tarfile.TarInfo("manifest.json")
                    rows = [{"Config": config_path}]
                    if name == "multiple images":
                        rows += rows
                    manifest = json.dumps(rows).encode()
                    entry.size = len(manifest)
                    image.addfile(entry, io.BytesIO(manifest))
                    if name != "missing config blob":
                        entry = tarfile.TarInfo(config_path)
                        entry.size = len(SAVED_CONFIG)
                        image.addfile(entry, io.BytesIO(SAVED_CONFIG))
                build = {"repository": "fin-assist/analytics", "service": "analytics",
                         "run_id": 15, "run_attempt": 1, "source_sha": SHA, "event": "push",
                         "ref": "refs/heads/main", "platform": "linux/amd64", "image_id": IMAGE_ID}
                (artifact / "build.json").write_text(json.dumps(build | changes))
                checksums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
                             for path in sorted(artifact.iterdir())]
                (artifact / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
                if tampered:
                    with (artifact / tampered).open("ab") as broken:
                        broken.write(b"changed")
                self.stub(root, "docker", 'if [[ "$2" == inspect ]]; then printf "%s\\n" "$LOADED"; fi\n')
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "RUNNER_TEMP": directory,
                       "GITHUB_REPOSITORY": "fin-assist/analytics", "SOURCE_SHA": SHA,
                       "SOURCE_RUN": "15", "SOURCE_ATTEMPT": "1", "LOADED": loaded,
                       "GITHUB_OUTPUT": str(root / "output")}
                result = subprocess.run(["bash", "-e", "-c", validate], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    self.assertEqual((root / "output").read_text(), f"config_digest={CONFIG_DIGEST}\n")
                else:
                    self.assertFalse((root / "output").exists())

    def test_publish_reuses_matching_tags_and_refuses_conflicts_and_registry_errors(self):
        steps = workflow("shared-production-image.yml")["jobs"]["publish"]["steps"]
        preflight = next(step["run"] for step in steps if step["name"] == "Check the caller and existing package are private")
        publish = next(step["run"] for step in steps if step.get("id") == "publish")
        cases = [
            ("retry after push", "private", "matching", CONFIG_DIGEST, [], True),
            ("new", "private", "missing", CONFIG_DIGEST, ["sha-" + SHA], True),
            ("tag conflict", "private", "conflict", CONFIG_DIGEST, [], False),
            ("Docker ID used as config digest", "private", "missing", IMAGE_ID, ["sha-" + SHA], False),
            ("registry denied", "private", "denied", CONFIG_DIGEST, [], False),
            ("public package", "public", "missing", CONFIG_DIGEST, [], False),
        ]
        for name, visibility, source_tag, config_digest, pushed, passed in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "service-image"
                artifact.mkdir()
                for item in ("image.tar", "build.json", "sbom.cdx.json", "vulnerabilities.json"):
                    (artifact / item).write_text("{}")
                self.stub(root, "gh", 'printf "%s\\n" "$VISIBILITY"\n')
                self.stub(root, "crane", """
case "$1" in
    auth) cat >/dev/null ;;
    manifest) printf '{"config":{"digest":"%s"}}\\n' "$CONFIG_DIGEST" ;;
    push) printf '%s\\n' "${3##*:}" >> "$CALLS" ;;
    digest)
        if [[ "$2" == --tarball ]]; then
            printf '%s\\n' "$DIGEST"
            exit 0
        fi
        tag="${2##*:}"
        state="$SOURCE_TAG"
        if [[ "$state" == matching ]] || grep -qxF "$tag" "$CALLS"; then
            printf '%s\\n' "$DIGEST"
        elif [[ "$state" == missing ]]; then
            printf 'unexpected status code 404 Not Found (HEAD responses have no body, use GET for details)\\n' >&2
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
                       "GITHUB_REPOSITORY": "fin-assist/analytics", "SOURCE_SHA": SHA, "GH_TOKEN": "test",
                       "GITHUB_ACTOR": "test", "GITHUB_WORKFLOW_REF": "main",
                       "GITHUB_OUTPUT": str(root / "output"), "GITHUB_STEP_SUMMARY": str(root / "summary"),
                       "PRIVATE_REPOSITORY": "true", "VISIBILITY": visibility, "IMAGE_ID": IMAGE_ID, "DIGEST": DIGEST,
                       "ARCHIVE_CONFIG_DIGEST": CONFIG_DIGEST,
                       "SOURCE_TAG": source_tag, "CONFIG_DIGEST": config_digest, "PUBLISHER_SHA": SHA, "CALLS": str(call_log)}
                result = subprocess.run(["bash", "-e", "-c", preflight + publish], env=env,
                                        timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, passed, result.stderr)
                self.assertEqual(call_log.read_text().splitlines(), pushed)
                if passed:
                    self.assertIn(f"image=ghcr.io/fin-assist/analytics@{DIGEST}",
                                  (root / "output").read_text())
                    evidence = json.loads((artifact / "provenance.json").read_text())
                    self.assertEqual(evidence["publisher_sha"], SHA)
                    self.assertEqual(evidence["config_digest"], CONFIG_DIGEST)
                    for item in ("image.tar", "build.json", "sbom.cdx.json", "vulnerabilities.json"):
                        expected = hashlib.sha256((artifact / item).read_bytes()).hexdigest()
                        self.assertIn(f"{expected}  {item}\n", evidence["checksums"])
                else:
                    self.assertFalse((root / "output").exists())

    @staticmethod
    def stub(directory, name, script):
        path = directory / name
        path.write_text("#!/bin/bash\n" + script)
        path.chmod(0o755)
