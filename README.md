# Anki · Notion → Anything

Convert Anki exports **or** Notion toggle pages into a clean 16:9 slide deck (**PDF**, **PowerPoint .pptx**, or **PNG** zip), or back into an **Anki deck** (`.apkg` or `.txt + media .zip`).

Front and back of each card become separate slides, in order (`front1, back1, front2, back2, …`), with shrink-to-fit text layout. Basic HTML is stripped, Anki's `<hr id="answer">` separator is respected, `[sound:…]` tags are dropped, and `{{c1::…}}` cloze deletions are unwrapped to their answer.

For Notion exports, every `<details><summary>front</summary>back</details>` toggle becomes one card. Nested toggles each become their own card. Non-toggle Notion content (regular paragraphs, tables, callouts, etc.) is intentionally ignored.

There are two ways to use it:

- **Web app** (this repo, deployed on Vercel) — drop any number of supported Anki and/or Notion exports at once, pick an output format, and each file is auto-classified and converted individually. Nothing is stored on the server.
- **CLI** (`anki_to_slides.py`) — runs locally from `.txt` + local media folder.

## Web app

The web app lives in:

- `index.html` — the frontend (vanilla HTML/CSS/JS, no build step).
- `api/convert.py` — a Vercel Python function that renders the deck in memory and streams it back.
- `api/blob-upload.js` — a tiny Vercel Edge function that mints short-lived Vercel Blob client-upload tokens so the browser can bypass the platform's 4.5 MB request-body limit.

Uploads are capped at 100 MB per file. Files ≤ 4 MB are POSTed directly to `/api/convert` (one round-trip, no Blob needed); larger files go browser → Vercel Blob → `/api/convert` fetches the blob by URL. Nothing is written to disk beyond a per-request temp directory that's deleted before the response returns. Input blobs are not auto-deleted — configure lifecycle rules in your Blob store or prune periodically.

### Which Anki export to use

| Anki export option | Supported? | Notes |
| --- | --- | --- |
| **Anki Deck Package (.apkg)** | Yes — recommended | One file, images bundled, nothing to tick. Works for newest zstd-compressed bundles. |
| Notes in Plain Text (.txt) | Yes | Text only. Upload as-is for text-only, or zip with `collection.media` for images. |
| Cards in Plain Text (.txt) | Yes | Same tab-separated shape, one row per card. |
| Anki Collection Package (.colpkg) | No | Full-profile backup — routinely multiple GB and always carries your whole media library. Export the deck you want as `.apkg` instead. |
| PDF / HTML exporter (add-on) | No | Those are output formats, not inputs. |

### Which Notion export to use

In Notion: **·· (top-right) → Export → Markdown & CSV** or **HTML**. "Include subpages" is fine; "Create folders for subpages" works either way (we flatten by basename).

| Notion export option | Supported? | Notes |
| --- | --- | --- |
| **Markdown & CSV (.zip)** | Yes — recommended | Each `.md` page is parsed; images alongside the `.md` are bundled. |
| **HTML (.zip)** | Yes | Same toggle parsing as Markdown; images bundled. |
| Single page exported as `.html` | Yes | Drop the file directly. No images unless they're absolute URLs (which we won't fetch). |
| Single page exported as `.md` | Yes | Same as `.html`. Inline `![alt](path)` images are preserved if you also drop a `.zip`. |
| PDF | No | We only parse toggle structure, which the PDF format hides. |

Only `<details>`-style toggle blocks become cards. Everything else on the page (regular paragraphs, headings, callouts, databases, etc.) is ignored. Toggle summary → card front, toggle contents → card back.

### Format quick reference

| Input | Text | Images | Single file | Notes |
| --- | :---: | :---: | :---: | --- |
| Anki `.apkg` | ✓ | ✓ | ✓ | Easiest Anki path. |
| Anki `.zip` (.txt + media) | ✓ | ✓ | ✓ | Use when you only have a `.txt`. |
| Anki `.txt` | ✓ | — | ✓ | Text-only deck. |
| Notion `.html` / `.md` | ✓ | — | ✓ | Single-page export. |
| Notion `.zip` (+ images) | ✓ | ✓ | ✓ | Markdown & CSV or HTML export. |

### Output formats

| Output | What you get |
| --- | --- |
| PDF | One PDF file, one slide per card side. |
| PowerPoint | Editable `.pptx`. |
| PNG | A `.zip` of one PNG per side. |
| Anki `.apkg` | Anki deck — drop into Anki to import. Images bundled. |
| Anki `.txt + media` | A `.zip` containing a tab-separated `.txt` and any images, ready to re-import or feed back into this tool. |

