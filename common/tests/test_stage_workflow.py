"""Execute the actual stage workflow guards and dispatch script with a fake GitHub API."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/shared-stage.yml"


class StageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["request"]["steps"]
        self.gate, self.dispatch = (step["run"] for step in steps)
        self.env = dict(os.environ, PATH=str(self.directory) + ":" + os.environ["PATH"],
                        RUNNER_TEMP=str(self.directory), GITHUB_STEP_SUMMARY=str(self.directory / "summary"),
                        GITHUB_REPOSITORY="fin-assist/assistant", GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
                        GH_TOKEN="synthetic", FIRST_ERROR="", CONCLUSION="success", APPLY_SUCCEEDED="1",
                        DISPATCH_ID="456", DISPATCH_FAIL="0", ALL_NOT_FOUND="0", PENDING_FIRST="0",
                        EVENT="workflow_dispatch", REF="refs/heads/main", PROTECTED="true",
                        OPERATION="deploy", WORKFLOW="fin-assist/assistant/.github/workflows/deploy-stage.yml@refs/heads/main")
        scripts = {
            "timeout": 'shift\nexec "$@"\n',
            "sleep": "exit 0\n",
            "gh": """
case "$*" in
    *'/dispatches '*)
        echo dispatch >> "$RUNNER_TEMP/calls"
        printf '%s\\n' "$@" > "$RUNNER_TEMP/args"
        [[ "$DISPATCH_FAIL" == 0 ]] || exit 1
        printf '%s\\n' "$DISPATCH_ID"
        ;;
    *'/jobs?'*)
        echo jobs >> "$RUNNER_TEMP/calls"
        [[ "$*" == *'/runs/456/jobs?'* ]] || exit 9
        echo "${APPLY_SUCCEEDED:-1}"
        ;;
    *'/runs/456 '*)
        echo get >> "$RUNNER_TEMP/calls"
        if [[ "$ALL_NOT_FOUND" == 1 ]]; then
            echo "HTTP 404" >&2
            exit 1
        fi
        if [[ "$PENDING_FIRST" == 1 && ! -e "$RUNNER_TEMP/pending" ]]; then
            touch "$RUNNER_TEMP/pending"
            echo pending
            exit 0
        fi
        if [[ -n "$FIRST_ERROR" && ! -e "$RUNNER_TEMP/first-get" ]]; then
            touch "$RUNNER_TEMP/first-get"
            echo "HTTP $FIRST_ERROR" >&2
            exit 1
        fi
        echo "${CONCLUSION:-success}"
        ;;
    *) echo unexpected >> "$RUNNER_TEMP/calls"; exit 9 ;;
esac
""",
        }
        for executable, body in scripts.items():
            script = self.directory / executable
            script.write_text("#!/bin/bash\n" + body)
            script.chmod(0o755)

    def shell(self, script, **env):
        return subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=self.env | env,
                              capture_output=True, text=True, timeout=5)

    def calls(self):
        path = self.directory / "calls"
        call_list = path.read_text().splitlines() if path.exists() else []
        path.unlink(missing_ok=True)
        return call_list

    def test_only_service_stage_workflow_on_protected_main(self):
        for change in ({}, {"OPERATION": "rollback",
                            "WORKFLOW": "fin-assist/assistant/.github/workflows/rollback-stage.yml@refs/heads/main"}):
            self.assertEqual(self.shell(self.gate, **change).returncode, 0)
        for key, value in (("EVENT", "pull_request"), ("REF", "refs/heads/topic"),
                           ("PROTECTED", "false"), ("WORKFLOW", "unrelated"), ("OPERATION", "remove"),
                           ("GITHUB_REPOSITORY", "fin-assist/go-service-template")):
            with self.subTest(key=key):
                self.assertNotEqual(self.shell(self.gate, **{key: value}).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_initial_404_does_not_repeat_dispatch_and_records_exact_request(self):
        result = self.shell(self.dispatch, FIRST_ERROR="404", OPERATION="rollback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["dispatch", "get", "get", "jobs"])
        self.assertEqual((self.directory / "args").read_text().splitlines(), [
            "api", "-X", "POST", "-H", "X-GitHub-Api-Version: 2026-03-10",
            "repos/fin-assist/infra/actions/workflows/stage-service.yml/dispatches",
            "-f", "ref=main", "-f", "inputs[repository]=fin-assist/assistant",
            "-f", "inputs[operation]=rollback", "-f", "inputs[run]=123", "-f", "inputs[attempt]=2",
            "--jq", ".workflow_run_id",
        ])
        self.assertIn("https://github.com/fin-assist/infra/actions/runs/456",
                      (self.directory / "summary").read_text())

    def test_missing_token_unsuccessful_runs_and_skipped_job_fail(self):
        self.assertNotEqual(self.shell(self.dispatch, GH_TOKEN="").returncode, 0)
        self.assertEqual(self.calls(), [])
        for result in ("failure", "skipped", "cancelled"):
            with self.subTest(result=result):
                self.assertNotEqual(self.shell(self.dispatch, CONCLUSION=result).returncode, 0)
                self.assertEqual(self.calls(), ["dispatch", "get"])
        self.assertNotEqual(self.shell(self.dispatch, APPLY_SUCCEEDED="0").returncode, 0)
        self.assertEqual(self.calls(), ["dispatch", "get", "jobs"])
        self.assertNotEqual(self.shell(self.dispatch, FIRST_ERROR="403").returncode, 0)
        self.assertEqual(self.calls(), ["dispatch", "get"])

    def test_rejects_invalid_dispatch_ids_and_post_failure(self):
        for dispatch_id in ("", "null", "0", "12x"):
            with self.subTest(dispatch_id=dispatch_id):
                self.assertNotEqual(self.shell(self.dispatch, DISPATCH_ID=dispatch_id).returncode, 0)
                self.assertEqual(self.calls(), ["dispatch"])
        self.assertNotEqual(self.shell(self.dispatch, DISPATCH_FAIL="1").returncode, 0)
        self.assertEqual(self.calls(), ["dispatch"])

    def test_persistent_404_fails_and_pending_run_can_succeed(self):
        self.assertNotEqual(self.shell(self.dispatch, ALL_NOT_FOUND="1").returncode, 0)
        self.assertEqual(self.calls(), ["dispatch", *["get"] * 7])
        self.assertEqual(self.shell(self.dispatch, PENDING_FIRST="1").returncode, 0)
        self.assertEqual(self.calls(), ["dispatch", "get", "get", "jobs"])
