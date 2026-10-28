"""Run the actual pinned linters against accepted and rejected Go/SQL fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

COMMON = Path(__file__).resolve().parent


def run(argv, root, accepted=True, expected="", env=None):
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=600, env=env)
    output = result.stdout + result.stderr
    if (result.returncode == 0) != accepted or expected not in output:
        raise RuntimeError("Probe failed: " + repr(argv) + "\n" + output)
    return result


def manifest(root, profile, smallint=False, g115=False):
    (root / ".finassist-ci.json").write_text(json.dumps({
        "revision": "0" * 40, "profile": profile, "gosec_g115": g115,
        "squawk_smallint": smallint}))


def render(root, env=None):
    run([sys.executable, str(COMMON / "render_lint.py"), "--root", str(root)], root, env=env)


STUBS = {
    "grpc": ("google.golang.org/grpc", {
        "status/status.go": 'package status\n\nimport "errors"\n\nfunc Error(_ int, message string) error { return errors.New(message) }\n',
    }),
    "genproto": ("google.golang.org/genproto/googleapis/rpc", {
        "errdetails/details.go": "package errdetails\n\ntype ErrorInfo struct { Reason string }\n",
    }),
    "contracts": ("github.com/fin-assist/contracts/gen/go", {
        "finerr/errors.go": 'package finerr\n\nimport "errors"\n\nfunc NotFound() error { return errors.New("not found") }\n',
    }),
    "gin": ("github.com/gin-gonic/gin", {
        "gin.go": "package gin\n\nfunc Value() int { return 1 }\n",
    }),
}

STATUS = '''package lintprobe

import peerstatus "google.golang.org/grpc/status"

func ClientError() error {
    return peerstatus.Error(3, "probe") // want:forbidigo
}
'''

DETAILS = '''package lintprobe

import details "google.golang.org/genproto/googleapis/rpc/errdetails"

func ClientDetails() any {
    return &details.ErrorInfo{Reason: "probe"} // want:forbidigo
}
'''

NARROW = '''package lintprobe

func Narrow(value int) uint16 {
    return uint16(value) // want:gosec:G115
}
'''

ENVIRONMENT = '''package lintprobe

import "os"

func Setting() string {
    return os.Getenv("PROBE") // want:forbidigo
}
'''

# Representative FIN-322 families, including aliases and typed method chains. The
# consumer libraries retain their exhaustive identifier/third-party probes.
KERNEL = '''package lintprobe

import (
    "flag"
    stdlog "log"
    "log/slog"
    "net/http"
    "os"
    "os/signal"
    "runtime"
    "runtime/debug"
)

func ProcessGlobals() {
    _ = os.Getenv // want:forbidigo
    _ = os.Exit // want:forbidigo
    _ = os.Stdout // want:forbidigo
    _ = os.Chdir // want:forbidigo
    _ = signal.Notify // want:forbidigo
    _ = flag.CommandLine // want:forbidigo
    _ = runtime.GOMAXPROCS // want:forbidigo
    _ = debug.SetGCPercent // want:forbidigo
    _ = http.DefaultClient // want:forbidigo
    stdlog.Default().Printf("probe") // want:forbidigo
    slog.With("key", "value").Info("probe") // want:forbidigo
}
'''

INJECTED = '''package lintprobe

import (
    "context"
    "flag"
    "fmt"
    "io"
    "log"
    "log/slog"
    "net/http"
    "os"
    "runtime/debug"
)

func Injected(ctx context.Context, logger *slog.Logger, client *http.Client, writer io.Writer) {
    logger.With("key", "value").InfoContext(ctx, "probe")
    standard := log.New(writer, "", 0)
    standard.Printf("probe")
    _ = client.Get
    flags := flag.NewFlagSet("probe", flag.ContinueOnError)
    _ = flags.Parse(nil)
    _ = fmt.Sprintf("client=%T", client)
    _ = os.Expand("$VALUE", func(string) string { return "" })
    _, _ = debug.ReadBuildInfo()
}
'''


def prepare_module(root, stubs):
    """Use typed local replacements; do not fetch or execute fixture dependencies."""
    root.mkdir()
    module = "module example.com/lintprobe\n\ngo 1.26.0\n"
    for name, (path, _) in STUBS.items():
        module += "\nrequire " + path + " v1.0.0\nreplace " + path + " => " + str(stubs / name) + "\n"
    (root / "go.mod").write_text(module)


def expected_issues(files):
    """Markers describe the required diagnostic, not a copy of the lint rules."""
    expected = {}
    for filename, content in files.items():
        for line, text in enumerate(content.splitlines(), 1):
            if "// want:" in text:
                linter, _, message = text.split("// want:", 1)[1].partition(":")
                expected[(linter, filename, line)] = message
    return expected


def go_probe(root, name, profile, files, versions, stubs, g115=False):
    """Assert exact typed diagnostics so a typecheck/tool failure cannot pass."""
    started = time.monotonic()
    prepare_module(root, stubs)
    for filename, content in files.items():
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # The full profile also checks formatting. Keep fixtures canonical and read
    # marker positions after gofmt, which can insert blank lines between helpers.
    run(["gofmt", "-w", *files], root)
    manifest(root, profile, g115=g115)
    render(root)
    lint = ["go", "run", "github.com/golangci/golangci-lint/v2/cmd/golangci-lint@" +
            versions["golangci-lint"], "run", "--config", str(root / ".finassist-ci/generated/.golangci.yml"),
            "--max-issues-per-linter", "0", "--max-same-issues", "0",
            "--output.json.path", "stdout", "--show-stats=false", "./..."]
    expected = expected_issues({filename: (root / filename).read_text() for filename in files})
    result = run(lint, root, accepted=not expected)
    try:
        issues = json.loads(result.stdout.strip().splitlines()[-1])["Issues"] or []
    except (IndexError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError("Missing real linter JSON for " + name + "\n" + result.stdout + result.stderr) from error
    actual = {}
    for issue in issues:
        filename = Path(issue["Pos"]["Filename"])
        if filename.is_absolute():
            filename = filename.relative_to(root)
        key = (issue["FromLinter"], filename.as_posix(), issue["Pos"]["Line"])
        if key in actual:
            raise RuntimeError("Duplicate diagnostic in " + name + ": " + repr(issue))
        actual[key] = issue["Text"]
    if actual.keys() != expected.keys() or any(message not in actual[key] for key, message in expected.items()):
        raise RuntimeError("Unexpected diagnostics in " + name + ": expected " + repr(expected) + "; got " + repr(actual))
    print(name + ": " + str(len(actual)) + " expected diagnostics, " + format(time.monotonic() - started, ".1f") + "s", flush=True)


def go_probes(root, versions):
    stubs = root / "stubs"
    stubs.mkdir()
    for name, (module, files) in STUBS.items():
        target = stubs / name
        target.mkdir()
        (target / "go.mod").write_text("module " + module + "\n\ngo 1.26.0\n")
        for filename, content in files.items():
            path = target / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    cases = []
    for profile in ("service", "library"):
        cases.extend([
            (profile + "-baseline", profile, {"probe.go": "package lintprobe\n\nfunc Value() int { return 42 }\n"}, False),
            (profile + "-framework", profile, {"probe.go": 'package lintprobe\n\nimport "github.com/gin-gonic/gin" // want:depguard\n\nfunc Value() int { return gin.Value() }\n'}, False),
            (profile + "-typed-status", profile, {"probe.go": STATUS}, False),
            (profile + "-typed-details", profile, {"probe.go": DETAILS}, False),
            (profile + "-finerr", profile, {"probe.go": 'package lintprobe\n\nimport "github.com/fin-assist/contracts/gen/go/finerr"\n\nfunc ClientError() error { return finerr.NotFound() }\n'}, False),
            (profile + "-lookalike-package", profile, {
                "probe.go": 'package lintprobe\n\nimport "example.com/lintprobe/lookalike/status"\n\nfunc LocalError() error { return status.Error() }\n',
                "lookalike/status/status.go": 'package status\n\nimport "errors"\n\nfunc Error() error { return errors.New("probe") }\n',
            }, False),
            (profile + "-test-exclusions", profile, {"probe_test.go": STATUS.replace(" // want:forbidigo", "") +
                NARROW.split("\n", 2)[2].replace(" // want:gosec:G115", "")}, False),
            (profile + "-g115-enabled", profile, {"probe.go": NARROW}, False),
            (profile + "-g115-excluded", profile, {"probe.go": NARROW.replace(" // want:gosec:G115", "")}, True),
            (profile + "-environment", profile, {"probe.go": ENVIRONMENT if profile == "library" else
                ENVIRONMENT.replace(" // want:forbidigo", "")}, False),
            (profile + "-injected", profile, {"probe.go": INJECTED}, False),
        ])
    cases.extend([
        ("library-kernel", "library", {"probe.go": KERNEL}, False),
        ("library-pgtest-helper", "library", {"internal/pgtest/helper.go": ENVIRONMENT.replace(" // want:forbidigo", "")}, False),
        ("library-pgtest-lookalike", "library", {"internal/pgtestlike/helper.go": ENVIRONMENT}, False),
    ])
    for name, profile, files, g115 in cases:
        go_probe(root / name, name, profile, files, versions, stubs, g115)
    return len(cases)


def main():
    versions = json.loads((COMMON / "versions.json").read_text())
    with tempfile.TemporaryDirectory(prefix="finassist-lint-probe-") as directory:
        root = Path(directory)
        count = go_probes(root, versions)
        (root / "go.mod").write_text("module example.com/lintprobe\n\ngo 1.26.0\n")
        sql = root / "probe.sql"
        sql.write_text("CREATE TABLE lint_probe (id bigint PRIMARY KEY);\n")
        manifest(root, "service")
        render(root)
        squawk = ["npx", "--yes", "squawk-cli@" + versions["squawk"], "--config",
                  str(root / ".finassist-ci/generated/.squawk.toml"), str(sql)]
        run(squawk, root)
        sql.write_text("CREATE TABLE lint_probe (id smallint PRIMARY KEY);\n")
        run(squawk, root, accepted=False, expected="prefer-bigint-over-smallint")
        # Only the approved repository can render the exception; the linter accepts it.
        env = dict(os.environ, GITHUB_ACTIONS="true", GITHUB_REPOSITORY="fin-assist/bank-statement-data")
        manifest(root, "service", smallint=True)
        render(root, env)
        run(squawk, root)
    print("Actual Go/SQL lint probes passed: " + str(count) + " typed Go cases and restricted smallint override.")


if __name__ == "__main__":
    main()
