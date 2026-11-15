#!/usr/bin/env python3
"""Bind prepared CI inputs to the Go manifests read from the PR head."""

import argparse
import hashlib
import json
from pathlib import Path
import re

FILES = ("go.mod", "go.sum", ".finassist-ci.json")
SHA = re.compile(r"[0-9a-f]{40}")


def hash_input(path: Path) -> str | None:
    if path.name == "go.sum" and not path.exists() and not path.is_symlink():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(directory: Path, revision: str) -> dict:
    if not SHA.fullmatch(revision):
        raise ValueError("A full go-ci SHA is required.")
    config = json.loads((directory / ".finassist-ci.json").read_bytes())
    if config.get("revision", revision) != revision:
        raise ValueError("Prepared source and consumer pin differ.")
    return {
        "revision": revision,
        "files": {
            name: hash_input(directory / name)
            for name in FILES
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("write", "verify"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    actual = manifest(args.directory, args.revision)
    if args.mode == "write":
        args.manifest.write_text(json.dumps(actual, sort_keys=True) + "\n")
    elif json.loads(args.manifest.read_bytes()) != actual:
        raise ValueError("The prepared dependencies do not match this checkout.")


if __name__ == "__main__":
    main()
