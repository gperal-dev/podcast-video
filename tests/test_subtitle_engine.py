"""Unit tests for ASS subtitle generation, safe zones, karaoke events, and font family extraction."""

from __future__ import annotations

from pathlib import Path

from backend.config import get_settings
from backend.core.subtitle_engine import (
    generate_ass_from_edited_lines,
    generate_ass_from_words,
    hex_to_ass_color,
    resolve_truetype_family,
)
from backend.models.job import WordTimestamp
from backend.models.profile import SubtitleStyleConfig


def test_ass_color_conversion_and_font_family() -> None:
    assert hex_to_ass_color("#FFD400", alpha=0.0) == "&H0000D4FF"
    settings = get_settings()
    font_path = settings.fonts_dir / "Montserrat-ExtraBold.ttf"
    assert font_path.exists()
    family = resolve_truetype_family(font_path, "Montserrat")
    assert "Montserrat" in family


def test_generate_ass_karaoke_safe_zones_and_censored_skip(tmp_path: Path) -> None:
    settings = get_settings()
    font_path = settings.fonts_dir / "Montserrat-ExtraBold.ttf"
    ass_path = tmp_path / "test_short.ass"
    style = SubtitleStyleConfig(
        font_name="Montserrat",
        font_file="Montserrat-ExtraBold.ttf",
        primary_color="#FFFFFF",
        highlight_color="#FFD400",
        words_per_line_short=3,
        uppercase=True,
    )

    words = [
        WordTimestamp(word="Esto", start=0.2, end=0.5),
        WordTimestamp(word="secreto", start=0.55, end=1.0),
        WordTimestamp(word="censurada", start=1.1, end=1.4, censored=True),
        WordTimestamp(word="cambia", start=1.5, end=1.9),
        WordTimestamp(word="todo", start=2.0, end=2.5),
    ]

    lines = generate_ass_from_words(
        words=words,
        output_ass_path=ass_path,
        aspect="9:16",
        style_cfg=style,
        is_short=True,
        font_path=font_path,
    )
    assert ass_path.exists()
    content = ass_path.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert ",360,1" in content  # 9:16 bottom safe margin
    assert "CENSURADA" not in content
    assert "&H0000D4FF" in content  # active word highlight color
    assert len(lines) >= 1

    # Verify editing lines in Review mode regenerates valid ASS
    lines[0].text = "ESTO CAMBIA TODO"
    ass_edited = tmp_path / "edited.ass"
    generate_ass_from_edited_lines(lines, ass_edited, "9:16", style, is_short=True, font_path=font_path)
    assert "ESTO" in ass_edited.read_text(encoding="utf-8")


