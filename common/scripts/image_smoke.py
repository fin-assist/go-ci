#!/usr/bin/env python3
"""Build and smoke-test the service images without publishing them."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


SERVICES = frozenset({"service", "user", "analytics", "assistant", "bank-statement-data", "bank-bff"})


def command(arguments, *, timeout=60, expected=0, capture=True):
    """Execute an argument vector with a deadline and no build-token inheritance."""
    environment = {key: value for key, value in os.environ.items() if key != "GO_MODULES_TOKEN"}
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT, env=environment, timeout=timeout)
    if result.returncode != expected:
        raise RuntimeError(f"{arguments[0]} {arguments[1]} returned {result.returncode}; expected {expected}")
    return result.stdout or ""


def require_text(output, text):
    if text not in output:
        raise RuntimeError(f"image smoke output did not contain {text}")


def bff_smoke(image, revision, run=command):
    """Check fail-closed production startup and isolated development listeners."""
    require_text(run(["docker", "run", "--rm", image, "serve"], expected=1), "USER_GRPC_ADDR")
    container = "bank-bff-smoke-" + uuid.uuid4().hex
    try:
        run(["docker", "run", "-d", "--name", container, "-e", "ENV=dev", "-e",
             "SHUTDOWN_DELAY=0s", "-p", "127.0.0.1::8080", "-p", "127.0.0.1::9091", image])
        addresses = []
        for port in ["8080/tcp", "9091/tcp"]:
            address = run(["docker", "port", container, port]).strip()
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", address):
                raise RuntimeError("unexpected smoke listener address")
            addresses.append("http://" + address)
        public, operational = addresses
        run(["curl", "--retry", "20", "--retry-all-errors", "--retry-delay", "1",
             "--max-time", "2", "--fail", operational + "/readyz"], timeout=90)
        run(["curl", "--max-time", "2", "--fail", operational + "/healthz"])
        require_text(run(["curl", "--max-time", "2", "--fail", operational + "/metrics"]), "go_goroutines")
        for path in ["/metrics", "/healthz", "/readyz", "/v1/profile"]:
            status = run(["curl", "--max-time", "2", "-s", "-o", "/dev/null", "-w",
                          "%{http_code}", public + path]).strip()
            if status != "404":
                raise RuntimeError("public listener exposed an unexpected route")
        require_text(run(["docker", "logs", container]), revision)
        run(["docker", "stop", "--time", "5", container], timeout=20)
        if run(["docker", "inspect", "--format", "{{.State.ExitCode}}", container]).strip() != "0":
            raise RuntimeError("image did not shut down cleanly")
    finally:
        run(["docker", "rm", "-f", container])


def content_id(image, run):
    """Capture immutable image identity before smoke and later extraction."""
    identity = run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise ValueError("expected an image content ID")
    return identity


def build_images(service, fake_llm, revision, *, environ=os.environ, run=command, image_ids=None):
    """Return the tested primary image tag; stage packaging uses that exact image."""
    if service not in SERVICES:
        raise ValueError("unsupported service image profile")
    if not isinstance(fake_llm, bool) or (fake_llm and service != "assistant"):
        raise ValueError("fake-llm is supported only for assistant")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("expected full source SHA")
    # user historically builds service:ci; its extracted binary is still named user.
    image = ("service" if service == "user" else service) + ":ci"
    secret_path = None
    try:
        arguments = ["docker", "build"]
        cache = environ.get("FINASSIST_GO_MODULE_CACHE")
        if cache:
            arguments.extend(["--secret", "id=go_module_cache,src=" + cache])
        token = environ.get("GO_MODULES_TOKEN", "")
        if token:
            with tempfile.NamedTemporaryFile(mode="w", prefix="finassist-netrc-", delete=False,
                                             dir=environ.get("RUNNER_TEMP")) as stream:
                secret_path = Path(stream.name)
                stream.write(f"machine github.com login x-access-token password {token}\n")
            arguments.extend(["--secret", "id=netrc,src=" + str(secret_path)])
        arguments.extend(["--build-arg", "VERSION=" + revision, "-t", image, "."])
        run(arguments, timeout=600, capture=False)
    finally:
        if secret_path is not None:
            secret_path.unlink()
    primary_id = content_id(image, run)
    require_text(run(["docker", "run", "--rm", primary_id, "help"], expected=2), "usage:")
    if service == "bank-bff":
        bff_smoke(primary_id, revision, run)
    if fake_llm:
        run(["docker", "build", "-f", "Dockerfile.fake-llm", "-t", "fake-llm:ci", "."],
            timeout=600, capture=False)
        fake_id = content_id("fake-llm:ci", run)
        require_text(run(["docker", "run", "--rm", fake_id, "-h"]), "loopback listen address")
    if image_ids is not None:
        image_ids["image_id"] = primary_id
        if fake_llm:
            image_ids["fake_image_id"] = fake_id
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=sorted(SERVICES))
    parser.add_argument("--fake-llm", choices=["true", "false"], default="false")
    args = parser.parse_args()
    image_ids = {}
    image = build_images(args.service, args.fake_llm == "true", os.environ["GITHUB_SHA"], image_ids=image_ids)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("image_tag=" + image + "\n")
        for key, value in image_ids.items():
            output.write(key + "=" + value + "\n")


if __name__ == "__main__":
    main()
