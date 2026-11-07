#!/usr/bin/env python3
"""Run consumer Go suites and the official coverage analyzer from one pinned source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrap import read_config, unique_object

ANALYZER = 'github.com/vladopajic/go-test-coverage/v2@v2.20.1'
# Match the existing shared Go job budget; Go's -timeout applies per test binary.
GO_PROCESS_TIMEOUT = 45 * 60
LEVELS = ('unit', 'integration', 'api')
BLOCK = re.compile(r'\S+:[0-9]+\.[0-9]+,[0-9]+\.[0-9]+\s+([0-9]+)\s+[0-9]+')
ORIGINAL_TEMPLATE = 'fin-assist/go-library-template'
ORIGINAL_ORIGINS = {
    'https://github.com/fin-assist/go-library-template',
    'https://github.com/fin-assist/go-library-template.git',
    'git@github.com:fin-assist/go-library-template.git',
    'ssh://git@github.com/fin-assist/go-library-template.git',
}


class Failed(RuntimeError):
    """Carry an unsuccessful process outcome without turning it into a green gate."""
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code if 0 < code < 126 else 1


def run(root, command, *, output=None, env=None, timeout=600, echo=True):
    """Bound the process group, including compiler, analyzer and fixture descendants."""
    stream = output.open('w') if output else None
    process = None

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('Quality subprocess interrupted.')

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=stream,
                                   start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise Failed('Quality subprocess exceeded its process timeout.', 124) from error
    finally:
        # Cancellation of the runner must not leave a compiler/test descendant alive.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        signal.signal(signal.SIGTERM, previous)
        if stream:
            stream.close()
        if echo and output and output.exists():
            with output.open() as result:
                for line in result:
                    print(line, end='')
    if status:
        raise Failed('Quality subprocess failed: ' + command[0], status)


def capture(root, command):
    """Read small Go/Git inventories with a bounded child process."""
    with tempfile.TemporaryDirectory(prefix='finassist-quality-') as directory:
        output = Path(directory) / 'inventory.txt'
        run(root, command, output=output, timeout=120, echo=False)
        return output.read_text().strip()


def timeout_seconds(value):
    """Accept a positive Go duration without shell syntax or an unbounded zero timeout."""
    unit = {'ns': 1e-9, 'us': 1e-6, 'µs': 1e-6, 'μs': 1e-6, 'ms': 1e-3,
            's': 1, 'm': 60, 'h': 3600}
    component = r'([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)'
    parts = re.findall(component, value)
    if len(value) > 64 or not parts or ''.join(number + suffix for number, suffix in parts) != value:
        raise argparse.ArgumentTypeError('test-timeout must be a positive Go duration')
    seconds = sum(float(number) * unit[suffix] for number, suffix in parts)
    if seconds <= 0:
        raise argparse.ArgumentTypeError('test-timeout must be a positive Go duration')
    return seconds


def test_names(root, profile):
    """Reject race-only or ordinary tests which all three collectors would omit."""
    output = root / 'test-names.txt'
    run(root, ['go', 'test', '-race', '-list', '^(Test|Fuzz|Example)', '-timeout=8m', './...'],
        output=output, timeout=GO_PROCESS_TIMEOUT)
    names = output.read_text().splitlines()
    invalid = [name for name in names if re.match(r'^(Test|Fuzz)', name)
               and not re.match(r'^(Test|Fuzz)(Unit|Integration|API).+', name)]
    if invalid:
        raise Failed('Name every test TestUnit..., TestIntegration... or TestAPI... and every fuzz target '
                     'FuzzUnit..., FuzzIntegration... or FuzzAPI...: ' + ', '.join(invalid))
    if profile == 'service' and any(name.startswith('Example') for name in names):
        raise Failed('Examples run outside the three suites; a service must use TestUnit/TestIntegration/TestAPI.')


def original_template(root, profile):
    """The module path or a new library fork cannot grant the original-template exception."""
    if profile != 'library':
        return False
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        return os.environ.get('GITHUB_REPOSITORY') == ORIGINAL_TEMPLATE
    try:
        return capture(root, ['git', 'config', '--get', 'remote.origin.url']) in ORIGINAL_ORIGINS
    except Failed:
        return False


def invalid_json_constant(value):
    """Go execution logs cannot contain non-JSON numeric constants."""
    raise ValueError('Invalid JSON constant in suite evidence: ' + value)


def validate(root, profile, level):
    """Validate executed-test evidence and profile syntax, never calculate coverage ratios."""
    passed = False
    with (root / (level + '.jsonl')).open() as log:
        for line in log:
            event = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_json_constant)
            if not isinstance(event, dict):
                raise Failed('Suite execution evidence must contain JSON objects.')
            if event.get('Action') == 'fail' or (event.get('Action') == 'skip' and 'Test' in event):
                raise Failed('Suite must execute and pass at least one test, with no skips or failures.')
            if event.get('Action') == 'pass' and isinstance(event.get('Test'), str) and event['Test']:
                passed = True
    if not passed:
        raise Failed('Suite must execute and pass at least one test, with no skips or failures.')
    data = (root / (level + '.out')).read_bytes().decode('utf-8')
    if data == 'mode: atomic\n' and original_template(root, profile):
        return 'N/A'
    lines = data.splitlines()
    if not lines or lines[0] != 'mode: atomic':
        raise Failed('Coverage profile must be a valid atomic race profile.')
    statements = False
    for line in lines[1:]:
        block = BLOCK.fullmatch(line)
        if not block:
            raise Failed('Coverage profile contains a malformed block.')
        statements |= int(block[1]) > 0
    if not statements:
        raise Failed('Coverage profile must contain a block with executable statements.')
    return 'statements'


def collect(root, profile, level):
    """Use the same production denominator for every suite and execute every package."""
    for suffix in ('.out', '.jsonl', '.report.txt', '.coverage.txt'):
        (root / (level + suffix)).unlink(missing_ok=True)
    test_names(root, profile)
    denominator = './...'
    if profile == 'library':
        module = capture(root, ['go', 'list', '-m'])
        packages = capture(root, ['go', 'list', '-race', './...']).splitlines()
        denominator = ','.join(package for package in packages if package != module + '/internal/pgtest')
        if not denominator:
            raise Failed('No production packages remain for coverage.')
    selector = '^(Test|Fuzz)' + level.title()
    if level == 'api':
        selector = '^((Test|Fuzz)API|Example)' if profile == 'library' else '^(Test|Fuzz)API'
    run(root, ['go', 'test', '-json', '-race', '-count=1', '-timeout=8m', '-coverpkg=' + denominator,
               '-coverprofile=' + level + '.out', '-run', selector, './...'],
        output=root / (level + '.jsonl'), env={**os.environ, 'CI': 'true'}, timeout=GO_PROCESS_TIMEOUT)
    validate(root, profile, level)


def analyze(root, profiles, report, breakdown, *, threshold):
    """Keep the analyzer's toolchain separate from the go.mod-selected test compiler."""
    command = ['go', 'run', ANALYZER, '--profile', ','.join(profiles), '--breakdown-file-name', breakdown]
    if threshold:
        command += ['--config', '.testcoverage.yml']
    run(root, command, output=root / report, env={**os.environ, 'GOTOOLCHAIN': 'auto'})


