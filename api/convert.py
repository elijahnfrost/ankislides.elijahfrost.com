"""Vercel Python serverless handler.

Accepts a POST in one of two shapes:

1. **Raw body** (legacy fast path, ≤ 4.5 MB)

   ``Content-Type: application/octet-stream | text/plain``
   Body: the raw bytes of a ``.txt``, ``.zip`` or ``.apkg``.

2. **JSON body referencing a Vercel Blob** (large uploads, no size cap)

   ``Content-Type: application/json``
   Body: ``{ "blobUrl": "https://...blob.vercel-storage.com/...",
           "filename": "deck.apkg" }``

   The client uploaded the file straight to Vercel Blob via
   ``/api/blob-upload`` — we just fetch it. An outbound fetch from a
   function is not capped, so this path bypasses Vercel's 4.5 MB
   request-body platform limit entirely.

Regardless of the upload shape, we recognise the same payload types:
  - a UTF-8 tab-separated Anki export (``.txt``), OR
  - a plain ``.zip`` containing one ``.txt`` export plus referenced
    images (the zip may mirror your ``collection.media`` folder — we
    flatten by basename), OR
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
  - filename: original upload filename (used to name the download);
              overridden by the JSON body's ``filename`` field if present.

Returns the rendered deck as a file download. Nothing is persisted on the
server; uploads are extracted to a per-request temp dir that is removed
before the response returns. Input blobs on the Blob store are not
deleted here — use Vercel's dashboard lifecycle rules (or manual pruning)
to keep storage from growing.
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
import urllib.error
import urllib.request
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


# Vercel's Python serverless runtime enforces a hard 4.5 MB **request-body**
# limit at the platform edge. For the raw-body code path (Content-Type:
# application/octet-stream | text/plain) we reject anything larger up front.
# The JSON/Blob code path reuses this as the cap on how many bytes we'll
# pull back down from Vercel Blob, but the ceiling there is much higher
# because the file never flowed *through* a Vercel request body.
MAX_UPLOAD_BYTES = 4_500_000  # 4.5 MB — Vercel request body hard limit
MAX_BLOB_BYTES = 100 * 1024 * 1024  # 100 MB — mirrors api/blob-upload.js

# Whitelist of hosts we'll blob-fetch from. Prevents the endpoint from
# being used as a generic "fetch-anything" proxy: if someone POSTs a
# `blobUrl` pointing at, say, an internal metadata service, we refuse.
# Public Vercel Blob URLs look like `<id>.public.blob.vercel-storage.com`
# — the common suffix covers any future CDN hostname changes too.
_BLOB_HOST_SUFFIX = ".vercel-storage.com"
_BLOB_FETCH_TIMEOUT = 25  # seconds — well under the 60 s function timeout

_ZIP_MAGIC = b"PK\x03\x04"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_NOTION_TEXT_EXTS = {".html", ".htm", ".md", ".markdown"}

_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "application/zip",
    "apkg": "application/octet-stream",
    "anki-txt": "application/zip",
}

_EXT = {
    "pdf": "pdf",
    "pptx": "pptx",
    "png": "zip",
    "apkg": "apkg",
    "anki-txt": "zip",
}

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


_NOTION_DETAILS_RE = re.compile(rb"<details\b", re.IGNORECASE)


def _looks_like_notion_html(body: bytes) -> bool:
    """Heuristic for single-file Notion exports: contains a ``<details>`` toggle."""
    return bool(_NOTION_DETAILS_RE.search(body[:65536]))


def _is_notion_zip(body: bytes) -> bool:
    """A zip is a Notion export if it carries any ``.html``/``.md`` entries.

    Notion's "Export as Markdown & CSV" produces a zip of ``.md`` files plus
    image folders; "Export as HTML" produces a zip of ``.html`` files plus
    image folders. Either signal is enough — Anki's ``.apkg`` was already
    matched ahead of us by ``_is_anki_bundle``, and a plain Anki txt+media
    zip won't contain ``.md`` or ``.html`` files.
    """
    if not _looks_like_zip(body):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                ext = os.path.splitext(info.filename)[1].lower()
                if ext in _NOTION_TEXT_EXTS:
                    return True
    except zipfile.BadZipFile:
        return False
    return False


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


def _extract_notion_zip(body: bytes, dest: Path) -> Tuple[str, str, int]:
    """Extract a Notion HTML/Markdown export zip into ``dest``.

    Returns ``(combined_text, kind, media_count)`` where ``kind`` is either
    ``"notion-html"`` or ``"notion-md"`` based on which payload type
    dominates the archive. All ``.html``/``.md`` files are concatenated in
    archive order (with a separating blank line); images are flattened into
    ``dest`` by basename so ``<img src>`` references resolve.

    Notion exports nest images in folders named after the page title (e.g.
    ``My Page abc123/foo.png``). We flatten by basename — the same strategy
    ``_extract_zip_flat`` uses for Anki txt+media zips.
    """
    html_chunks: list[tuple[str, str]] = []  # (filename, text)
    md_chunks: list[tuple[str, str]] = []
    media_count = 0

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            basename = os.path.basename(info.filename)
            if not basename or basename.startswith("."):
                continue
            if "__MACOSX" in info.filename.split("/"):
                continue

            ext = os.path.splitext(basename)[1].lower()
            with zf.open(info, "r") as src:
                data = src.read()

            if ext in (".html", ".htm"):
                try:
                    html_chunks.append((info.filename, data.decode("utf-8")))
                except UnicodeDecodeError:
                    try:
                        html_chunks.append((info.filename, data.decode("utf-8-sig")))
                    except UnicodeDecodeError:
                        continue
                continue

            if ext in (".md", ".markdown"):
                try:
                    md_chunks.append((info.filename, data.decode("utf-8")))
                except UnicodeDecodeError:
                    try:
                        md_chunks.append((info.filename, data.decode("utf-8-sig")))
                    except UnicodeDecodeError:
                        continue
                continue

            if ext in _IMAGE_EXTS:
                out_path = dest / basename
                try:
                    with open(out_path, "wb") as out:
                        out.write(data)
                    media_count += 1
                except OSError:
                    continue

    html_chunks.sort(key=lambda t: t[0])
    md_chunks.sort(key=lambda t: t[0])
    if html_chunks:
        return "\n\n".join(text for _, text in html_chunks), "notion-html", media_count
    if md_chunks:
        return "\n\n".join(text for _, text in md_chunks), "notion-md", media_count
    return "", "notion-html", media_count


def _decode_text_candidate(data: bytes) -> Optional[str]:
    """Best-effort UTF-8 decode (with BOM fallback) for a zip entry's bytes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None


