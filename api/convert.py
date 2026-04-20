"""Vercel Python serverless handler.

Accepts a POST with one of:
  - a UTF-8 tab-separated Anki export (``.txt``) as the raw body, OR
  - a plain ``.zip`` containing one ``.txt`` export plus referenced images
    (the zip may mirror your ``collection.media`` folder — we flatten by
    basename), OR
  - an Anki ``.apkg`` bundle: we read the SQLite collection inside,
    reconstruct a TSV from the ``notes`` table, and extract media with
    their original filenames so ``<img>`` tags resolve.

``.colpkg`` (Anki's full-profile backup) uses the same binary layout as
``.apkg`` internally, but it always carries the user's entire media
library and is routinely multiple GB — so it is rejected up front by
filename extension with a message pointing the user at the ``.apkg``
export workflow instead.

Query params:
  - format: one of ``pdf``, ``pptx``, ``png``
  - filename: original upload filename (used to name the download)

Returns the rendered deck as a file download. Nothing is persisted on the
server; uploads are extracted to a per-request temp dir that is removed
before the response returns.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
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


# Vercel's Python serverless runtime enforces a hard 4.5 MB request-body
# limit at the platform edge — anything larger is rejected with a bare 413
# before this handler runs. Match that ceiling here so locally and on
# Vercel we surface the same friendly message when a file is oversize.
MAX_UPLOAD_BYTES = 4_500_000  # 4.5 MB — Vercel request body hard limit

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


def _looks_like_zip(body: bytes) -> bool:
    return body[:4] == _ZIP_MAGIC


_ANKI_DB_NAMES = ("collection.anki21b", "collection.anki21", "collection.anki2")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _is_anki_bundle(body: bytes) -> bool:
    """Check whether a zip body is an Anki .apkg/.colpkg (vs. a plain .zip)."""
    if not _looks_like_zip(body):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return False
    return any(n in names for n in _ANKI_DB_NAMES)


def _zstd_decompress(raw: bytes) -> bytes:
    """Decompress a zstd frame that may lack the content size in its header.

    Anki writes zstd frames without the declared content size, so the
    one-shot ``ZstdDecompressor.decompress(raw)`` call raises
    ``ZstdError: could not determine content size in frame header``. A
    streaming reader handles both variants transparently.
    """
    try:
        import zstandard as zstd  # lazy import — only needed for newest .apkg
    except ImportError as exc:  # pragma: no cover - requirements.txt pins it
        raise RuntimeError(
            "this .apkg uses the newest zstd-compressed schema but the "
            "`zstandard` package is not installed on the server"
        ) from exc
    dctx = zstd.ZstdDecompressor()
    buf = io.BytesIO()
    with dctx.stream_reader(io.BytesIO(raw)) as reader:
        shutil.copyfileobj(reader, buf)
    return buf.getvalue()


def _decompress_anki_db(raw: bytes, db_name: str) -> bytes:
    """Return uncompressed SQLite bytes for an Anki collection file."""
    if db_name.endswith(".anki21b"):
        return _zstd_decompress(raw)
    return raw


def _maybe_zstd(raw: bytes) -> bytes:
    """If ``raw`` begins with the zstd magic, decompress it; otherwise return as-is.

    Anki's v3 package format stores both the ``media`` manifest and the
    individual media blobs as zstd-compressed streams. Older formats
    store them raw. Sniffing the magic makes the extractor format-agnostic.
    """
    if len(raw) >= 4 and raw[:4] == _ZSTD_MAGIC:
        try:
            return _zstd_decompress(raw)
        except Exception:
            return raw
    return raw


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Minimal protobuf varint reader. Returns ``(value, new_pos)``."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _skip_proto_field(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:  # varint
        _, pos = _read_varint(data, pos)
    elif wire_type == 2:  # length-delimited
        length, pos = _read_varint(data, pos)
        pos += length
    elif wire_type == 1:  # 64-bit
        pos += 8
    elif wire_type == 5:  # 32-bit
        pos += 4
    else:
        raise ValueError(f"unsupported wire type {wire_type}")
    return pos


def _parse_media_entries_proto(data: bytes) -> dict:
    """Parse Anki's ``MediaEntries`` protobuf into ``{index_str: filename}``.

    Anki's v3 ``media`` manifest is a protobuf message ``MediaEntries`` with
    a single repeated field ``entries`` (field #1) of ``MediaEntry`` submessages,
    whose first field is the original filename (field #1, string). Each entry's
    index within the list matches the numeric filename of the media blob inside
    the archive (``"0"``, ``"1"``, ``"2"``, …).
    """
    result: dict = {}
    index = 0
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1 and wire_type == 2:
            entry_len, pos = _read_varint(data, pos)
            entry_end = pos + entry_len
            name: Optional[str] = None
            while pos < entry_end:
                sub_tag, pos = _read_varint(data, pos)
                sub_field = sub_tag >> 3
                sub_wire = sub_tag & 0x7
                if sub_field == 1 and sub_wire == 2:
                    name_len, pos = _read_varint(data, pos)
                    name = data[pos : pos + name_len].decode("utf-8", errors="replace")
                    pos += name_len
                else:
                    pos = _skip_proto_field(data, pos, sub_wire)
            if name:
                result[str(index)] = name
            index += 1
        else:
            pos = _skip_proto_field(data, pos, wire_type)
    return result


def _parse_media_manifest(raw: bytes) -> dict:
    """Parse Anki's ``media`` manifest regardless of package version.

    - v1/v2 (``.anki2`` / ``.anki21``): JSON ``{"<numeric_id>": "<name>"}``.
    - v3    (``.anki21b``): zstd-compressed protobuf ``MediaEntries``.
    """
    payload = _maybe_zstd(raw)
    try:
        parsed = json.loads(payload.decode("utf-8"))
        if isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items() if isinstance(v, str)}
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        return _parse_media_entries_proto(payload)
    except Exception:
        return {}


def _extract_anki_bundle(body: bytes, dest: Path) -> Tuple[str, int]:
    """Extract an ``.apkg`` or ``.colpkg`` into ``dest``.

    Returns ``(reconstructed_tsv_text, media_count)``. The TSV mirrors
    Anki's "Notes in Plain Text (.txt)" export with HTML included so the
    downstream parser can treat it identically to a direct text upload.
    Media files are renamed to their original filenames so ``<img src>``
    references resolve against ``dest``.
    """
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
        db_name = next((n for n in _ANKI_DB_NAMES if n in names), None)
        if db_name is None:
            raise ValueError("no Anki collection file found inside the archive")

        db_bytes = _decompress_anki_db(zf.read(db_name), db_name)
        db_path = dest / "_collection.sqlite"
        db_path.write_bytes(db_bytes)

        media_map: dict = {}
        if "media" in names:
            media_map = _parse_media_manifest(zf.read("media"))

        media_count = 0
        for numeric_id, original_name in media_map.items():
            if not isinstance(original_name, str):
                continue
            basename = os.path.basename(original_name)
            if not basename:
                continue
            ext = os.path.splitext(basename)[1].lower()
            if ext not in _IMAGE_EXTS:
                continue  # skip audio/video — slide decks can't use them
            try:
                data = zf.read(str(numeric_id))
            except KeyError:
                continue
            data = _maybe_zstd(data)  # v3 media blobs are zstd-compressed
            try:
                (dest / basename).write_bytes(data)
                media_count += 1
            except OSError:
                continue

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = None
        cur = conn.cursor()
        # Detect tables present — newer Anki still uses `notes`, but be defensive.
        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "notes" not in tables:
            raise ValueError(
                "this collection has no `notes` table (found: "
                + ", ".join(sorted(tables)) + ")"
            )
        cur.execute("SELECT flds FROM notes ORDER BY id")
        rows = cur.fetchall()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    for (flds,) in rows:
        if flds is None:
            continue
        parts = str(flds).split("\x1f")
        front = parts[0] if len(parts) >= 1 else ""
        back = parts[1] if len(parts) >= 2 else parts[0] if parts else ""
        if not front and not back:
            continue
        writer.writerow([front, back])

    # Best-effort cleanup of the db file so it isn't handed to the renderer.
    try:
        db_path.unlink()
    except OSError:
        pass

    return buf.getvalue(), media_count


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
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        try:
            sys.stderr.write(f"[convert] {status}: {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

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

        raw_filename = qs.get("filename", ["deck"])[0] or ""
        stem = _clean_stem(raw_filename)

        # Anki's .colpkg is a full-profile backup — binary-identical in format
        # to .apkg but routinely multiple GB and containing every image, audio,
        # and video file in the user's collection. We gate it out by extension
        # so accidental uploads fail fast with a clear remedy rather than
        # eating the whole request body first.
        if raw_filename.lower().endswith(".colpkg"):
            return self._send_json_error(
                415,
                "collection backups (.colpkg) aren't accepted. In Anki, "
                "use File \u2192 Export \u2192 Current deck, choose Anki "
                "Deck Package (.apkg), and upload that instead.",
            )

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            return self._send_json_error(400, "empty request body")
        if content_length > MAX_UPLOAD_BYTES:
            return self._send_json_error(
                413,
                f"file too large ({content_length / (1024 * 1024):.2f} MB, "
                f"max {MAX_UPLOAD_BYTES / (1024 * 1024):.1f} MB). "
                "Re-export from Anki as File \u2192 Export \u2192 Current deck "
                "and untick \u201CInclude media\u201D if you don't need images.",
            )

        raw = self.rfile.read(content_length)

        workdir: Optional[str] = None
        source_kind = "txt"
        try:
            if _looks_like_zip(raw):
                workdir = tempfile.mkdtemp(prefix="anki-slides-")
                if _is_anki_bundle(raw):
                    source_kind = "apkg"
                    try:
                        text, media_count = _extract_anki_bundle(raw, Path(workdir))
                    except zipfile.BadZipFile:
                        traceback.print_exc()
                        return self._send_json_error(400, "the Anki bundle is not a valid archive")
                    except sqlite3.DatabaseError as exc:
                        traceback.print_exc()
                        return self._send_json_error(
                            400,
                            "could not read the Anki collection database inside "
                            "this .apkg — re-exporting from Anki with "
                            "\u201CSupport older Anki versions\u201D checked "
                            "usually fixes it. "
                            f"(details: {exc})",
                        )
                    except Exception as exc:
                        traceback.print_exc()
                        return self._send_json_error(
                            400,
                            "could not parse this Anki bundle. Try re-exporting "
                            "from Anki as \u201CNotes in Plain Text (.txt)\u201D, "
                            "or tick \u201CSupport older Anki versions\u201D when "
                            "exporting the .apkg. "
                            f"(details: {type(exc).__name__}: {exc})",
                        )
                    if not text.strip():
                        return self._send_json_error(
                            400, "the Anki bundle contains no notes"
                        )
                else:
                    source_kind = "zip"
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
                            "unrecognized upload — expected a .txt, .zip, or .apkg",
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
        self.send_header("X-Source-Kind", source_kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-Slide-Count, X-Media-Count, X-Source-Kind, Content-Disposition",
        )
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # silence default stderr spam
        return
