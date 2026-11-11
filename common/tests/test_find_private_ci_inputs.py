"""Exercise the private artifact lookup without external credentials or requests."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "find_private_ci_inputs", Path(__file__).resolve().parents[1] / "scripts/find_private_ci_inputs.py",
)
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)

REPOSITORY = "fin-assist/assistant"
HEAD = "1" * 40
BASE = "2" * 40
NAME = f"private-ci-{HEAD}-{BASE}"


def artifact(run_id, *, name=NAME, expired=False):
    return {"name": name, "expired": expired, "workflow_run": {"id": run_id}}


def run(run_id, **overrides):
    return {
        "id": run_id,
        "event": "pull_request_target",
        "path": ".github/workflows/private-ci-inputs.yml@main",
        "repository": {"full_name": REPOSITORY},
        "status": "completed",
        "conclusion": "success",
    } | overrides


class PrivateInputsDiscoveryTest(unittest.TestCase):
    def test_name_binds_full_shas_in_order(self):
        self.assertEqual(inputs.artifact_name(HEAD, BASE), NAME)
        for head, base in (("main", BASE), (HEAD, BASE[:12]), (HEAD, "a\n")):
            with self.subTest(head=head, base=base), self.assertRaises(ValueError):
                inputs.artifact_name(head, base)

    def test_rejects_expired_and_foreign_runs_and_reuses_valid_run(self):
        artifacts = [artifact(6), artifact(1), artifact(3), artifact(5), artifact(4),
                     artifact(8, name="wrong"), artifact(7, expired=True), artifact(2)]
        get = Mock(side_effect=[
            {"total_count": len(artifacts), "artifacts": artifacts},
            run(6, event="pull_request"),
            run(5, path=".github/workflows/other.yml@main"),
            run(4, repository={"full_name": "fin-assist/user"}),
            run(3, conclusion="failure"),
            run(2),
        ])
        self.assertEqual(inputs.producer_run(get, REPOSITORY, NAME), 2)
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         ["artifacts", "runs/6", "runs/5", "runs/4", "runs/3", "runs/2"])
        self.assertEqual(get.call_args_list[0].kwargs["name"], NAME)

    def test_missing_success_waits_until_producer_completes(self):
        match = {"total_count": 1, "artifacts": [artifact(4)]}
        get = Mock(side_effect=[
            inputs.TemporaryApiError("HTTP 502"),
            {"total_count": 0, "artifacts": []},
            match, run(4, status="in_progress", conclusion=None),
            match, run(4),
        ])
        with patch.object(inputs.time, "sleep") as sleep:
            self.assertEqual(inputs.wait_for_producer(get, REPOSITORY, NAME, time.monotonic() + 1), 4)
            self.assertEqual(sleep.call_count, 3)

    def test_rejects_ambiguous_match_in_successful_run(self):
        get = Mock(side_effect=[{"total_count": 2, "artifacts": [artifact(1), artifact(1)]}, run(1)])
        with self.assertRaisesRegex(RuntimeError, "multiple matching"):
            inputs.producer_run(get, REPOSITORY, NAME)

    def test_exhausted_wait_fails_without_a_request(self):
        get = Mock()
        with self.assertRaisesRegex(RuntimeError, "timed out waiting"):
            inputs.wait_for_producer(get, REPOSITORY, NAME, time.monotonic() - 1)
        get.assert_not_called()

    def test_api_keeps_token_out_of_redirect_headers_and_uses_fixed_origin(self):
        get = inputs.github_api(REPOSITORY, "test-token", time.monotonic() + 1)
        with patch.object(inputs, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"artifacts":[]}'
            self.assertEqual(get("artifacts", name=NAME), {"artifacts": []})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts?name={NAME}")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer test-token")
