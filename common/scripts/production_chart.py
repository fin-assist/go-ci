"""Create a deterministic Helm OCI chart using the protected infrastructure renderer."""

import gzip
import io
import json
from pathlib import Path
import re
import tarfile


def chart(service, image, source_sha, version, renderer, attempts, destination):
    if service not in renderer.SERVICES or not re.fullmatch(r"1\.[1-9][0-9]{0,18}\.[1-9][0-9]{0,8}", version):
        raise ValueError("Invalid production service or release version")
    package = "infra-service-" + service
    prefix = f"ghcr.io/fin-assist/{package}@"
    if not re.fullmatch(re.escape(prefix) + r"sha256:[0-9a-f]{64}", image) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected a validated private image and source revision")
    resources = []
    if service in renderer.PEERS:
        resources.append(renderer.migration(service, image.removeprefix(prefix), attempts.get(service, 1), package))
    resources.append(renderer.rollout(service, image))
    name = "production-" + service
    files = {
        "Chart.yaml": json.dumps(dict(apiVersion="v2", name=name, version=version, appVersion=source_sha)),
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
