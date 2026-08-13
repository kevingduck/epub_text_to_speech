# EPUB Reader - Text to Speech for Audio Books

Self-hosted EPUB → audiobook server. Upload a DRM-free EPUB from a web page;
the server reads it aloud with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
TTS and produces a single MP3 you can play from any browser — including
iPhone Safari with the screen locked and the phone in your pocket.

Built for a Mac on your local network / Tailscale tailnet. Personal use only:
there is **no authentication** — the network perimeter (Tailscale) is the
security boundary. Never expose it to the public internet.

## How it works

The design is driven by one iOS fact: when the screen locks, Safari suspends
JavaScript, Web Audio, and the Web Speech API — but keeps playing a plain
`<audio>` element streaming a real file. So:

- **TTS runs on the server**, never in the browser.
- **Each book becomes ONE MP3.** Chapter navigation seeks within the file
  (`currentTime`); nothing depends on JS firing while locked.
- Position saving is best-effort (beacons on pause/lock/close), never
  load-bearing.

Pipeline, per book:

1. **Parse** (`app/epub_parse.py`) — walk the EPUB spine in reading order,
   strip footnotes/tables/page numbers, detect chapter titles from headings
   or the TOC, merge Calibre-split continuation files, auto-exclude front
   matter (cover, copyright, TOC…) by name and size heuristics.
2. **Normalize** (`app/normalize.py`) — expand abbreviations (Mr. → Mister),
   convert roman-numeral headings, drop citation markers and page numbers,
   normalize quotes/dashes, then split into ≤400-char segments (Kokoro
   garbles anything past ~510 phoneme tokens). Heavily unit-tested.
3. **Synthesize** (`app/synth.py`) — a `multiprocessing` pool of Kokoro
   workers, GPU (Apple MPS) by default on Apple Silicon. Long chapters are
   split into ~8k-char parts so one giant chapter can't bottleneck the run.
   Part WAVs are written atomically and kept on failure, so interrupted jobs
   resume instead of restarting. ~74x realtime on an M2 Ultra — a novel
   renders in minutes.
4. **Encode** (`app/encode.py`) — one ffmpeg pass concatenates the WAVs into
   a 64kbps mono MP3 (with Xing header so Safari can seek), and per-chapter
   millisecond offsets are stored for the chapter list.

Jobs run one at a time as a subprocess (`python -m app.worker --book-id …`)
spawned by the FastAPI server (`app/main.py`), so the web process never
loads PyTorch and a crashed render can't take the site down. State lives in
SQLite (WAL). The frontend (`static/`) is one HTML page, vanilla JS.

## Setup

```bash
brew install ffmpeg espeak-ng python@3.12
python3.12 -m venv .venv
.venv/bin/pip install kokoro soundfile ebooklib beautifulsoup4 lxml \
    "fastapi>=0.115" "uvicorn[standard]" pillow python-multipart
```

The Kokoro model (~330MB) downloads from Hugging Face on first synthesis
(cached in `~/.cache/huggingface`). If phonemizer can't find espeak:
`export PHONEMIZER_ESPEAK_LIBRARY=/opt/homebrew/lib/libespeak-ng.dylib`.

## Use

**Server:**

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 — upload an EPUB, pick a voice, watch progress,
play. In the player, "Chapter selection & re-render" lets you exclude
misdetected front matter or switch voices (re-renders reuse cached audio
where possible).

**CLI (no server):**

```bash
.venv/bin/python -m app.worker book.epub --dry-run   # preview chapter table
.venv/bin/python -m app.worker book.epub -o book.mp3 # render
```

**From your phone (Tailscale):**

```bash
tailscale serve --bg 8000
```

Then open `https://<machine>.<tailnet>.ts.net` in iOS Safari. Do **not** use
`tailscale funnel` or port-forwarding — no auth, remember.

**Run at login (launchd):** edit the paths in
`scripts/com.local.audiobooks.plist`, then:

```bash
cp scripts/com.local.audiobooks.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.audiobooks.plist
```

And enable *System Settings → Energy → Prevent automatic sleeping when the
display is off*.

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `AUDIOBOOK_DEVICE` | `auto` | `mps` (Apple GPU) \| `cpu`; auto picks MPS on Apple Silicon |
| `AUDIOBOOK_WORKERS` | 6 (MPS) / core-based (CPU) | parallel synthesis processes |
| `AUDIOBOOK_TORCH_THREADS` | 1 (MPS) / 2 (CPU) | CPU threads per worker |

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/books` | multipart `file`, `voice`; returns `{id}` |
| GET | `/api/books` | library list with status/progress |
| GET | `/api/books/{id}` | detail + chapters with `start_ms` |
| GET | `/api/books/{id}/audio` | the MP3 (supports HTTP Range) |
| GET | `/api/books/{id}/cover` | JPEG or 404 |
| POST | `/api/books/{id}/position` | save `{position_ms}` |
| GET | `/api/books/{id}/position` | `{position_ms}` |
| POST | `/api/books/{id}/rerender` | `{voice?, chapters?: [{idx, included}]}` |
| DELETE | `/api/books/{id}` | delete book + files (cancels running job) |
| GET | `/api/voices` | Kokoro voice list |

All user data (uploads, audio, covers, database, logs) stays in `data/`,
which is gitignored.

## Known limitations

- DRM'd books (Kindle, Adobe ADEPT, Apple) will not open — DRM-free EPUB only.
- Chapter detection is heuristic; fix misfires with the chapter toggles +
  re-render.
- Kokoro has a fixed voice list; no cloning.
- If audio sits paused >~30s on iOS, the lock-screen play button may stop
  responding until the page is foregrounded (WebKit issue).
- Uploading books you don't own may violate copyright — this is a personal
  tool for your own library.
