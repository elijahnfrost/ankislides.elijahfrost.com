# Anki → Slides

Convert an Anki plain-text card export into a clean 16:9 slide deck — **PDF**, **PowerPoint (.pptx)**, or **PNG** (bundled as a `.zip`).

Front and back of each card become separate slides, in order (`front1, back1, front2, back2, …`), with shrink-to-fit text layout. Basic HTML is stripped and Anki's `<hr id="answer">` separator is respected.

There are two ways to use it:

- **Web app** (this repo, deployed on Vercel) — upload a `.txt`, pick a format, download the deck. Nothing is stored on the server.
- **CLI** (`anki_to_slides.py`) — runs locally, can also embed images from your Anki media folder.

## Web app

The web app lives in:

- `index.html` — the frontend (vanilla HTML/CSS/JS, no build step).
- `api/convert.py` — a Vercel Python function that renders the deck in memory and streams it back.

Uploads are capped at 10 MB, never written to disk, and the function's working directory is read-only anyway.

### Which Anki export to use

| Anki export option | Supported? | Notes |
| --- | --- | --- |
| **Notes in Plain Text (.txt)** | Yes — use this | Tab-separated front/back. `<img>` references are kept when "Include HTML" is on. |
| Cards in Plain Text (.txt) | Works | Same tab-separated shape, one row per card rather than per note. |
| Anki Deck Package (.apkg) | No | Binary SQLite bundle. Convert to Notes in Plain Text first. |
| Anki Collection Package (.colpkg) | No | Full profile backup; same binary format. |
| PDF / HTML exporter (add-on) | No | Those are output formats, not inputs. |

### Export steps (with images)

1. In Anki: **File → Export…**
2. Export format: **Notes in Plain Text (.txt)**
3. Check **Include HTML and media references** (required for images to survive the export).
4. Leave "Include tags" and "Include deck name" off — they just clutter the slides.
5. Save the `.txt`.
6. Locate your media folder:
   - macOS: `~/Library/Application Support/Anki2/<profile>/collection.media`
   - Windows: `%APPDATA%\Anki2\<profile>\collection.media`
   - Linux: `~/.local/share/Anki2/<profile>/collection.media`
   - Or use `Tools → Check Media…` in Anki to see the path.
7. Select the `.txt` **and** the `collection.media` folder, right-click → **Compress** (macOS) or **Send to → Compressed folder** (Windows). Drop the resulting `.zip` on the page.

For a text-only deck, upload the `.txt` directly — no zip needed.

Uploads are capped at 50 MB. The server extracts into a per-request temp dir and deletes it before the response returns.

### What makes it into the slide vs. what gets dropped

| Content | Result |
| --- | --- |
| Card front / back text | One slide each (front1, back1, front2, back2, …) |
| Line breaks (`<br>`, newlines) | Preserved |
| Images (`<img src>`) | Embedded if you upload the zip with media; dropped otherwise |
| Bold / italic / color / fonts | Stripped — slides use a single typeface |
| MathJax / LaTeX (`\[ … \]`) | Kept as raw source, not rendered |
| Audio / video (`[sound:…]`) | Silently dropped |
| Cloze deletions (`{{c1::…}}`) | Shown as raw text — export with "Include HTML" to get the rendered HTML |
| Tags, deck name, scheduling data | Not included |

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
│   └── convert.py          # Vercel serverless function (in-memory conversion)
├── anki_to_slides.py       # shared rendering core + CLI entry point
├── index.html              # web frontend
├── requirements.txt        # Python deps (reportlab, python-pptx, Pillow)
├── vercel.json             # function timeout
└── .python-version         # Python 3.12
```

## License

MIT
