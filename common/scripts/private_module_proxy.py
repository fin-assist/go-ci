"""Serve prepared private Go module files on the current runner."""

import argparse
from contextlib import ExitStack
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote


PRIVATE_NAMESPACE = ("github.com", "fin-assist")
MAX_PATH_LENGTH = 512
MAX_ZIP_SIZE = 500 << 20
MAX_METADATA_SIZE = 16 << 20
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
PATH_CHARS = re.compile(r"/[a-zA-Z0-9._~!+/@-]+")
UNSUPPORTED_ESCAPE = re.compile(r"%(?!(?:21|2[bB]|40))")
MODULE_SEGMENT = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
NUMBER = r"(?:0|[1-9][0-9]*)"
PRERELEASE_PART = r"(?:[0-9a-z-]|![a-z])+"
VERSION_FILE = re.compile(
    rf"v{NUMBER}\.{NUMBER}\.{NUMBER}(?:-{PRERELEASE_PART}(?:\.{PRERELEASE_PART})*)?"
    r"(?:\+incompatible)?\.(?:info|mod|zip)"
)


def proxy_path(target):
    """Classify a request before accessing the prepared file tree."""
    if len(target) > MAX_PATH_LENGTH or UNSUPPORTED_ESCAPE.search(target):
        return HTTPStatus.FORBIDDEN, ()
    decoded = unquote(target)
    if not PATH_CHARS.fullmatch(decoded):
        return HTTPStatus.FORBIDDEN, ()
    parts = tuple(decoded[1:].split("/"))
    if any(part in ("", ".", "..") for part in parts):
        return HTTPStatus.FORBIDDEN, ()

    # Go escapes uppercase characters as ! followed by lowercase. Classify
    # aliases as private before requiring the exact prepared namespace.
    namespace = tuple(part.replace("!", "").lower().rstrip(".") for part in parts[:2])
    if namespace != PRIVATE_NAMESPACE:
        return HTTPStatus.NOT_FOUND, ()
    if parts[:2] != PRIVATE_NAMESPACE:
        return HTTPStatus.FORBIDDEN, ()

    if parts[-1] == "@latest":
        module = parts[2:-1]
    elif len(parts) >= 3 and parts[-2] == "@v" and (parts[-1] == "list" or VERSION_FILE.fullmatch(parts[-1])):
        module = parts[2:-2]
    else:
        return HTTPStatus.FORBIDDEN, ()
    if not module or any(not MODULE_SEGMENT.fullmatch(part) for part in module):
        return HTTPStatus.FORBIDDEN, ()
    return HTTPStatus.OK, parts


def open_fixture(root_fd, parts):
    # Open each segment without following symlinks, including during replacement
    # of a directory between validation and open.
    with ExitStack() as directories:
        current = root_fd
        for segment in parts[:-1]:
            current = os.open(segment, DIRECTORY_FLAGS, dir_fd=current)
            directories.callback(os.close, current)
        return os.open(parts[-1], FILE_FLAGS, dir_fd=current)


class ProxyHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        # Avoid putting internal module names and versions into job logs.
        pass

    def do_GET(self):
        self.respond(send_body=True)

    def do_HEAD(self):
        self.respond(send_body=False)

    def empty(self, status):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def respond(self, send_body):
        # The base handler normalizes a leading double slash in self.path.
        status, parts = proxy_path(self.requestline.split()[1])
        if status != HTTPStatus.OK:
            self.empty(status)
            return
        try:
            fixture_fd = open_fixture(self.server.root_fd, parts)
        except OSError:
            self.empty(HTTPStatus.FORBIDDEN)
            return

        try:
            props = os.fstat(fixture_fd)
            limit = MAX_ZIP_SIZE if parts[-1].endswith(".zip") else MAX_METADATA_SIZE
            if not stat.S_ISREG(props.st_mode) or props.st_size > limit:
                self.empty(HTTPStatus.FORBIDDEN)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", str(props.st_size))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            if send_body:
                remaining = props.st_size
                while remaining:
                    data = os.read(fixture_fd, min(remaining, 64 << 10))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        finally:
            os.close(fixture_fd)


class ModuleProxy(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(self, root_fd):
        self.root_fd = root_fd
        super().__init__(("127.0.0.1", 0), ProxyHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--address-file", required=True, type=Path)
    args = parser.parse_args()

    root_fd = os.open(args.root, DIRECTORY_FLAGS)
    try:
        with ModuleProxy(root_fd) as proxy:
            host, port = proxy.server_address
            with args.address_file.open("x", encoding="ascii") as destination:
                destination.write(f"http://{host}:{port}\n")
            proxy.serve_forever()
    finally:
        os.close(root_fd)


if __name__ == "__main__":
    main()
