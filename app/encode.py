"""Concatenate chapter WAVs and encode one MP3 with ffmpeg."""
import subprocess
from pathlib import Path

import soundfile as sf


def encode_book(
    chapter_parts: list[tuple[int, list[Path]]], out_mp3: Path
) -> tuple[int, int, dict[int, tuple[int, int]]]:
    """chapter_parts: [(chapter_idx, [part WAVs in order])] in playback order.
    Returns (duration_ms, size_bytes, {chapter_idx: (start_ms, dur_ms)})."""
    offsets: dict[int, tuple[int, int]] = {}
    wav_paths: list[Path] = []
    cursor = 0
    for idx, paths in chapter_parts:
        start = cursor
        for p in paths:
            info = sf.info(str(p))
            cursor += round(info.frames / info.samplerate * 1000)
            wav_paths.append(p)
        offsets[idx] = (start, cursor - start)

    workdir = wav_paths[0].parent
    concat_file = workdir / "concat.txt"
    lines = []
    for p in wav_paths:
        escaped = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n")

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    partial = out_mp3.with_name(out_mp3.stem + ".partial.mp3")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1",
        "-write_xing", "1",
        str(partial),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        tail = proc.stderr.strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))
    partial.replace(out_mp3)

    return cursor, out_mp3.stat().st_size, offsets
