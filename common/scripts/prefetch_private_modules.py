"""Build a Go proxy containing only the locked private dependencies of a PR."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PRIVATE_ROOT = "github.com/fin-assist"
PRIVATE_PATTERN = f"{PRIVATE_ROOT}/*"
REPOSITORY = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
MODULE = re.compile(r"github\.com/fin-assist/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*")
NUMBER = r"(?:0|[1-9][0-9]*)"
PRERELEASE = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
VERSION = re.compile(rf"v{NUMBER}\.{NUMBER}\.{NUMBER}(?:-{PRERELEASE})?(?:\+incompatible)?")
CHECKSUM = re.compile(r"h1:[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=")
INPUT_LIMIT = 2 * 1024 * 1024
PRIVATE_VERSION_LIMIT = 128
DOWNLOAD_TIMEOUT = 120


def is_private(module):
    name = module.lower()
    return name == PRIVATE_ROOT or name.startswith(f"{PRIVATE_ROOT}/")


def validate_reference(module, version):
    if len(module) > 256 or MODULE.fullmatch(module) is None:
        raise ValueError("invalid private module path")
    if len(version) > 128 or VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid version for private module {module}")


def read_input(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular module input file")
    with path.open("rb") as source:
        data = source.read(INPUT_LIMIT + 1)
    if len(data) > INPUT_LIMIT:
        raise ValueError("module input exceeds the size limit")
    return data


def read_locks(go_mod, go_sum):
    required = set()
    for requirement in go_mod.get("Require") or []:
        module = requirement["Path"]
        if is_private(module):
            version = requirement["Version"]
            validate_reference(module, version)
            required.add((module, version))

    for replacement in go_mod.get("Replace") or []:
        if any(is_private(replacement[side]["Path"]) for side in ("Old", "New")):
            raise ValueError("private module replacements are unsupported")

    locks = {}
    for line in go_sum.splitlines():
        fields = line.split()
        if not fields or not is_private(fields[0]):
            continue
        if len(fields) != 3:
            raise ValueError("invalid private module checksum entry")
        module, version, checksum = fields
        suffix = "mod" if version.endswith("/go.mod") else "zip"
        version = version.removesuffix("/go.mod")
        validate_reference(module, version)
        if CHECKSUM.fullmatch(checksum) is None:
            raise ValueError(f"invalid checksum for {module}@{version}")
        hashes = locks.setdefault((module, version), {})
        if hashes.setdefault(suffix, checksum) != checksum:
            raise ValueError(f"conflicting checksums for {module}@{version}")

    if len(locks) > PRIVATE_VERSION_LIMIT:
        raise ValueError("too many locked private module versions")
    for module, version in required:
        if locks.get((module, version), {}).keys() != {"mod", "zip"}:
            raise ValueError(f"missing required module checksums for {module}@{version}")
    for (module, version), hashes in locks.items():
        if "mod" not in hashes:
            raise ValueError(f"missing go.mod checksum for {module}@{version}")
    return locks


def go_environment(work):
    home = work / "home"
    home.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GOENV": "off",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOPATH": str(work / "gopath"),
        "GOMODCACHE": str(work / "modules"),
        "GOPROXY": "direct",
        "GOPRIVATE": PRIVATE_PATTERN,
        "GOVCS": f"{PRIVATE_PATTERN}:git,*:off",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def go_json(args, cwd, env, operation):
    try:
        result = subprocess.run(
            ["go", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, check=True, timeout=DOWNLOAD_TIMEOUT,
        )
        parsed = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Go and Git diagnostics are untrusted and can include auth configuration.
        raise ValueError(f"could not {operation}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"could not {operation}")
    return parsed


def download_module(module, version, hashes, work, env, proxy):
    reference = f"{module}@{version}"
    data = go_json(["mod", "download", "-json", reference], work, env, f"download {reference}")
    if "Error" in data or data.get("Path") != module or data.get("Version") != version:
        raise ValueError(f"download did not resolve the exact version for {reference}")
    if data.get("GoModSum") != hashes["mod"] or ("zip" in hashes and data.get("Sum") != hashes["zip"]):
        raise ValueError(f"download checksum mismatch for {reference}")

    package = Path(module) / "@v"
    cached = Path(env["GOMODCACHE"]) / "cache" / "download" / package
    target = proxy / package
    target.mkdir(parents=True, exist_ok=True)
    info = json.loads((cached / f"{version}.info").read_bytes())
    if info.get("Version") != version or not isinstance(info.get("Time"), str):
        raise ValueError(f"invalid proxy metadata for {reference}")
    # The Go proxy requires version and time. Origin details stay in the temporary cache.
    (target / f"{version}.info").write_text(json.dumps({"Version": version, "Time": info["Time"]}), encoding="utf-8")
    for suffix in hashes:
        shutil.copyfile(cached / f"{version}.{suffix}", target / f"{version}.{suffix}")
    if "zip" in hashes:
        (target / f"{version}.ziphash").write_text(hashes["zip"], encoding="ascii")


def prefetch(go_mod, go_sum, output, token, *, allowed_repositories):
    if any(REPOSITORY.fullmatch(repo) is None for repo in allowed_repositories):
        raise ValueError("invalid repository in the private module allowlist")
    if output.exists() or output.is_symlink():
        raise ValueError("proxy output must not already exist")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".module-prefetch-") as directory:
        work = Path(directory)
        env = go_environment(work)
        try:
            subprocess.run(["go", "telemetry", "off"], cwd=work, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, check=True, timeout=DOWNLOAD_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("could not disable Go telemetry in the temporary home") from None
        input_mod = work / "input.mod"
        input_mod.write_bytes(read_input(go_mod))
        parsed_mod = go_json(["mod", "edit", "-json", str(input_mod)], work, env, "parse go.mod")
        locks = read_locks(parsed_mod, read_input(go_sum).decode("utf-8"))
        repositories = {module.split("/")[2] for module, _ in locks}
        blocked = repositories - allowed_repositories
        if blocked:
            raise ValueError("private module repositories outside the reviewed access boundary: " + ", ".join(sorted(blocked)))
        if locks and not token:
            raise ValueError("GO_MODULES_TOKEN is required to fetch private modules")

        proxy = work / "proxy"
        proxy.mkdir()
        if locks:
            authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
            download_env = env | {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"http.https://{PRIVATE_ROOT}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
                "GIT_CONFIG_KEY_1": "http.followRedirects",
                "GIT_CONFIG_VALUE_1": "false",
            }
            for (module, version), hashes in sorted(locks.items()):
                download_module(module, version, hashes, work, download_env, proxy)
        proxy.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-mod", required=True, type=Path)
    parser.add_argument("--go-sum", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allowed-repositories", required=True)
    args = parser.parse_args()
    try:
        prefetch(args.go_mod, args.go_sum, args.output.absolute(), os.environ.get("GO_MODULES_TOKEN"),
                 allowed_repositories=set(args.allowed_repositories.split()))
    except (OSError, ValueError) as error:
        parser.exit(1, f"private module prefetch failed: {error}\n")


if __name__ == "__main__":
    main()
