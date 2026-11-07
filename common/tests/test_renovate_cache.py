"""Real policy tests must leave every byte of the immutable source tree intact."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


class RenovateCacheTests(unittest.TestCase):
    def test_real_policy_driver_and_children_preserve_the_complete_source_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            (root / 'common').mkdir(parents=True)
            shutil.copytree(SOURCE / 'common/renovate', root / 'common/renovate',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copyfile(SOURCE / 'common/bootstrap.py', root / 'common/bootstrap.py')
            shutil.copytree(SOURCE / '.github', root / '.github')
            for name in ('renovate.json', 'renovate-base.json'):
                shutil.copyfile(SOURCE / name, root / name)
            before = snapshot(root)
            # -I ignores PYTHON* environment flags; intentionally omit -B so the
            # driver must protect imports, including the credential-free CLI child.
            result = subprocess.run([sys.executable, '-I', str(root / 'common/renovate/test.py'),
                                     '--root', str(root), '--mode', 'policy'],
                                    cwd=directory, env={'PATH': os.defpath},
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(snapshot(root), before, 'Policy tests changed the immutable source cache')
