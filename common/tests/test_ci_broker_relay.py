"""Private CI relay admission, API identity and failure propagation scenarios."""

import datetime
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_broker_relay", ROOT / "common/scripts/ci_broker_relay.py")
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)
SHA = "a" * 40
HEAD = "b" * 40
BASE = "c" * 40
SOURCE = "d" * 40
BROKER_SHA = "e" * 40


def environment():
    return {"GITHUB_REPOSITORY": "fin-assist/go-service-template", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": SHA, "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/42/merge", "SOURCE_HEAD_SHA": HEAD,
            "SOURCE_BASE_SHA": BASE, "SOURCE_PR_NUMBER": "42", "GO_CI_SHA": SOURCE,
            "SOURCE_HEAD_REPOSITORY": "fin-assist/go-service-template"}


def completed_run(inputs):
    return {"id": 999, "display_title": relay.run_title(inputs), "event": "workflow_dispatch",
            "path": ".github/workflows/service-ci.yml", "head_branch": "main", "head_sha": BROKER_SHA,
            "run_attempt": 1, "repository": {"full_name": "fin-assist/infra"},
            "head_repository": {"full_name": "fin-assist/infra"},
            "status": "completed", "conclusion": "success"}


def completed_jobs():
    return [{"name": gate, "run_id": 999, "run_attempt": 1, "head_sha": BROKER_SHA,
             "status": "completed", "conclusion": "success"} for gate in relay.GATES]


class AdmissionTests(unittest.TestCase):
    def test_same_repository_pr_binds_both_parents_and_shared_source(self):
        inputs = relay.request_inputs(environment())
        self.assertEqual(inputs, {"source_repository": "fin-assist/go-service-template",
                                 "source_run_id": "123", "source_run_attempt": "2", "source_sha": SHA,
                                 "source_head_sha": HEAD, "source_base_sha": BASE,
                                 "source_pr_number": "42", "go_ci_sha": SOURCE})

    def test_main_push_has_no_pr_identity(self):
        env = environment() | {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
                               "SOURCE_HEAD_SHA": SHA, "SOURCE_BASE_SHA": "", "SOURCE_PR_NUMBER": ""}
        inputs = relay.request_inputs(env)
        jobs = completed_jobs()
        jobs[-1]["conclusion"] = "skipped"
        self.assertEqual(relay.gate_results(completed_run(inputs), jobs, inputs)["accepted"], "true")
        for update in ({"GITHUB_REF": "refs/heads/other"}, {"SOURCE_PR_NUMBER": "42"},
                       {"SOURCE_BASE_SHA": BASE}, {"SOURCE_HEAD_SHA": HEAD}):
            with self.subTest(update=update), self.assertRaises(relay.Rejected):
                relay.request_inputs(env | update)

    def test_forks_wrong_events_and_malformed_identity_fail_before_api_access(self):
        for update in ({"SOURCE_HEAD_REPOSITORY": "outsider/template"},
                       {"GITHUB_REPOSITORY": "fin-assist/infra"},
                       {"GITHUB_EVENT_NAME": "pull_request_target"},
                       {"GITHUB_RUN_ID": "123\naccepted=true"}, {"GITHUB_RUN_ATTEMPT": "0"},
                       {"SOURCE_PR_NUMBER": "-1"}, {"GO_CI_SHA": "main"},
                       {"GITHUB_SHA": "A" * 40}, {"SOURCE_BASE_SHA": ""}):
            with self.subTest(update=update), self.assertRaises(relay.Rejected):
                relay.request_inputs(environment() | update)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.inputs = relay.request_inputs(environment())
        self.run = completed_run(self.inputs)

    def test_unrelated_runs_are_ignored_but_duplicates_are_rejected(self):
        unrelated = self.run | {"id": 998, "display_title": "another source attempt"}
        self.assertIsNone(relay.select_run([unrelated], self.inputs))
        self.assertEqual(relay.select_run([unrelated, self.run], self.inputs), self.run)
        with self.assertRaises(relay.Rejected):
            relay.select_run([self.run, self.run | {"id": 1000}], self.inputs)

    def test_run_name_is_not_sufficient_without_protected_workflow_identity(self):
        for update in ({"id": True}, {"id": -1}, {"path": ".github/workflows/other.yml"},
                       {"event": "push"}, {"head_branch": "tooling/unreviewed"},
                       {"head_sha": "main"}, {"run_attempt": 2},
                       {"repository": {"full_name": "fin-assist/user"}},
                       {"head_repository": {"full_name": "outsider/infra"}}):
            with self.subTest(update=update), self.assertRaises(relay.Rejected):
                relay.select_run([self.run | update], self.inputs)

    def test_exact_current_jobs_are_required_once(self):
        good = completed_jobs()
        self.assertEqual(relay.gate_results(self.run, good, self.inputs)["accepted"], "true")
        for jobs in (good[:-1], good + good[:1],
                     [good[0] | {"run_id": 998}, *good[1:]],
                     [good[0] | {"run_attempt": 2}, *good[1:]],
                     [good[0] | {"head_sha": HEAD}, *good[1:]],
                     [good[0] | {"status": "in_progress"}, *good[1:]],
                     [good[0] | {"conclusion": None}, *good[1:]]):
            with self.subTest(jobs=jobs), self.assertRaises(relay.Rejected):
                relay.gate_results(self.run, jobs, self.inputs)

    def test_api_optional_attempt_field_does_not_replace_attempt_endpoint(self):
        jobs = completed_jobs()
        for job in jobs:
            del job["run_attempt"]
        self.assertEqual(relay.gate_results(self.run, jobs, self.inputs)["accepted"], "true")

    def test_failed_skipped_or_cancelled_required_gate_never_accepts(self):
        for conclusion in relay.CONCLUSIONS - {"success"}:
            for gate in range(len(relay.GATES)):
                jobs = completed_jobs()
                jobs[gate]["conclusion"] = conclusion
                with self.subTest(conclusion=conclusion, gate=gate):
                    self.assertEqual(relay.gate_results(self.run, jobs, self.inputs)["accepted"], "false")
        self.assertEqual(relay.gate_results(self.run | {"conclusion": "failure"},
                                            completed_jobs(), self.inputs)["accepted"], "false")


