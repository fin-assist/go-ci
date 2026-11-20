"""Create a deterministic Helm OCI chart using the protected infrastructure renderer."""

import gzip
import io
import json
from pathlib import Path
import re
import tarfile


def chart(service, image, source_sha, version, renderer, destination, *, expected_stable=None, rollback=False):
    if service not in renderer.SERVICES or not re.fullmatch(r"1\.[1-9][0-9]{0,18}\.[1-9][0-9]{0,8}", version):
        raise ValueError("Invalid production service or release version")
    repository, sep, digest = image.partition("@")
    package = "infra-service-" + service
    allowed = {f"ghcr.io/fin-assist/{package}"}
    if rollback:
        allowed.add(f"ghcr.io/fin-assist/{service}")
    if (repository not in allowed or not sep or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or digest[7:] == "0" * 64 or not re.fullmatch(r"[0-9a-f]{40}", source_sha)):
        raise ValueError("Expected an immutable service image and a protected request revision")
    operation = "rollback" if rollback else "deploy"
    resources = [renderer.release_receipt(service, image, version, operation, expected_stable=expected_stable)]
    if not rollback and service in renderer.PEERS:
        resources.append(renderer.migration(service, digest, version, package))
    workload = renderer.rollout(service, image)
    strategy = workload["spec"]["strategy"]
    if set(strategy) != {"blueGreen"} or strategy["blueGreen"].get("activeService") != service or any(
        key in strategy["blueGreen"] for key in ("prePromotionAnalysis", "postPromotionAnalysis", "autoPromotionSeconds")
    ) or strategy["blueGreen"].get("autoPromotionEnabled") is not True:
        raise ValueError("The infrastructure renderer must switch the ready service directly")
    if not rollback:
        # Argo also schedules skipped prune tasks for retained Jobs. Put a
        # deploy last, after any previous rollback finish (+1), so its manual
        # pause cannot hold the sync open and prevent a subsequent rollback.
        workload["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] = "2"
    resources.append(workload)
    if rollback:
        resources.append(renderer.rollback_finish(service, image, version))
    return archive(service, version, operation, source_sha, digest[7:] if rollback else source_sha, resources, destination)


def observation(service, source_sha, version, operation, renderer, destination):
    if (service not in renderer.SERVICES or operation not in ("deploy", "rollback")
            or not re.fullmatch(r"1\.[1-9][0-9]{0,18}\.[1-9][0-9]{0,8}", version)
            or not re.fullmatch(r"[0-9a-f]{40}", source_sha)):
        raise ValueError("Invalid production observation")
    job = renderer.release_observation(service, version, operation)
    if job["kind"] != "Job" or job["metadata"]["namespace"] != "plainfin-apps":
        raise ValueError("Expected one namespaced observation Job")
    return archive(service, "2" + version[1:], operation, source_sha, source_sha, [job], destination)


def archive(service, version, operation, source_sha, app_version, resources, destination):
    name = "production-" + service
    annotations = {"plainfin.ru/operation": operation, "plainfin.ru/request-sha": source_sha}
    files = {
        "Chart.yaml": json.dumps(dict(apiVersion="v2", name=name, version=version,
                                     appVersion=app_version, annotations=annotations)),
        # Values are already rendered by the infrastructure renderer. Never evaluate them as Helm templates.
        "templates/workloads.yaml": '{{ .Files.Get "workloads.yaml" }}\n',
        "workloads.yaml": "\n---\n".join(json.dumps(obj, sort_keys=True) for obj in resources) + "\n",
    }
    target = Path(destination) / f"{name}-{version}.tgz"
    with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for filename, data in files.items():
                content = data.encode()
                entry = tarfile.TarInfo(f"{name}/{filename}")
                entry.size = len(content)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(content))
    return target
