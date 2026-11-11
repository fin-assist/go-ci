"""Verify the Qase 90 fixture action's caller boundary and package format."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github/actions/prepare-qase90/action.yml"
REPOSITORY = "fin-assist/bank-bff"


def execute(script, cwd, **env):
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=cwd,
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=20)


class PrepareQase90Tests(unittest.TestCase):
    def setUp(self):
        self.steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]

    def test_only_trusted_bank_bff_inputs_reach_pinned_checkouts(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text('#!/bin/sh\ncase "$2" in\n  repos/fin-assist/bank-statement-data/compare/*) echo "$BSD_REVISION";;\n  repos/fin-assist/analytics/compare/*) echo "$ANALYTICS_REVISION";;\n  *) exit 1;;\nesac\n')
            gh.chmod(0o755)
            valid = {
                "PATH": directory + os.pathsep + os.environ["PATH"],
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_REPOSITORY": REPOSITORY, "HEAD_REPO": REPOSITORY,
                "BSD_REVISION": "a" * 40, "ANALYTICS_REVISION": "b" * 40,
            }
            script = self.steps[0]["run"]
            self.assertEqual(execute(script, directory, **valid).returncode, 0)
            for invalid in ({"HEAD_REPO": "fork/bank-bff"}, {"GITHUB_REPOSITORY": "fin-assist/other"},
                            {"GITHUB_EVENT_NAME": "pull_request"}, {"BSD_REVISION": "main"}):
                with self.subTest(invalid=invalid):
                    self.assertNotEqual(execute(script, directory, **(valid | invalid)).returncode, 0)
        checkouts = [step["with"] for step in self.steps if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual({config["repository"] for config in checkouts},
                         {"fin-assist/bank-statement-data", "fin-assist/analytics"})
        self.assertTrue(all(config["persist-credentials"] is False for config in checkouts))

    def test_packaging_creates_two_git_bundles_and_preserves_lockfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp = root / "runner"
            temp.mkdir()
            revisions = {}
            for fixture in ("bsd", "analytics"):
                source = root / ".finassist-ci/qase90" / fixture
                source.mkdir(parents=True)
                for name in ("go.mod", "go.sum"):
                    (source / name).write_text(f"{fixture} fixture {name}\n")
                def git(*args):
                    return subprocess.check_output(["git", "-C", str(source), *args],
                                                   text=True, timeout=5).strip()
                git("init", "--quiet")
                git("add", ".")
                git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture")
                revisions[f"{fixture.upper()}_REVISION"] = git("rev-parse", "HEAD")
            result = execute(self.steps[-1]["run"], root, RUNNER_TEMP=str(temp), **revisions)
            self.assertEqual(result.returncode, 0, result.stderr)
            for fixture in ("bsd", "analytics"):
                package = temp / "private-ci-output/qase90" / f"{fixture}.bundle"
                head = subprocess.check_output(["git", "bundle", "list-heads", str(package)], text=True, timeout=5)
                self.assertEqual(head.strip(), revisions[f"{fixture.upper()}_REVISION"] + " HEAD")
                for name in ("go.mod", "go.sum"):
                    self.assertEqual((temp / "private-ci-input/qase90" / fixture / name).read_text(),
                                     f"{fixture} fixture {name}\n")
