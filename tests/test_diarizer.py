"""Unit tests for local multi-speaker acoustic diarization and unlimited-speaker colors."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from backend.config import get_settings
from backend.core.diarizer import assign_speaker_colors, diarize_clip_words, diarize_draft_sentences
from backend.models.job import EDLSegment, SentenceSegment, WordTimestamp


def _write_multi_voice_wav(path: Path, single_voice: bool = False) -> None:
    """Synthesize speech-like harmonic formant signals for 1 or 3 distinct synthetic speakers."""
    sr = 16000
    dur = 12.0
    t = np.linspace(0.0, dur, int(sr * dur), endpoint=False)
    sig = np.zeros_like(t, dtype=np.float32)

    def voice_tone(mask: np.ndarray, f0: float, formant_scale: float) -> None:
        tt = t[mask]
        wave_ = (
            0.40 * np.sin(2 * math.pi * f0 * tt)
            + 0.30 * np.sin(2 * math.pi * (f0 * 2.0 * formant_scale) * tt)
            + 0.25 * np.sin(2 * math.pi * (f0 * 4.5 * formant_scale) * tt)
            + 0.15 * np.sin(2 * math.pi * (f0 * 7.2 * formant_scale) * tt)
        )
        sig[mask] = 0.35 * wave_

    if single_voice:
        voice_tone((t >= 0.0) & (t < 12.0), f0=125.0, formant_scale=1.0)
    else:
        # 3 distinct speakers alternating turns (baritone, tenor, alto)
        voice_tone((t >= 0.0) & (t < 2.0), f0=105.0, formant_scale=0.9)
        voice_tone((t >= 2.0) & (t < 4.0), f0=225.0, formant_scale=1.45)
        voice_tone((t >= 4.0) & (t < 6.0), f0=155.0, formant_scale=1.15)
        voice_tone((t >= 6.0) & (t < 8.0), f0=108.0, formant_scale=0.9)
        voice_tone((t >= 8.0) & (t < 10.0), f0=228.0, formant_scale=1.45)
        voice_tone((t >= 10.0) & (t < 12.0), f0=152.0, formant_scale=1.15)

    pcm = np.clip(sig * 32767.0, -32767, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def test_diarizer_single_vs_multi_speaker_and_unlimited_colors(tmp_path: Path) -> None:
    settings = get_settings()

    wav_1 = tmp_path / "single.wav"
    _write_multi_voice_wav(wav_1, single_voice=True)
    sents_1 = [
        SentenceSegment(id=f"S{i+1:04d}", start=i * 2.0, end=(i + 1) * 2.0 - 0.1, text="Uno")
        for i in range(6)
    ]
    labeled_1, spk_1 = diarize_draft_sentences(wav_1, sents_1, settings=settings)
    assert len(spk_1) == 1
    assert all(s.speaker == "SPEAKER_01" for s in labeled_1)

    wav_3 = tmp_path / "three.wav"
    _write_multi_voice_wav(wav_3, single_voice=False)
    sents_3 = [
        SentenceSegment(id=f"S{i+1:04d}", start=i * 2.0, end=(i + 1) * 2.0 - 0.1, text=f"Turno {i+1}")
        for i in range(6)
    ]
    labeled_3, spk_3 = diarize_draft_sentences(wav_3, sents_3, settings=settings)
    assert len(spk_3) == 3
    assert labeled_3[0].speaker == labeled_3[3].speaker
    assert labeled_3[1].speaker == labeled_3[4].speaker
    assert labeled_3[2].speaker == labeled_3[5].speaker

    # Unlimited speaker color assignment beyond palette size (e.g., 12 speakers)
    many_speakers = [f"SPEAKER_{i:02d}" for i in range(1, 13)]
    cmap = assign_speaker_colors(many_speakers, ["#FFD400", "#38BDF8", "#4ADE80"])
    assert len(cmap) == 12
    assert len(set(cmap.values())) == 12


def test_clip_diarization_respects_episode_speakers(tmp_path: Path) -> None:
    settings = get_settings()
    wav_multi = tmp_path / "dummy_clip.wav"
    # Even if clip audio has multiple acoustic profiles, episode_speakers=['SPEAKER_01'] locks solo narrator
    _write_multi_voice_wav(wav_multi, single_voice=False)
    words = [
        WordTimestamp(word="Uno", start=0.2, end=1.0),
        WordTimestamp(word="dos", start=2.2, end=3.5),
        WordTimestamp(word="tres", start=4.2, end=5.5),
    ]
    solo_words, solo_spks = diarize_clip_words(
        wav_multi,
        words,
        settings=settings,
        episode_speakers=["SPEAKER_01"],
    )
    assert solo_spks == ["SPEAKER_01"]
    assert all(w.speaker == "SPEAKER_01" for w in solo_words)

    # Multi-speaker episode inherits exact draft speaker IDs mapped through EDL segments & smooths <1s islands
    draft = [
        SentenceSegment(id="S0001", start=100.0, end=104.0, text="Pregunta inicial.", speaker="SPEAKER_01"),
        SentenceSegment(id="S0002", start=104.0, end=104.4, text="Ajá.", speaker="SPEAKER_02"),
        SentenceSegment(id="S0003", start=104.4, end=109.0, text="Continuación larga.", speaker="SPEAKER_01"),
        SentenceSegment(id="S0004", start=109.0, end=114.0, text="Respuesta larga del invitado.", speaker="SPEAKER_02"),
    ]
    edl_segs = [
        EDLSegment(index=0, orig_start=100.0, orig_end=114.0, out_start=0.0, out_end=14.0, crossfade_in_sec=0.0),
    ]
    clip_words = [
        WordTimestamp(word="Pregunta", start=0.2, end=1.5),
        WordTimestamp(word="inicial.", start=1.6, end=3.8),
        WordTimestamp(word="Ajá.", start=4.05, end=4.35),  # 0.3s island inside SPEAKER_01 turn -> smoothed!
        WordTimestamp(word="Continuación", start=4.5, end=6.5),
        WordTimestamp(word="larga.", start=6.6, end=8.8),
        WordTimestamp(word="Respuesta", start=9.1, end=11.0),
        WordTimestamp(word="larga.", start=11.1, end=13.5),
    ]
    multi_words, multi_spks = diarize_clip_words(
        wav_multi,
        clip_words,
        settings=settings,
        episode_speakers=["SPEAKER_01", "SPEAKER_02"],
        draft_sentences=draft,
        edl_segments=edl_segs,
    )
    assert multi_spks == ["SPEAKER_01", "SPEAKER_02"]
    assert [w.speaker for w in multi_words[:5]] == ["SPEAKER_01"] * 5
    assert [w.speaker for w in multi_words[5:]] == ["SPEAKER_02", "SPEAKER_02"]
