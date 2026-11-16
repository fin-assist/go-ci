"""Private CI relay admission, API identity and failure propagation scenarios."""

import datetime
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import http.client
import importlib.util
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import urllib.response

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


class MemoryResponse(io.BytesIO):
    """Minimal successful opener response, including HTTP header access."""

    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}


def http_response(body, *, declared_length=None, status=200):
    """Parse a real HTTP response from socket bytes, without a network socket."""
    length = str(len(body)) if declared_length is None else str(declared_length)
    wire = (f"HTTP/1.1 {status} Fixture\r\nContent-Length: {length}\r\n"
            "Connection: close\r\n\r\n").encode() + body

    class Socket:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(wire)

    response = http.client.HTTPResponse(Socket())
    response.begin()
    return response


class MemoryHTTPS(urllib.request.BaseHandler):
    """Feed a real urllib opener without making a network connection."""

    handler_order = 0

    def __init__(self, body=b"{}", status=200, headers=None):
        self.body, self.status = body, status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        response = urllib.response.addinfourl(io.BytesIO(self.body), self.headers,
                                             request.full_url, self.status)
        response.msg = "fixture response"
        return response


class AdmissionTests(unittest.TestCase):
    def test_same_repository_pr_binds_both_parents_and_shared_source(self):
        inputs = relay.request_inputs(environment())
        self.assertEqual(inputs, {"source_repository": "fin-assist/go-service-template",
                                 "source_run_id": "123", "source_run_attempt": "2", "source_sha": SHA,
                                 "source_head_sha": HEAD, "source_base_sha": BASE,
                                 "source_pr_number": "42", "go_ci_sha": SOURCE,
                                 "mode": "service", "qase90_revisions": ""})

    def test_all_six_services_keep_exact_identity_and_bff_skips_only_migrations(self):
        for name in ('go-service-template', 'assistant', 'user', 'analytics', 'bank-bff', 'bank-statement-data'):
            repository = 'fin-assist/' + name
            with self.subTest(repository=repository):
                inputs = relay.request_inputs(environment() | {'GITHUB_REPOSITORY': repository,
                    'SOURCE_HEAD_REPOSITORY': repository})
                jobs = completed_jobs()
                if name == 'bank-bff': jobs[-1]['conclusion'] = 'skipped'
                self.assertEqual(relay.gate_results(completed_run(inputs), jobs, inputs)['accepted'], 'true')
                jobs[-1]['conclusion'] = 'success' if name == 'bank-bff' else 'skipped'
                self.assertEqual(relay.gate_results(completed_run(inputs), jobs, inputs)['accepted'], 'false')

    def test_qase_mode_is_bff_only_with_two_bounded_immutable_revisions(self):
        env = environment() | {'GITHUB_REPOSITORY': 'fin-assist/bank-bff',
            'SOURCE_HEAD_REPOSITORY': 'fin-assist/bank-bff', 'CI_BROKER_MODE': 'qase90',
            'QASE90_REVISIONS': json.dumps({'bsd': HEAD, 'analytics': BASE})}
        inputs = relay.request_inputs(env)
        self.assertEqual(json.loads(inputs['qase90_revisions']), {'bsd': HEAD, 'analytics': BASE})
        self.assertEqual(relay.run_title(inputs), 'Qase 90 CI fin-assist/bank-bff 123/2')
        run = completed_run(inputs)
        qase = dict(completed_jobs()[0], name='qase90')
        self.assertEqual(relay.gate_results(run, [qase], inputs)['accepted'], 'true')
        for status in ('failure', 'skipped', 'cancelled', 'timed_out'):
            self.assertEqual(relay.gate_results(run, [qase | {'conclusion': status}], inputs)['accepted'], 'false')
        for jobs in ([], completed_jobs(), [qase, qase]):
            with self.assertRaises(relay.Rejected): relay.gate_results(run, jobs, inputs)
        for change in ({'CI_BROKER_MODE': 'unknown'}, {'CI_BROKER_MODE': 'service'},
                       {'GITHUB_REPOSITORY': 'fin-assist/assistant'},
                       {'SOURCE_HEAD_REPOSITORY': 'outsider/bank-bff'}):
            with self.subTest(change=change), self.assertRaises(relay.Rejected): relay.request_inputs(env | change)
        for value in ('', '{}', '[]', 'null', '{"bsd":"main","analytics":"main"}',
                      json.dumps({'bsd': HEAD, 'analytics': BASE, 'repository': 'outsider/source'}),
                      '{"bsd":"' + HEAD + '","bsd":"' + HEAD + '","analytics":"' + BASE + '"}',
                      ' ' * 257):
            with self.subTest(value=value), self.assertRaises(relay.Rejected):
                relay.request_inputs(env | {'QASE90_REVISIONS': value})
        self.assertIsNone(relay.select_run([run | {'display_title': 'Service CI fin-assist/bank-bff 123/2'}], inputs))

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

        result = relay.relay(API(), inputs, clock=lambda: ticks[0], sleep=sleep,
                             now=datetime.datetime(2026, 11, 16, tzinfo=datetime.timezone.utc))
        self.assertEqual(result["accepted"], "true")
        self.assertEqual(result["source_run_attempt"], "2")
        self.assertEqual(calls[0], ("/repos/fin-assist/infra/actions/workflows/service-ci.yml/dispatches",
                                   {"ref": "main", "inputs": {
                                       "source_repository": "fin-assist/go-service-template",
                                       "source_run_id": "123", "source_run_attempt": "2", "source_sha": SHA,
                                       "source_head_sha": HEAD, "source_base_sha": BASE,
                                       "source_pr_number": "42", "go_ci_sha": SOURCE}}))
        self.assertEqual(sum(path.endswith("/dispatches") for path, _ in calls), 1)
        self.assertEqual(calls[-1], ("/repos/fin-assist/infra/actions/runs/999/attempts/1/jobs", "jobs"))
        self.assertIn("branch=main", calls[1][0])
        self.assertIn("created=", calls[1][0])

    def test_qase_dispatch_adds_only_the_two_bounded_mode_inputs(self):
        inputs = relay.request_inputs(environment() | {
            "GITHUB_REPOSITORY": "fin-assist/bank-bff", "SOURCE_HEAD_REPOSITORY": "fin-assist/bank-bff",
            "CI_BROKER_MODE": "qase90", "QASE90_REVISIONS": json.dumps({"bsd": HEAD, "analytics": BASE})})
        calls = []

        class API:
            def request(self, path, body=None): calls.append((path, body))
            def pages(self, path, key):
                if key == "jobs": return [dict(completed_jobs()[0], name="qase90")]
                return [completed_run(inputs)]

        self.assertEqual(relay.relay(API(), inputs)["accepted"], "true")
        self.assertEqual(calls, [("/repos/fin-assist/infra/actions/workflows/service-ci.yml/dispatches",
            {"ref": "main", "inputs": {
                "source_repository": "fin-assist/bank-bff", "source_run_id": "123", "source_run_attempt": "2",
                "source_sha": SHA, "source_head_sha": HEAD, "source_base_sha": BASE, "source_pr_number": "42",
                "go_ci_sha": SOURCE, "mode": "qase90",
                "qase90_revisions": json.dumps({"analytics": BASE, "bsd": HEAD}, separators=(",", ":"))}})])

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
            relay.relay(API(), inputs, clock=lambda: ticks[0], sleep=sleep)
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
        self.assertEqual(workflow["permissions"], {})
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", job["if"])
        checkout, execute = job["steps"]
        self.assertEqual(checkout["with"]["repository"], "fin-assist/go-ci")
        self.assertEqual(checkout["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertEqual(checkout["with"]["token"], "${{ secrets.GO_MODULES_TOKEN }}")
        self.assertEqual(execute["run"], "python3 -I common/scripts/ci_broker_relay.py")
        self.assertNotIn("CI_BROKER_READ_TOKEN", execute["env"])
        self.assertEqual(execute["env"]["CI_BROKER_TOKEN"], "${{ secrets.CI_BROKER_TOKEN }}")
        self.assertEqual(execute["env"]["GO_CI_SHA"], "${{ job.workflow_sha }}")
        self.assertEqual(execute['env']['CI_BROKER_MODE'], '${{ inputs.mode }}')
        self.assertEqual(execute['env']['QASE90_REVISIONS'], '${{ inputs.qase90_revisions }}')
        self.assertEqual(workflow[True]['workflow_call']['inputs'], {
            'mode': {'type': 'string', 'default': 'service'},
            'qase90_revisions': {'type': 'string', 'default': ''},
        })
        self.assertEqual(set(workflow[True]["workflow_call"]["outputs"]),
                         {*relay.GATES, "qase90", "accepted", "source_run_attempt", "broker_url"})
        self.assertFalse(any("upload-artifact" in step.get("uses", "") for step in job["steps"]))


class HTTPAdapterTests(unittest.TestCase):
    path = "/repos/fin-assist/infra/actions/runs"

    def test_real_opener_rejects_302_without_following_location_or_leaking_body(self):
        api = relay.GitHub("private-token-sentinel")
        handler = MemoryHTTPS(b"private-response-sentinel", 302,
                              {"Location": "https://outsider.example/collect"})
        api.opener.add_handler(handler)
        with patch("socket.create_connection", side_effect=AssertionError("network must not be used")):
            with self.assertRaisesRegex(relay.Rejected, "^GitHub API returned HTTP 302$") as raised:
                api.request(self.path)
        self.assertEqual([request.full_url for request in handler.requests],
                         ["https://api.github.com" + self.path])
        self.assertNotIn("sentinel", str(raised.exception))

    def test_response_byte_limit_reads_only_one_extra_byte_and_accepts_exact_limit(self):
        class Response(MemoryResponse):
            def __init__(self, payload):
                super().__init__(payload)
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        api = relay.GitHub("fixture-token")
        exact = b'{"ok":true}'.ljust(32, b" ")
        for payload, accepted in ((exact, True), (exact + b" ", False)):
            with self.subTest(size=len(payload)):
                response = Response(payload)
                with patch.object(relay, "MAX_RESPONSE_BYTES", 32), \
                     patch.object(api.opener, "open", return_value=response):
                    if accepted:
                        self.assertEqual(api.request(self.path), {"ok": True})
                    else:
                        with self.assertRaisesRegex(relay.Rejected, "^GitHub API response is too large$"):
                            api.request(self.path)
                self.assertEqual(response.read_sizes, [33])
                self.assertTrue(response.closed)

    def test_real_http_response_accepts_exact_length_and_empty_204(self):
        for status, payload, expected in ((200, b'{"ok":true}'.ljust(32, b" "), {"ok": True}),
                                          (204, b"", None)):
            with self.subTest(status=status):
                api = relay.GitHub("fixture-token")
                response = http_response(payload, status=status)
                with patch.object(relay, "MAX_RESPONSE_BYTES", 32), \
                     patch.object(api.opener, "open", return_value=response):
                    self.assertEqual(api.request(self.path), expected)
                self.assertTrue(response.closed)

    def test_real_http_response_short_body_is_transient_before_json_or_empty_success(self):
        for payload in (b'{"private-body-sentinel":', b'{"ok":true}', b""):
            with self.subTest(payload=payload):
                # Sized read() really returns short bytes at EOF without IncompleteRead.
                with http_response(payload, declared_length=100) as probe:
                    self.assertEqual(probe.read(101), payload)
                api = relay.GitHub("private-token-sentinel")
                response = http_response(payload, declared_length=100)
                with patch.object(api.opener, "open", return_value=response), \
                     patch.object(relay.json, "loads", side_effect=AssertionError("short body must not reach JSON")):
                    with self.assertRaises(relay.Transient) as raised:
                        api.request(self.path)
                self.assertNotIn("sentinel", str(raised.exception))
                self.assertTrue(response.closed)

    def test_real_http_response_declared_oversize_is_rejected_before_reading(self):
        api = relay.GitHub("fixture-token")
        response = http_response(b'{"ok":true}', declared_length=33)
        with patch.object(relay, "MAX_RESPONSE_BYTES", 32), \
             patch.object(api.opener, "open", return_value=response), \
             patch.object(response, "read", wraps=response.read) as read:
            with self.assertRaisesRegex(relay.Rejected, "^GitHub API response is too large$") as raised:
                api.request(self.path)
        self.assertNotIsInstance(raised.exception, relay.Transient)
        read.assert_not_called()
        self.assertTrue(response.closed)

    def test_real_http_response_invalid_content_length_is_terminal_without_body_read(self):
        for length in ("private-length-sentinel", "-1", "1.5", ""):
            with self.subTest(length=length):
                api = relay.GitHub("fixture-token")
                response = http_response(b'{"ok":true}', declared_length=length)
                with patch.object(api.opener, "open", return_value=response), \
                     patch.object(response, "read", wraps=response.read) as read:
                    with self.assertRaises(relay.Rejected) as raised:
                        api.request(self.path)
                self.assertNotIsInstance(raised.exception, relay.Transient)
                self.assertNotIn("sentinel", str(raised.exception))
                read.assert_not_called()
                self.assertTrue(response.closed)

    def test_invalid_json_utf8_and_nonobject_payloads_fail_without_echoing_content(self):
        for payload, message in ((b'{"private-response-sentinel":', "invalid JSON"),
                                 (b"\xff", "invalid JSON"),
                                 (b'["private-response-sentinel"]', "an unexpected object"),
                                 (b'"private-response-sentinel"', "an unexpected object"),
                                 (b"null", "an unexpected object"), (b"true", "an unexpected object"),
                                 (b"123", "an unexpected object")):
            with self.subTest(payload=payload):
                api = relay.GitHub("fixture-token")
                api.opener.add_handler(MemoryHTTPS(payload))
                with self.assertRaisesRegex(relay.Rejected, "^GitHub API returned " + message + "$") as raised:
                    api.request(self.path)
                self.assertNotIn("sentinel", str(raised.exception))

    def test_empty_204_dispatch_preserves_post_identity_and_has_no_json_result(self):
        api = relay.GitHub("private-token-sentinel")
        handler = MemoryHTTPS(b"", 204)
        api.opener.add_handler(handler)
        inputs = relay.request_inputs(environment())
        body = {"ref": "main", "inputs": inputs}
        path = "/repos/fin-assist/infra/actions/workflows/service-ci.yml/dispatches"
        self.assertIsNone(api.request(path, body))
        self.assertEqual(len(handler.requests), 1)
        request = handler.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.github.com" + path)
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token-sentinel")

    def test_network_errors_are_transient_and_redact_the_underlying_exception(self):
        for error in (urllib.error.URLError("private-network-sentinel"),
                      TimeoutError("private-timeout-sentinel"),
                      http.client.RemoteDisconnected("private-disconnect-sentinel"),
                      ConnectionResetError("private-reset-sentinel"),
                      ssl.SSLError("private-tls-sentinel"),
                      http.client.IncompleteRead(b"private-body-sentinel", 100)):
            for phase in ("open", "read"):
                with self.subTest(error=type(error).__name__, phase=phase):
                    api = relay.GitHub("private-token-sentinel")
                    response = MemoryResponse()
                    with patch.object(api.opener, "open", return_value=response,
                                      side_effect=error if phase == "open" else None) as opened, \
                         patch.object(response, "read", side_effect=error):
                        with self.assertRaisesRegex(relay.Transient, "^GitHub API request failed$") as raised:
                            api.request(self.path)
                    self.assertEqual(opened.call_count, 1)
                    self.assertNotIn("sentinel", str(raised.exception))
                    self.assertIsInstance(raised.exception, relay.Rejected)
                    if phase == "read":
                        self.assertTrue(response.closed)
                    response.close()

    def test_only_rate_limited_403_and_retryable_http_statuses_are_transient(self):
        cases = ((429, {}, True), (500, {}, True), (503, {}, True),
                 (403, {"Retry-After": "2"}, True), (403, {"X-RateLimit-Remaining": "0"}, True),
                 (403, {}, False), (403, {"X-RateLimit-Remaining": "10"}, False),
                 (401, {}, False), (404, {}, False))
        for status, headers, transient in cases:
            with self.subTest(status=status, headers=headers):
                api = relay.GitHub("fixture-token")
                error = urllib.error.HTTPError("https://api.github.com", status, "private-http-sentinel",
                                               headers, io.BytesIO(b"private-body-sentinel"))
                with patch.object(api.opener, "open", side_effect=error):
                    with self.assertRaises(relay.Rejected) as raised:
                        api.request(self.path)
                self.assertEqual(isinstance(raised.exception, relay.Transient), transient)
                self.assertNotIn("sentinel", str(raised.exception))
                if "Retry-After" in headers:
                    self.assertGreaterEqual(raised.exception.retry_after, 2)
                self.assertTrue(error.fp.closed)

    def test_each_page_uses_remaining_deadline_and_expiry_stops_before_another_request(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                ticks = [10]
                api = relay.GitHub("fixture-token")
                api.clock = lambda: ticks[0]
                api.deadline = 14.5
                responses = iter(({"jobs": [{"id": index} for index in range(100)]}, {"jobs": []}))

                def opened(request, *, timeout):
                    ticks[0] += 5 if expired else 3
                    return MemoryResponse(json.dumps(next(responses)).encode())

                with patch.object(api.opener, "open", side_effect=opened) as opening:
                    if expired:
                        with self.assertRaisesRegex(relay.Rejected, "Timed out"):
                            api.pages(self.path + "/999/attempts/1/jobs", "jobs")
                    else:
                        self.assertEqual(len(api.pages(self.path + "/999/attempts/1/jobs", "jobs")), 100)
                self.assertEqual([call.kwargs["timeout"] for call in opening.call_args_list],
                                 [4.5] if expired else [4.5, 1.5])
                self.assertEqual([call.args[0].full_url.rsplit("page=", 1)[1]
                                  for call in opening.call_args_list], ["1"] if expired else ["1", "2"])


class RetryTests(unittest.TestCase):
    def test_real_short_get_response_is_retried_after_exactly_one_dispatch(self):
        inputs, ticks, sleeps = relay.request_inputs(environment()), [0], []
        api = relay.GitHub("fixture-token")
        responses = [http_response(b"", status=204),
                     http_response(b'{"workflow_runs":', declared_length=100),
                     http_response(json.dumps({"workflow_runs": [completed_run(inputs)]}).encode()),
                     http_response(json.dumps({"jobs": completed_jobs()}).encode())]

        def sleep(seconds):
            sleeps.append(seconds)
            ticks[0] += seconds

        with patch.object(api.opener, "open", side_effect=responses) as opened:
            result = relay.relay(api, inputs, clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(result["accepted"], "true")
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["POST", "GET", "GET", "GET"])
        self.assertTrue(requests[0].full_url.endswith("/service-ci.yml/dispatches"))
        self.assertEqual(requests[1].full_url, requests[2].full_url)
        self.assertIn("/runs/999/attempts/1/jobs?", requests[3].full_url)
        self.assertEqual(sleeps, [relay.POLL_SECONDS])
        self.assertTrue(all(response.closed for response in responses))

    def test_real_short_dispatch_response_is_not_repeated(self):
        api = relay.GitHub("fixture-token")
        response = http_response(b"", declared_length=100)
        with patch.object(api.opener, "open", return_value=response) as opened, \
             patch.object(relay.time, "sleep") as sleep:
            with self.assertRaises(relay.Transient):
                relay.relay(api, relay.request_inputs(environment()), sleep=sleep)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0].get_method(), "POST")
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    def test_relay_installs_one_deadline_covering_dispatch_discovery_and_jobs(self):
        inputs, ticks = relay.request_inputs(environment()), [100]
        api = relay.GitHub("fixture-token")
        clock = lambda: ticks[0]
        responses = iter((None, {"workflow_runs": [completed_run(inputs)]}, {"jobs": completed_jobs()}))

        def opened(request, *, timeout):
            ticks[0] += 5
            response = next(responses)
            return MemoryResponse(b"" if response is None else json.dumps(response).encode())

        with patch.object(relay, "TIMEOUT_SECONDS", 25), \
             patch.object(api.opener, "open", side_effect=opened) as opening:
            result = relay.relay(api, inputs, clock=clock)
        self.assertEqual(result["accepted"], "true")
        self.assertIs(api.clock, clock)
        self.assertEqual(api.deadline, 125)
        self.assertEqual([call.kwargs["timeout"] for call in opening.call_args_list], [25, 20, 15])
        self.assertEqual([call.args[0].get_method() for call in opening.call_args_list], ["POST", "GET", "GET"])

    def test_transient_get_failures_retry_both_discovery_and_exact_attempt_jobs(self):
        inputs, ticks = relay.request_inputs(environment()), [0]
        dispatches, reads, failures = [], [], {"workflow_runs", "jobs"}

        class API:
            def request(self, path, body=None):
                dispatches.append((path, body))

            def pages(self, path, key):
                reads.append((path, key))
                if key in failures:
                    failures.remove(key)
                    raise relay.Transient("temporary API failure", retry_after=3)
                return [completed_run(inputs)] if key == "workflow_runs" else completed_jobs()

        def sleep(seconds):
            ticks[0] += seconds

        result = relay.relay(API(), inputs, clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(result["accepted"], "true")
        self.assertEqual(len(dispatches), 1)
        self.assertFalse(failures)
        self.assertGreaterEqual(ticks[0], 6)
        self.assertEqual(sum(key == "jobs" for _, key in reads), 2)
        self.assertTrue(all(path.endswith("/999/attempts/1/jobs") for path, key in reads if key == "jobs"))

    def test_ambiguous_dispatch_failure_is_never_retried_or_followed_by_get(self):
        dispatches, reads = [], []

        class API:
            def request(self, path, body=None):
                dispatches.append((path, body))
                raise relay.Transient("GitHub API request failed")

            def pages(self, path, key):
                reads.append((path, key))
                return []

        with self.assertRaises(relay.Transient), patch.object(relay.time, "sleep") as sleep:
            relay.relay(API(), relay.request_inputs(environment()), sleep=sleep)
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(reads, [])
        sleep.assert_not_called()

    def test_permanent_get_rejection_is_not_retried(self):
        reads = []

        class API:
            def request(self, path, body=None):
                return None

            def pages(self, path, key):
                reads.append(path)
                raise relay.Rejected("GitHub API returned invalid JSON")

        with patch.object(relay.time, "sleep") as sleep:
            with self.assertRaisesRegex(relay.Rejected, "^GitHub API returned invalid JSON$"):
                relay.relay(API(), relay.request_inputs(environment()), sleep=sleep)
        self.assertEqual(len(reads), 1)
        sleep.assert_not_called()

    def test_retry_after_cannot_extend_the_overall_deadline(self):
        ticks, reads, dispatches, sleeps = [0], [], [], []

        class API:
            def request(self, path, body=None):
                dispatches.append(body)

            def pages(self, path, key):
                reads.append(ticks[0])
                raise relay.Transient("rate limited", retry_after=600)

        def sleep(seconds):
            sleeps.append(seconds)
            ticks[0] += seconds

        with patch.object(relay, "TIMEOUT_SECONDS", 30), self.assertRaisesRegex(relay.Rejected, "Timed out"):
            relay.relay(API(), relay.request_inputs(environment()),
                        clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(reads, [0])
        self.assertEqual(sleeps, [30])


class MainTests(unittest.TestCase):
    def test_main_appends_exact_outputs_and_summary_without_credentials(self):
        inputs = relay.request_inputs(environment())
        result = relay.gate_results(completed_run(inputs), completed_jobs(), inputs)
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "output", Path(tmp) / "summary"
            output.write_text("existing=output\n")
            summary.write_text("Existing summary\n")
            env = environment() | {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                                   "CI_BROKER_TOKEN": "private-dispatch-sentinel"}
            with patch.dict(relay.os.environ, env, clear=True), \
                 patch.object(relay, "GitHub") as api, patch.object(relay, "relay", return_value=result) as invoke:
                relay.main()
            self.assertEqual([call.args for call in api.call_args_list],
                             [("private-dispatch-sentinel",)])
            self.assertEqual(invoke.call_args.args[1], inputs)
            self.assertEqual(output.read_text(), "existing=output\n"
                             "go=success\nlint=success\nimage=success\nmigrations=success\n"
                             "accepted=true\nsource_run_attempt=2\nbroker_run_id=999\n"
                             "broker_url=https://github.com/fin-assist/infra/actions/runs/999\n")
            self.assertEqual(summary.read_text(), "Existing summary\n"
                             "Private CI: [999](https://github.com/fin-assist/infra/actions/runs/999)\n\n"
                             f"Go CI source: `{SOURCE}`\n\n"
                             "go: success | lint: success | image: success | migrations: success\n")
            self.assertNotIn("sentinel", output.read_text() + summary.read_text())

    def test_main_failed_gate_exits_nonzero_after_preserving_outputs_and_summary(self):
        inputs = relay.request_inputs(environment())
        jobs = completed_jobs()
        jobs[0]["conclusion"] = "failure"
        run = completed_run(inputs) | {"conclusion": "failure"}
        responses = [http_response(b"", status=204),
                     http_response(json.dumps({"workflow_runs": [run]}).encode()),
                     http_response(json.dumps({"jobs": jobs}).encode())]
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "output", Path(tmp) / "summary"
            output.write_text("existing=output\n")
            summary.write_text("Existing summary\n")
            env = environment() | {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                                   "CI_BROKER_TOKEN": "private-dispatch-sentinel"}
            with patch.dict(relay.os.environ, env, clear=True), \
                 patch.object(urllib.request.OpenerDirector, "open", side_effect=responses) as opened:
                with self.assertRaises(SystemExit) as raised:
                    relay.main()
            self.assertNotIn(raised.exception.code, (None, 0))
            self.assertEqual([call.args[0].get_method() for call in opened.call_args_list],
                             ["POST", "GET", "GET"])
            self.assertEqual(output.read_text(), "existing=output\n"
                             "go=failure\nlint=success\nimage=success\nmigrations=success\n"
                             "accepted=false\nsource_run_attempt=2\nbroker_run_id=999\n"
                             "broker_url=https://github.com/fin-assist/infra/actions/runs/999\n")
            self.assertEqual(summary.read_text(), "Existing summary\n"
                             "Private CI: [999](https://github.com/fin-assist/infra/actions/runs/999)\n\n"
                             f"Go CI source: `{SOURCE}`\n\n"
                             "go: failure | lint: success | image: success | migrations: success\n")
            self.assertNotIn("sentinel", str(raised.exception.code) + output.read_text() + summary.read_text())
        self.assertTrue(all(response.closed for response in responses))

    def test_main_rejects_with_redacted_system_exit_and_no_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "output", Path(tmp) / "summary"
            env = environment() | {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                                   "CI_BROKER_TOKEN": "private-dispatch-sentinel"}
            error = urllib.error.HTTPError("https://api.github.com/private-url-sentinel", 403,
                                           "private-error-sentinel", {}, io.BytesIO(b"private-body-sentinel"))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(relay.os.environ, env, clear=True), \
                 patch.object(urllib.request.OpenerDirector, "open", side_effect=error) as opened, \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    relay.main()
            self.assertEqual(raised.exception.code, "GitHub API returned HTTP 403")
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
            self.assertFalse(output.exists())
            self.assertFalse(summary.exists())


if __name__ == "__main__":
    unittest.main()