def test_visual_hook_skip_before_and_multispeaker_colors(tmp_path: Path) -> None:
    settings = get_settings()
    font_path = settings.fonts_dir / "Montserrat-ExtraBold.ttf"
    ass_path = tmp_path / "test_hook_speakers.ass"
    style = SubtitleStyleConfig(
        font_name="Montserrat",
        font_file="Montserrat-ExtraBold.ttf",
        primary_color="#FFFFFF",
        highlight_color="#FFD400",
        speaker_colors_enabled=True,
        words_per_line_short=5,
        uppercase=True,
    )

    words = [
        WordTimestamp(word="Gancho", start=0.0, end=0.8, speaker="SPEAKER_01"),
        WordTimestamp(word="inicial", start=0.85, end=2.0, speaker="SPEAKER_01"),
        WordTimestamp(word="Pregunta", start=2.3, end=2.8, speaker="SPEAKER_01"),
        WordTimestamp(word="uno", start=2.85, end=3.2, speaker="SPEAKER_01"),
        WordTimestamp(word="Respuesta", start=3.3, end=3.9, speaker="SPEAKER_02"),
        WordTimestamp(word="dos", start=3.95, end=4.4, speaker="SPEAKER_02"),
    ]
    speaker_colors = {"SPEAKER_01": "#FFD400", "SPEAKER_02": "#38BDF8"}

    lines = generate_ass_from_words(
        words=words,
        output_ass_path=ass_path,
        aspect="9:16",
        style_cfg=style,
        is_short=True,
        font_path=font_path,
        skip_before_sec=2.15,
        speaker_color_map=speaker_colors,
    )
    content = ass_path.read_text(encoding="utf-8")
    # Hook words must be completely absent during [0, 2.15]
    assert "GANCHO" not in content
    assert "INICIAL" not in content
    assert "PREGUNTA" in content
    assert "RESPUESTA" in content
    # Lines must break between speakers
    assert len(lines) == 2
    assert lines[0].speaker == "SPEAKER_01"
    assert lines[1].speaker == "SPEAKER_02"
    # Distinct ASS highlight colors: #FFD400 -> &H0000D4FF, #38BDF8 -> &H00F8BD38
    assert "&H0000D4FF" in content
    assert "&H00F8BD38" in content

    # Edited lines respect skip_before_sec and preserve speaker colors
    ass_edited = tmp_path / "edited_hook_speakers.ass"
    generate_ass_from_edited_lines(
        lines,
        ass_edited,
        "9:16",
        style,
        is_short=True,
        font_path=font_path,
        skip_before_sec=3.25,
        speaker_color_map=speaker_colors,
    )
    edited_content = ass_edited.read_text(encoding="utf-8")
    assert "PREGUNTA" not in edited_content
    assert "RESPUESTA" in edited_content
    assert "&H00F8BD38" in edited_content


def _parse_ass_time_cs(raw: str) -> int:
    h, m, s_cs = raw.split(":")
    s, cs = s_cs.split(".")
    return int(h) * 360000 + int(m) * 6000 + int(s) * 100 + int(cs)


def test_ass_dialogue_events_never_overlap(tmp_path: Path) -> None:
    settings = get_settings()
    font_path = settings.fonts_dir / "Montserrat-ExtraBold.ttf"
    style = SubtitleStyleConfig(words_per_line_short=3, uppercase=True)

    # Deliberately pack ultra-close timestamps and rounding collisions (matching Prueba 3 failure)
    words = [
        WordTimestamp(word="Francia,", start=65.32, end=65.52),
        WordTimestamp(word="Royal", start=65.52, end=65.524),
        WordTimestamp(word="Aircraft", start=65.524, end=65.528),
        WordTimestamp(word="Establishment,", start=65.53, end=66.10),
        WordTimestamp(word="comentaristas", start=66.102, end=67.00),
    ]
    ass_path = tmp_path / "tight_nonoverlap.ass"
    lines = generate_ass_from_words(words, ass_path, "9:16", style, is_short=True, font_path=font_path)
    dialogue_lines = [ln for ln in ass_path.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogue_lines) == 5

    prev_end = -1
    for ln in dialogue_lines:
        parts = ln.split(",", 9)
        s_cs = _parse_ass_time_cs(parts[1])
        e_cs = _parse_ass_time_cs(parts[2])
        assert s_cs < e_cs
        assert s_cs >= prev_end
        prev_end = e_cs

    # Verify edited lines regeneration is also strictly non-overlapping
    lines[0].end = lines[0].start + 0.02
    lines[1].start = lines[0].start + 0.01  # deliberate slight overlap in user input
    ass_ed = tmp_path / "tight_edited.ass"
    generate_ass_from_edited_lines(lines, ass_ed, "9:16", style, is_short=True, font_path=font_path)
    ed_diags = [ln for ln in ass_ed.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")]
    prev_end = -1
    for ln in ed_diags:
        parts = ln.split(",", 9)
        s_cs = _parse_ass_time_cs(parts[1])
        e_cs = _parse_ass_time_cs(parts[2])
        assert s_cs < e_cs
        assert s_cs >= prev_end
        prev_end = e_cs