def _summarize_zip_contents(counts: dict) -> str:
    """Render a short ``"3 .apkg, 12 .png, 1 .csv"`` summary for error messages."""
    if not counts:
        return "(empty)"
    parts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(
        f"{n} {ext or '(no extension)'}" for ext, n in parts
    )


def _extract_zip_flat(body: bytes, dest: Path) -> Tuple[Optional[str], int, dict]:
    """Extract every file from ``body`` into ``dest`` using its basename.

    Returns ``(text, media_file_count, contents_by_ext)``. ``text`` is the
    deck text we'll feed to the parser, sourced (in priority order) from:

      1. A ``.txt`` entry — Anki's "Notes in Plain Text (.txt)" export.
         If multiple are present we pick the largest.
      2. A ``.csv``/``.tsv`` entry — same shape as the Anki .txt export
         (tab-separated front/back), just a different extension. Some
         Anki addons and cross-platform tools default to these.
      3. One or more nested ``.apkg``/``.colpkg`` archives — we extract
         each and concatenate their reconstructed TSVs. This is the
         shape of a "batch export" of several decks bundled together
         (e.g. ``anki-decks-<hash>.zip``); without this branch every such
         upload failed with "no .txt file found inside the zip".

    ``contents_by_ext`` is a ``{ext: count}`` map used to build a
    diagnostic error message when we still can't find a payload.

    Directory-only entries, hidden files, ``__MACOSX`` resource-fork
    junk, and name collisions (later wins) are ignored.
    """
    txt_candidates: list[tuple[int, str]] = []  # (size, text)
    csv_candidates: list[tuple[int, str]] = []
    apkg_candidates: list[tuple[str, bytes]] = []  # (basename, raw bytes)
    media_count = 0
    contents: dict[str, int] = {}

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
            contents[ext] = contents.get(ext, 0) + 1
            with zf.open(info, "r") as src:
                data = src.read()

            if ext == ".txt":
                decoded = _decode_text_candidate(data)
                if decoded is not None:
                    txt_candidates.append((len(data), decoded))
                continue

            if ext in (".csv", ".tsv"):
                decoded = _decode_text_candidate(data)
                if decoded is not None:
                    csv_candidates.append((len(data), decoded))
                continue

            if ext in (".apkg", ".colpkg"):
                apkg_candidates.append((basename, data))
                continue

            if ext in _IMAGE_EXTS:
                out_path = dest / basename
                with open(out_path, "wb") as out:
                    out.write(data)
                media_count += 1

    if txt_candidates:
        txt_candidates.sort(reverse=True)
        return txt_candidates[0][1], media_count, contents

    if csv_candidates:
        csv_candidates.sort(reverse=True)
        return csv_candidates[0][1], media_count, contents

    if apkg_candidates:
        # Concatenate the TSVs from every nested Anki bundle. Media files
        # are extracted into ``dest`` by ``_extract_anki_bundle`` itself
        # (using each bundle's media manifest to get original filenames),
        # so ``<img src>`` references resolve as if the user had uploaded
        # a single .apkg. If any individual bundle fails to parse we skip
        # it and surface what we *did* manage to extract — better to
        # convert 4 of 5 decks than fail the whole upload.
        combined: list[str] = []
        for name, blob in apkg_candidates:
            try:
                inner_text, inner_media = _extract_anki_bundle(blob, dest)
            except Exception:
                continue
            if inner_text.strip():
                combined.append(inner_text)
                media_count += inner_media
        if combined:
            return "\n".join(combined), media_count, contents

    return None, media_count, contents


