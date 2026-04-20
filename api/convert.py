"""Vercel Python serverless handler.

Accepts a POST with one of:
  - a UTF-8 tab-separated Anki export (``.txt``) as the raw body, OR
  - a ``.zip`` containing exactly one ``.txt`` export plus any referenced
    image files (the zip may mirror your ``collection.media`` folder; we
    flatten by basename when resolving ``<img src="...">`` references).

Query params:
  - format: one of ``pdf``, ``pptx``, ``png``
  - filename: original upload filename (used to name the download)

Returns the rendered deck as a file download. Nothing is persisted on the
server; uploads are extracted to a per-request temp dir that is removed
before the response returns.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

# `anki_to_slides.py` sits at the repo root; make it importable from /api.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import anki_to_slides as ats  # noqa: E402


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB safeguard for zips with media

_ZIP_MAGIC = b"PK\x03\x04"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

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


def _looks_like_zip(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").lower()
    if "zip" in ct:
        return True
    return body[:4] == _ZIP_MAGIC


def _extract_zip_flat(body: bytes, dest: Path) -> Tuple[Optional[str], int]:
    """Extract every file from ``body`` into ``dest`` using its basename.

    Returns ``(txt_contents, media_file_count)``. If the archive contains
    multiple ``.txt`` files we pick the largest (most likely the deck).
    Directory-only entries, hidden files, and name collisions (later wins)
    are ignored.
    """
    txt_candidates: list[tuple[int, str]] = []  # (size, text)
    media_count = 0

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            basename = os.path.basename(info.filename)
            if not basename or basename.startswith("."):
                continue
            # Skip the macOS resource-fork junk Finder-made zips include.
            if "__MACOSX" in info.filename.split("/"):
                continue

            ext = os.path.splitext(basename)[1].lower()
            with zf.open(info, "r") as src:
                data = src.read()

            if ext == ".txt":
                try:
                    txt_candidates.append((len(data), data.decode("utf-8")))
                except UnicodeDecodeError:
                    try:
                        txt_candidates.append((len(data), data.decode("utf-8-sig")))
                    except UnicodeDecodeError:
                        continue
                continue

            if ext in _IMAGE_EXTS:
                out_path = dest / basename
                with open(out_path, "wb") as out:
                    out.write(data)
                media_count += 1

    txt_candidates.sort(reverse=True)
    text = txt_candidates[0][1] if txt_candidates else None
    return text, media_count


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
        content_type = self.headers.get("Content-Type", "")

        workdir: Optional[str] = None
        try:
            if _looks_like_zip(content_type, raw):
                workdir = tempfile.mkdtemp(prefix="anki-slides-")
                try:
                    text, media_count = _extract_zip_flat(raw, Path(workdir))
                except zipfile.BadZipFile:
                    return self._send_json_error(400, "uploaded file is not a valid .zip")
                if text is None:
                    return self._send_json_error(
                        400,
                        "no .txt file found inside the zip — include your Anki "
                        "'Notes in Plain Text (.txt)' export alongside the images",
                    )
                media_dir = Path(workdir)
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        return self._send_json_error(
                            400,
                            "input must be a UTF-8 tab-separated .txt (or a .zip "
                            "containing the .txt plus your media files)",
                        )
                media_count = 0
                media_dir = Path("/nonexistent-media")

            sides = ats.read_cards_from_text(text, media_dir)
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
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

        filename = f"{stem}.{_EXT[fmt]}"
        self.send_response(200)
        self.send_header("Content-Type", _MIME[fmt])
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.send_header("X-Slide-Count", str(len(sides)))
        self.send_header("X-Media-Count", str(media_count))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-Slide-Count, X-Media-Count, Content-Disposition",
        )
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # silence default stderr spam
        return
