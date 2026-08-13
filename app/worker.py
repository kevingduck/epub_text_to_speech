"""Pipeline orchestrator.

Two entry points, same pipeline:
  CLI (P1):     python -m app.worker book.epub -o out.mp3 [--voice V] [--workers N]
  Server job:   python -m app.worker --book-id ID [--data-dir data]

The server spawns the job form as a subprocess; the worker owns all torch/
kokoro imports so the API process stays small and a crashed job can't take
the server down.
"""
import argparse
import signal
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from .encode import encode_book
from .epub_parse import parse
from .normalize import normalize, segment
from .synth import DEFAULT_WORKERS, synthesize_chapters


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_chapter_segments(chapters) -> dict[int, list[tuple[str, bool]]]:
    """Normalize + segment each included chapter. Prepends the chapter title
    when the text doesn't already start with it (title came from the TOC)."""
    out = {}
    for ch in chapters:
        if not ch.included:
            continue
        text = ch.text
        if ch.title and ch.title.lower() not in text[:200].lower():
            text = ch.title + ".\n\n" + text
        segs = segment(normalize(text))
        if segs:
            out[ch.idx] = segs
    return out


def run_pipeline(
    epub_path: Path,
    out_mp3: Path,
    workdir: Path,
    voice: str = "af_heart",
    workers: int = DEFAULT_WORKERS,
    included_override: dict[int, bool] | None = None,
    progress_cb=None,
    log=lambda msg: print(msg, flush=True),
):
    """Parse -> normalize -> synthesize -> encode. Returns (parsed, duration_ms,
    size_bytes, {idx: (start_ms, dur_ms)})."""
    log(f"Parsing {epub_path.name} ...")
    parsed = parse(epub_path)
    if included_override:
        for ch in parsed.chapters:
            if ch.idx in included_override:
                ch.included = included_override[ch.idx]

    included = [ch for ch in parsed.chapters if ch.included]
    if not included:
        raise RuntimeError("No chapters left after exclusion heuristics")
    total_chars = sum(ch.chars for ch in included)
    log(f"{parsed.title} — {len(parsed.chapters)} spine items, "
        f"{len(included)} included, {total_chars:,} chars")

    chapter_segments = build_chapter_segments(parsed.chapters)
    log(f"Synthesizing {len(chapter_segments)} chapters "
        f"(voice={voice}, workers={workers}) ...")
    wav_paths = synthesize_chapters(chapter_segments, voice, workdir,
                                    workers=workers, progress_cb=progress_cb)

    log("Encoding MP3 ...")
    duration_ms, size_bytes, chapter_offsets = encode_book(
        sorted(wav_paths.items()), out_mp3
    )
    log(f"Done: {out_mp3} ({size_bytes / 1e6:.1f} MB, {_fmt_ms(duration_ms)})")
    return parsed, duration_ms, size_bytes, chapter_offsets


# --- server job mode -------------------------------------------------------

