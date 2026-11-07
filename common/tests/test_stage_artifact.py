"""Exercise extraction and failure handling without a Docker daemon."""
import importlib.util
import hashlib
import json
import os
import re
import subprocess
import sys
import yaml
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("stage_artifact", Path(__file__).resolve().parents[1] / "scripts/stage_artifact.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


BINARY = b"ELF-test-binary" + bytes(range(256)) * 9000


class StageArtifactTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(GITHUB_SHA="a" * 40, GITHUB_REPOSITORY="fin-assist/user", GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
                        GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main")
        self.calls = []
        self.consumer = tempfile.TemporaryDirectory()
        self.addCleanup(self.consumer.cleanup)
        self.root = Path(self.consumer.name)
        (self.root / "go.mod").write_text("module github.com/fin-assist/user\nrequire github.com/fin-assist/contracts/gen/go v0.8.3\n")

    def docker(self, *args):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            return "linux/amd64" if args[3] == "{{.Os}}/{{.Architecture}}" else "sha256:" + "b" * 64
        if args[0] == "create":
            return "container-id"
        if args[0] == "cp":
            Path(args[2]).write_bytes(BINARY)
        return ""

    def test_package_binds_exact_image_source_and_binary(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", self.docker):
            result = stage.package("user", "user:ci", Path(tmp) / "out", self.env, root=self.root)
            self.assertEqual(result["source_sha"], "a" * 40)
            self.assertEqual(result["platform"], "linux/amd64")
            self.assertEqual(result["run_id"], 123)
            out = Path(tmp) / "out"
            digest = hashlib.sha256(BINARY).hexdigest()
            self.assertEqual(result["binary_sha256"], digest)
            self.assertEqual((out / "SHA256SUMS").read_text(), f"{digest}  user\n")
            self.assertEqual(json.loads((out / "manifest.json").read_text()), result)
            self.assertEqual(result["image_id"], "sha256:" + "b" * 64)
            self.assertEqual(result["repository"], "fin-assist/user")
            self.assertEqual(result["run_attempt"], 2)
            self.assertEqual(result["event"], "push")
            self.assertEqual(result["ref"], "refs/heads/main")
            version = re.search(r"github.com/fin-assist/contracts/gen/go\s+(v\S+)", (self.root / "go.mod").read_text()).group(1)
            self.assertEqual(result["modules"]["contracts/gen/go"], version)
            self.assertEqual((out / "user").stat().st_mode & 0o777, 0o755)
            self.assertIn(("cp", "container-id:/user", str(out / "user")), self.calls)
            self.assertIn(("create", "sha256:" + "b" * 64), self.calls)
            self.assertIn(("rm", "-f", "container-id"), self.calls)
            self.assertTrue((Path(tmp) / "out/manifest.json").is_file())

    def test_invalid_metadata_rejected_before_docker(self):
        for key, value in [("GITHUB_SHA", "main"), ("GITHUB_REPOSITORY", "evil/repo"), ("GITHUB_RUN_ID", "x")]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", self.docker):
                with self.assertRaises(ValueError):
                    stage.package("user", "user:ci", Path(tmp) / "out", self.env | {key: value}, root=self.root)
                self.assertEqual(self.calls, [])

    def test_service_cannot_escape_output(self):
        with self.assertRaises(ValueError):
            stage.package("../user", "user:ci", Path("unused"), self.env, root=self.root)

    def test_container_removed_when_copy_fails(self):
        def fail_copy(*args):
            if args[0] == "cp":
                raise RuntimeError("copy failed")
            return self.docker(*args)
        with tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", fail_copy):
            with self.assertRaises(RuntimeError):
                stage.package("user", "user:ci", Path(tmp) / "out", self.env, root=self.root)
            self.assertIn(("rm", "-f", "container-id"), self.calls)

    def test_foreign_platform_and_bad_image_id_are_independent(self):
        for image_id, platform in [("sha256:" + "b" * 64, "linux/arm64"), ("sha256:short", "linux/amd64")]:
            self.calls = []
            def inspect(*args):
                self.calls.append(args)
                return platform if args[3] == "{{.Os}}/{{.Architecture}}" else image_id
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", inspect):
                with self.assertRaises(ValueError):
                    stage.package("user", "user:ci", Path(tmp) / "out", self.env, root=self.root)
                self.assertFalse((Path(tmp) / "out").exists())
                self.assertFalse(any(call[0] == "create" for call in self.calls))

    def test_nonregular_missing_and_empty_binary_are_rejected(self):
        for kind in ["symlink", "directory", "missing", "empty"]:
            self.calls = []
            def copy(*args):
                if args[0] != "cp":
                    return self.docker(*args)
                target = Path(args[2])
                if kind == "symlink":
                    real = target.parent / "real"
                    real.write_bytes(b"host-data")
                    target.symlink_to(real)
                elif kind == "directory":
                    target.mkdir()
                elif kind == "empty":
                    target.write_bytes(b"")
                return ""
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", copy):
                out = Path(tmp) / "out"
                with self.assertRaises(ValueError):
                    stage.package("user", "user:ci", out, self.env, root=self.root)
                self.assertFalse((out / "SHA256SUMS").exists())
                self.assertFalse((out / "manifest.json").exists())
                self.assertIn(("rm", "-f", "container-id"), self.calls)

    def test_copy_failure_not_replaced_by_cleanup_failure(self):
        def fail(*args):
            if args[0] in ["cp", "rm"]:
                raise RuntimeError(args[0] + " failed")
            return self.docker(*args)
        with tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", fail), patch("sys.stderr"):
            with self.assertRaisesRegex(RuntimeError, "cp failed"):
                stage.package("user", "user:ci", Path(tmp) / "out", self.env, root=self.root)

    def test_docker_failure_includes_diagnostic(self):
        result = subprocess.CompletedProcess([], 1, "", "No such image")
        with patch.object(stage.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "No such image"):
                stage.docker("image", "inspect", "missing")
            self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_cli_requires_explicit_root_and_binds_relative_output_to_it(self):
        with patch("sys.argv", ["stage_artifact.py", "user", "user:ci", "out"]):
            with self.assertRaises(SystemExit):
                stage.main()
        with patch("sys.argv", ["stage_artifact.py", "--root", str(self.root), "user", "user:ci", "out"]), \
                patch.dict(os.environ, self.env), patch.object(stage, "docker", self.docker):
            stage.main()
        manifest = json.loads((self.root / "out/manifest.json").read_text())
        self.assertEqual(manifest["modules"], {"contracts/gen/go": "v0.8.3"})

    def test_attempt_is_validated_before_docker(self):
        for attempt in ("0", "-1", "x", ""):
            with self.subTest(attempt=attempt), patch.object(stage, "docker", self.docker):
                with self.assertRaises(ValueError):
                    stage.package("user", "user:ci", self.root / "out", self.env | {"GITHUB_RUN_ATTEMPT": attempt}, root=self.root)
                self.assertEqual(self.calls, [])

    def test_existing_directory_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(stage, "docker", self.docker):
            with self.assertRaises(FileExistsError):
                stage.package("user", "user:ci", Path(tmp), self.env, root=self.root)
