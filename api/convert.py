"""Vercel Python serverless handler.

Accepts a POST of a UTF-8 tab-separated Anki export (raw body) with query params:
  - format: one of ``pdf``, ``pptx``, ``png``
  - filename: original upload filename (used to name the download)

Returns the rendered deck as a file download. Nothing is persisted on the
server; all conversion happens in memory and the bytes are streamed back.
"""
from __future__ import annotations

import os
import re
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# `anki_to_slides.py` sits at the repo root; make it importable from /api.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anki_to_slides as ats  # noqa: E402


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB safeguard

_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "application/zip",
}

_EXT = {"pdf": "pdf", "pptx": "pptx", "png": "zip"}

_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


def _clean_stem(raw: str) -> str:
    stem = Path(raw or "deck").stem or "deck"
    stem = _SAFE_STEM.sub("_", stem).strip("._-") or "deck"
    return stem[:80]


class handler(BaseHTTPRequestHandler):
    def _send_json_error(self, status: int, message: str) -> None:
        body = f'{{"error": {message!r}}}'.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS preflight (harmless same-origin too)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        fmt = (qs.get("format", ["pdf"])[0] or "pdf").lower()
        if fmt not in ats.SUPPORTED_FORMATS:
            return self._send_json_error(
                400, f"unsupported format: {fmt!r} (expected pdf, pptx, or png)"
            )

        stem = _clean_stem(qs.get("filename", ["deck"])[0])

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            return self._send_json_error(400, "empty request body")
        if content_length > MAX_UPLOAD_BYTES:
            return self._send_json_error(
                413, f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
            )

        raw = self.rfile.read(content_length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                return self._send_json_error(
                    400, "input must be a UTF-8 encoded tab-separated text file"
                )

        # No media directory on the server; <img> references are skipped.
        sides = ats.read_cards_from_text(text, Path("/nonexistent-media"))
        if not sides:
            return self._send_json_error(
                400,
                "no cards found — expected tab-separated rows (front <TAB> back) "
                "exported from Anki as 'Notes in Plain Text (.txt)'",
            )

        try:
            if fmt == "pdf":
                payload = ats.render_pdf_bytes(sides)
            elif fmt == "pptx":
                payload = ats.render_pptx_bytes(sides)
            else:
                payload = ats.render_png_zip_bytes(sides, stem=stem)
        except Exception as exc:  # surface rendering issues to the client
            return self._send_json_error(500, f"render failed: {exc}")

        filename = f"{stem}.{_EXT[fmt]}"
        self.send_response(200)
        self.send_header("Content-Type", _MIME[fmt])
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.send_header("X-Slide-Count", str(len(sides)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Slide-Count, Content-Disposition")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # silence default stderr spam
        return