def run_job(book_id: str, data_dir: Path) -> None:
    conn = dbmod.connect(data_dir / "library.db")

    def set_status(status: str, **cols) -> None:
        sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in cols])
        conn.execute(f"UPDATE books SET {sets} WHERE id = ?",
                     [status, *cols.values(), book_id])
        conn.commit()

    def on_progress(frac: float) -> None:
        conn.execute("UPDATE books SET progress = ? WHERE id = ?",
                     (0.02 + 0.88 * frac, book_id))
        conn.commit()

    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        sys.exit(f"unknown book id {book_id}")

    epub_path = data_dir / "uploads" / f"{book_id}.epub"
    workdir = data_dir / "work" / book_id
    out_mp3 = data_dir / "out" / f"{book_id}.mp3"

    try:
        set_status("parsing", progress=0.0, error=None)
        parsed = parse(epub_path)

        # Rerender keeps the user's chapter selection; fresh books get heuristics.
        existing = conn.execute(
            "SELECT idx, included FROM chapters WHERE book_id = ? ORDER BY idx",
            (book_id,),
        ).fetchall()
        if len(existing) == len(parsed.chapters):
            for r in existing:
                parsed.chapters[r["idx"]].included = bool(r["included"])
        conn.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
        conn.executemany(
            "INSERT INTO chapters (book_id, idx, title, included, chars) "
            "VALUES (?, ?, ?, ?, ?)",
            [(book_id, ch.idx, ch.title, int(ch.included), ch.chars)
             for ch in parsed.chapters],
        )
        conn.execute(
            "UPDATE books SET title = ?, author = ? WHERE id = ?",
            (parsed.title, parsed.author, book_id),
        )
        conn.commit()

        if parsed.cover_jpeg:
            cover_path = data_dir / "covers" / f"{book_id}.jpg"
            cover_path.parent.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(parsed.cover_jpeg)

        if not any(ch.included for ch in parsed.chapters):
            raise RuntimeError("No chapters selected for synthesis")

        set_status("synthesizing", progress=0.02)
        chapter_segments = build_chapter_segments(parsed.chapters)
        wav_paths = synthesize_chapters(
            chapter_segments, row["voice"], workdir, progress_cb=on_progress
        )

        set_status("encoding", progress=0.92)
        duration_ms, size_bytes, offsets = encode_book(
            sorted(wav_paths.items()), out_mp3
        )
        conn.executemany(
            "UPDATE chapters SET start_ms = ?, dur_ms = ? WHERE book_id = ? AND idx = ?",
            [(start, dur, book_id, idx)
             for idx, (start, dur) in offsets.items()],
        )
        set_status("done", progress=1.0, duration_ms=duration_ms,
                   size_bytes=size_bytes, completed_at=_now())

        for p in workdir.glob("*"):
            p.unlink()
        workdir.rmdir()
    except Exception as exc:
        traceback.print_exc()
        set_status("failed", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
    finally:
        conn.close()


# --- CLI mode ---------------------------------------------------------------

def run_cli(args) -> None:
    epub_path = Path(args.epub)
    out_mp3 = Path(args.output or epub_path.with_suffix(".mp3").name)
    workdir = Path(args.workdir or f"data/work/_cli_{epub_path.stem}")

    if args.dry_run:
        parsed = parse(epub_path)
        print(f"{parsed.title} — {parsed.author or 'unknown author'}")
        print(f"cover: {'yes' if parsed.cover_jpeg else 'no'}\n")
        for ch in parsed.chapters:
            mark = "+" if ch.included else "-"
            print(f" {mark} [{ch.idx:3d}] {ch.chars:>8,} chars  {ch.title}")
        print("\n(+ = will be synthesized, - = auto-excluded)")
        return

    override = None
    if args.all:
        parsed = parse(epub_path)
        override = {ch.idx: ch.chars > 0 for ch in parsed.chapters}

    last = -1.0

    def progress(frac):
        nonlocal last
        if frac - last >= 0.01:
            last = frac
            print(f"\r  synthesis {frac * 100:5.1f}%", end="", flush=True)

    parsed, duration_ms, _size, chapter_offsets = run_pipeline(
        epub_path, out_mp3, workdir,
        voice=args.voice, workers=args.workers,
        included_override=override, progress_cb=progress,
    )
    print()
    for idx, (start, _dur) in sorted(chapter_offsets.items()):
        print(f"  {_fmt_ms(start)}  {parsed.chapters[idx].title}")
    for p in workdir.glob("*"):
        p.unlink()
    workdir.rmdir()


def main() -> None:
    # SIGTERM (server cancelling this job) must raise SystemExit so the
    # multiprocessing.Pool context manager tears down its child processes;
    # the default handler would exit without cleanup and orphan them.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    ap = argparse.ArgumentParser(description="EPUB -> audiobook MP3")
    ap.add_argument("epub", nargs="?", help="EPUB file (CLI mode)")
    ap.add_argument("-o", "--output", help="output MP3 path")
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--workdir", help="scratch dir for chapter WAVs")
    ap.add_argument("--all", action="store_true",
                    help="include every non-empty spine item")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse only; print the chapter table")
    ap.add_argument("--book-id", help="server job mode: process this library book")
    ap.add_argument("--data-dir", default="data", type=Path)
    args = ap.parse_args()

    if args.book_id:
        run_job(args.book_id, args.data_dir)
    elif args.epub:
        run_cli(args)
    else:
        ap.error("provide an EPUB path or --book-id")


if __name__ == "__main__":
    main()
