#!/usr/bin/env python3
"""Find a private inputs artifact published by the trusted pull request producer."""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PRODUCER_PATH = ".github/workflows/private-ci-inputs.yml"
PRODUCER_EVENT = "pull_request_target"
API_ORIGIN = "https://api.github.com"
PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 1024 * 1024
POLL_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 15


class TemporaryApiError(RuntimeError):
    """The API request may succeed on retry."""


def artifact_name(head_sha, base_sha):
    for sha in (head_sha, base_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("expected full pull request head and base SHAs")
    return f"private-ci-{head_sha}-{base_sha}"


def github_api(repository, token, deadline):
    if not re.fullmatch(r"fin-assist/[a-z][a-z0-9-]*", repository):
        raise ValueError("expected a FinAssist consumer repository")
    if not token:
        raise ValueError("a read-only repository Actions token is required")

    def get(path, **query):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TemporaryApiError("producer discovery deadline elapsed")
        suffix = "?" + urlencode(query) if query else ""
        request = Request(f"{API_ORIGIN}/repos/{repository}/actions/{path}{suffix}", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with urlopen(request, timeout=min(remaining, REQUEST_TIMEOUT_SECONDS)) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            error = f"GitHub Actions API returned HTTP {exc.code} for {path}"
            if exc.code == 429 or exc.code >= 500:
                raise TemporaryApiError(error) from None
            raise RuntimeError(error) from None
        except (URLError, TimeoutError):
            raise TemporaryApiError("GitHub Actions API request failed") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("GitHub Actions API response exceeded its size limit")
        return json.loads(body)

    return get


def producer_run(get, repository, name):
    # A reopened pull request may produce the same inputs in a later run, so
    # enforce uniqueness within the selected run.
    result = get("artifacts", name=name, per_page=PAGE_SIZE)
    if result["total_count"] > PAGE_SIZE:
        raise RuntimeError("too many artifacts match the requested private CI inputs")
    runs = defaultdict(list)
    for artifact in result["artifacts"]:
        if artifact.get("name") != name or artifact.get("expired") is not False:
            continue
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if type(run_id) is int and run_id > 0:
            runs[run_id].append(artifact)

    for run_id in sorted(runs, reverse=True):
        run = get(f"runs/{run_id}")
        if (run.get("id") != run_id
                or run.get("event") != PRODUCER_EVENT
                or run.get("path", "").partition("@")[0] != PRODUCER_PATH
                or (run.get("repository") or {}).get("full_name") != repository
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"):
            continue
        if len(runs[run_id]) != 1:
            raise RuntimeError("successful producer run has multiple matching private CI artifacts")
        return run_id
    return None


def wait_for_producer(get, repository, name, deadline):
    while time.monotonic() < deadline:
        try:
            run_id = producer_run(get, repository, name)
        except TemporaryApiError:
            run_id = None
        if run_id is not None:
            return run_id
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    raise RuntimeError(f"timed out waiting for {name} from a successful {PRODUCER_PATH} run")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int, choices=range(1, 601), metavar="1-600")
    args = parser.parse_args()
    try:
        name = artifact_name(args.head_sha, args.base_sha)
        repository = os.environ["GITHUB_REPOSITORY"]
        deadline = time.monotonic() + args.timeout_seconds
        get = github_api(repository, os.environ.get("INPUTS_TOKEN"), deadline)
        run_id = wait_for_producer(get, repository, name, deadline)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"run-id={run_id}\nartifact-name={name}\n")
    except (KeyError, OSError, ValueError, RuntimeError) as exc:
        print(f"Private CI inputs discovery failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
