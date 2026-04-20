# Anki → Slides

Convert any Anki export into a clean 16:9 slide deck — **PDF**, **PowerPoint (.pptx)**, or **PNG** (bundled as a `.zip`).

Front and back of each card become separate slides, in order (`front1, back1, front2, back2, …`), with shrink-to-fit text layout. Basic HTML is stripped, Anki's `<hr id="answer">` separator is respected, `[sound:…]` tags are dropped, and `{{c1::…}}` cloze deletions are unwrapped to their answer.

There are two ways to use it:

- **Web app** (this repo, deployed on Vercel) — drop any number of supported Anki exports at once, pick a format, and each is converted and downloaded individually. Nothing is stored on the server.
- **CLI** (`anki_to_slides.py`) — runs locally from `.txt` + local media folder.

## Web app

The web app lives in:

- `index.html` — the frontend (vanilla HTML/CSS/JS, no build step).
- `api/convert.py` — a Vercel Python function that renders the deck in memory and streams it back.

Uploads are capped at 50 MB per file (no limit on how many files you drop at once), never written to disk beyond a per-request temp directory that's deleted before the response returns.

### Which Anki export to use

| Anki export option | Supported? | Notes |
| --- | --- | --- |
| **Anki Deck Package (.apkg)** | Yes — recommended | One file, images bundled, nothing to tick. Works for newest zstd-compressed bundles. |
| Anki Collection Package (.colpkg) | Yes | Full-profile backup. Same binary format as `.apkg`. |
| Notes in Plain Text (.txt) | Yes | Text only. Upload as-is for text-only, or zip with `collection.media` for images. |
| Cards in Plain Text (.txt) | Yes | Same tab-separated shape, one row per card. |
| PDF / HTML exporter (add-on) | No | Those are output formats, not inputs. |

### Format quick reference

| Input | Text | Images | Single file | Notes |
| --- | :---: | :---: | :---: | --- |
| `.apkg` | ✓ | ✓ | ✓ | Easiest path. |
| `.colpkg` | ✓ | ✓ | ✓ | Whole profile → lots of slides. |
| `.zip` (.txt + media) | ✓ | ✓ | ✓ | Use when you only have a `.txt`. |
| `.txt` | ✓ | — | ✓ | Text-only deck. |

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

Uploads are capped at 50 MB. Server extracts into a per-request temp dir and deletes it before responding.

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
pip install -r requirements.txt
vercel dev                 # serves index.html + /api/convert on http://localhost:3000
```

### Deploying to Vercel

1. Push this repo to GitHub.
2. In Vercel: **Add New… → Project**, import the repo, accept defaults, deploy.
3. `vercel.json` already extends the function timeout to 60s for larger decks.

No environment variables are required.

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

| format | result                               |
| ------ | ------------------------------------ |
| pdf    | `export/<stem>.pdf`                  |
| pptx   | `export/<stem>/<stem>.pptx`          |
| png    | `export/<stem>/slide_NNN.png`        |

## Project structure

```
.
├── api/
│   └── convert.py          # Vercel serverless function — ingests .apkg/.colpkg/.zip/.txt
├── anki_to_slides.py       # shared rendering core + CLI entry point
├── dev_server.py           # local server that reuses the Vercel handler
├── index.html              # web frontend
├── requirements.txt        # Python deps (reportlab, python-pptx, Pillow, zstandard)
├── vercel.json             # function timeout
└── .python-version         # Python 3.12
```

## License

MIT
