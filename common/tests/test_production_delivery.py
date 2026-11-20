import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock


spec = importlib.util.spec_from_file_location(
    "production_delivery", Path(__file__).resolve().parents[1] / "scripts/production_delivery.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class MainPublicationTests(unittest.TestCase):
    def test_source_head_or_ci_mismatch_stops_selection(self):
        repo, sha = "fin-assist/user", "a" * 40
        main = {"protected": True, "commit": {"sha": sha}}
        run = {"status": "completed", "conclusion": "success", "head_sha": sha,
               "event": "push", "path": ".github/workflows/ci.yml"}
        for update in ({"conclusion": "failure"}, {"status": "in_progress"}, {"head_sha": "b" * 40},
                       {"event": "pull_request"}, {"path": ".github/workflows/release.yml"}):
            with self.subTest(update=update):
                get = Mock(side_effect=[main, {"total_count": 1, "workflow_runs": [{**run, **update}]}])
                with self.assertRaises(ValueError):
                    delivery.current_push(repo, sha, get)
        for invalid in ({**main, "protected": False}, {"protected": True, "commit": {"sha": "b" * 40}}):
            with self.assertRaises(ValueError):
                delivery.current_push(repo, sha, Mock(return_value=invalid))
        for total in (0, 2):
            with self.assertRaises(ValueError):
                delivery.current_push(repo, sha, Mock(side_effect=[main, {"total_count": total, "workflow_runs": []}]))
        self.assertEqual(delivery.current_push(
            repo, sha, Mock(side_effect=[main, {"total_count": 1, "workflow_runs": [run]}])), run)

    def test_publisher_search_uses_time_filter_and_checks_run_fields_locally(self):
        good = {"status": "completed", "conclusion": "success", "event": "workflow_run", "head_branch": "main"}
        invalid = [{**good, key: value} for key, value in (
            ("status", "in_progress"), ("conclusion", "failure"), ("event", "push"), ("head_branch", "feature"))]
        get = Mock(return_value={"total_count": 5, "workflow_runs": [*invalid, good]})
        now = datetime(2026, 11, 20, 8, 30, tzinfo=timezone.utc)
        self.assertEqual(list(delivery.publisher_runs("fin-assist/infra", "2026-11-20T08:00:00Z", get, now)), [good])
        query = get.call_args.args[0]
        self.assertIn("created=", query)
        self.assertNotIn("status=", query)
        self.assertNotIn("branch=", query)
        self.assertNotIn("event=", query)


class ProductionDispatchTests(unittest.TestCase):
    def setUp(self):
        import re
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        ci = SimpleNamespace(SHARED="fin-assist/go-ci", SERVICES={"fin-assist/user": "user"},
                             SHA=re.compile("[0-9a-f]{40}"))
        modules = {"render_release": Mock(), "service_delivery": SimpleNamespace(ci=ci)}
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [str(scripts), *sys.path]):
            import publish_production
        self.publisher = publish_production
        self.repo = "fin-assist/user"
        self.sha = "a" * 40
        self.run = dict(event="workflow_dispatch", status="in_progress", id=101, run_attempt=1,
                        head_branch="main", head_sha=self.sha, path=".github/workflows/deploy.yml",
                        repository={"full_name": self.repo}, head_repository={"full_name": self.repo},
                        referenced_workflows=[{"path": publish_production.SHARED_RELAY + "@main",
                                               "sha": self.sha, "ref": "refs/heads/main"}])
        self.branch = {"protected": True, "commit": {"sha": self.sha}}

    def test_only_current_manual_service_main_and_trusted_relay_can_publish(self):
        get = Mock(side_effect=[self.run, self.branch, self.branch, {"merge_base_commit": {"sha": self.sha}}])
        self.assertEqual(self.publisher.authorize(self.repo, "101", "1", get), self.sha)
        for key, value in (("event", "push"), ("status", "completed"), ("id", 102), ("run_attempt", 2),
                           ("head_branch", "test"), ("path", ".github/workflows/ci.yml"),
                           ("head_repository", {"full_name": "fork/user"}), ("referenced_workflows", [])):
            with self.subTest(key=key):
                get = Mock(side_effect=[{**self.run, key: value}, self.branch])
                with self.assertRaises(ValueError):
                    self.publisher.authorize(self.repo, "101", "1", get)
        for source in ({**self.branch, "protected": False}, {"protected": True, "commit": {"sha": "b" * 40}}):
            with self.assertRaises(ValueError):
                self.publisher.authorize(self.repo, "101", "1", Mock(side_effect=[self.run, source]))
        with self.assertRaises(ValueError):
            self.publisher.authorize(self.repo, "101", "1", Mock(side_effect=[
                self.run, self.branch, self.branch, {"merge_base_commit": {"sha": "b" * 40}}]))

    def test_private_registry_rejects_public_and_existing_different_content(self):
        from types import SimpleNamespace
        import tempfile
        from unittest.mock import patch
        env = {"REGISTRY_TOKEN": "test", "REGISTRY_USER": "publisher"}
        meta = dict(name="production-user", package_type="container", visibility="private", owner={"login": "fin-assist"})
        with tempfile.TemporaryDirectory() as folder, patch.dict(self.publisher.os.environ, env):
            root = Path(folder)
            file = root / "production-user-1.101.1.tgz"
            file.write_bytes(b"approved")
            def helm(cmd, **kwargs):
                if "pull" in cmd:
                    (root / "pulled" / file.name).write_bytes(b"other")
                return SimpleNamespace(returncode=0)
            with patch.object(self.publisher.ci, "api", return_value={**meta, "visibility": "public"}, create=True):
                with self.assertRaisesRegex(ValueError, "private release package"):
                    self.publisher.publish_chart(file, "production-user", "1.101.1", root, Mock())
            with patch.object(self.publisher.ci, "api", return_value=meta, create=True), patch.object(
                    self.publisher.subprocess, "run", side_effect=helm) as calls:
                with self.assertRaisesRegex(ValueError, "differs"):
                    self.publisher.publish_chart(file, "production-user", "1.101.1", root, Mock())
                self.assertFalse(any("push" in call.args[0] for call in calls.call_args_list))

    def test_publication_verifies_bytes_and_argo_version_selection(self):
        from types import SimpleNamespace
        import tempfile
        from unittest.mock import patch
        env = {"REGISTRY_TOKEN": "test", "REGISTRY_USER": "publisher"}
        meta = dict(name="production-user", package_type="container", visibility="private", owner={"login": "fin-assist"})
        for selected, denied in (("1.101.1", False), ("1.102.1", False), ("1.101.1", True)):
            with self.subTest(selected=selected, denied=denied), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                file = root / "production-user-1.101.1.tgz"
                file.write_bytes(b"approved")
                stored = {}
                def helm(cmd, **kwargs):
                    if "push" in cmd:
                        stored["1.101.1"] = file.read_bytes()
                    if "pull" in cmd:
                        wanted = cmd[cmd.index("--version") + 1]
                        version = selected if wanted == "1.*" else wanted
                        if denied:
                            return SimpleNamespace(returncode=1, stderr="403 forbidden")
                        if version not in stored and wanted != "1.*":
                            return SimpleNamespace(returncode=1, stderr="manifest unknown")
                        (root / "pulled" / f"production-user-{version}.tgz").write_bytes(stored.get(version, b"newer"))
                    return SimpleNamespace(returncode=0)
                check = Mock()
                with patch.dict(self.publisher.os.environ, env), patch.object(
                        self.publisher.ci, "api", return_value=meta, create=True), patch.object(
                        self.publisher.subprocess, "run", side_effect=helm):
                    if denied or selected != "1.101.1":
                        with self.assertRaises(ValueError):
                            self.publisher.publish_chart(file, "production-user", "1.101.1", root, check)
                    else:
                        self.publisher.publish_chart(file, "production-user", "1.101.1", root, check)
                self.assertEqual(check.call_count, 0 if denied else 1)
                self.assertEqual(bool(stored), not denied)
