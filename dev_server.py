#!/usr/bin/env python3
"""Local dev server that mirrors the Vercel runtime.

Serves ``index.html`` (and any other static files) at ``/`` and routes
``/api/convert`` to the exact same ``BaseHTTPRequestHandler`` subclass used by
the Vercel function. This means what you test locally is what ships.

Usage:
    python dev_server.py [--port 3000] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_api_handler():
    """Import ``api/convert.py`` as a module without requiring a package init."""
    spec = importlib.util.spec_from_file_location(
        "api_convert", ROOT / "api" / "convert.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load api/convert.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handler


ApiHandler = _load_api_handler()


class DevHandler(SimpleHTTPRequestHandler):
    """Serve static files from the repo root; delegate /api/* to the Vercel handler."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _delegate_to_api(self) -> None:
        api = ApiHandler.__new__(ApiHandler)
        # Copy state BaseHTTPRequestHandler needs, bypass its __init__ which would
        # try to re-handle the request from rfile.
        api.rfile = self.rfile
        api.wfile = self.wfile
        api.headers = self.headers
        api.command = self.command
        api.path = self.path
        api.request_version = self.request_version
        api.client_address = self.client_address
        api.server = self.server
        api.connection = self.connection
        api.requestline = self.requestline
        api.raw_requestline = self.raw_requestline
        api.close_connection = False

        method = getattr(api, f"do_{self.command}", None)
        if method is None:
            self.send_error(405, f"Method {self.command} not allowed")
            return
        try:
            method()
        except Exception as exc:  # surface tracebacks during local dev
            import traceback
            traceback.print_exc()
            try:
                self.send_error(500, f"api handler crashed: {exc}")
            except Exception:
                pass

    def _is_api(self) -> bool:
        return self.path.split("?", 1)[0].rstrip("/") == "/api/convert"

    def do_GET(self) -> None:
        if self._is_api():
            self.send_error(405, "use POST")
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self._is_api():
            self._delegate_to_api()
            return
        self.send_error(404, "not found")

    def do_OPTIONS(self) -> None:
        if self._is_api():
            self._delegate_to_api()
            return
        self.send_error(404, "not found")

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(
            f"[{self.log_date_time_string()}] {self.command} {self.path} -> "
            f"{fmt % args}\n"
        )
        sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Local dev server for Anki → Slides.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3000)
    args = ap.parse_args()

    os.chdir(ROOT)
    httpd = ThreadingHTTPServer((args.host, args.port), DevHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Anki → Slides dev server listening on {url}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
