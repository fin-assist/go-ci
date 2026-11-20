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
        self.assertEqual(self.publisher.authorize(self.repo, "101", "1", get), ("deploy", self.sha))
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

    def test_rollback_is_tied_to_rollback_caller_and_its_protected_relay(self):
        deploy, = self.run["referenced_workflows"]
        rollback = {**deploy, "path": deploy["path"].replace("-deploy.yml", "-rollback.yml")}
        run = {**self.run, "path": ".github/workflows/rollback.yml"}
        for refs in ([rollback], [rollback, deploy]):
            get = Mock(side_effect=[{**run, "referenced_workflows": refs}, self.branch, self.branch,
                                     {"merge_base_commit": {"sha":self.sha}}])
            self.assertEqual(self.publisher.authorize(self.repo, "101", "1", get), ("rollback", self.sha))
        for refs in ([deploy], [{**rollback, "ref":"refs/heads/topic"}], [rollback, rollback]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.publisher.authorize(self.repo, "101", "1",
                    Mock(side_effect=[{**run, "referenced_workflows": refs}, self.branch]))

    def test_dispatch_rollback_uses_observed_image_without_build_or_migration_inputs(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        pub = self.publisher
        old, new = ["ghcr.io/fin-assist/infra-service-user@sha256:" + digit*64 for digit in ("1", "2")]
        head = pub.production_rollback.Head("1.100.1", new, "deploy", self.sha, "checksum")
        payload = {"schema": 1, "service": "user", "version": head.version,
                   "image": new, "previous_image": old}
        recording = {"creator": {"id": 77}, "task": "record-production-release",
                     "environment": "production-user", "payload": payload}
        env = dict(GITHUB_REPOSITORY="fin-assist/go-ci", GITHUB_REF="refs/heads/main",
                   GITHUB_REF_PROTECTED="true", GITHUB_EVENT_NAME="workflow_dispatch",
                   SERVICE_REPOSITORY=self.repo, SERVICE_RUN="101", SERVICE_ATTEMPT="1",
                   RECEIPT_CREATOR_ID="77", DELIVERY_READ_TOKEN="read")
        def get(path, token):
            self.assertEqual(token, "read")
            if path == "repos/fin-assist/infra":
                return {"private": True}
            if path == "repos/fin-assist/infra/branches/main":
                return self.branch
            if path.startswith("repos/fin-assist/infra/deployments?"):
                return [recording]
            raise AssertionError(path)
        def publish(file, name, version, folder, check, helm):
            self.assertEqual((name, version), ("production-user", "1.101.1"))
            check()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(pub.os.environ, env))
            stack.enter_context(patch.object(pub.ci, "INFRA", "fin-assist/infra", create=True))
            stack.enter_context(patch.object(pub.ci, "command", return_value=self.sha, create=True))
            stack.enter_context(patch.object(pub.ci, "api", side_effect=get, create=True))
            stack.enter_context(patch.object(pub, "authorize", return_value=("rollback", self.sha)))
            stack.enter_context(patch.object(pub, "registry"))
            stack.enter_context(patch.object(pub, "publish_chart", side_effect=publish))
            source = stack.enter_context(patch.object(pub.production_delivery, "resolve"))
            selected = stack.enter_context(patch.object(pub.production_rollback, "selected_release", return_value=head))
            chart = stack.enter_context(patch.object(pub.production_chart, "chart"))
            pub.main()
            self.assertEqual(chart.call_args.args[:6], ("user", old, self.sha, "1.101.1", pub.render_release, {}))
            self.assertEqual(chart.call_args.kwargs, {"rollback": True})
            source.assert_not_called()
            self.assertEqual(selected.call_count, 2)
            selected.side_effect = [head, pub.production_rollback.Head("1.120.1", new, "deploy", self.sha, "other")]
            with self.assertRaisesRegex(pub.production_rollback.RollbackError, "changed during rollback"):
                pub.main()

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


class PublisherReceiptTests(unittest.TestCase):
    def test_only_exact_complete_private_evidence_is_returned(self):
        import re
        from types import SimpleNamespace
        from unittest.mock import patch
        sha, repo = "a"*40, "fin-assist/user"
        run = dict(id=50, run_attempt=1, head_sha="c"*40, event="workflow_run")
        source = dict(id=10, run_attempt=2, run_started_at="2026-11-20T08:00:00Z")
        ci = SimpleNamespace(INFRA="fin-assist/infra", SERVICES={repo:"user"}, SHA=re.compile("[0-9a-f]{40}"),
                             validate_source=Mock(), receipt_inputs=Mock())
        private = Mock(ci=ci)
        receipt = dict(source_repository=repo, source_sha=sha, source_run_id="10", source_run_attempt="2",
                       service="user", event="push")
        image = "ghcr.io/fin-assist/infra-service-user@sha256:"+"b"*64
        artifact = dict(expired=False, name="delivery-evidence-1", workflow_run={"id":50})
        cases = ("success", "expired", "previous-attempt", "foreign-source", "foreign-sha", "foreign-run",
                 "foreign-publisher", "missing-gate", "bad-image", "foreign-artifact", "missing-artifact", "other-service",
                 "ambiguous", "foreign-event")
        skip = {"expired", "previous-attempt", "foreign-source", "foreign-sha", "foreign-run",
                "missing-artifact", "other-service"}
        for kind in cases:
            with self.subTest(kind=kind):
                private.reset_mock()
                good_provenance = dict(source=receipt, publisher_run_id=51, publisher_run_attempt=1,
                                       publisher_sha=run["head_sha"], image=image)
                good_item = dict(image=image, provenance=good_provenance)
                provenance = {**good_provenance, "source": dict(receipt), "publisher_run_id": 50}
                item = dict(image=image, provenance=provenance)
                meta = dict(artifact)
                if kind == "expired":
                    meta["expired"] = True
                if kind == "foreign-artifact":
                    meta["workflow_run"] = {"id": 888}
                other_source = {"previous-attempt": ("source_run_attempt", "1"),
                                "foreign-source": ("source_repository", "fin-assist/assistant"),
                                "foreign-sha": ("source_sha", "f" * 40), "foreign-run": ("source_run_id", "9")}
                if kind in other_source:
                    key, value = other_source[kind]
                    provenance["source"][key] = value
                if kind == "foreign-publisher":
                    provenance["publisher_run_id"] = 52
                private.gates.side_effect = ValueError("isolated-stage failed") if kind == "missing-gate" else None
                private.reference.side_effect = ValueError("image mismatch") if kind == "bad-image" else None
                entries = {50: {} if kind == "other-service" else {"user": item}, 51: {"user": good_item}}
                evidence = lambda run_id, attempt: entries[run_id]
                artifacts = {"total_count": 1, "artifacts": [meta]}
                runs = [run]
                responses = [{"total_count": 0, "artifacts": []} if kind == "missing-artifact" else artifacts]
                if kind in skip:
                    runs.append({**run, "id": 51})
                    responses.extend([{"total_count": 1, "artifacts": [{**artifact, "workflow_run": {"id": 51}}]}, runs[1]])
                else:
                    responses.append({**run, "event": "workflow_dispatch"} if kind == "foreign-event" else run)
                if kind == "ambiguous":
                    responses[0] = {"total_count": 2, "artifacts": [meta, meta]}
                with patch.object(delivery, "current_push", return_value=source), patch.object(
                        delivery, "publisher_runs", return_value=runs):
                    get = Mock(side_effect=responses)
                    if kind == "success" or kind in skip:
                        expected = good_item if kind in skip else item
                        self.assertEqual(delivery.resolve(repo, sha, Mock(), get, private, evidence), expected)
                        self.assertEqual(private.gates.call_args.kwargs["required"], ("publish", "isolated-stage"))
                        private.source_success.assert_called_once()
                        private.broker_success.assert_called_once()
                    elif kind in ("ambiguous", "foreign-event"):
                        message = "Ambiguous publication artifact" if kind == "ambiguous" else "Expected a trusted publisher event"
                        with self.assertRaisesRegex(ValueError, message):
                            delivery.resolve(repo, sha, Mock(), get, private, evidence)
                        private.gates.assert_not_called()
                    else:
                        with self.assertRaises(ValueError):
                            delivery.resolve(repo, sha, Mock(), get, private, evidence)


class ReleaseManifestTests(unittest.TestCase):
    def test_chart_keeps_service_scope_and_workload_order(self):
        import json
        import sys
        import tarfile
        import tempfile
        from unittest.mock import patch
        scripts=Path(__file__).resolve().parents[1]/"scripts"
        with patch.object(sys,"path",[str(scripts),*sys.path]):
            import production_chart as charts
            import production_argocd as argo
        renderer=Mock(SERVICES=argo.SERVICES, PEERS=set(argo.SERVICES)-{"bank-bff"})
        for service in argo.SERVICES:
            renderer.reset_mock()
            image="ghcr.io/fin-assist/infra-service-"+service+"@sha256:"+"a"*64
            renderer.rollout.return_value={"kind":"Rollout","spec":{"literal":"{{ unchanged }}",
                "strategy":{"blueGreen":{"activeService":service,"autoPromotionEnabled":True}}}}
            renderer.release_receipt.return_value={"kind":"Job","metadata":{"name":"receipt"}}
            renderer.migration.return_value={"kind":"Job"}
            with tempfile.TemporaryDirectory() as tmp:
                archive=charts.chart(service,image,"b"*40,"1.20.1",renderer,{service:2},tmp)
                first=archive.read_bytes()
                renderer.reset_mock()
                self.assertEqual(charts.chart(service,image,"b"*40,"1.20.1",renderer,{service:2},tmp).read_bytes(),first)
                with tarfile.open(archive) as tar:
                    data=tar.extractfile(f"production-{service}/workloads.yaml").read().decode()
                    objects=[json.loads(doc) for doc in data.split("---\n")]
                self.assertEqual(objects[-1],renderer.rollout.return_value)
                self.assertEqual([o["kind"] for o in objects],["Job", "Rollout"] if service=="bank-bff" else ["Job","Job","Rollout"])
                self.assertEqual([p["target"]["kind"] for p in argo.exclusions(service)],list(reversed([o["kind"] for o in objects[1:]])))
                self.assertFalse(argo.application(service)["spec"]["syncPolicy"]["automated"]["enabled"])
                if service!="bank-bff":
                    renderer.migration.assert_called_once_with(service,"sha256:"+"a"*64,2,"infra-service-"+service)
                renderer.rollout.assert_called_once_with(service,image)

    def test_rollback_preserves_exact_observed_image_without_sql(self):
        import json
        import tarfile
        import tempfile
        import sys
        from unittest.mock import patch
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            from production_chart import chart
        renderer = Mock(SERVICES=("user",), PEERS={"user"})
        renderer.release_receipt.return_value = {"kind": "Job", "metadata": {"name": "receipt"}}
        renderer.rollout.return_value = {"kind": "Rollout", "spec": {
            "strategy": {"blueGreen": {"activeService": "user", "autoPromotionEnabled": True}}}}
        sha = "a" * 40
        for package in ("user", "infra-service-user"):
            image = "ghcr.io/fin-assist/" + package + "@sha256:" + "b" * 64
            with tempfile.TemporaryDirectory() as tmp:
                path = chart("user", image, sha, "1.25.1", renderer, {}, tmp, rollback=True)
                with tarfile.open(path) as archive:
                    manifest = json.load(archive.extractfile("production-user/Chart.yaml"))
                self.assertEqual(manifest["annotations"]["plainfin.ru/operation"], "rollback")
                self.assertEqual(manifest["appVersion"], "b" * 64)
                renderer.migration.assert_not_called()
                renderer.release_receipt.assert_called_with("user", image, "1.25.1", "rollback")
                renderer.rollout.assert_called_with("user", image)
                with self.assertRaises(ValueError):
                    chart("user", image.replace(package+"@", "assistant@"), sha, "1.25.1", renderer, {}, tmp, rollback=True)