def check(root, profile, level):
    """Enforce one independent threshold with the pinned official analyzer."""
    for suffix in ('.report.txt', '.coverage.txt'):
        (root / (level + suffix)).unlink(missing_ok=True)
    if validate(root, profile, level) == 'N/A':
        text = 'N/A: original go-library-template has no executable production statements; suite passed.\n'
        (root / (level + '.report.txt')).write_text(text)
        print(text, end='')
        return
    analyze(root, [level + '.out'], level + '.report.txt', level + '.coverage.txt', threshold=True)


def combined(root, profile):
    """Delegate informational union to the official analyzer; never replace a failed level."""
    for name in ('combined.report.txt', 'combined.coverage.txt'):
        (root / name).unlink(missing_ok=True)
    applicability = [validate(root, profile, level) for level in LEVELS]
    if len(set(applicability)) != 1:
        raise Failed('Coverage profiles disagree on production statement applicability.')
    if applicability[0] == 'N/A':
        text = 'N/A: original go-library-template has no executable production statements; all suites passed.\n'
        (root / 'combined.report.txt').write_text(text)
        print(text, end='')
        return
    analyze(root, [level + '.out' for level in LEVELS], 'combined.report.txt', 'combined.coverage.txt', threshold=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('command', choices=('test-names', 'test', 'collect', 'check', 'combined'))
    parser.add_argument('level', nargs='?', choices=LEVELS)
    parser.add_argument('--test-timeout', default='8m', type=str)
    args = parser.parse_args(argv)
    if (args.command in ('collect', 'check')) != (args.level is not None):
        parser.error('Only collect/check require a suite level.')
    try:
        seconds = timeout_seconds(args.test_timeout)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if args.command != 'test' and args.test_timeout != '8m':
        parser.error('test-timeout only applies to the full test command.')
    root = args.root.resolve()
    try:
        profile = read_config(root)['profile']
        if args.command == 'test-names':
            test_names(root, profile)
        elif args.command == 'test':
            test_names(root, profile)
            run(root, ['go', 'test', '-race', '-count=1', '-timeout=' + args.test_timeout, './...'],
                timeout=max(GO_PROCESS_TIMEOUT, seconds + 120))
        elif args.command == 'collect':
            collect(root, profile, args.level)
        elif args.command == 'check':
            check(root, profile, args.level)
        else:
            combined(root, profile)
    except (Failed, OSError, ValueError, subprocess.SubprocessError) as error:
        print('Shared quality check failed: ' + str(error), file=sys.stderr)
        return error.code if isinstance(error, Failed) else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
