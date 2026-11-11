"""Prepare checksum-locked private Go files and optional version metadata."""

import argparse
import base64
from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zipfile import BadZipFile, ZipFile


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
BASELINE_LIMIT = 2
DOWNLOAD_TIMEOUT = 120


def is_private(module):
    name = module.lower()
    return name == PRIVATE_ROOT or name.startswith(f"{PRIVATE_ROOT}/")


def validate_reference(module, version):
    if len(module) > 256 or MODULE.fullmatch(module) is None:
        raise ValueError("invalid private module path")
    if len(version) > 128 or VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid version for private module {module}")


def escape_version(version):
    return re.sub(r"[A-Z]", lambda match: "!" + match.group().lower(), version)


def read_input(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular module input file")
    with path.open("rb") as source:
        data = source.read(INPUT_LIMIT + 1)
    if len(data) > INPUT_LIMIT:
        raise ValueError("module input exceeds the size limit")
    return data


def read_locks(go_mod, go_sum, *, required_only=False):
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
        expected = {"mod"} if required_only else {"mod", "zip"}
        if not expected.issubset(locks.get((module, version), {})):
            raise ValueError(f"missing required module checksums for {module}@{version}")
    for (module, version), hashes in locks.items():
        if "mod" not in hashes:
            raise ValueError(f"missing go.mod checksum for {module}@{version}")
    return {ref: locks[ref] for ref in required} if required_only else locks


def merge_locks(target, extra):
    changed = []
    for ref, hashes in sorted(extra.items()):
        merged = target.setdefault(ref, {})
        if set(hashes) - set(merged):
            changed.append(ref)
        for kind, checksum in hashes.items():
            if merged.setdefault(kind, checksum) != checksum:
                module, version = ref
                raise ValueError(f"conflicting release checksums for {module}@{version}")
    if len(target) > PRIVATE_VERSION_LIMIT:
        raise ValueError("too many locked private module versions")
    return changed


def locked_inputs(inputs, work, env):
    locks = {}
    for go_mod, go_sum in inputs:
        input_mod = work / "input.mod"
        input_mod.write_bytes(read_input(go_mod))
        parsed = go_json(["mod", "edit", "-json", str(input_mod)], work, env, "parse go.mod")
        sums = read_input(go_sum).decode("utf-8") if go_sum.exists() or go_sum.is_symlink() else ""
        merge_locks(locks, read_locks(parsed, sums))
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
    filename = escape_version(version)
    info = json.loads((cached / f"{filename}.info").read_bytes())
    if info.get("Version") != version or not isinstance(info.get("Time"), str):
        raise ValueError(f"invalid proxy metadata for {reference}")
    # The Go proxy requires version and time. Origin details stay in the temporary cache.
    (target / f"{filename}.info").write_text(json.dumps({"Version": version, "Time": info["Time"]}), encoding="utf-8")
    for suffix in hashes:
        shutil.copyfile(cached / f"{filename}.{suffix}", target / f"{filename}.{suffix}")
    if "zip" in hashes:
        (target / f"{filename}.ziphash").write_text(hashes["zip"], encoding="ascii")


def archived_requirements(module, version, work, env, proxy):
    """Read dependency locks as data from an already checksum-verified module."""
    target = proxy / module / "@v"
    filename = escape_version(version)
    go_mod = target / f"{filename}.mod"
    parsed = go_json(["mod", "edit", "-json", str(go_mod)], work, env, f"read go.mod of {module}@{version}")
    archive_path = target / f"{filename}.zip"
    try:
        with ZipFile(archive_path) as archive:
            try:
                with archive.open(f"{module}@{version}/go.sum") as sums:
                    content = sums.read(INPUT_LIMIT + 1)
            except KeyError:
                content = b""
    except BadZipFile:
        raise ValueError(f"could not read the verified module archive for {module}@{version}") from None
    if len(content) > INPUT_LIMIT:
        raise ValueError(f"go.sum in {module}@{version} exceeds the size limit")
    # A dependency's replace directives do not apply to its importers.
    parsed.pop("Replace", None)
    return read_locks(parsed, content.decode("utf-8"), required_only=True)


def require_allowed(locks, allowed_repositories):
    blocked = {module.split("/")[2] for module, _ in locks} - allowed_repositories
    if blocked:
        raise ValueError("private module repositories outside the reviewed access boundary: " + ", ".join(sorted(blocked)))


def download_locked_graph(locks, work, env, download_env, proxy, allowed_repositories, *, transitive):
    pending = deque(sorted(locks))
    downloaded = {}
    while pending:
        ref = pending.popleft()
        hashes = locks[ref]
        if hashes == downloaded.get(ref):
            continue
        module, version = ref
        download_module(module, version, hashes, work, download_env, proxy)
        downloaded[ref] = hashes.copy()
        if transitive and "zip" in hashes:
            try:
                requirements = archived_requirements(module, version, work, env, proxy)
                require_allowed(requirements, allowed_repositories)
                pending.extend(merge_locks(locks, requirements))
            except ValueError as error:
                raise ValueError(f"private requirements from {module}@{version}: {error}") from None


def snapshot_metadata(module, work, env, proxy):
    """Expose Git version metadata to gorelease without adding unlocked archives."""
    query = ["list", "-m", "-json"]
    latest = go_json([*query, "-retracted", "-versions", module + "@latest"], work, env,
                     f"list versions of {module}")
    update = go_json([*query, "-e", module + "@latest"], work, env, f"resolve latest allowed version of {module}")
    # An all-retracted module can have no eligible version. Keep its retraction metadata
    # and let Go report the locked version as retracted. Other errors fail preparation.
    no_matching_version = {"Err": f'module {module}: no matching versions for query "latest"'}
    results = (latest,) if update.get("Path") == module and update.get("Error") == no_matching_version else (latest, update)
    selected_versions = []
    for result in results:
        version = result.get("Version")
        if result.get("Path") != module or not isinstance(version, str) or "Error" in result:
            raise ValueError(f"unexpected latest version metadata for {module}")
        validate_reference(module, version)
        selected_versions.append(version)
    versions = latest.get("Versions") or []
    if not isinstance(versions, list):
        raise ValueError(f"invalid version list for {module}")
    for listed in versions:
        if not isinstance(listed, str):
            raise ValueError(f"invalid version list for {module}")
        validate_reference(module, listed)
    version_list = "".join(item + "\n" for item in versions)
    if len(version_list) > INPUT_LIMIT:
        raise ValueError(f"version list is too large for {module}")

    root = proxy / module
    target = root / "@v"
    cached = Path(env["GOMODCACHE"]) / "cache" / "download" / module / "@v"
    # Go reads the newest go.mod for retractions and may select an older update.
    # Other mod/info pairs were already added from the checksummed locks.
    for selected in set(selected_versions):
        filename = escape_version(selected)
        info = json.loads(read_input(cached / f"{filename}.info"))
        if info.get("Version") != selected or not isinstance(info.get("Time"), str):
            raise ValueError(f"invalid version metadata for {module}@{selected}")
        (target / f"{filename}.info").write_text(json.dumps({"Version": selected, "Time": info["Time"]}),
                                                encoding="utf-8")
        # Keep the exact modfile already checked against any known lock.
        target_mod = target / f"{filename}.mod"
        if not target_mod.exists():
            target_mod.write_bytes(read_input(cached / f"{filename}.mod"))
    (target / "list").write_text(version_list, encoding="ascii")
    (root / "@latest").write_bytes((target / f"{escape_version(selected_versions[0])}.info").read_bytes())


def prefetch(go_mod, go_sum, output, token, *, allowed_repositories, baselines=(), include_metadata=False):
    if any(REPOSITORY.fullmatch(repo) is None for repo in allowed_repositories):
        raise ValueError("invalid repository in the private module allowlist")
    if len(baselines) > BASELINE_LIMIT:
        raise ValueError("too many baseline releases")
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
        locks = locked_inputs(((go_mod, go_sum), *baselines), work, env)
        require_allowed(locks, allowed_repositories)
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
            download_locked_graph(locks, work, env, download_env, proxy, allowed_repositories,
                                  transitive=include_metadata)
            if include_metadata:
                for module in sorted({module for module, _ in locks}):
                    snapshot_metadata(module, work, download_env, proxy)
        proxy.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-mod", required=True, type=Path)
    parser.add_argument("--go-sum", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allowed-repositories", required=True)
    parser.add_argument("--baseline", nargs=2, action="append", default=[], type=Path, metavar=("GO_MOD", "GO_SUM"))
    parser.add_argument("--include-metadata", action="store_true")
    args = parser.parse_args()
    try:
        prefetch(args.go_mod, args.go_sum, args.output.absolute(), os.environ.get("GO_MODULES_TOKEN"),
                 allowed_repositories=set(args.allowed_repositories.split()), baselines=args.baseline,
                 include_metadata=args.include_metadata)
    except (OSError, ValueError) as error:
        parser.exit(1, f"private module prefetch failed: {error}\n")


if __name__ == "__main__":
    main()
