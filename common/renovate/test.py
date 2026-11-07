#!/usr/bin/env python3
"""Run reviewed Renovate regressions against explicit consumer data, never its code."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parents[1]
SOURCE = 'fin-assist/go-ci'
spec = importlib.util.spec_from_file_location('trusted_bootstrap', SOURCE_ROOT / 'common/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=bootstrap.unique_object)


def contract(root, revision=None):
    if os.environ.get('RENOVATE_POLICY_TOKEN'):
        raise ValueError('Read-only Renovate tests must not receive a mutation credential')
    config = read_json(root / '.github/renovate-policy.json')
    names = config.get('requiredChecks')
    if (not isinstance(names, list) or not names or len(names) != len(set(names))
            or not all(isinstance(name, str) and name for name in names)
            or not isinstance(config.get('controllerLogin'), str) or not config['controllerLogin']
            or config.get('ciWorkflow', 'ci.yml') not in ('ci.yml', 'stage.yml')
            or not set(config.get('ciChecks', [])) <= set(names)):
        raise ValueError('Invalid consumer controller configuration')
    for name in ('Renovate policy tests', 'Renovate runtime contract'):
        if name not in names:
            raise ValueError('Consumer required policy checks must be preserved')
    # The source host cannot pin its own future commit. Its protected main workflow
    # runs the local action after an explicit main checkout; PR tests are read-only.
    host = root == SOURCE_ROOT
    if not host:
        manifest = root / '.finassist-ci.json'
        if manifest.is_file():
            manifest_config = bootstrap.read_config(root)
        else:
            manifest = root / '.github/renovate-source.json'
            if manifest.stat().st_size > 4096:
                raise ValueError('Renovate source manifest exceeds the bounded JSON contract')
            manifest_config = read_json(manifest)
            if not isinstance(manifest_config, dict) or set(manifest_config) != {'revision'}:
                raise ValueError('Specialized source manifest must contain only revision')
        pin = manifest_config.get('revision')
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{40}', pin):
            raise ValueError('Renovate source pin must be a full lowercase SHA')
        if revision is None:
            raise ValueError('Remote shared tests require an explicit source-revision')
        if revision != pin:
            raise ValueError('Renovate test action revision differs from consumer pin')
        revision = pin
        if (root / 'scripts/common_ci.py').read_bytes() != (SOURCE_ROOT / 'common/bootstrap.py').read_bytes():
            raise ValueError('Consumer bootstrap differs from its pinned source transport')
    elif revision:
        raise ValueError('Source self-tests use the local reviewed action, not a remote self pin')
    for filename, action in (('renovate-policy.yml', 'renovate-policy'),
                             ('renovate-policy-tests.yml', 'renovate-policy-tests')):
        workflow = (root / '.github/workflows' / filename).read_text()
        refs = re.findall(r'(?m)^\s*(?:-\s+)?uses:\s+([^\s#]+)', workflow)
        expected = ('./.github/actions/' + action if host else
                    SOURCE + '/.github/actions/' + action + '@' + revision)
        matching = [ref for ref in refs if ref.split('@', 1)[0].endswith('/actions/' + action)]
        if not matching or any(ref != expected for ref in matching):
            raise ValueError('Consumer action refs differ from the Renovate source pin')
        if not host and filename == 'renovate-policy-tests.yml':
            passed = re.findall(r'(?m)^\s*source-revision:\s*[\"\']?([0-9a-f]{40})[\"\']?\s*$', workflow)
            declared = re.findall(r'(?m)^\s*source-revision:', workflow)
            if not passed or len(passed) != len(declared) or any(value != revision for value in passed):
                raise ValueError('Consumer source-revision inputs differ from the single source pin')
        if re.search(r'python\S*\s+scripts/renovate_policy.py', workflow):
            raise ValueError('Consumer controller code must not run with the mutation credential')
    root_config = read_json(root / 'renovate.json')
    if root_config.get('automerge') is not False:
        raise ValueError('Native Renovate automerge must stay disabled')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('policy', 'runtime'), required=True)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--source-revision')
    args = parser.parse_args()
    root = args.root.resolve()
    contract(root, args.source_revision)
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['RENOVATE_CONSUMER_ROOT'] = str(root)
    env['RENOVATE_POLICY_SCRIPT'] = str(HERE / 'renovate_policy.py')
    env['RENOVATE_POLICY_CONFIG'] = str(root / 'renovate.json')
    if args.mode == 'policy':
        subprocess.run([sys.executable, '-I', '-B', '-m', 'unittest', 'discover',
                        '-s', str(HERE), '-p', 'test_*.py'], cwd=HERE, env=env, timeout=90, check=True)
    else:
        if args.runtime is None:
            parser.error('--runtime is required for runtime fixtures')
        env['RENOVATE_RUNTIME'] = str(args.runtime.resolve())
        env['RENOVATE_RUNTIME_MANIFEST'] = str(root / 'scripts/renovate-runtime/package.json')
        for fixture in ('test_renovate_runtime.mjs', 'test_renovate_metadata_runtime.mjs',
                        'test_renovate_go_runtime.mjs'):
            subprocess.run(['node', '--experimental-test-module-mocks', str(HERE / fixture)],
                           cwd=HERE, env=env, timeout=90, check=True)


if __name__ == '__main__':
    main()
