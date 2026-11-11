"""Exercise the prepared file boundary and Go proxy fallback rules."""

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import zipfile


spec = importlib.util.spec_from_file_location(
    "private_module_proxy", Path(__file__).resolve().parents[1] / "scripts/private_module_proxy.py",
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

MODULE = "github.com/fin-assist/contracts/gen/go"
VERSION = "v0.11.0"
INFO = json.dumps({"Version": VERSION, "Time": "2026-11-02T09:41:53Z"}).encode()


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class PrivateModuleProxyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fixtures = self.root / "prepared"
        self.package = self.fixtures / MODULE / "@v"
        self.package.mkdir(parents=True)
        (self.package.parent / "@latest").write_bytes(INFO)
        (self.package / "list").write_text(f"{VERSION}\n")
        (self.package / f"{VERSION}.info").write_bytes(INFO)
        go_mod = f"module {MODULE}\n\ngo 1.26.0\n"
        (self.package / f"{VERSION}.mod").write_text(go_mod)
        with zipfile.ZipFile(self.package / f"{VERSION}.zip", "w") as archive:
            archive.writestr(f"{MODULE}@{VERSION}/go.mod", go_mod)
            archive.writestr(f"{MODULE}@{VERSION}/api.go", "package contracts\n")

        fd = os.open(self.fixtures, proxy.DIRECTORY_FLAGS)
        self.addCleanup(os.close, fd)
        self.address = self.enterContext(serving(proxy.ModuleProxy(fd)))

    def request(self, path, method="GET"):
        connection = HTTPConnection(*self.address, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_serves_exact_go_proxy_files_and_encoded_prerelease(self):
        release_candidate = "v0.12.0-!r!c.1.info"
        (self.package / release_candidate).write_bytes(INFO)
        (self.package / "v2.0.0+incompatible.mod").write_bytes(b"legacy module")
        for fixture in ("@latest", "@v/list", f"@v/{VERSION}.info", f"@v/{VERSION}.mod", f"@v/{VERSION}.zip"):
            with self.subTest(fixture=fixture):
                expected = (self.package.parent / fixture).read_bytes()
                self.assertEqual(self.request(f"/{MODULE}/{fixture}"), (200, expected))
        self.assertEqual(self.request(f"/{MODULE}/@v/{release_candidate.replace('!', '%21')}"), (200, INFO))
        self.assertEqual(self.request(f"/{MODULE}/@v/v2.0.0%2Bincompatible.mod"), (200, b"legacy module"))
        self.assertEqual(self.request(f"/{MODULE}/@latest", method="HEAD"), (200, b""))

    def test_only_public_requests_receive_fallback_status(self):
        for path in (
            "/github.com/another-org/tool/@v/list",
            "/github.com/fin-assist-extra/tool/@latest",
            "/example.com/%21module/@v/list",
            "/sumdb/sum.golang.org/supported",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path), (404, b""))

        for path in (
            "/github.com/fin-assist",
            "/github.com/fin-assist/not-prepared/@latest",
            "/github.com/fin-assist./repo/@v/list",
            "/github.com/%21fin-assist/repo/@latest",
            f"/{MODULE}/@v/v1.0.0.ziphash",
            f"/{MODULE}/@v/branch.info",
            f"/{MODULE}/@v/../@latest",
            f"/{MODULE}/@v/%2e%2e/@latest",
            f"/{MODULE}%2f@latest",
            f"/{MODULE}/@latest?debug=1",
            f"//{MODULE}/@latest",
            f"/{MODULE}/{'a' * proxy.MAX_PATH_LENGTH}/@latest",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path), (403, b""))

    def test_refuses_symlinks_special_files_and_oversized_metadata(self):
        outside = self.root / "unprepared"
        outside.mkdir()
        (outside / "@latest").write_bytes(b"unprepared")
        (self.fixtures / "github.com/fin-assist/alias").symlink_to(outside)
        (self.package / "v0.1.0.info").symlink_to(outside / "@latest")
        (self.package / "v0.2.0.info").mkdir()
        os.mkfifo(self.package / "v0.3.0.info")
        with (self.package / "v0.4.0.info").open("wb") as oversized:
            oversized.truncate(proxy.MAX_METADATA_SIZE + 1)

        self.assertEqual(self.request("/github.com/fin-assist/alias/@latest"), (403, b""))
        for version in ("v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"):
            with self.subTest(version=version):
                self.assertEqual(self.request(f"/{MODULE}/@v/{version}.info"), (403, b""))

    def test_native_go_download_and_public_fallback_without_private_leak(self):
        fallback_requests = []

        class FallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                fallback_requests.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        with serving(ThreadingHTTPServer(("127.0.0.1", 0), FallbackHandler)) as fallback:
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(self.root),
                "GOENV": "off",
                "GOWORK": "off",
                "GOTOOLCHAIN": "local",
                "GOMODCACHE": str(self.root / "cache"),
                "GOPROXY": f"http://{self.address[0]}:{self.address[1]},http://{fallback[0]}:{fallback[1]}",
                "GONOPROXY": "none",
                "GOSUMDB": "off",
                "GOVCS": "*:off",
            }

            def go(*args):
                return subprocess.run(["go", *args], cwd=self.root, env=env, capture_output=True, timeout=15)

            completed = go("mod", "download", "-json", f"{MODULE}@{VERSION}")
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            downloaded = json.loads(completed.stdout)
            self.assertEqual(downloaded["Version"], VERSION)
            completed = go("list", "-m", "-json", "-versions", "-retracted", f"{MODULE}@latest")
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(json.loads(completed.stdout)["Versions"], [VERSION])

            latest = "v0.12.0"
            latest_info = json.dumps({"Version": latest, "Time": "2026-11-03T09:41:53Z"})
            (self.package.parent / "@latest").write_text(latest_info)
            (self.package / "list").write_text(f"{VERSION}\n{latest}\n")
            (self.package / f"{latest}.info").write_text(latest_info)
            (self.package / f"{latest}.mod").write_text(f"module {MODULE}\n\ngo 1.26.0\n\nretract {VERSION}\n")
            (self.root / "go.mod").write_text(f"module example.com/library\n\ngo 1.26.0\n\nrequire {MODULE} {VERSION}\n")
            (self.root / "go.sum").write_text(
                f"{MODULE} {VERSION} {downloaded['Sum']}\n{MODULE} {VERSION}/go.mod {downloaded['GoModSum']}\n",
            )
            completed = go("list", "-m", "-u", "-f", "{{if .Retracted}}{{.Path}}: {{index .Retracted 0}}{{end}}", "all")
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertIn(f"{MODULE}: retracted by module author", completed.stdout.decode())

            completed = go("mod", "download", "-json", "github.com/fin-assist/unprepared@v1.0.0")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("403 Forbidden", json.loads(completed.stdout)["Error"])
            self.assertEqual(fallback_requests, [])

            completed = go("mod", "download", "-json", "example.com/public@v1.0.0")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("/example.com/public/@v/v1.0.0.info", fallback_requests)
