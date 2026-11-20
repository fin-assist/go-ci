import json
from pathlib import Path
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, "path", [str(ROOT / "common/scripts"), *sys.path]):
    import production_chart as charts
    import production_observation as obs
    import production_rollback as rollback

SERVICE, VERSION, SHA = "user", "1.40.2", "a" * 40
A, B, C = [f"ghcr.io/fin-assist/infra-service-user@sha256:{i:064x}" for i in range(1, 4)]
BASE = {"schema": 1, "service": SERVICE, "version": VERSION}
OBSERVE, RECORD = "observe-production-release", "record-production-release"
REJECT, FINISH = "reject-production-release", "finish-production-rollback"


def row(task, **fields):
    return dict(creator={"id": 7}, environment="production-user", task=task, payload={**BASE, **fields})


def api(*rows):
    return lambda path: [r for r in rows if f"task={r['task']}&" in path]


class ObservationTests(unittest.TestCase):
    def test_trusted_exact_command_and_absent_service(self):
        valid = row(OBSERVE, operation="deploy", stable_image=None, candidate=False)
        wrong_version = {**valid, "payload": {**valid["payload"], "version": "1.40.1"}}
        foreign = [{**valid, **change} for change in ({"task": RECORD}, {"environment": "production-other"},
                                                      {"creator": {"id": 8}})]
        get = Mock(side_effect=[[], foreign + [wrong_version], [], [valid, valid]])
        with patch.object(obs.time, "sleep") as delay:
            self.assertEqual(obs.wait(SERVICE, VERSION, "deploy", "7", get), valid["payload"])
            delay.assert_called_once_with(10)
        with self.assertRaisesRegex(rollback.RollbackError, "could not establish"):
            obs.wait(SERVICE, VERSION, "deploy", "7", api(row("reject-production-observation", operation="deploy", status="failure")))
        for change in ({"candidate": 0}, {"schema": True}, {"operation": "rollback"}, {"stable_image": "untrusted"},
                       {"extra": 1}, {"candidate": True}):
            revised = {**valid, "payload": {**valid["payload"], **change}}
            with self.subTest(change=change), self.assertRaises(rollback.RollbackError):
                obs.wait(SERVICE, VERSION, "deploy", "7", api(valid, revised))

    def test_distinct_working_image_with_live_candidate_promotion_and_failed_deploy(self):
        head = rollback.Head("1.30.1", C, "deploy", SHA, "")
        def history(desired, previous):
            return {"image": desired, "previous_image": previous}
        records = {"1.15.1": history(B, A), "1.20.1": history(B, B),
                   "1.30.1": history(C, B)}
        self.assertEqual(rollback.previous_image(head, records, B, True), A)
        self.assertEqual(rollback.previous_image(head, records, C, False), B)
        self.assertEqual(rollback.previous_image(head, records, B, False), B)
        self.assertEqual(rollback.previous_image(head, records, None, False), B)
        del records["1.30.1"]  # Guard may have rejected C before its receipt.
        self.assertEqual(rollback.previous_image(head, records, B, True), A)

    def test_redeployed_candidate_can_share_digest_with_previous_serving_revision(self):
        # A previously served, B is stable, and a new release of A is now a
        # candidate. Its former stable revision is the right rollback target.
        head = rollback.Head("1.30.1", A, "deploy", SHA, "")
        history = {"1.10.1": {"image": A, "previous_image": C},
                   "1.20.1": {"image": B, "previous_image": A},
                   "1.30.1": {"image": A, "previous_image": B}}
        self.assertEqual(rollback.previous_image(head, history, B, True), A)

    def test_gated_deploy_and_rollback_completion(self):
        accepted = row(RECORD, image=C, previous_image=B)
        finish = row(FINISH, image=A, status="success", operation="rollback")
        self.assertEqual(obs.wait(SERVICE, VERSION, "deploy", "7", api(accepted), image=C), accepted["payload"])
        with patch.object(obs.time, "sleep") as delay:
            self.assertEqual(obs.wait(SERVICE, VERSION, "rollback", "7", Mock(side_effect=[[], [], [], [finish]]),
                                      image=A), finish["payload"])
            delay.assert_called_once()
        for op, task, fields in (("deploy", REJECT, {"image": C, "status": "failure"}),
                                 ("rollback", FINISH, {"image": C, "status": "failure"}),
                                 ("rollback", FINISH, {"image": A, "status": "success"}),
                                 ("deploy", RECORD, {"image": A, "previous_image": None})):
            if task != RECORD:
                fields["operation"] = op
            with self.subTest(op=op, task=task), self.assertRaises(rollback.RollbackError):
                obs.wait(SERVICE, VERSION, op, "7", api(row(task, **fields)), image=C)
        with patch.object(obs.time, "sleep"), self.assertRaisesRegex(rollback.RollbackError, "Timed out"):
            obs.wait(SERVICE, VERSION, "rollback", "7", api(accepted), image=C)

    def test_deterministic_observation_package_contains_only_one_job(self):
        renderer = Mock(SERVICES=["user"])
        job = {"kind": "Job", "metadata": {"namespace": "plainfin-apps", "name": "observe-40-2"}}
        renderer.release_observation.return_value = job
        with tempfile.TemporaryDirectory() as tmp:
            def build():
                return charts.observation(SERVICE, SHA, VERSION, "deploy", renderer, tmp)
            package = build()
            first = package.read_bytes()
            self.assertEqual(build().read_bytes(), first)
            self.assertEqual(package.name, "production-user-2.40.2.tgz")
            with tarfile.open(package) as source:
                self.assertEqual(json.load(source.extractfile("production-user/workloads.yaml")), job)
                self.assertEqual(json.load(source.extractfile("production-user/Chart.yaml"))["version"], "2.40.2")
            renderer.release_observation.assert_called_with(SERVICE, VERSION, "deploy")
            renderer.release_observation.return_value = {"kind": "Rollout"}
            with self.assertRaises(ValueError):
                build()

    def test_initial_get_retry_does_not_repeat_dispatch_and_other_errors_fail(self):
        script = yaml.safe_load((ROOT/".github/workflows/shared-production-deploy.yml").read_text())["jobs"][
            "release"]["steps"][-1]["run"]
        for status, expected in ((404, 0), (403, 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                tmp = Path(folder)
                for name, text in {"sleep": "exit 0", "gh": """
if [[ "$*" == *dispatches* ]]; then echo dispatch >> "$RUNNER_TEMP/calls"; echo 123; exit 0; fi
echo get >> "$RUNNER_TEMP/calls"
if [[ ! -f "$RUNNER_TEMP/retry" ]]; then
  touch "$RUNNER_TEMP/retry"
  echo "gh: error (HTTP $STATUS)" >&2; exit 1
fi
echo success
"""}.items():
                    file = tmp/name
                    file.write_text("#!/bin/bash\n" + text)
                    file.chmod(0o755)
                env = dict(os.environ, RUNNER_TEMP=folder, PATH=folder+":"+os.environ["PATH"], STATUS=str(status),
                           GITHUB_STEP_SUMMARY=str(tmp/"summary"))
                run = subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, capture_output=True, timeout=3)
                self.assertEqual(run.returncode, expected)
                self.assertEqual((tmp/"calls").read_text().splitlines(), ["dispatch", "get", "get"] if status == 404
                                 else ["dispatch", "get"])