def _fetch_blob_bytes(blob_url: str) -> bytes:
    """Download a Vercel Blob URL's contents into memory, capped at MAX_BLOB_BYTES.

    We validate the hostname first so this endpoint can't be coerced into
    fetching arbitrary URLs (SSRF protection). The fetch streams in 256 KB
    chunks and aborts early if we cross the cap — important because a
    malicious client could otherwise register a 5 GB blob and force us to
    download it before failing.
    """
    parsed = urlparse(blob_url)
    if parsed.scheme != "https":
        raise ValueError("blobUrl must use https://")
    host = (parsed.hostname or "").lower()
    if not host.endswith(_BLOB_HOST_SUFFIX):
        raise ValueError(
            f"blobUrl host {host!r} is not a Vercel Blob URL "
            f"(expected *{_BLOB_HOST_SUFFIX})"
        )

    req = urllib.request.Request(blob_url, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=_BLOB_FETCH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"blobUrl returned HTTP {exc.code} — the upload may have "
            f"expired or been deleted"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"could not reach blob storage: {exc.reason}") from exc

    # Honour Content-Length when the CDN provides one; fall back to a
    # streaming read otherwise. Either way enforce the cap.
    content_length_hdr = resp.headers.get("Content-Length")
    if content_length_hdr:
        try:
            declared = int(content_length_hdr)
        except ValueError:
            declared = -1
        if declared > MAX_BLOB_BYTES:
            raise ValueError(
                f"blob is {declared / (1024 * 1024):.1f} MB, exceeds the "
                f"{MAX_BLOB_BYTES / (1024 * 1024):.0f} MB cap"
            )

    buf = bytearray()
    chunk = 256 * 1024
    while True:
        data = resp.read(chunk)
        if not data:
            break
        buf.extend(data)
        if len(buf) > MAX_BLOB_BYTES:
            raise ValueError(
                f"blob body exceeds the "
                f"{MAX_BLOB_BYTES / (1024 * 1024):.0f} MB cap"
            )
    return bytes(buf)


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

        # Two upload shapes: a tiny JSON envelope referencing a Vercel Blob
        # (the escape hatch for > 4.5 MB files), or the raw file bytes
        # (legacy fast path, ≤ 4.5 MB). We dispatch on Content-Type.
        content_type = (self.headers.get("Content-Type") or "").lower()
        is_json_body = content_type.startswith("application/json")

        if is_json_body:
            # The JSON envelope itself is tiny — reject anything bigger than
            # a kilobyte so we can't be made to buffer garbage.
            if content_length > 8 * 1024:
                return self._send_json_error(
                    400, "JSON envelope too large (max 8 KB)"
                )
            try:
                envelope = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self._send_json_error(
                    400, f"invalid JSON body: {exc}"
                )
            blob_url = envelope.get("blobUrl") if isinstance(envelope, dict) else None
            if not isinstance(blob_url, str) or not blob_url:
                return self._send_json_error(
                    400, "JSON body must include a string `blobUrl`"
                )
            # JSON filename overrides the query-param filename when both
            # are supplied — the JSON one reflects the *user's* original
            # upload, the query one is cosmetic.
            json_filename = envelope.get("filename")
            if isinstance(json_filename, str) and json_filename:
                raw_filename = json_filename
                stem = _clean_stem(raw_filename)
                if raw_filename.lower().endswith(".colpkg"):
                    return self._send_json_error(
                        415,
                        "collection backups (.colpkg) aren't accepted. In Anki, "
                        "use File \u2192 Export \u2192 Current deck, choose Anki "
                        "Deck Package (.apkg), and upload that instead.",
                    )
            try:
                raw = _fetch_blob_bytes(blob_url)
            except ValueError as exc:
                return self._send_json_error(400, f"blob fetch failed: {exc}")
            except Exception as exc:
                traceback.print_exc()
                return self._send_json_error(
                    500, f"blob fetch failed: {type(exc).__name__}: {exc}"
                )
            if not raw:
                return self._send_json_error(400, "blob is empty")
        else:
            if content_length > MAX_UPLOAD_BYTES:
                return self._send_json_error(
                    413,
                    f"file too large ({content_length / (1024 * 1024):.2f} MB, "
                    f"max {MAX_UPLOAD_BYTES / (1024 * 1024):.1f} MB for direct "
                    "upload). Files above this cap are routed through Vercel "
                    "Blob by the frontend automatically — if you're seeing "
                    "this error, Blob may not be configured on the server.",
                )
            raw = self.rfile.read(content_length)

        workdir: Optional[str] = None
        source_kind = "txt"
        # Filename hint is used as a tiebreaker when content sniffing alone
        # can't distinguish (e.g. a single-file Notion .md export with no
        # toggles still wants the markdown parser, not the TSV parser).
        name_lower = raw_filename.lower() if raw_filename else ""
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
                    sides = ats.read_cards_from_text(text, Path(workdir))
                elif _is_notion_zip(raw):
                    try:
                        text, source_kind, media_count = _extract_notion_zip(
                            raw, Path(workdir)
                        )
                    except zipfile.BadZipFile:
                        return self._send_json_error(400, "uploaded file is not a valid .zip")
                    if not text.strip():
                        return self._send_json_error(
                            400,
                            "the Notion export zip contains no .html or .md pages",
                        )
                    if source_kind == "notion-md":
                        sides = ats.read_cards_from_notion_markdown(text, Path(workdir))
                    else:
                        sides = ats.read_cards_from_notion_html(text, Path(workdir))
                else:
                    source_kind = "zip"
                    try:
                        text, media_count, contents = _extract_zip_flat(
                            raw, Path(workdir)
                        )
                    except zipfile.BadZipFile:
                        return self._send_json_error(400, "uploaded file is not a valid .zip")
                    if text is None:
                        return self._send_json_error(
                            400,
                            "couldn't find a deck inside the zip (saw: "
                            + _summarize_zip_contents(contents)
                            + "). Include either an Anki 'Notes in Plain Text "
                            "(.txt)' export, a .csv/.tsv with the same shape, or "
                            "one or more .apkg files alongside the images.",
                        )
                    sides = ats.read_cards_from_text(text, Path(workdir))
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
                            "unrecognized upload — expected a .txt, .zip, .apkg, "
                            ".html, or .md (Notion export)",
                        )
                media_count = 0
                media_dir = Path("/nonexistent-media")

                ext = os.path.splitext(name_lower)[1] if name_lower else ""
                # Prefer the filename extension for routing — Anki .txt
                # exports with HTML enabled can legitimately contain a
                # ``<details>`` tag inside a card field, and we don't want
                # the content sniffer to capture them. ``.md`` needs the
                # markdown image-syntax pre-pass that the HTML branch
                # skips. We only fall back to content sniffing for
                # unhinted uploads (filename empty or unrecognised
                # extension).
                if ext in (".md", ".markdown"):
                    source_kind = "notion-md"
                    sides = ats.read_cards_from_notion_markdown(text, media_dir)
                elif ext in (".html", ".htm"):
                    source_kind = "notion-html"
                    sides = ats.read_cards_from_notion_html(text, media_dir)
                elif ext == ".txt":
                    sides = ats.read_cards_from_text(text, media_dir)
                elif _looks_like_notion_html(raw):
                    source_kind = "notion-html"
                    sides = ats.read_cards_from_notion_html(text, media_dir)
                else:
                    sides = ats.read_cards_from_text(text, media_dir)

            if not sides:
                if source_kind in ("notion-html", "notion-md"):
                    return self._send_json_error(
                        400,
                        "no cards found — Notion exports must contain at least "
                        "one toggle block (the front becomes the toggle title, "
                        "the back becomes its contents)",
                    )
                return self._send_json_error(
                    400,
                    "no cards found — expected tab-separated rows (front <TAB> back) "
                    "exported from Anki as 'Notes in Plain Text (.txt)', or a "
                    "Notion export with toggle blocks",
                )

            try:
                if fmt == "pdf":
                    payload = ats.render_pdf_bytes(sides)
                elif fmt == "pptx":
                    payload = ats.render_pptx_bytes(sides)
                elif fmt == "png":
                    payload = ats.render_png_zip_bytes(sides, stem=stem)
                elif fmt == "apkg":
                    payload = ats.render_apkg_bytes(sides, deck_name=stem)
                elif fmt == "anki-txt":
                    payload = ats.render_anki_txt_zip_bytes(sides, stem=stem)
                else:  # pragma: no cover — guarded by SUPPORTED_FORMATS check
                    return self._send_json_error(
                        400, f"unsupported format: {fmt!r}"
                    )
            except Exception as exc:  # surface rendering issues to the client
                traceback.print_exc()
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
