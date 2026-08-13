"""Kokoro synthesis: pool of worker processes, one chapter *part* per task.

Long chapters are split into ~8k-char parts at segment boundaries so a
single monster chapter (some books put 150k+ chars in one spine item) can't
tail-bind the whole render to one worker. Concatenating parts is seamless:
every segment already carries its trailing pause, and the chapter tail pause
goes only on the final part.

Each worker process loads KPipeline once (in the pool initializer) and caps
its torch thread count so N workers don't fight over cores. Part WAVs are
written atomically (tmp + rename), so a WAV that exists is complete and a
killed job resumes instead of restarting.
"""
import multiprocessing as mp
import os
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 24000
PAUSE_SENTENCE = 0.15   # after an intra-paragraph split
PAUSE_PARAGRAPH = 0.35  # after a paragraph
PAUSE_CHAPTER = 0.7     # chapter tail

PART_TARGET_CHARS = 8000


def _resolve_device() -> str:
    pref = os.environ.get("AUDIOBOOK_DEVICE", "auto")
    if pref in ("mps", "cpu"):
        return pref
    import platform
    import sys

    return "mps" if sys.platform == "darwin" and platform.machine() == "arm64" else "cpu"


DEVICE = _resolve_device()
# Measured on M2 Ultra: 6 parallel MPS streams ≈ 74x realtime aggregate and
# still leave GPU headroom; per-stream throughput drops past that. On CPU,
# workers are bound by physical cores instead.
_default = 6 if DEVICE == "mps" else min(8, max(2, (os.cpu_count() or 8) // 3))
DEFAULT_WORKERS = int(os.environ.get("AUDIOBOOK_WORKERS", str(_default)))
TORCH_THREADS = int(os.environ.get(
    "AUDIOBOOK_TORCH_THREADS", "1" if DEVICE == "mps" else "2"))

_pipeline = None


def _init_worker(threads: int, device: str) -> None:
    global _pipeline
    # Must be set before torch dispatches any op; harmless on CPU.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch

    torch.set_num_threads(threads)
    from kokoro import KPipeline

    _pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=device)


def _synth_one(args) -> tuple[int, int]:
    idx, part, segments, voice, out_path, is_last_part = args
    parts: list[np.ndarray] = []
    for text, is_para_end in segments:
        for result in _pipeline(text, voice=voice):
            audio = result.audio
            if audio is None or len(audio) == 0:
                continue
            parts.append(np.asarray(audio, dtype=np.float32))
        pause = PAUSE_PARAGRAPH if is_para_end else PAUSE_SENTENCE
        parts.append(np.zeros(int(SR * pause), dtype=np.float32))
    if is_last_part:
        parts.append(np.zeros(int(SR * PAUSE_CHAPTER), dtype=np.float32))
    audio = np.concatenate(parts)

    tmp = out_path.with_suffix(".wav.tmp")
    sf.write(str(tmp), audio, SR, format="WAV", subtype="PCM_16")
    os.replace(tmp, out_path)
    return idx, part


def split_parts(
    segments: list[tuple[str, bool]], target: int = PART_TARGET_CHARS
) -> list[list[tuple[str, bool]]]:
    parts, cur, size = [], [], 0
    for seg in segments:
        cur.append(seg)
        size += len(seg[0])
        if size >= target:
            parts.append(cur)
            cur, size = [], 0
    if cur:
        parts.append(cur)
    return parts


def synthesize_chapters(
    chapter_segments: dict[int, list[tuple[str, bool]]],
    voice: str,
    workdir: Path,
    workers: int = DEFAULT_WORKERS,
    progress_cb=None,
) -> dict[int, list[Path]]:
    """Synthesize to workdir/chapter_NNN_pMMM.wav, one task per part. Resumes
    from existing WAVs. Returns {chapter_idx: [part paths in order]}.
    progress_cb gets a 0..1 float, weighted by character count."""
    workdir.mkdir(parents=True, exist_ok=True)

    out_paths: dict[int, list[Path]] = {}
    weights: dict[tuple[int, int], int] = {}
    todo = []
    done = 0

    for idx, segs in sorted(chapter_segments.items()):
        parts = split_parts(segs)
        out_paths[idx] = []
        for p, part_segs in enumerate(parts):
            path = workdir / f"chapter_{idx:03d}_p{p:03d}.wav"
            out_paths[idx].append(path)
            weight = max(1, sum(len(t) for t, _ in part_segs))
            weights[(idx, p)] = weight
            if path.exists():
                done += weight
            else:
                todo.append((idx, p, part_segs, voice, path, p == len(parts) - 1))

    total = sum(weights.values())
    if progress_cb and total:
        progress_cb(done / total)
    if not todo:
        return out_paths

    workers = max(1, min(workers, len(todo)))
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers, initializer=_init_worker,
                  initargs=(TORCH_THREADS, DEVICE)) as pool:
        for idx, part in pool.imap_unordered(_synth_one, todo):
            done += weights[(idx, part)]
            if progress_cb and total:
                progress_cb(done / total)
    return out_paths
