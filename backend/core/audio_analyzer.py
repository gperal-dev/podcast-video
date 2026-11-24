"""Audio duration, lightweight RMS energy curves, and silence detection via FFmpeg/FFprobe."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from backend.config import Settings, get_settings


_SILENCE_START_RE = re.compile(r"silence_start:\s*([-+]?\d*\.?\d+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([-+]?\d*\.?\d+)")


def get_audio_duration(audio_path: Path, settings: Optional[Settings] = None) -> float:
    """Return media duration in seconds using ffprobe."""
    cfg = settings or get_settings()
    if not cfg.ffprobe_path or not cfg.ffprobe_path.exists():
        raise RuntimeError("FFprobe no encontrado en el sistema.")

    cmd = [
        str(cfg.ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Error ejecutando ffprobe sobre {audio_path}: {proc.stderr.strip()}")
    try:
        return max(0.0, float(proc.stdout.strip()))
    except ValueError as exc:
        raise RuntimeError(f"No se pudo leer la duración de {audio_path}: {proc.stdout!r}") from exc


def compute_rms_energy_blocks(
    audio_path: Path,
    block_sec: float = 300.0,
    settings: Optional[Settings] = None,
) -> list[dict]:
    """Stream mono 8kHz PCM via FFmpeg to compute per-block RMS dB and energy class."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

    sample_rate = 8000
    samples_per_block = max(1, int(block_sec * sample_rate))
    bytes_per_block = samples_per_block * 2  # int16 = 2 bytes

    cmd = [
        str(cfg.ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    raw_blocks: list[dict] = []
    block_idx = 0

    try:
        assert proc.stdout is not None
        while True:
            buf = bytearray()
            while len(buf) < bytes_per_block:
                chunk = proc.stdout.read(bytes_per_block - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
            if not buf:
                break

            usable_len = len(buf) - (len(buf) % 2)
            if usable_len == 0:
                break
            samples = np.frombuffer(buf[:usable_len], dtype=np.int16).astype(np.float32)
            dur = len(samples) / float(sample_rate)
            rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) > 0 else 0.0
            rms_db = float(20.0 * np.log10(max(rms, 1e-6) / 32768.0))
            start_t = block_idx * block_sec
            raw_blocks.append(
                {
                    "block_index": block_idx,
                    "start": round(start_t, 3),
                    "end": round(start_t + dur, 3),
                    "rms_db": round(rms_db, 2),
                    "energy": "media",
                }
            )
            block_idx += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
        ret = proc.wait()
        if ret != 0 and not raw_blocks:
            raise RuntimeError(f"FFmpeg falló analizando RMS: {stderr_out.strip()}")

    if raw_blocks:
        db_vals = [b["rms_db"] for b in raw_blocks]
        if len(db_vals) >= 3:
            low_th = float(np.percentile(db_vals, 33))
            high_th = float(np.percentile(db_vals, 67))
            for b in raw_blocks:
                if b["rms_db"] >= high_th and high_th > low_th + 0.3:
                    b["energy"] = "alta"
                elif b["rms_db"] <= low_th and high_th > low_th + 0.3:
                    b["energy"] = "baja"
                else:
                    b["energy"] = "media"
    return raw_blocks


def detect_silences(
    audio_path: Path,
    threshold_db: float = -38.0,
    min_duration: float = 0.7,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
    settings: Optional[Settings] = None,
) -> list[tuple[float, float]]:
    """Detect silent intervals `(start, end)` in seconds using FFmpeg silencedetect."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

    cmd = [str(cfg.ffmpeg_path), "-hide_banner", "-nostats"]
    offset = max(0.0, float(start_sec or 0.0))
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{offset:.3f}"])
    if end_sec is not None and end_sec > offset:
        cmd.extend(["-t", f"{end_sec - offset:.3f}"])

    cmd.extend(
        [
            "-i",
            str(audio_path),
            "-vn",
            "-af",
            f"silencedetect=noise={threshold_db:.1f}dB:d={min_duration:.3f}",
            "-f",
            "null",
            "-",
        ]
    )

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 and "silencedetect" not in proc.stderr:
        raise RuntimeError(f"FFmpeg silencedetect falló: {proc.stderr.strip()[:300]}")

    intervals: list[tuple[float, float]] = []
    pending_start: Optional[float] = None

    for line in proc.stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            pending_start = float(m_start.group(1)) + offset
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end and pending_start is not None:
            s_end = float(m_end.group(1)) + offset
            if s_end > pending_start:
                intervals.append((round(pending_start, 4), round(s_end, 4)))
            pending_start = None

    if pending_start is not None:
        clip_limit = end_sec if end_sec is not None else get_audio_duration(audio_path, cfg)
        if clip_limit > pending_start + 0.05:
            intervals.append((round(pending_start, 4), round(float(clip_limit), 4)))

    return intervals


def _merge_time_ranges(
    intervals: Iterable[tuple[float, float]],
    gap_tolerance: float = 0.05,
) -> list[tuple[float, float]]:
    ordered = sorted((float(s), float(e)) for s, e in intervals if float(e) > float(s))
    if not ordered:
        return []
    merged: list[tuple[float, float]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s <= pe + gap_tolerance:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def merge_non_speech_with_silences(
    silences: list[tuple[float, float]],
    non_speech: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return the unified sorted union of acoustic silences and VAD non-speech/music intervals."""
    combined = [*silences, *non_speech]
    return [
        (round(s, 4), round(e, 4))
        for s, e in _merge_time_ranges(combined, gap_tolerance=0.04)
    ]


def detect_non_speech_intervals(
    audio_path: Path,
    min_duration: float = 0.55,
    vad_threshold: float = 0.45,
    music_rms_threshold_db: float = -36.0,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    settings: Optional[Settings] = None,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Detect all non-speech intervals (silences AND background music interludes) using Silero VAD.

    Returns `(non_speech_intervals, music_intervals)`.
    """
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if progress_cb:
        progress_cb(0.0, "Decodificando audio a 16kHz para detector neuronal Silero VAD...")

    sr = 16000
    cmd = [
        str(cfg.ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sr),
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if proc.returncode != 0 or not proc.stdout:
        stderr_txt = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise RuntimeError(f"FFmpeg falló decodificando audio para VAD: {stderr_txt.strip()[:250]}")

    usable = len(proc.stdout) - (len(proc.stdout) % 4)
    if usable <= 0:
        return [], []
    samples = np.frombuffer(proc.stdout[:usable], dtype=np.float32)
    total_samples = len(samples)
    total_dur = total_samples / float(sr)
    if total_dur < 0.2:
        return [], []

    opts = VadOptions(
        threshold=vad_threshold,
        min_speech_duration_ms=150,
        min_silence_duration_ms=380,
        speech_pad_ms=90,
    )

    # Process in 15-minute chunks with 2s overlap to avoid cutting words at window boundaries
    chunk_samples = 900 * sr
    overlap_samples = 2 * sr
    step_samples = max(sr, chunk_samples - overlap_samples)

    raw_speech: list[tuple[float, float]] = []
    starts = list(range(0, total_samples, step_samples))
    total_chunks = max(1, len(starts))

    for idx_c, i0 in enumerate(starts):
        i1 = min(total_samples, i0 + chunk_samples)
        win_sec = i0 / float(sr)
        chunk_arr = samples[i0:i1]
        if len(chunk_arr) < int(0.15 * sr):
            continue
        chunks = get_speech_timestamps(chunk_arr, vad_options=opts, sampling_rate=sr)
        for ch in chunks:
            s_t = max(0.0, win_sec + float(ch["start"]) / float(sr))
            e_t = min(total_dur, win_sec + float(ch["end"]) / float(sr))
            if e_t > s_t:
                raw_speech.append((s_t, e_t))
        if progress_cb:
            pct = ((idx_c + 1) / total_chunks) * 100.0
            progress_cb(pct, f"Silero VAD analizando voz vs música ({idx_c + 1}/{total_chunks})...")

    speech_merged = _merge_time_ranges(raw_speech, gap_tolerance=0.10)

    non_speech: list[tuple[float, float]] = []
    music_intervals: list[tuple[float, float]] = []
    cursor = 0.0

    def _check_and_add(ns_s: float, ns_e: float) -> None:
        if ns_e - ns_s < min_duration:
            return
        i0 = max(0, int(round(ns_s * sr)))
        i1 = min(total_samples, int(round(ns_e * sr)))
        if i1 <= i0:
            return
        seg = samples[i0:i1]
        rms = float(np.sqrt(np.mean(seg * seg) + 1e-12))
        rms_db = float(20.0 * np.log10(max(rms, 1e-6)))
        pair = (round(ns_s, 4), round(ns_e, 4))
        non_speech.append(pair)
        if rms_db >= music_rms_threshold_db:
            music_intervals.append(pair)

    for sp_s, sp_e in speech_merged:
        if sp_s > cursor:
            _check_and_add(cursor, sp_s)
        cursor = max(cursor, sp_e)
    if total_dur > cursor:
        _check_and_add(cursor, total_dur)

    return non_speech, music_intervals