### Using a `.txt` export with images

1. In Anki: **File → Export…** → **Notes in Plain Text (.txt)**.
2. Check **Include HTML and media references** (required — without it the `<img>` tags never make it into the file).
3. Leave "Include tags" and "Include deck name" off.
4. Save the `.txt`.
5. Locate your media folder (or use `Tools → Check Media…`):
   - macOS: `~/Library/Application Support/Anki2/<profile>/collection.media`
   - Windows: `%APPDATA%\Anki2\<profile>\collection.media`
   - Linux: `~/.local/share/Anki2/<profile>/collection.media`
6. Select the `.txt` **and** the `collection.media` folder, right-click → **Compress**. Drop the resulting `.zip`.

Or skip all of this and export an `.apkg` — it already contains everything.

Uploads are capped at 100 MB. Files above ~4 MB are routed through Vercel Blob automatically (see Deploying below for one-time Blob setup).

### What always gets dropped

| Content | Result |
| --- | --- |
| Bold / italic / colors / custom fonts | Stripped — slides use a single typeface |
| Audio / video (`[sound:…]`) | Dropped |
| MathJax / LaTeX (`\[ … \]`) | Kept as raw source, not rendered |
| Cloze deletions (`{{c1::…}}`) | Unwrapped to the answer text |
| Tags, deck name, scheduling | Not included |
| Line breaks (`<br>`, newlines) | **Preserved** |

### Running locally (web)

```bash
npm i -g vercel            # once
npm install                # pulls @vercel/blob for the edge upload helper
pip install -r requirements.txt
vercel link                # associate with your Vercel project (needed to pull Blob token)
vercel env pull            # writes BLOB_READ_WRITE_TOKEN into .env.local
vercel dev                 # serves index.html + /api/* on http://localhost:3000
```

The Blob `onUploadCompleted` webhook doesn't reach `localhost`, so for local tests either upload small files (raw-body path — no Blob needed) or run `vercel dev` behind `ngrok` and set `VERCEL_BLOB_CALLBACK_URL` to your tunnel URL.

### Deploying to Vercel

1. Push this repo to GitHub.
2. In Vercel: **Add New… → Project**, import the repo, accept defaults, deploy.
3. **One-time Blob setup (required for uploads above ~4 MB):**
   - In the project: **Storage → Create → Blob → Connect to project**.
   - Pick any name (e.g. `anki-uploads`), leave access as Public.
   - Select the environments (Production + Preview + Development) where the token should be injected.
   - Redeploy once so the newly-added `BLOB_READ_WRITE_TOKEN` env var takes effect.
4. `vercel.json` already extends the Python function timeout to 60 s for larger decks.

Without Blob configured, the app still works for files ≤ 4 MB via the raw-body fast path — only larger uploads will fail with a clear "Vercel Blob isn't set up" message.

## CLI

The same rendering code is exposed as a standalone script, which additionally supports embedding images from your local Anki media folder.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Single .txt in ./import, output PDF in ./export
python anki_to_slides.py

# Choose a format
python anki_to_slides.py --format pptx
python anki_to_slides.py --format png

# Explicit paths
python anki_to_slides.py import/deck.txt --format pdf --out export/deck.pdf

# Override media location (defaults to macOS Anki2 collection.media)
python anki_to_slides.py --media /path/to/collection.media
```

Output layout:

| format    | result                               |
| --------- | ------------------------------------ |
| pdf       | `export/<stem>.pdf`                  |
| pptx      | `export/<stem>/<stem>.pptx`          |
| png       | `export/<stem>/slide_NNN.png`        |
| apkg      | `export/<stem>.apkg`                 |
| anki-txt  | `export/<stem>.zip`                  |

## Project structure

```
.
├── api/
│   ├── convert.py          # Vercel serverless function — ingests .apkg/.zip/.txt
│   └── blob-upload.js      # Vercel Edge function — mints client-upload tokens
├── anki_to_slides.py       # shared rendering core + CLI entry point
├── dev_server.py           # local server that reuses the Vercel handler
├── index.html              # web frontend
├── package.json            # @vercel/blob dependency for blob-upload.js
├── requirements.txt        # Python deps (reportlab, python-pptx, Pillow, zstandard, genanki)
├── vercel.json             # function timeout
└── .python-version         # Python 3.12
```

## License

MIT
