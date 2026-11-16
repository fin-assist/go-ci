"""Dispatch private CI and relay exact-attempt outcomes without checking out PR code."""

import datetime
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


BROKER_REPOSITORY = "fin-assist/infra"
BROKER_WORKFLOW = "service-ci.yml"
BROKER_PATH = f".github/workflows/{BROKER_WORKFLOW}"
BROKER_REF = "main"
SERVICE_REPOSITORIES = frozenset(
    f"fin-assist/{name}" for name in (
        "go-service-template", "assistant", "user", "analytics", "bank-bff",
        "bank-statement-data",
    )
)
GATES = ("go", "lint", "image", "migrations")
CONCLUSIONS = frozenset(("success", "failure", "cancelled", "skipped", "timed_out",
                         "action_required", "neutral", "stale", "startup_failure"))
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 20
POLL_SECONDS = 15
TIMEOUT_SECONDS = 75 * 60
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
NUMBER_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")


class Rejected(ValueError):
    """A response or request cannot establish the required CI identity."""


class Transient(Rejected):
    """A failed read may be repeated within the existing operation deadline."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def request_inputs(environ):
    fields = {
        "source_repository": "GITHUB_REPOSITORY",
        "source_run_id": "GITHUB_RUN_ID",
        "source_run_attempt": "GITHUB_RUN_ATTEMPT",
        "source_sha": "GITHUB_SHA",
        "source_head_sha": "SOURCE_HEAD_SHA",
        "source_base_sha": "SOURCE_BASE_SHA",
        "source_pr_number": "SOURCE_PR_NUMBER",
        "go_ci_sha": "GO_CI_SHA",
    }
    values = {key: environ.get(name, "") for key, name in fields.items()}
    if values["source_repository"] not in SERVICE_REPOSITORIES:
        raise Rejected("Unsupported source repository")
    for key in ("source_run_id", "source_run_attempt"):
        if not NUMBER_PATTERN.fullmatch(values[key]):
            raise Rejected(f"Invalid {key}")
    for key in ("source_sha", "source_head_sha", "go_ci_sha"):
        if not SHA_PATTERN.fullmatch(values[key]):
            raise Rejected(f"Invalid {key}")
    event = environ.get("GITHUB_EVENT_NAME")
    if event == "pull_request":
        if (not NUMBER_PATTERN.fullmatch(values["source_pr_number"])
                or not SHA_PATTERN.fullmatch(values["source_base_sha"])
                or environ.get("SOURCE_HEAD_REPOSITORY") != values["source_repository"]):
            raise Rejected("Only same-repository pull requests are admitted")
    elif event == "push":
        if (environ.get("GITHUB_REF") != "refs/heads/main"
                or values["source_pr_number"] or values["source_base_sha"]
                or values["source_sha"] != values["source_head_sha"]):
            raise Rejected("Only main pushes are admitted")
    else:
        raise Rejected("Unsupported source event")
    return values


def run_title(inputs):
    return (f"Service CI {inputs['source_repository']} "
            f"{inputs['source_run_id']}/{inputs['source_run_attempt']}")


class GitHub:
    """Fixed-origin API client; neither redirects nor API bodies reach logs."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def __init__(self, token):
        if not token:
            raise Rejected("A GitHub API token is required")
        self.token = token
        self.opener = urllib.request.build_opener(self.NoRedirect())
        self.clock = time.monotonic
        self.deadline = None

    def request(self, path, body=None):
        if not path.startswith(f"/repos/{BROKER_REPOSITORY}/actions/"):
            raise Rejected("Unexpected API destination")
        timeout = 30
        if self.deadline is not None:
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise Rejected("Timed out waiting for the exact private CI run")
            timeout = min(timeout, remaining)
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            retry = error.headers.get("Retry-After", "") if error.headers else ""
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            retry_after = int(retry) if re.fullmatch(r"[0-9]{1,9}", retry) else 0
            error.close()
            if code == 429 or 500 <= code < 600 or (code == 403 and (retry_after or remaining == "0")):
                raise Transient(f"GitHub API returned HTTP {code}", retry_after) from None
            raise Rejected(f"GitHub API returned HTTP {code}") from None
        except (OSError, http.client.HTTPException):
            # urllib does not wrap getresponse()/read() failures in URLError.
            raise Transient("GitHub API request failed") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise Rejected("GitHub API response is too large")
        if not data:
            return None
        try:
            result = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise Rejected("GitHub API returned invalid JSON") from None
        if not isinstance(result, dict):
            raise Rejected("GitHub API returned an unexpected object")
        return result

    def pages(self, path, key):
        rows = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            payload = self.request(f"{path}{separator}per_page=100&page={page}")
            chunk = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(chunk, list) or not all(isinstance(row, dict) for row in chunk):
                raise Rejected("Malformed paginated API response")
            rows.extend(chunk)
            if len(chunk) < 100:
                return rows
        raise Rejected("GitHub API pagination limit exceeded")


