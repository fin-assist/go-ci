"""Select a rollback image from immutable GHCR intent and trusted cluster observations."""

from dataclasses import dataclass
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import time


class RollbackError(ValueError):
    """Safe operational messages: never include registry responses, images or tokens."""


def version_key(version):
    if not isinstance(version, str) or not re.fullmatch(r"1\.[1-9][0-9]{0,18}\.[1-9][0-9]{0,8}", version):
        raise RollbackError("Invalid release version in production history")
    return tuple(int(n) for n in version.split(".")[1:])


def validate_image(service, image):
    prefix = rf"ghcr\.io/fin-assist/(?:infra-service-)?{re.escape(service)}@sha256:"
    if not isinstance(image, str) or not re.fullmatch(prefix + "(?!0{64})[a-f0-9]{64}", image):
        raise RollbackError("Unexpected image identity in production history")


@dataclass(frozen=True)
class Head:
    version: str
    image: str
    operation: str
    request_sha: str
    checksum: str


def selected_release(helm, service, folder):
    name = "production-" + service
    with tempfile.TemporaryDirectory(dir=folder) as target:
        command = helm(["pull", "oci://ghcr.io/fin-assist/" + name, "--version", "1.*", "--destination", target])
        archives = list(Path(target).glob(name + "-*.tgz"))
        if command.returncode or len(archives) != 1:
            raise RollbackError("No readable current production chart in GHCR")
        archive = archives[0]
        if archive.stat().st_size > 2 * 1024 * 1024:
            raise RollbackError("Unexpected production chart size")
        raw = archive.read_bytes()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            unpacked = compressed.read(2 * 1024 * 1024 + 1)
        if len(unpacked) > 2 * 1024 * 1024:
            raise RollbackError("Unexpected uncompressed production chart size")
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as release:
            def read_member(filename):
                member = release.getmember(name + "/" + filename)
                if not member.isfile() or member.size > 1024 * 1024:
                    raise RollbackError("Invalid production chart member")
                return release.extractfile(member).read().decode()

            chart = json.loads(read_member("Chart.yaml"))
            objects = [json.loads(doc) for doc in read_member("workloads.yaml").split("\n---\n")]
    version = chart["version"]
    version_key(version)
    if chart["name"] != name or archive.name != f"{name}-{version}.tgz":
        raise RollbackError("Production chart identity differs from its GHCR tag")
    rollout, = [obj for obj in objects if obj["kind"] == "Rollout"]
    if rollout["metadata"]["name"] != service or rollout["metadata"]["namespace"] != "plainfin-apps":
        raise RollbackError("Unexpected service in production chart")
    app, = rollout["spec"]["template"]["spec"]["containers"]
    image = app["image"]
    validate_image(service, image)
    annotations = chart.get("annotations", {})
    return Head(version, image, annotations.get("plainfin.ru/operation", ""),
                annotations.get("plainfin.ru/request-sha", ""), hashlib.sha256(raw).hexdigest())


def trusted_payloads(service, creator_id, task, get):
    if not isinstance(creator_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", creator_id):
        raise RollbackError("Configure the dedicated production receipt writer's numeric GitHub ID")
    environment = "production-" + service
    path = f"repos/fin-assist/infra/deployments?environment={environment}&task={task}"
    page = 1
    while True:
        rows = get(f"{path}&per_page=100&page={page}")
        for row in rows:
            if ((row.get("creator") or {}).get("id") != int(creator_id) or row.get("environment") != environment or
                    row.get("task") != task):
                continue
            entry = row.get("payload")
            if not isinstance(entry, dict) or type(entry.get("schema")) is not int or entry["schema"] != 1 or entry.get("service") != service:
                raise RollbackError("Invalid cluster release receipt")
            version_key(entry.get("version"))
            yield entry
        if len(rows) < 100:
            return
        page += 1


def receipts(service, creator_id, get):
    history = {}
    for entry in trusted_payloads(service, creator_id, "record-production-release", get):
        if set(entry) != {"schema", "service", "version", "image", "previous_image"}:
            raise RollbackError("Invalid cluster release receipt")
        version = entry["version"]
        validate_image(service, entry["image"])
        if entry["previous_image"] is not None:
            validate_image(service, entry["previous_image"])
        if version in history and history[version] != entry:
            raise RollbackError("Conflicting cluster receipts for one release")
        history[version] = entry
    return history


def previous_image(head, history, stable_image=None, candidate=False):
    current = history.get(head.version)
    if current is not None and current["image"] != head.image:
        raise RollbackError("Cluster receipt image differs from the selected GHCR chart")
    if any(version_key(key) > version_key(head.version) for key in history):
        raise RollbackError("GHCR head is older than an observed cluster release")
    # A failed transition with no candidate is recovered by reasserting the
    # current working image. A live candidate is discarded AND the stable
    # version is rolled back to the previous distinct working image.
    if stable_image and not candidate and head.image != stable_image:
        return stable_image
    # Only previous_image was actually observed serving traffic. A desired image in
    # any receipt might have failed migrations or readiness and is not evidence.
    for version in sorted(history, key=version_key, reverse=True):
        previous = history[version]["previous_image"]
        if previous and previous != (stable_image or head.image):
            return previous
    raise RollbackError("No previous distinct working image in cluster history")


def target_for_request(head, version, sha, service, creator_id, get, *, stable_image=None, candidate=None):
    head_run, head_attempt = version_key(head.version)
    run, attempt = version_key(version)
    if (head_run, head_attempt) > (run, attempt):
        raise RollbackError("A newer production action has superseded this rollback")
    if head_run == run:
        if head.operation != "rollback" or head.request_sha != sha:
            raise RollbackError("Existing release does not match this rollback request")
        return None  # Publishing another attempt would undo this same rollback.
    # The selected release might have been published just before this command.
    # Wait for its pre-migration observation, never infer the result from its tag.
    for poll in range(25):
        history = receipts(service, creator_id, get)
        if candidate is not None or head.version in history:
            return previous_image(head, history, stable_image, candidate)
        if poll < 24:
            time.sleep(10)
    raise RollbackError("No cluster receipt for the current release; check the release receipt Job")
