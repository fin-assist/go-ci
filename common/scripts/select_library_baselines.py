"""Select immutable stable release commits from paginated GitHub tag metadata."""

import argparse
import json
from pathlib import Path
import re

STABLE_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


def library_baselines(pages, limit=2):
    releases = {}
    for tags in pages:
        for tag in tags:
            name = tag["name"]
            if len(name) > 64 or not (version := STABLE_TAG.fullmatch(name)):
                continue
            commit = tag["commit"]["sha"]
            if not SHA.fullmatch(commit):
                raise ValueError(f"tag {name} does not identify an immutable commit")
            order = tuple(map(int, version.groups()))
            if releases.setdefault(order, (name, commit)) != (name, commit):
                raise ValueError(f"tag {name} changed during lookup")
    return [releases[order] for order in sorted(releases, reverse=True)[:limit]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", required=True, type=Path)
    args = parser.parse_args()
    try:
        for name, commit in library_baselines(json.loads(args.tags.read_bytes())):
            print(name, commit)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"could not resolve release baselines: {error}\n")


if __name__ == "__main__":
    main()
