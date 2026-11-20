"""Resolve the private publisher receipt for a service's current protected main."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


def current_push(repo, sha, get):
    main = get(f"repos/{repo}/branches/main")
    if main.get("protected") is not True or main["commit"]["sha"] != sha:
        raise ValueError("The selected commit is no longer the protected main head")
    query = urlencode(dict(branch="main", event="push", head_sha=sha, per_page=100))
    candidates = get(f"repos/{repo}/actions/workflows/ci.yml/runs?{query}")
    if candidates["total_count"] != 1 or len(candidates["workflow_runs"]) != 1:
        raise ValueError("Expected one push CI run for the selected main commit")
    run = candidates["workflow_runs"][0]
    if (run["status"], run["conclusion"], run["head_sha"], run["event"], run["path"]) != (
            "completed", "success", sha, "push", ".github/workflows/ci.yml"):
        raise ValueError("CI for current main has not completed successfully")
    return run


def publisher_runs(infra, start, get, now):
    # Only the source attempt that produced this digest is eligible. A busy
    # repository can have newer publishers for other services; search forward
    # from this attempt instead of assuming the most recent publisher is ours.
    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if start < now - timedelta(days=90):
        raise ValueError("Publication evidence may have expired; rerun source CI")
    while start <= now:
        end = min(start + timedelta(days=1) - timedelta(seconds=1), now)
        interval = start.isoformat(timespec="seconds") + ".." + end.isoformat(timespec="seconds")
        for page in range(1, 11):
            query = urlencode(dict(created=interval, per_page=100, page=page))
            result = get(f"repos/{infra}/actions/workflows/service-publish.yml/runs?{query}")
            if result["total_count"] > 1000:
                raise ValueError("Publisher search exceeds GitHub's result limit")
            for run in result["workflow_runs"]:
                if (run["event"], run["head_branch"], run["status"], run["conclusion"]) == (
                        "workflow_run", "main", "completed", "success"):
                    yield run
            if page * 100 >= result["total_count"]:
                break
        start = end + timedelta(seconds=1)


def resolve(repo, sha, get_source, get_private, delivery, read_evidence, *, now=None):
    ci = delivery.ci
    service = ci.SERVICES.get(repo)
    if service in (None, "service") or not ci.SHA.fullmatch(sha):
        raise ValueError("Expected a production service and a full commit SHA")
    source = current_push(repo, sha, get_source)
    expected = (repo, sha, str(source["id"]), str(source["run_attempt"]))
    for run in publisher_runs(ci.INFRA, source["run_started_at"], get_private, now or datetime.now(timezone.utc)):
        run_id, attempt = run["id"], run["run_attempt"]
        path = f"repos/{ci.INFRA}/actions/runs/{run_id}"
        artifacts = get_private(f"{path}/artifacts?name=delivery-evidence-{attempt}&per_page=2")
        if artifacts["total_count"] == 0:
            continue  # Successful broker PRs do not publish.
        if artifacts["total_count"] != 1:
            raise ValueError("Ambiguous publication artifact")
        artifact = artifacts["artifacts"][0]
        if artifact["expired"]:
            continue
        if artifact["name"] != f"delivery-evidence-{attempt}" or artifact["workflow_run"]["id"] != run_id:
            raise ValueError("Publication artifact belongs to a different run")
        delivery.validate_run(run, ci.INFRA, delivery.PUBLISH_WORKFLOW, run_id, attempt, run["head_sha"])
        item = read_evidence(run_id, attempt).get(service)
        if item is None:
            continue
        provenance = item["provenance"]
        receipt = provenance["source"]
        if tuple(receipt[key] for key in ("source_repository", "source_sha", "source_run_id", "source_run_attempt")) != expected:
            continue
        if (provenance["publisher_run_id"], provenance["publisher_run_attempt"], provenance["publisher_sha"],
                receipt["event"], receipt["service"], provenance["image"]) != (
                run_id, attempt, run["head_sha"], "push", service, item["image"]):
            raise ValueError("Publication identity differs from its receipt")
        delivery.reference(service, item["image"])
        ci.validate_source(ci.receipt_inputs(receipt), get_source)
        delivery.source_success(receipt, get_source)
        delivery.broker_success(receipt, get_private)
        current = get_private(path)
        delivery.validate_run(current, ci.INFRA, delivery.PUBLISH_WORKFLOW, run_id, attempt, run["head_sha"])
        if current["event"] != "workflow_run":
            raise ValueError("Expected a trusted publisher event")
        delivery.gates(delivery.jobs(get_private, ci.INFRA, run_id, attempt), run_id, attempt,
                       run["head_sha"], required=("publish", "isolated-stage"))
        return item
    raise ValueError("No successful private publication and isolated stage for this main CI attempt")
