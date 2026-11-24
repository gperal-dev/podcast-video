"""100% local multi-speaker acoustic diarization (unlimited speakers) using NumPy + SciPy."""

from __future__ import annotations

import colorsys
import subprocess
from pathlib import Path
from typing import Any, Optional

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.fft import dct
from scipy.spatial.distance import pdist, squareform

from backend.config import Settings, get_settings
from backend.models.job import SentenceSegment, WordTimestamp


def _load_mono_16k_float32(audio_path: Path, settings: Optional[Settings] = None) -> np.ndarray:
    """Decode any audio file to 16kHz mono float32 waveform via FFmpeg."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

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
        "16000",
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"Error decodificando audio para diarización: {proc.stderr.decode('utf-8', 'replace')}")
    usable = len(proc.stdout) - (len(proc.stdout) % 4)
    return np.frombuffer(proc.stdout[:usable], dtype=np.float32)


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(sr: int = 16000, n_fft: int = 512, n_mels: int = 26) -> np.ndarray:
    low_mel = _hz_to_mel(np.array([80.0]))[0]
    high_mel = _hz_to_mel(np.array([7600.0]))[0]
    mel_points = np.linspace(low_mel, high_mel, n_mels + 2)
    hz_points = _mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sr).astype(int)

    fbank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        for k in range(left, min(center, fbank.shape[1])):
            fbank[m - 1, k] = (k - left) / float(center - left)
        for k in range(center, min(right, fbank.shape[1])):
            fbank[m - 1, k] = (right - k) / float(right - center)
    return fbank


_MEL_FBANK = _mel_filterbank(16000, 512, 26)


def _extract_speaker_embedding(chunk: np.ndarray, sr: int = 16000) -> Optional[np.ndarray]:
    """Compute a 43-dim acoustic embedding (MFCC mean/std + pitch F0 + spectral shape) for active speech."""
    if len(chunk) < int(0.45 * sr):
        return None

    # Pre-emphasis
    emphasized = np.append(chunk[0], chunk[1:] - 0.97 * chunk[:-1]).astype(np.float32)
    win_len = 400  # 25ms
    hop_len = 160  # 10ms
    if len(emphasized) < win_len:
        return None

    num_frames = 1 + (len(emphasized) - win_len) // hop_len
    idx = np.arange(win_len)[None, :] + np.arange(num_frames)[:, None] * hop_len
    frames = emphasized[idx] * np.hamming(win_len)[None, :].astype(np.float32)

    # Energy VAD mask inside segment to exclude pauses
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    peak_rms = float(np.percentile(frame_rms, 90))
    if peak_rms < 1e-4:
        return None
    active_mask = frame_rms >= max(1e-4, peak_rms * 0.16)
    active_frames = frames[active_mask]
    if len(active_frames) < 12:
        return None

    # Power spectrum & MFCCs
    spec = np.abs(np.fft.rfft(active_frames, n=512)) ** 2
    power = spec / 512.0
    mel_e = np.dot(power, _MEL_FBANK.T) + 1e-10
    log_mel = np.log(mel_e)
    mfccs = dct(log_mel, type=2, axis=1, norm="ortho")[:, 1:21]  # C1..C20 (drop C0 loudness)

    mfcc_mean = np.mean(mfccs, axis=0)
    mfcc_std = np.std(mfccs, axis=0) * 0.45

    # Spectral centroid (normalized 0..1)
    freqs = np.fft.rfftfreq(512, 1.0 / sr)
    spec_mag = np.sqrt(spec + 1e-10)
    centroid = np.sum(spec_mag * freqs[None, :], axis=1) / (np.sum(spec_mag, axis=1) + 1e-9)
    centroid_feat = float(np.median(centroid) / 4000.0)

    # Voiced fundamental frequency (F0) via autocorrelation across active raw frames
    raw_active = chunk[idx][active_mask]
    min_lag = sr // 340  # ~340 Hz
    max_lag = sr // 68   # ~68 Hz
    f0_vals: list[float] = []
    for fr in raw_active[::2]:
        fr_z = fr - np.mean(fr)
        norm0 = float(np.dot(fr_z, fr_z))
        if norm0 < 1e-7:
            continue
        ac = np.correlate(fr_z, fr_z, mode="full")[len(fr_z) - 1 :]
        seg_ac = ac[min_lag : min(max_lag, len(ac))]
        if len(seg_ac) == 0:
            continue
        best_i = int(np.argmax(seg_ac))
        if seg_ac[best_i] / norm0 >= 0.35:
            f0_vals.append(float(sr) / float(min_lag + best_i))

    if f0_vals:
        log_f0 = np.log2(np.array(f0_vals) / 150.0)
        f0_med = float(np.median(log_f0)) * 2.2
        f0_iqr = float(np.percentile(log_f0, 75) - np.percentile(log_f0, 25)) * 0.6
    else:
        f0_med = 0.0
        f0_iqr = 0.0

    vec = np.concatenate([mfcc_mean, mfcc_std, np.array([f0_med, f0_iqr, centroid_feat], dtype=np.float32)])
    return vec.astype(np.float32)


def _cluster_embeddings_auto(
    embeddings: list[np.ndarray],
    durations: list[float],
    max_speakers: int = 16,
) -> list[int]:
    """Cluster utterance embeddings into 1..N distinct speakers without over-splitting 1 voice."""
    n = len(embeddings)
    if n <= 1:
        return [0] * n

    mat = np.vstack(embeddings)
    # Standardize feature scales across the episode/clip, clipping extreme outliers
    stds = np.std(mat, axis=0)
    stds = np.where(stds < 1e-4, 1.0, stds)
    # Emphasize pitch & lower MFCCs for timbre discrimination while preventing noise dominance
    scaled = (mat - np.mean(mat, axis=0)) / stds
    norms = np.linalg.norm(scaled, axis=1, keepdims=True)
    norms = np.where(norms < 1e-6, 1.0, norms)
    norm_mat = scaled / norms

    pairwise = pdist(norm_mat, metric="cosine")
    pairwise = np.nan_to_num(pairwise, nan=0.0, posinf=1.0, neginf=0.0)
    dist_square = squareform(pairwise)

    if float(np.percentile(pairwise, 90)) < 0.32:
        return [0] * n

    Z = linkage(pairwise, method="average")
    upper_k = min(max_speakers, n)

    best_k = 1
    best_score = -1.0

    for k in range(2, upper_k + 1):
        labels = fcluster(Z, t=k, criterion="maxclust") - 1
        counts = np.bincount(labels, minlength=k)
        # Every speaker must represent a real presence (avoid tiny outlier clusters)
        min_items = 2 if n >= 4 else 1
        if np.min(counts) < min_items:
            continue

        # Check cluster speech durations
        cluster_durs = [sum(durations[i] for i in range(n) if labels[i] == c) for c in range(k)]
        if min(cluster_durs) < (4.5 if sum(durations) > 90.0 else 1.4):
            continue

        # Compute raw unscaled centroids to verify genuine vocal tract + F0 separation
        raw_centroids = []
        scaled_centroids = []
        for c in range(k):
            idxs = [i for i in range(n) if labels[i] == c]
            rc = np.mean(mat[idxs], axis=0)
            rc_n = rc / max(1e-6, float(np.linalg.norm(rc)))
            raw_centroids.append(rc_n)
            sc = np.mean(norm_mat[idxs], axis=0)
            sc_n = sc / max(1e-6, float(np.linalg.norm(sc)))
            scaled_centroids.append(sc_n)

        # Minimum cosine distance between closest pair of speaker centroids
        min_raw_dist = 1.0
        for a in range(k):
            for b in range(a + 1, k):
                d = 1.0 - float(np.dot(raw_centroids[a], raw_centroids[b]))
                if d < min_raw_dist:
                    min_raw_dist = d

        # Require meaningful acoustic separation in unscaled MFCC+F0 space
        if min_raw_dist < 0.055:
            continue

        # Silhouette score on normalized features
        sil_vals = []
        for i in range(n):
            ci = labels[i]
            same = [dist_square[i, j] for j in range(n) if labels[j] == ci and j != i]
            a_i = float(np.mean(same)) if same else 0.0
            b_i = min(
                float(np.mean([dist_square[i, j] for j in range(n) if labels[j] == cj]))
                for cj in range(k)
                if cj != ci
            )
            denom = max(a_i, b_i, 1e-6)
            sil_vals.append((b_i - a_i) / denom)

        sil = float(np.mean(sil_vals))
        if sil >= 0.19 and sil > best_score:
            best_score = sil
            best_k = k

    if best_k == 1:
        return [0] * n

    final_labels = (fcluster(Z, t=best_k, criterion="maxclust") - 1).tolist()

    # Order speaker IDs deterministically by chronological first appearance
    remap: dict[int, int] = {}
    ordered: list[int] = []
    for lbl in final_labels:
        if lbl not in remap:
            remap[lbl] = len(remap)
        ordered.append(remap[lbl])
    return ordered


def assign_speaker_colors(
    speakers: list[str],
    palette: list[str],
    default_highlight: str = "#FFD400",
) -> dict[str, str]:
    """Map unlimited speakers to distinct high-contrast bright subtitle highlight hex colors."""
    if not speakers:
        return {}
    mapping: dict[str, str] = {}
    base = [c for c in palette if c.startswith("#")] or [default_highlight]

    for idx, spk in enumerate(speakers):
        if idx < len(base):
            mapping[spk] = base[idx]
        else:
            # Golden-angle hue stepping in bright pastel/vivid range for unlimited speakers
            hue = ((idx * 137.508) % 360.0) / 360.0
            r, g, b = colorsys.hls_to_rgb(hue, 0.64, 0.90)
            mapping[spk] = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
    return mapping


def _diarize_segments_on_waveform(
    samples: np.ndarray,
    spans: list[tuple[float, float]],
    sr: int = 16000,
) -> list[str]:
    """Cluster arbitrary time spans on an in-memory 16kHz float32 waveform into SPEAKER_01..NN."""
    if not spans:
        return []

    embs: list[np.ndarray] = []
    durs: list[float] = []
    valid_indices: list[int] = []

    for idx, (s, e) in enumerate(spans):
        i0 = max(0, int(round(s * sr)))
        i1 = min(len(samples), int(round(e * sr)))
        emb = _extract_speaker_embedding(samples[i0:i1], sr=sr)
        if emb is not None:
            embs.append(emb)
            durs.append(max(0.1, e - s))
            valid_indices.append(idx)

    if len(embs) < 2:
        return ["SPEAKER_01"] * len(spans)

    cluster_ids = _cluster_embeddings_auto(embs, durs)
    assigned: list[Optional[int]] = [None] * len(spans)
    for pos, cid in zip(valid_indices, cluster_ids):
        assigned[pos] = cid

    # Propagate nearest valid speaker to very short segments (<0.45s)
    last_cid = cluster_ids[0]
    for i in range(len(assigned)):
        if assigned[i] is None:
            assigned[i] = last_cid
        else:
            last_cid = assigned[i]  # type: ignore[assignment]

    # Smooth isolated single-segment flip-flops inside longer turns (A B A where B < 1.2s)
    for i in range(1, len(assigned) - 1):
        if assigned[i - 1] == assigned[i + 1] and assigned[i] != assigned[i - 1]:
            if (spans[i][1] - spans[i][0]) < 1.2:
                assigned[i] = assigned[i - 1]

    # Renumber in chronological appearance order starting at SPEAKER_01
    remap: dict[int, str] = {}
    out: list[str] = []
    for cid in assigned:
        assert cid is not None
        if cid not in remap:
            remap[cid] = f"SPEAKER_{len(remap) + 1:02d}"
        out.append(remap[cid])
    return out


def diarize_draft_sentences(
    audio_path: Path,
    sentences: list[SentenceSegment],
    settings: Optional[Settings] = None,
) -> tuple[list[SentenceSegment], list[str]]:
    """Label Pass 1 draft sentences with local acoustic speaker IDs (100% offline/free)."""
    if not sentences:
        return sentences, []
    samples = _load_mono_16k_float32(audio_path, settings)
    spans = [(s.start, s.end) for s in sentences]
    labels = _diarize_segments_on_waveform(samples, spans, sr=16000)

    updated = [s.model_copy(update={"speaker": lbl}) for s, lbl in zip(sentences, labels)]
    speakers = list(dict.fromkeys(labels))
    return updated, speakers


def _out_to_orig_time(out_t: float, edl_segments: list[Any]) -> float:
    """Invert edited EDL timeline timestamp back to original audio timestamp."""
    if not edl_segments:
        return out_t
    for seg in edl_segments:
        if seg.out_start - 0.02 <= out_t <= seg.out_end + 0.02:
            offset = max(0.0, min(seg.orig_end - seg.orig_start, out_t - seg.out_start))
            return seg.orig_start + offset
    best_seg = min(
        edl_segments,
        key=lambda sg: min(abs(out_t - sg.out_start), abs(out_t - sg.out_end)),
    )
    if abs(out_t - best_seg.out_start) <= abs(out_t - best_seg.out_end):
        return best_seg.orig_start
    return best_seg.orig_end


def _smooth_word_speaker_runs(words: list[WordTimestamp], min_island_sec: float = 1.0) -> list[WordTimestamp]:
    """Eliminate isolated short A-B-A speaker flip-flops shorter than `min_island_sec`."""
    if len(words) < 3:
        return words

    runs: list[list[int]] = []
    cur: list[int] = []
    for idx, w in enumerate(words):
        sp = w.speaker or "SPEAKER_01"
        if cur and (words[cur[-1]].speaker or "SPEAKER_01") != sp:
            runs.append(cur)
            cur = []
        cur.append(idx)
    if cur:
        runs.append(cur)

    labels = [w.speaker or "SPEAKER_01" for w in words]
    for r_i in range(1, len(runs) - 1):
        prev_sp = labels[runs[r_i - 1][0]]
        cur_sp = labels[runs[r_i][0]]
        nxt_sp = labels[runs[r_i + 1][0]]
        if prev_sp == nxt_sp and cur_sp != prev_sp:
            run_dur = words[runs[r_i][-1]].end - words[runs[r_i][0]].start
            if run_dur < min_island_sec:
                for wi in runs[r_i]:
                    labels[wi] = prev_sp

    return [w.model_copy(update={"speaker": labels[i]}) for i, w in enumerate(words)]


def diarize_clip_words(
    clean_wav_path: Path,
    words: list[WordTimestamp],
    settings: Optional[Settings] = None,
    episode_speakers: Optional[list[str]] = None,
    draft_sentences: Optional[list[SentenceSegment]] = None,
    edl_segments: Optional[list[Any]] = None,
) -> tuple[list[WordTimestamp], list[str]]:
    """Assign speaker IDs to words on the clean edited clip timeline, respecting episode-level diarization."""
    if not words:
        return words, []

    # 1. Single-speaker episode detected globally -> never over-split a solo narrator on short clips
    if episode_speakers is not None and len(episode_speakers) <= 1:
        sole_speaker = episode_speakers[0] if episode_speakers else "SPEAKER_01"
        updated = [w.model_copy(update={"speaker": sole_speaker}) for w in words]
        return updated, [sole_speaker]

    # 2. Multi-speaker episode -> inherit consistent episode-level speaker identity across all clips
    if episode_speakers is not None and len(episode_speakers) >= 2 and draft_sentences and edl_segments:
        assigned_words: list[WordTimestamp] = []
        for w in words:
            orig_mid = _out_to_orig_time((w.start + w.end) * 0.5, edl_segments)
            matched = next(
                (s for s in draft_sentences if s.start - 0.05 <= orig_mid <= s.end + 0.05),
                None,
            )
            if matched is None:
                matched = min(
                    draft_sentences,
                    key=lambda s: min(abs(orig_mid - s.start), abs(orig_mid - s.end)),
                )
            sp_lbl = matched.speaker or episode_speakers[0]
            assigned_words.append(w.model_copy(update={"speaker": sp_lbl}))

        smoothed = _smooth_word_speaker_runs(assigned_words, min_island_sec=1.0)
        present = {w.speaker for w in smoothed if w.speaker}
        speakers = [sp for sp in episode_speakers if sp in present]
        for w in smoothed:
            if w.speaker and w.speaker not in speakers:
                speakers.append(w.speaker)
        return smoothed, speakers

    # 3. Standalone clip diarization (fallback when no episode-level diarization was passed)
    samples = _load_mono_16k_float32(clean_wav_path, settings)

    # Group words into speech turns separated by pauses >= 0.35s or terminal punctuation
    turns: list[list[int]] = []
    cur: list[int] = []
    for idx, w in enumerate(words):
        if cur:
            prev_w = words[cur[-1]]
            gap = w.start - prev_w.end
            prev_end = prev_w.word.strip().endswith((".", "?", "!"))
            if gap >= 0.35 or (prev_end and gap >= 0.12):
                turns.append(cur)
                cur = []
        cur.append(idx)
    if cur:
        turns.append(cur)

    turn_spans = [(words[t[0]].start, words[t[-1]].end) for t in turns]
    turn_labels = _diarize_segments_on_waveform(samples, turn_spans, sr=16000)

    word_speakers: dict[int, str] = {}
    for t_indices, lbl in zip(turns, turn_labels):
        for w_idx in t_indices:
            word_speakers[w_idx] = lbl

    updated = [
        w.model_copy(update={"speaker": word_speakers.get(i, "SPEAKER_01")})
        for i, w in enumerate(words)
    ]
    speakers = list(dict.fromkeys(turn_labels))
    return updated, speakers
