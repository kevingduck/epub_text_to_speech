"""FastAPI server: upload, library, playback, positions.

Jobs run as subprocesses (python -m app.worker --book-id ID), one at a time,
so torch never loads in this process and a crashed job can't take the API
down. WAL mode lets the worker write progress while we read it.
"""
import asyncio
import shutil
import sys
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db as dbmod

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
MAX_UPLOAD = 100 * 1024 * 1024

VOICES = [
    {"id": "af_heart", "name": "Heart (US female)"},
    {"id": "af_bella", "name": "Bella (US female)"},
    {"id": "af_nicole", "name": "Nicole (US female, soft)"},
    {"id": "af_sarah", "name": "Sarah (US female)"},
    {"id": "af_sky", "name": "Sky (US female)"},
    {"id": "am_adam", "name": "Adam (US male)"},
    {"id": "am_michael", "name": "Michael (US male)"},
    {"id": "am_fenrir", "name": "Fenrir (US male)"},
    {"id": "bf_emma", "name": "Emma (UK female)"},
    {"id": "bf_isabella", "name": "Isabella (UK female)"},
    {"id": "bm_george", "name": "George (UK male)"},
    {"id": "bm_lewis", "name": "Lewis (UK male)"},
]
VOICE_IDS = {v["id"] for v in VOICES}

ACTIVE_STATUSES = ("queued", "parsing", "synthesizing", "encoding")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    return dbmod.connect(DATA_DIR / "library.db")


# --- job queue --------------------------------------------------------------

class JobRunner:
    """Serial job queue; each job is a worker subprocess."""

    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.current_id: str | None = None
        self.proc: asyncio.subprocess.Process | None = None

    def enqueue(self, book_id: str) -> None:
        self.queue.put_nowait(book_id)

    async def cancel(self, book_id: str) -> None:
        if self.current_id == book_id and self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def run_forever(self) -> None:
        while True:
            book_id = await self.queue.get()
            conn = db()
            try:
                row = conn.execute(
                    "SELECT status FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None or row["status"] not in ACTIVE_STATUSES:
                continue  # deleted or already finished

            self.current_id = book_id
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.worker",
                "--book-id", book_id, "--data-dir", str(DATA_DIR),
                cwd=str(ROOT),
            )
            await self.proc.wait()
            self.current_id = None
            self.proc = None
            # A worker that died uncleanly (kill -9, OOM) never wrote a
            # terminal status; don't leave the book stuck in "synthesizing".
            conn = db()
            try:
                row = conn.execute(
                    "SELECT status FROM books WHERE id = ?", (book_id,)
                ).fetchone()
                if row and row["status"] in ACTIVE_STATUSES:
                    conn.execute(
                        "UPDATE books SET status = 'failed', error = ? WHERE id = ?",
                        ("worker exited unexpectedly; use re-render to resume", book_id),
                    )
                    conn.commit()
            finally:
                conn.close()


