"""Private production release publication from the trusted go-ci dispatcher."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

# Both checkouts are protected main revisions selected by the workflow.
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "infra/scripts"), str(ROOT / "infra/apps")]
import production_chart
import production_delivery
import render_release
import service_delivery as delivery

ci = delivery.ci
SHARED_RELAY = ci.SHARED + "/.github/workflows/shared-production-deploy.yml"


def authorize(repo, run_id, attempt, get):
    if repo not in ci.SERVICES or ci.SERVICES[repo] == "service":
        raise ValueError("Unknown production service")
    if not all(re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in (run_id, attempt)):
        raise ValueError("Invalid manual dispatch identity")
    run = get(f"repos/{repo}/actions/runs/{run_id}")
    expected = dict(id=int(run_id), run_attempt=int(attempt), event="workflow_dispatch",
                    head_branch="main", path=".github/workflows/deploy.yml", status="in_progress")
    if (any(run.get(key) != value for key, value in expected.items()) or
            any(run.get(key, {}).get("full_name") != repo for key in ("repository", "head_repository"))):
        raise ValueError("Expected the active manual service deployment on main")
    sha = run["head_sha"]
    branch = get(f"repos/{repo}/branches/main")
    if not ci.SHA.fullmatch(sha) or not branch.get("protected") or branch["commit"]["sha"] != sha:
        raise ValueError("The service main head changed")
    refs = [item for item in run.get("referenced_workflows", []) if item.get("path") in
            (SHARED_RELAY + "@main", SHARED_RELAY + "@refs/heads/main")]
    if len(refs) != 1 or refs[0].get("ref") != "refs/heads/main" or not ci.SHA.fullmatch(refs[0].get("sha", "")):
        raise ValueError("Expected the protected reusable deployment workflow")
    shared = get(f"repos/{ci.SHARED}/branches/main")
    relay_sha = refs[0]["sha"]
    if not shared.get("protected") or get(f"repos/{ci.SHARED}/compare/{relay_sha}...{shared['commit']['sha']}")["merge_base_commit"]["sha"] != relay_sha:
        raise ValueError("Deployment workflow is outside protected main")
    return sha


def publish_chart(file, name, version, folder, check):
    token = os.environ["REGISTRY_TOKEN"]
    info = ci.api("orgs/fin-assist/packages/container/" + name, token)
    if (info["name"], info["package_type"], info["visibility"], info["owner"]["login"]) != (name, "container", "private", "fin-assist"):
        raise ValueError("Expected a preconfigured private release package")
    config = str(folder / "registry.json")
    def helm(args, *, stdin=None):
        return subprocess.run(["helm", *args, "--registry-config", config], input=stdin, text=True,
                              capture_output=True, timeout=90)
    if helm(["registry", "login", "ghcr.io", "--username", os.environ["REGISTRY_USER"], "--password-stdin"], stdin=token).returncode:
        raise ValueError("GHCR authentication failed")
    pulled = folder / "pulled"
    pulled.mkdir()
    def pull(selector=version):
        return helm(["pull", "oci://ghcr.io/fin-assist/" + name, "--version", selector, "--destination", str(pulled)])
    existing = pull()
    if existing.returncode:
        if not re.search(r"manifest unknown|manifest_unknown|: not found|404 not found", existing.stderr, re.I):
            raise ValueError("Could not verify whether the release already exists")
        check()
        if helm(["push", str(file), "oci://ghcr.io/fin-assist"]).returncode or pull().returncode:
            raise ValueError("GHCR release publication or read-back failed")
    if (pulled / file.name).read_bytes() != file.read_bytes():
        raise ValueError("Release tag content differs from the verified chart")
    # A re-run of an older manual run can still point at today's main. Report it
    # as superseded when Argo's version range already selects a newer release.
    (pulled / file.name).unlink()
    if pull("1.*").returncode or not (pulled / file.name).is_file() or (pulled / file.name).read_bytes() != file.read_bytes():
        raise ValueError("Argo CD selects a different release version")


def main():
    env = os.environ
    if (env["GITHUB_REPOSITORY"], env["GITHUB_REF"], env["GITHUB_REF_PROTECTED"], env["GITHUB_EVENT_NAME"]) != (
            ci.SHARED, "refs/heads/main", "true", "workflow_dispatch"):
        raise ValueError("Protected central dispatcher required")
    repo, run_id, attempt = env["SERVICE_REPOSITORY"], env["SERVICE_RUN"], env["SERVICE_ATTEMPT"]
    def get(path):
        return ci.api(path, env["DELIVERY_READ_TOKEN"])
    if get("repos/" + ci.INFRA)["private"] is not True:
        raise ValueError("Private infrastructure repository required")
    infra = get(f"repos/{ci.INFRA}/branches/main")
    if not infra.get("protected") or ci.command(["git", "rev-parse", "HEAD"], cwd=ROOT / "infra") != infra["commit"]["sha"]:
        raise ValueError("Infrastructure checkout differs from protected main")
    sha = authorize(repo, run_id, attempt, get)
    def evidence(run, number):
        return delivery.publisher_evidence(dict(publisher_run_id=run, publisher_run_attempt=number), env["DELIVERY_READ_TOKEN"])
    item = production_delivery.resolve(repo, sha, get, get, delivery, evidence)
    service = ci.SERVICES[repo]
    version = f"1.{run_id}.{attempt}"
    attempts = delivery.read_json(ROOT / "infra/overlays/prod/app-runtime/migration-attempts.json")
    def check():
        if authorize(repo, run_id, attempt, get) != sha:
            raise ValueError("Manual dispatch advanced")
        delivery.source_success(item["provenance"]["source"], get)
        delivery.broker_success(item["provenance"]["source"], get)
        pub = item["provenance"]
        current = get(f"repos/{ci.INFRA}/actions/runs/{pub['publisher_run_id']}")
        delivery.validate_run(current, ci.INFRA, delivery.PUBLISH_WORKFLOW, pub["publisher_run_id"],
                              pub["publisher_run_attempt"], pub["publisher_sha"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        file = production_chart.chart(service, item["image"], sha, version, render_release, attempts, root)
        publish_chart(file, "production-" + service, version, root, check)
    print("Release published for Argo CD reconciliation.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # The central workflow logs may be public. Hide private evidence and subprocess diagnostics.
        print(f"Production release failed ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
