"""Consumer JSON locations for the shared, read-only contract tests."""
import json
import os
from pathlib import Path
import re

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def consumer_root():
    return Path(os.environ.get('RENOVATE_CONSUMER_ROOT', SOURCE_ROOT)).resolve()


def consumer_repository():
    config = json.loads((consumer_root() / 'renovate.json').read_text())
    for value in config.get('extends', []):
        match = re.fullmatch(r'local>(fin-assist/[a-z0-9-]+):base#renovate-policy', value)
        if match:
            return match[1]
    raise ValueError('Consumer must retain its own reviewed baseline preset')
