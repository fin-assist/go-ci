import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "scripts"), *sys.path]):
    import production_rollback as rollback


def image(n, *, legacy=False):
    package = "user" if legacy else "infra-service-user"
    return f"ghcr.io/fin-assist/{package}@sha256:{n:064x}"


def record(version, requested, previous):
    return {"version": version, "service": "user", "schema": 1, "image": requested, "previous_image": previous}


def deployment(payload):
    return dict(creator={"id": 12}, environment="production-user", task="record-production-release", payload=payload)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.bad = image(1, legacy=True), image(2), image(3)
        self.sha = "b" * 40
        self.head = rollback.Head("1.101.1", self.b, "deploy", self.sha, "checksum")

    def test_working_legacy_baseline_or_failed_target_returns_actual_observed_image(self):
        # The very first FIN-148 deploy retained a live pre-GHCR baseline. The
        # same evidence permits undo regardless of whether the new pod started.
        records = {"1.101.1": record("1.101.1", self.b, self.a)}
        self.assertEqual(rollback.previous_image(self.head, records), self.a)
        records["1.105.1"] = record("1.105.1", self.bad, self.a)
        head = rollback.Head("1.105.1", self.bad, "deploy", self.sha, "")
        self.assertEqual(rollback.previous_image(head, records), self.a)

    def test_noop_skips_unapplied_target_and_uses_latest_distinct_observation(self):
        history = {v: record(v, requested, old) for v, requested, old in (
            ("1.3.1", self.b, self.a), ("1.10.1", self.bad, self.b), ("1.101.1", self.b, self.b))}
        self.assertEqual(rollback.previous_image(self.head, history), self.a)
        self.assertEqual(rollback.previous_image(
            rollback.Head("1.102.1", self.b, "rollback", self.sha, ""),
            {**history, "1.102.1": record("1.102.1", self.b, None)}), self.a)

    def test_empty_baseline_insufficient_observations_and_mismatched_or_newer_head(self):
        for history in (
            {"1.101.1": record("1.101.1", self.b, None)},
            {"1.101.1": record("1.101.1", self.b, self.b)},
            {"1.101.1": record("1.101.1", self.bad, self.b)},
            {"1.101.1": record("1.101.1", self.b, self.a), "1.102.1": record("1.102.1", self.bad, self.b)},
        ):
            with self.subTest(history=history), self.assertRaises(rollback.RollbackError):
                rollback.previous_image(self.head, history)

    def test_identity_pagination_and_duplicates(self):
        rec = deployment(record("1.101.1", self.b, self.a))
        foreign = {**rec, "creator": {"id": 9}}
        get = Mock(side_effect=[[foreign]*96 + [rec, {**rec, "environment": "production-assistant"},
                        {**rec, "task": "deploy"}, {**rec, "creator": None}], [rec]])
        self.assertEqual(rollback.receipts("user", "12", get), {"1.101.1": rec["payload"]})
        self.assertTrue(get.call_args.args[0].endswith("page=2"))
        bad = {**rec, "payload": {**rec["payload"], "previous_image": self.bad}}
        with self.assertRaisesRegex(rollback.RollbackError, "Conflicting"):
            rollback.receipts("user", "12", Mock(return_value=[rec, bad]))
        for fields in ({"version": "1.x.0"}, {"service": "assistant"}, {"image": image(0)}, {"image": self.b + "/bad"},
                       {"schema": True}, {"extra": True}, {"previous_image": "ghcr.io/evil/user:main"}):
            with self.subTest(fields=fields), self.assertRaises(rollback.RollbackError):
                rollback.receipts("user", "12", Mock(return_value=[{**rec, "payload": {**rec["payload"], **fields}}]))
        with self.assertRaises(rollback.RollbackError):
            rollback.receipts("user", "", Mock())

    def test_waits_for_exact_receipt_and_fails_closed_if_it_never_arrives(self):
        rec = deployment(record("1.101.1", self.b, self.a))
        with patch.object(rollback.time, "sleep") as wait:
            result = rollback.target_for_request(self.head, "1.111.1", self.sha, "user", "12", Mock(side_effect=[[], [rec]]))
            self.assertEqual(result, self.a)
            wait.assert_called_once()
            with self.assertRaisesRegex(rollback.RollbackError, "No cluster receipt"):
                rollback.target_for_request(self.head, "1.111.1", self.sha, "user", "12", Mock(return_value=[]))

    def test_same_rollback_run_is_idempotent_and_stale_runs_cannot_publish(self):
        head = rollback.Head("1.101.1", self.b, "rollback", self.sha, "")
        get = Mock()
        for version in ("1.101.1", "1.101.3"):
            self.assertIsNone(rollback.target_for_request(head, version, self.sha, "user", "12", get))
        for version, sha, current in (("1.100.3", self.sha, head), ("1.101.2", "f"*40, head),
                                      ("1.101.2", self.sha, self.head)):
            with self.assertRaises(rollback.RollbackError):
                rollback.target_for_request(current, version, sha, "user", "12", get)
        get.assert_not_called()

    def test_reads_pulled_immutable_chart_and_detects_foreign_workload(self):
        image_ref = self.b
        def helm(command):
            self.assertEqual(command[command.index("--version")+1], "1.*")
            archive = Path(command[-1])/"production-user-1.77.1.tgz"
            chart = {"name":"production-user","version":"1.77.1",
                     "annotations":{"plainfin.ru/operation":"rollback", "plainfin.ru/request-sha": self.sha}}
            obj = {"kind":"Rollout","metadata":{"name":"user","namespace":"plainfin-apps"},
                   "spec":{"template":{"spec":{"containers":[{"image": image_ref}]}}}}
            with tarfile.open(archive, "w:gz") as tar:
                for path, data in (("Chart.yaml", json.dumps(chart)),
                                   ("workloads.yaml", json.dumps({"kind":"Job"})+"\n---\n"+json.dumps(obj)+"\n")):
                    member = tarfile.TarInfo("production-user/"+path)
                    content = data.encode()
                    member.size = len(content)
                    tar.addfile(member, io.BytesIO(content))
            return Mock(returncode=0)
        with tempfile.TemporaryDirectory() as work:
            head = rollback.selected_release(helm, "user", work)
            self.assertEqual((head.version,head.operation,head.image,head.request_sha), ("1.77.1","rollback",self.b,self.sha))
            image_ref = self.b.replace("infra-service-user", "infra-service-assistant")
            with self.assertRaises(rollback.RollbackError):
                rollback.selected_release(helm, "user", work)
