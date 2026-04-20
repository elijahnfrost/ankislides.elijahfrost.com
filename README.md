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

### How to export from Anki

1. In Anki desktop: **File → Export…**
2. Export format: **Notes in Plain Text (.txt)**
3. Pick a deck and save the file.
4. Upload that `.txt` to the web app and pick PDF, PowerPoint, or PNG.

> Image references (`<img src="...">`) are skipped on the web because the media folder isn't uploaded. For decks with embedded images, use the CLI locally — see below.

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
