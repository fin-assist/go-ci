"""Verify the common reader boundary before publishing private module archives."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
import subprocess
import sys


ORGANIZATION = "fin-assist"
REPOSITORY = re.compile(r"fin-assist/[A-Za-z0-9_.-]+")
READ_PERMISSIONS = {"read", "write"}


def github_pages(path):
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--paginate", "--slurp", path],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def repository_readers(repository, get):
    if REPOSITORY.fullmatch(repository) is None:
        raise ValueError("unexpected repository in the organization inventory")
    pages = get(f"repos/{repository}/collaborators?affiliation=all&per_page=100")
    return {user["id"] for page in pages for user in page if user["permissions"]["pull"]}


def verify(repository, get=github_pages):
    if REPOSITORY.fullmatch(repository) is None:
        raise ValueError("the artifact destination must belong to fin-assist")
    pages = get(f"orgs/{ORGANIZATION}/installations?per_page=100")
    apps = [app for page in pages for app in page["installations"]]
    if not pages or len(apps) != pages[0]["total_count"]:
        raise ValueError("the organization app inventory is incomplete")
    for app in apps:
        permissions = app["permissions"]
        if app["suspended_at"] or permissions.get("actions") not in READ_PERMISSIONS:
            continue
        if app["repository_selection"] != "all" or permissions.get("contents") not in READ_PERMISSIONS:
            raise ValueError("an Actions reader does not have verified organization-wide contents access")

    pages = get(f"orgs/{ORGANIZATION}/repos?type=all&per_page=100")
    inventory = {item["full_name"]: item["visibility"] for page in pages for item in page}
    if inventory.get(repository) != "private":
        raise ValueError("the artifact destination must be private")
    readers = repository_readers(repository, get)
    if not readers:
        raise ValueError("the artifact destination has no verified readers")
    dependencies = [name for name, visibility in inventory.items() if name != repository and visibility != "public"]
    with ThreadPoolExecutor(max_workers=6) as workers:
        for dependency_readers in workers.map(lambda name: repository_readers(name, get), dependencies):
            if not readers.issubset(dependency_readers):
                raise ValueError("an artifact reader cannot read all organization modules")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    args = parser.parse_args()
    try:
        verify(args.repository)
    except (KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("GitHub could not verify the private artifact audience", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("GitHub returned invalid artifact access metadata", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