class ProtocolTests(unittest.TestCase):
    def test_dispatch_once_and_poll_exact_workflow_then_exact_attempt_jobs(self):
        inputs = relay.request_inputs(environment())
        run = completed_run(inputs)
        calls, ticks = [], [0]

        class API:
            def request(self, path, body=None):
                calls.append((path, body))

            def pages(self, path, key):
                calls.append((path, key))
                if key == "jobs":
                    return completed_jobs()
                return [run | {"status": "in_progress"}] if ticks[0] == 0 else [run]

        def sleep(seconds):
            ticks[0] += seconds

        result = relay.relay(API(), API(), inputs, clock=lambda: ticks[0], sleep=sleep,
                             now=datetime.datetime(2026, 11, 16, tzinfo=datetime.timezone.utc))
        self.assertEqual(result["accepted"], "true")
        self.assertEqual(result["source_run_attempt"], "2")
        self.assertEqual(calls[0], ("/repos/fin-assist/infra/actions/workflows/service-ci.yml/dispatches",
                                   {"ref": "main", "inputs": inputs}))
        self.assertEqual(sum(path.endswith("/dispatches") for path, _ in calls), 1)
        self.assertEqual(calls[-1], ("/repos/fin-assist/infra/actions/runs/999/attempts/1/jobs", "jobs"))
        self.assertIn("branch=main", calls[1][0])
        self.assertIn("created=", calls[1][0])

    def test_timeout_does_not_redispatch_or_use_an_old_attempt(self):
        inputs, ticks = relay.request_inputs(environment()), [0]
        dispatches = []

        class API:
            def request(self, path, body=None):
                dispatches.append(body)

            def pages(self, path, key):
                return []

        def sleep(seconds):
            ticks[0] += seconds

        with patch.object(relay, "TIMEOUT_SECONDS", 30), self.assertRaisesRegex(relay.Rejected, "Timed out"):
            relay.relay(API(), API(), inputs, clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(len(dispatches), 1)

    def test_pagination_and_bounds(self):
        api = relay.GitHub("fixture-token")
        pages = [{"jobs": [{"id": n} for n in range(100)]}, {"jobs": [{"id": 100}]}]
        with patch.object(api, "request", side_effect=pages) as request:
            self.assertEqual(len(api.pages("/repos/fin-assist/infra/actions/runs/999/attempts/1/jobs", "jobs")), 101)
            self.assertTrue(request.call_args.args[0].endswith("page=2"))
        with patch.object(relay, "MAX_PAGES", 1), patch.object(api, "request", return_value=pages[0]):
            with self.assertRaisesRegex(relay.Rejected, "pagination"):
                api.pages("/repos/fin-assist/infra/actions/runs/999/attempts/1/jobs", "jobs")
        with patch.object(api, "request", return_value={"jobs": "not a list"}):
            with self.assertRaises(relay.Rejected):
                api.pages("/repos/fin-assist/infra/actions/runs/999/attempts/1/jobs", "jobs")

    def test_http_errors_are_redacted_and_foreign_destinations_are_rejected(self):
        api = relay.GitHub("fixture-token")
        error = urllib.error.HTTPError("https://api.github.com", 403, "private response", {}, io.BytesIO(b"secret"))
        with patch.object(api.opener, "open", side_effect=error):
            with self.assertRaisesRegex(relay.Rejected, "^GitHub API returned HTTP 403$"):
                api.request("/repos/fin-assist/infra/actions/runs")
        for path in ("https://example.com/", "/repos/other/infra/actions/runs", "/user"):
            with self.subTest(path=path), self.assertRaises(relay.Rejected):
                api.request(path)
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_workflow_executes_only_resolved_shared_code_and_exports_all_gates(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/shared-remote-ci.yml").read_text())
        job = workflow["jobs"]["relay"]
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", job["if"])
        checkout, execute = job["steps"]
        self.assertEqual(checkout["with"]["repository"], "fin-assist/go-ci")
        self.assertEqual(checkout["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertEqual(checkout["with"]["token"], "${{ secrets.GO_MODULES_TOKEN }}")
        self.assertEqual(execute["run"], "python3 -I common/scripts/ci_broker_relay.py")
        self.assertEqual(execute["env"]["CI_BROKER_READ_TOKEN"], "${{ secrets.GO_MODULES_TOKEN }}")
        self.assertEqual(execute["env"]["GO_CI_SHA"], "${{ job.workflow_sha }}")
        self.assertEqual(set(workflow[True]["workflow_call"]["outputs"]),
                         {*relay.GATES, "accepted", "source_run_attempt", "broker_url"})
        self.assertFalse(any("upload-artifact" in step.get("uses", "") for step in job["steps"]))


if __name__ == "__main__":
    unittest.main()