def select_run(rows, inputs):
    matches = [row for row in rows if row.get("display_title") == run_title(inputs)]
    if len(matches) > 1:
        raise Rejected("Multiple private CI runs claim the same source attempt")
    if not matches:
        return None
    run = matches[0]
    if (type(run.get("id")) is not int or run["id"] < 1
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != BROKER_REF
            or run.get("path") != BROKER_PATH
            or run.get("repository", {}).get("full_name") != BROKER_REPOSITORY
            or run.get("head_repository", {}).get("full_name") != BROKER_REPOSITORY
            or run.get("run_attempt") != 1
            or not SHA_PATTERN.fullmatch(run.get("head_sha", ""))):
        raise Rejected("Private CI run identity does not match the trusted workflow")
    return run


def gate_results(run, jobs, inputs):
    if run.get("status") != "completed":
        raise Rejected("Private CI is not complete")
    results = {}
    for gate in GATES:
        matches = [job for job in jobs if job.get("name") == gate]
        if len(matches) != 1:
            raise Rejected(f"Expected exactly one {gate} job")
        job = matches[0]
        if (job.get("run_id") != run["id"] or job.get("status") != "completed"
                or job.get("conclusion") not in CONCLUSIONS
                or ("run_attempt" in job and job["run_attempt"] != 1)
                or job.get("head_sha") != run["head_sha"]):
            raise Rejected(f"Unverified {gate} job outcome")
        results[gate] = job["conclusion"]
    expected = {gate: "success" for gate in GATES}
    if not inputs["source_pr_number"]:
        expected["migrations"] = "skipped"
    results["accepted"] = str(run.get("conclusion") == "success" and results == expected).lower()
    results["source_run_attempt"] = inputs["source_run_attempt"]
    results["broker_run_id"] = str(run["id"])
    results["broker_url"] = f"https://github.com/{BROKER_REPOSITORY}/actions/runs/{run['id']}"
    return results


def relay(api, inputs, *, clock=time.monotonic, sleep=time.sleep, now=None):
    started = clock()
    deadline = started + TIMEOUT_SECONDS
    api.clock = clock
    api.deadline = deadline
    now = now or datetime.datetime.now(datetime.timezone.utc)
    # A small clock-skew margin does not weaken uniqueness: the source run/attempt is exact.
    earliest = (now - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    query = urllib.parse.urlencode({"event": "workflow_dispatch", "branch": BROKER_REF,
                                   "created": f">={earliest}"})
    workflow = f"/repos/{BROKER_REPOSITORY}/actions/workflows/{BROKER_WORKFLOW}"
    # Dispatch is deliberately not retried: a lost response must not launch duplicate CI.
    api.request(workflow + "/dispatches", {"ref": BROKER_REF, "inputs": inputs})
    while clock() < deadline:
        delay = POLL_SECONDS
        try:
            run = select_run(api.pages(workflow + "/runs?" + query, "workflow_runs"), inputs)
            if run and run.get("status") == "completed":
                jobs = api.pages(f"/repos/{BROKER_REPOSITORY}/actions/runs/{run['id']}/attempts/1/jobs", "jobs")
                return gate_results(run, jobs, inputs)
        except Transient as error:
            # Only GET requests are inside this block. Identity/format failures
            # remain terminal, and another dispatch is never issued.
            delay = max(delay, error.retry_after)
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(delay, remaining))
    raise Rejected("Timed out waiting for the exact private CI run")


def main():
    try:
        inputs = request_inputs(os.environ)
        result = relay(GitHub(os.environ.get("CI_BROKER_TOKEN", "")), inputs)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            for name, value in result.items():
                output.write(f"{name}={value}\n")
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(f"Private CI: [{result['broker_run_id']}]({result['broker_url']})\n\n")
            summary.write(f"Go CI source: `{inputs['go_ci_sha']}`\n\n")
            summary.write(" | ".join(f"{gate}: {result[gate]}" for gate in GATES) + "\n")
    except Rejected as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