runner = JobRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    for sub in ("uploads", "work", "out", "covers"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    # Requeue jobs orphaned by a server restart; chapter WAVs make this cheap.
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id FROM books WHERE status IN (?, ?, ?, ?) ORDER BY created_at",
            ACTIVE_STATUSES,
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE books SET status = 'queued' WHERE id = ?", (r["id"],))
            runner.enqueue(r["id"])
        conn.commit()
    finally:
        conn.close()
    task = asyncio.create_task(runner.run_forever())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


# --- books -------------------------------------------------------------------

def _book_dict(row, chapters=None) -> dict:
    d = dict(row)
    if chapters is not None:
        d["chapters"] = [dict(c) for c in chapters]
    return d


@app.post("/api/books")
async def upload_book(file: UploadFile, voice: str = "af_heart"):
    if voice not in VOICE_IDS:
        raise HTTPException(400, f"unknown voice {voice!r}")

    book_id = uuid.uuid4().hex
    dest = DATA_DIR / "uploads" / f"{book_id}.epub"
    size = 0
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                dest.unlink()
                raise HTTPException(413, "file exceeds 100MB")
            f.write(chunk)

    try:
        with zipfile.ZipFile(dest) as z:
            names = z.namelist()
            if "META-INF/container.xml" not in names:
                raise ValueError("no container.xml")
    except (zipfile.BadZipFile, ValueError):
        dest.unlink()
        raise HTTPException(400, "not a valid EPUB (DRM-free .epub required)")

    conn = db()
    try:
        conn.execute(
            "INSERT INTO books (id, title, author, voice, status, created_at) "
            "VALUES (?, ?, NULL, ?, 'queued', ?)",
            (book_id, file.filename or "Untitled", voice, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    runner.enqueue(book_id)
    return {"id": book_id}


@app.get("/api/books")
def list_books():
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, title, author, voice, status, progress, error, "
            "duration_ms, size_bytes, created_at FROM books ORDER BY created_at DESC"
        ).fetchall()
        return [_book_dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/books/{book_id}")
def get_book(book_id: str):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        chapters = conn.execute(
            "SELECT idx, title, included, chars, start_ms, dur_ms "
            "FROM chapters WHERE book_id = ? ORDER BY idx",
            (book_id,),
        ).fetchall()
        return _book_dict(row, chapters)
    finally:
        conn.close()


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
            raise HTTPException(404)
        await runner.cancel(book_id)
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM positions WHERE book_id = ?", (book_id,))
        conn.commit()
    finally:
        conn.close()
    for p in [DATA_DIR / "uploads" / f"{book_id}.epub",
              DATA_DIR / "out" / f"{book_id}.mp3",
              DATA_DIR / "covers" / f"{book_id}.jpg"]:
        p.unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR / "work" / book_id, ignore_errors=True)
    return {"ok": True}


@app.post("/api/books/{book_id}/rerender")
async def rerender(book_id: str, request: Request):
    body = await request.json() if await request.body() else {}
    voice = body.get("voice")
    chapters = body.get("chapters")  # optional [{idx, included}]
    if voice is not None and voice not in VOICE_IDS:
        raise HTTPException(400, f"unknown voice {voice!r}")

    conn = db()
    try:
        row = conn.execute(
            "SELECT status, voice FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        if row["status"] in ACTIVE_STATUSES:
            raise HTTPException(409, "a job is already running for this book")

        # Cached chapter WAVs stay valid unless the voice changes, so a
        # re-render after a crash resumes instead of restarting. A chapter
        # toggled back ON simply has no WAV yet and gets synthesized.
        invalidate = voice is not None and voice != row["voice"]
        if voice:
            conn.execute("UPDATE books SET voice = ? WHERE id = ?", (voice, book_id))
        if chapters:
            conn.executemany(
                "UPDATE chapters SET included = ? WHERE book_id = ? AND idx = ?",
                [(int(bool(c["included"])), book_id, int(c["idx"])) for c in chapters],
            )
        conn.execute(
            "UPDATE books SET status = 'queued', progress = 0, error = NULL, "
            "completed_at = NULL WHERE id = ?",
            (book_id,),
        )
        conn.commit()
    finally:
        conn.close()
    if invalidate:
        shutil.rmtree(DATA_DIR / "work" / book_id, ignore_errors=True)
    runner.enqueue(book_id)
    return {"ok": True}


# --- media -------------------------------------------------------------------

@app.get("/api/books/{book_id}/audio")
def get_audio(book_id: str):
    path = DATA_DIR / "out" / f"{book_id}.mp3"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg", filename=f"{book_id}.mp3")


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: str):
    path = DATA_DIR / "covers" / f"{book_id}.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


# --- positions ----------------------------------------------------------------

@app.post("/api/books/{book_id}/position")
async def save_position(book_id: str, request: Request):
    body = await request.json()
    pos = int(body["position_ms"])
    conn = db()
    try:
        conn.execute(
            "INSERT INTO positions (book_id, position_ms, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(book_id) DO UPDATE SET position_ms = ?, updated_at = ?",
            (book_id, pos, _now(), pos, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/books/{book_id}/position")
def get_position(book_id: str):
    conn = db()
    try:
        row = conn.execute(
            "SELECT position_ms FROM positions WHERE book_id = ?", (book_id,)
        ).fetchone()
        return {"position_ms": row["position_ms"] if row else 0}
    finally:
        conn.close()


@app.get("/api/voices")
def get_voices():
    return VOICES


# --- static frontend -----------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
