"""Unit tests for audio_analyzer, audio_editor EDL, censorship, and TimeMapper precision."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from backend.config import get_settings
from backend.core.audio_analyzer import compute_rms_energy_blocks, detect_silences, get_audio_duration
from backend.core.audio_editor import (
    build_edl,
    find_censored_intervals,
    normalize_token,
    render_clean_audio,
)
from backend.models.job import ChapterMark, WordTimestamp
from backend.models.profile import AudioEditConfig


def _write_synthetic_podcast_wav(path: Path) -> None:
    """Create a 12.0s synthetic WAV with 3 speech-like bursts separated by 2.0s silences.

    Timeline:
      0.0 - 2.5s : Tone burst 1 (440 Hz)
      2.5 - 4.5s : Silence (2.0s)
      4.5 - 7.5s : Tone burst 2 (554 Hz) -> includes censored word in [5.5, 6.1]
      7.5 - 9.5s : Silence (2.0s)
      9.5 - 12.0s: Tone burst 3 (659 Hz) -> includes marker tone at [10.8, 11.2]
    """
    sr = 48000
    total_sec = 12.0
    t = np.linspace(0.0, total_sec, int(sr * total_sec), endpoint=False)
    sig = np.zeros_like(t, dtype=np.float32)

    def add_tone(s: float, e: float, freq: float, amp: float = 0.25) -> None:
        mask = (t >= s) & (t < e)
        sig[mask] = amp * np.sin(2.0 * math.pi * freq * t[mask])

    add_tone(0.0, 2.5, 440.0)
    add_tone(4.5, 7.5, 554.0)
    add_tone(9.5, 12.0, 659.0)

    pcm = np.clip(sig * 32767.0, -32767, 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm]).ravel()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def test_normalize_token_and_censorship_matching() -> None:
    assert normalize_token("¡Maldición!") == "maldicion"
    assert normalize_token("  P*ta? ") == "p ta"

    words = [
        WordTimestamp(word="Hola", start=0.2, end=0.5),
        WordTimestamp(word="esto", start=0.6, end=0.9),
        WordTimestamp(word="es", start=1.0, end=1.2),
        WordTimestamp(word="Maldición,", start=5.5, end=6.1),
        WordTimestamp(word="sigue", start=6.3, end=6.7),
        WordTimestamp(word="tema", start=9.8, end=10.2),
        WordTimestamp(word="prohibido!", start=10.25, end=10.7),
        WordTimestamp(word="final", start=10.8, end=11.2),
    ]

    intervals, censored_idx, matched = find_censored_intervals(
        words,
        censored_terms=["maldicion", "tema prohibido"],
        pad_sec=0.025,
    )
    assert censored_idx == {3, 5, 6}
    assert set(matched) == {"maldicion", "tema prohibido"}
    assert len(intervals) == 2
    assert intervals[0][0] >= 5.47 and intervals[0][1] <= 6.13
    assert intervals[1][0] >= 9.77 and intervals[1][1] <= 10.73


def test_edl_silences_censorship_render_and_timemapper_sync(tmp_path: Path) -> None:
    src_wav = tmp_path / "synthetic_podcast.wav"
    out_wav = tmp_path / "clean_clip.wav"
    out_mp3 = tmp_path / "preview.mp3"
    _write_synthetic_podcast_wav(src_wav)

    settings = get_settings()
    dur = get_audio_duration(src_wav, settings)
    assert abs(dur - 12.0) < 0.05

    blocks = compute_rms_energy_blocks(src_wav, block_sec=4.0, settings=settings)
    assert len(blocks) == 3

    silences = detect_silences(src_wav, threshold_db=-38.0, min_duration=0.7, settings=settings)
    assert len(silences) == 2
    assert 2.4 <= silences[0][0] <= 2.6 and 4.4 <= silences[0][1] <= 4.6
    assert 7.4 <= silences[1][0] <= 7.6 and 9.4 <= silences[1][1] <= 9.6

    words = [
        WordTimestamp(word="Inicio", start=0.2, end=0.7),
        WordTimestamp(word="claro.", start=0.8, end=2.3),
        WordTimestamp(word="Segundo", start=4.6, end=5.2),
        WordTimestamp(word="Maldición,", start=5.5, end=6.1),
        WordTimestamp(word="continuamos.", start=6.3, end=7.3),
        WordTimestamp(word="Cierre", start=9.7, end=10.5),
        WordTimestamp(word="final", start=10.8, end=11.4),
    ]

    audio_cfg = AudioEditConfig(
        remove_silences=True,
        silence_threshold_db=-38.0,
        min_silence_sec=0.7,
        keep_silence_sec=0.18,
        censored_words=["maldicion"],
        crossfade_ms=20,
        target_lufs=-14.0,
    )

    edl, mapper, mapped_words = build_edl(
        clip_id="clip_01",
        clip_start=0.0,
        clip_end=12.0,
        words=words,
        silence_intervals=silences,
        audio_config=audio_cfg,
    )

    # Expect 2 silences cut + 1 censored word cut -> 4 kept segments
    assert edl.silences_removed_count == 2
    assert edl.censored_hits_count == 1
    assert len(edl.segments) == 4
    assert len(mapped_words) == len(words) - 1
    assert all("Maldición" not in w.word for w in mapped_words)

    # Render real audio via FFmpeg with crossfades and loudnorm
    render_clean_audio(
        source_audio=src_wav,
        edl_plan=edl,
        output_wav=out_wav,
        audio_config=audio_cfg,
        preview_mp3_path=out_mp3,
        settings=settings,
    )
    assert out_wav.exists() and out_mp3.exists()

    actual_rendered_dur = get_audio_duration(out_wav, settings)
    assert abs(actual_rendered_dur - edl.edited_duration) <= 0.035

    # Verify TimeMapper maps "final" (10.8s -> ~6.7s) accurately onto the actual rendered audio
    final_word = [w for w in mapped_words if w.word == "final"][0]
    expected_final_start = mapper.map_time(10.8)
    assert abs(final_word.start - expected_final_start) < 0.005

    # Verify chapters remap consistently
    chapters = mapper.map_chapters([ChapterMark(title="Parte final", orig_time_sec=9.7)])
    assert 0.0 < chapters[0].edited_time_sec < actual_rendered_dur

    # Verify rendered WAV actually has non-zero energy where "final" mapped, and no long >0.4s silence left
    post_silences = detect_silences(out_wav, threshold_db=-45.0, min_duration=0.45, settings=settings)
    assert post_silences == []


def test_multispan_edl_stitching_and_glossary_repair(tmp_path: Path) -> None:
    from backend.core.audio_editor import build_edl_for_spans
    from backend.core.transcriber import apply_glossary_to_words

    src_wav = tmp_path / "multi.wav"
    out_wav = tmp_path / "stitched.wav"
    _write_synthetic_podcast_wav(src_wav)
    settings = get_settings()

    words = [
        WordTimestamp(word="Pregunta", start=0.3, end=0.8, speaker="SPEAKER_01"),
        WordTimestamp(word="clave.", start=0.9, end=1.8, speaker="SPEAKER_01"),
        WordTimestamp(word="Relleno", start=4.7, end=5.5, speaker="SPEAKER_01"),
        WordTimestamp(word="Línea", start=9.6, end=10.1, speaker="SPEAKER_02"),
        WordTimestamp(word="CanHuber", start=10.2, end=11.0, speaker="SPEAKER_02"),
        WordTimestamp(word="falló.", start=11.1, end=11.8, speaker="SPEAKER_02"),
    ]
    repaired = apply_glossary_to_words(words, {"Línea CanHuber": "Línea Kammhuber"})
    assert [w.word for w in repaired] == ["Pregunta", "clave.", "Relleno", "Línea", "Kammhuber", "falló."]
    assert repaired[4].speaker == "SPEAKER_02"

    audio_cfg = AudioEditConfig(remove_silences=False, crossfade_ms=20, level_multi_speaker_voices=True)
    # Stitch [0.2, 2.0] (question) + [9.5, 11.9] (answer), skipping middle [2.0, 9.5]
    edl, mapper, mapped = build_edl_for_spans(
        clip_id="multi_01",
        spans=[(0.2, 2.0), (9.5, 11.9)],
        words=repaired,
        silence_intervals=[],
        audio_config=audio_cfg,
    )
    assert len(edl.segments) == 2
    assert edl.segments[1].crossfade_in_sec >= 0.045
    assert [w.word for w in mapped] == ["Pregunta", "clave.", "Línea", "Kammhuber", "falló."]
    # First word of second span starts cleanly near ~1.75s (after 1.8s - 0.045s crossfade)
    assert 1.7 <= mapped[2].start <= 1.95
    render_clean_audio(src_wav, edl, out_wav, audio_cfg, settings=settings)
    actual_dur = get_audio_duration(out_wav, settings)
    assert abs(actual_dur - edl.edited_duration) <= 0.045


def test_snap_spans_to_word_edges_and_duplicate_verification() -> None:
    from backend.core.audio_editor import build_edl_for_spans, snap_spans_to_word_edges, verify_edl_no_duplicates

    words = [
        WordTimestamp(word="El", start=1.00, end=1.20),
        WordTimestamp(word="secreto", start=1.25, end=1.90),
        WordTimestamp(word="del", start=1.95, end=2.15),
        WordTimestamp(word="reactor", start=2.20, end=2.85),
        WordTimestamp(word="nuclear", start=2.90, end=3.55),
        WordTimestamp(word="cambiará", start=6.00, end=6.60),
        WordTimestamp(word="todo.", start=6.65, end=7.10),
    ]
    # Simulate slice cutting into the middle of "secreto" (1.35) and an overlapping/adjacent slice (3.10)
    merged_overlap = snap_spans_to_word_edges([(1.35, 3.40), (3.10, 7.20)], words)
    assert len(merged_overlap) == 1
    assert merged_overlap[0][0] <= 1.25  # snapped back to onset of "secreto"
    assert merged_overlap[0][1] >= 7.10  # snapped cleanly past "todo."

    # Non-adjacent clipped edge (3.44 cuts only 16% of "nuclear") drops clipped fragment instead of duplicating
    snapped_no_dup = snap_spans_to_word_edges([(1.35, 3.40), (3.44, 7.20)], words)
    assert len(snapped_no_dup) == 2
    assert snapped_no_dup[0][1] >= 3.55
    assert snapped_no_dup[1][0] >= 5.90  # starts cleanly at "cambiará" without repeating "nuclear"

    # Separate non-adjacent spans should preserve both snapped cleanly without overlap
    sep = snap_spans_to_word_edges([(1.35, 3.40), (6.05, 7.05)], words)
    assert len(sep) == 2
    assert sep[0][0] <= 1.25 and sep[0][1] >= 3.55
    assert sep[1][0] <= 6.00 and sep[1][1] >= 7.10

    audio_cfg = AudioEditConfig(remove_silences=False, crossfade_ms=20)
    edl, _, mapped = build_edl_for_spans("clean_01", sep, words, [], audio_cfg)
    assert verify_edl_no_duplicates(edl, mapped) == []

    # Repeated 3-gram consecutively triggers detector
    dup_words = [
        WordTimestamp(word="en", start=0.1, end=0.2),
        WordTimestamp(word="aquella", start=0.25, end=0.5),
        WordTimestamp(word="noche", start=0.55, end=0.8),
        WordTimestamp(word="oscuro", start=0.85, end=1.1),
        WordTimestamp(word="en", start=1.2, end=1.3),
        WordTimestamp(word="aquella", start=1.35, end=1.6),
        WordTimestamp(word="noche", start=1.65, end=1.9),
    ]
    assert len(verify_edl_no_duplicates(edl, dup_words)) == 1


def test_nonlinear_cold_open_edl_render_and_trim_pass2_edge_orphans(tmp_path: Path) -> None:
    from backend.core.audio_editor import (
        build_edl_for_spans,
        snap_spans_to_word_edges,
        trim_pass2_edge_orphans,
        verify_edl_no_duplicates,
    )

    src_wav = tmp_path / "cold_open.wav"
    out_wav = tmp_path / "cold_open_out.wav"
    _write_synthetic_podcast_wav(src_wav)
    settings = get_settings()

    # Words across 0..2.5s (context), 4.6..7.3s (core), 9.6..11.8s (cold_open_hook from later!)
    words = [
        WordTimestamp(word="En", start=0.20, end=0.45),
        WordTimestamp(word="1940", start=0.50, end=0.95),
        WordTimestamp(word="el", start=1.00, end=1.20),
        WordTimestamp(word="Spitfire", start=1.25, end=2.00),
        WordTimestamp(word="fallaba.", start=2.05, end=2.45),
        WordTimestamp(word="Shilling", start=4.65, end=5.30),
        WordTimestamp(word="llegó", start=5.35, end=6.00),
        WordTimestamp(word="al", start=6.05, end=6.30),
        WordTimestamp(word="aeródromo.", start=6.35, end=7.30),
        WordTimestamp(word="¡Es", start=9.65, end=10.10),
        WordTimestamp(word="el", start=10.15, end=10.45),
        WordTimestamp(word="carburador!", start=10.50, end=11.75),
    ]
    # Playback order: cold open (9.65..11.80) FIRST, then chronological context (0.20..2.45) + core (4.65..7.30)
    snapped = snap_spans_to_word_edges([(9.68, 11.70), (0.22, 2.40), (4.70, 7.25)], words, preserve_order=True)
    assert len(snapped) == 3
    assert snapped[0][0] >= 9.5  # cold open preserved at index 0

    audio_cfg = AudioEditConfig(remove_silences=False, crossfade_ms=20)
    edl, mapper, mapped = build_edl_for_spans("cold_open_01", snapped, words, [], audio_cfg)
    assert verify_edl_no_duplicates(edl, mapped) == []
    assert [w.word for w in mapped][:3] == ["¡Es", "el", "carburador!"]
    assert [w.word for w in mapped][3:] == ["En", "1940", "el", "Spitfire", "fallaba.", "Shilling", "llegó", "al", "aeródromo."]

    # TimeMapper must map chronological timestamps cleanly even when segment 0 is a later cold open
    t_spitfire = mapper.map_time(1.30)
    assert 2.5 <= t_spitfire <= 4.0

    render_clean_audio(src_wav, edl, out_wav, audio_cfg, settings=settings)
    assert out_wav.exists()
    assert abs(get_audio_duration(out_wav, settings) - edl.edited_duration) <= 0.045

    # trim_pass2_edge_orphans removes leading lowercase orphan ("así de claro lo tenía ella.") and trailing cut-off
    orphan_words = [
        WordTimestamp(word="así", start=1.00, end=1.25),
        WordTimestamp(word="de", start=1.30, end=1.45),
        WordTimestamp(word="claro", start=1.50, end=1.80),
        WordTimestamp(word="lo", start=1.85, end=2.00),
        WordTimestamp(word="tenía", start=2.05, end=2.35),
        WordTimestamp(word="ella.", start=2.40, end=2.75),
        WordTimestamp(word="Ya", start=2.85, end=3.10),
        WordTimestamp(word="a", start=3.15, end=3.25),
        WordTimestamp(word="finales", start=3.30, end=3.80),
        WordTimestamp(word="de", start=3.85, end=4.00),
        WordTimestamp(word="los", start=4.05, end=4.25),
        WordTimestamp(word="años", start=4.30, end=4.65),
        WordTimestamp(word="30", start=4.70, end=5.00),
        WordTimestamp(word="contrataron", start=5.05, end=6.00),
        WordTimestamp(word="a", start=6.05, end=6.20),
        WordTimestamp(word="Beatrice", start=6.25, end=7.00),
        WordTimestamp(word="Shilling.", start=7.05, end=8.90),
        WordTimestamp(word="por", start=9.00, end=9.30),
        WordTimestamp(word="lo", start=9.35, end=9.70),
    ]
    trimmed = trim_pass2_edge_orphans([(1.00, 9.75)], orphan_words)
    assert len(trimmed) == 1
    assert trimmed[0][0] >= 2.75  # starts cleanly at "Ya a finales..."
    assert 8.90 <= trimmed[0][1] <= 9.10  # ends cleanly at "Shilling." dropping "por lo"


def test_build_edl_removes_music_interludes_with_qsin_crossfade(tmp_path: Path) -> None:
    from backend.core.audio_analyzer import merge_non_speech_with_silences
    from backend.core.audio_editor import build_edl_for_spans, render_clean_audio, verify_edl_no_duplicates

    src_wav = tmp_path / "music_gap_podcast.wav"
    out_wav = tmp_path / "music_gap_clean.wav"
    sr = 48000
    t_arr = np.linspace(0, 22.0, int(sr * 22.0), endpoint=False)
    sig = 0.25 * np.sin(2 * math.pi * 440 * t_arr)
    pcm = (np.clip(sig, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(src_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    settings = get_settings()

    # Speech 0.4..4.2, then 10.5s background music stinger (4.2..14.7), then speech 14.7..19.5
    words = [
        WordTimestamp(word="En", start=0.40, end=0.70),
        WordTimestamp(word="1940", start=0.75, end=1.30),
        WordTimestamp(word="los", start=1.35, end=1.65),
        WordTimestamp(word="Spitfire", start=1.70, end=2.50),
        WordTimestamp(word="tenían", start=2.55, end=3.10),
        WordTimestamp(word="un", start=3.15, end=3.40),
        WordTimestamp(word="fallo.", start=3.45, end=4.15),
        # 10.5s music interlude between 4.15 and 14.70 (loud music that bypassed -38dB silence)
        WordTimestamp(word="Beatrice", start=14.75, end=15.40),
        WordTimestamp(word="Shilling", start=15.45, end=16.20),
        WordTimestamp(word="creó", start=16.25, end=16.80),
        WordTimestamp(word="una", start=16.85, end=17.20),
        WordTimestamp(word="solución", start=17.25, end=18.20),
        WordTimestamp(word="brillante.", start=18.25, end=19.40),
    ]
    non_speech = [(4.20, 14.70)]
    music_ivs = [(4.20, 14.70)]
    unified = merge_non_speech_with_silences([], non_speech)
    assert unified == [(4.2, 14.7)]

    audio_cfg = AudioEditConfig(remove_silences=True, keep_silence_sec=0.14, min_silence_sec=0.45, crossfade_ms=45)
    edl, mapper, mapped = build_edl_for_spans(
        clip_id="short_music_test",
        spans=[(0.30, 19.60)],
        words=words,
        silence_intervals=[],
        audio_config=audio_cfg,
        non_speech_intervals=non_speech,
        music_intervals=music_ivs,
    )
    assert verify_edl_no_duplicates(edl, mapped) == []
    assert edl.music_gaps_removed_count == 1
    assert edl.music_gaps_removed_sec > 9.0
    assert edl.edited_duration < 10.5
    assert len(edl.segments) >= 2
    assert edl.segments[1].crossfade_curve == "qsin"
    assert edl.segments[1].crossfade_in_sec >= 0.13

    max_gap = max(mapped[i].start - mapped[i - 1].end for i in range(1, len(mapped)))
    assert max_gap < 0.8

    render_clean_audio(src_wav, edl, out_wav, audio_cfg, settings=settings)
    assert out_wav.exists()
    assert abs(get_audio_duration(out_wav, settings) - edl.edited_duration) <= 0.12


def test_trim_pass2_edge_orphans_trims_internal_spans() -> None:
    from backend.core.audio_editor import trim_pass2_edge_orphans

    words = [
        # Span 0 (clean)
        WordTimestamp(word="En", start=1.00, end=1.25),
        WordTimestamp(word="1940", start=1.30, end=1.90),
        WordTimestamp(word="hubo", start=1.95, end=2.40),
        WordTimestamp(word="problema.", start=2.45, end=3.20),
        # Span 1 (internal span >= 1.8s with leading lowercase orphan & trailing orphan stub)
        WordTimestamp(word="pero", start=10.00, end=10.25),
        WordTimestamp(word="ella.", start=10.30, end=10.75),
        WordTimestamp(word="Beatrice", start=10.85, end=11.50),
        WordTimestamp(word="diseñó", start=11.55, end=12.15),
        WordTimestamp(word="un", start=12.20, end=12.40),
        WordTimestamp(word="limitador.", start=12.45, end=13.40),
        WordTimestamp(word="y", start=13.50, end=13.70),
    ]
    trimmed = trim_pass2_edge_orphans([(0.95, 3.25), (9.95, 13.75)], words)
    assert len(trimmed) == 2
    assert trimmed[1][0] >= 10.75
    assert 13.40 <= trimmed[1][1] <= 13.55
