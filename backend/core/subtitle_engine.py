"""ASS subtitle generator with word-by-word karaoke highlighting, safe zones, and custom fonts."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import ImageFont

from backend.models.job import EditableSubtitleLine, WordTimestamp
from backend.models.profile import AspectRatio, SubtitleStyleConfig


def hex_to_ass_color(hex_color: str, alpha: float = 0.0) -> str:
    """Convert `#RRGGBB` and opacity alpha (0=opaque, 1=transparent) to ASS `&HAABBGGRR`."""
    cleaned = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(cleaned) != 6:
        cleaned = "FFFFFF"
    try:
        r = int(cleaned[0:2], 16)
        g = int(cleaned[2:4], 16)
        b = int(cleaned[4:6], 16)
    except ValueError:
        r, g, b = 255, 255, 255
    a = max(0, min(255, int(round(alpha * 255.0))))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _to_cs(seconds: float) -> int:
    """Convert seconds to non-negative integer centiseconds."""
    return max(0, int(round(float(seconds) * 100.0)))


def _from_cs(cs: int) -> float:
    """Convert integer centiseconds back to seconds."""
    return max(0, int(cs)) / 100.0


def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS timestamp `H:MM:SS.CC` (centiseconds)."""
    total_cs = _to_cs(seconds)
    cs = total_cs % 100
    total_sec = total_cs // 100
    sec = total_sec % 60
    total_min = total_sec // 60
    minutes = total_min % 60
    hours = total_min // 60
    return f"{hours}:{minutes:02d}:{sec:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    """Escape braces and linebreaks for ASS dialogue text."""
    return text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def resolve_truetype_family(font_path: Optional[Path], fallback_name: str) -> str:
    """Extract exact font family name embedded in the TTF/OTF file for libass matching."""
    if font_path and font_path.exists():
        try:
            font = ImageFont.truetype(str(font_path), 40)
            family, _ = font.getname()
            if family:
                return family
        except Exception:
            pass
    return fallback_name or "Arial"


def get_canvas_and_safe_margins(aspect: AspectRatio) -> tuple[int, int, int, int, int]:
    """Return `(play_res_x, play_res_y, margin_l, margin_r, margin_v)` respecting platform UI safe zones."""
    if aspect == "9:16":
        # Safe from TikTok / Reels / YT Shorts bottom caption & right action bar
        return 1080, 1920, 72, 145, 360
    if aspect == "1:1":
        return 1080, 1080, 80, 80, 120
    # 16:9
    return 1920, 1080, 130, 130, 88


def group_words_into_lines(
    words: list[WordTimestamp],
    words_per_line: int,
    uppercase: bool = True,
) -> list[list[WordTimestamp]]:
    """Group surviving words into balanced visual lines respecting pauses and punctuation."""
    surviving = [w for w in words if not w.censored and w.word.strip()]
    if not surviving:
        return []

    lines: list[list[WordTimestamp]] = []
    cur: list[WordTimestamp] = []
    limit = max(1, int(words_per_line))

    for w in surviving:
        if cur:
            gap = w.start - cur[-1].end
            prev_ends_sentence = cur[-1].word.strip().endswith((".", "?", "!"))
            speaker_changed = bool(cur[-1].speaker and w.speaker and cur[-1].speaker != w.speaker)
            if len(cur) >= limit or gap >= 0.45 or prev_ends_sentence or speaker_changed:
                lines.append(cur)
                cur = []
        tok = w.word.strip().upper() if uppercase else w.word.strip()
        cur.append(w.model_copy(update={"word": tok}))

    if cur:
        lines.append(cur)
    return lines


def _build_ass_header(
    aspect: AspectRatio,
    style_cfg: SubtitleStyleConfig,
    font_family: str,
    is_short: bool,
) -> str:
    res_x, res_y, ml, mr, mv = get_canvas_and_safe_margins(aspect)
    font_size = style_cfg.font_size_short if is_short else style_cfg.font_size_long

    primary_ass = hex_to_ass_color(style_cfg.primary_color, alpha=0.0)
    secondary_ass = hex_to_ass_color(style_cfg.highlight_color, alpha=0.0)
    outline_ass = hex_to_ass_color(style_cfg.outline_color, alpha=0.0)

    if style_cfg.box_enabled:
        border_style = 3
        back_alpha = max(0.0, min(1.0, 1.0 - float(style_cfg.box_opacity)))
        back_ass = hex_to_ass_color(style_cfg.box_color, alpha=back_alpha)
        outline_color_final = back_ass
        outline_w = max(8.0, float(style_cfg.outline_width) + 6.0)
    else:
        border_style = 1
        back_ass = hex_to_ass_color("#000000", alpha=0.35)
        outline_color_final = outline_ass
        outline_w = float(style_cfg.outline_width)

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "Title: PodcastForge Studio Subtitles\n"
        f"PlayResX: {res_x}\n"
        f"PlayResY: {res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_family},{font_size},{primary_ass},{secondary_ass},{outline_color_final},{back_ass},"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline_w:.1f},{style_cfg.shadow_depth:.1f},"
        f"2,{ml},{mr},{mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def generate_ass_from_words(
    words: list[WordTimestamp],
    output_ass_path: Path,
    aspect: AspectRatio,
    style_cfg: SubtitleStyleConfig,
    is_short: bool = True,
    font_path: Optional[Path] = None,
    skip_before_sec: float = 0.0,
    speaker_color_map: Optional[dict[str, str]] = None,
) -> list[EditableSubtitleLine]:
    """Generate ASS subtitle file with active word highlighting and return editable phrase lines."""
    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    font_family = resolve_truetype_family(font_path, style_cfg.font_name)
    header = _build_ass_header(aspect, style_cfg, font_family, is_short)

    cutoff = float(skip_before_sec or 0.0)
    if cutoff > 0.0:
        visible_words = [
            w for w in words
            if w.start >= (cutoff - 0.05) and w.end > cutoff
        ]
    else:
        visible_words = list(words)

    wpl = style_cfg.words_per_line_short if is_short else style_cfg.words_per_line_long
    lines = group_words_into_lines(visible_words, words_per_line=wpl, uppercase=style_cfg.uppercase)

    primary_ass = hex_to_ass_color(style_cfg.primary_color, alpha=0.0)
    default_highlight_ass = hex_to_ass_color(style_cfg.highlight_color, alpha=0.0)
    speaker_ass_map: dict[str, str] = {}
    if style_cfg.speaker_colors_enabled and speaker_color_map:
        for sp_id, hex_col in speaker_color_map.items():
            try:
                speaker_ass_map[sp_id] = hex_to_ass_color(hex_col, alpha=0.0)
            except Exception:
                continue

    events: list[str] = []
    editable_lines: list[EditableSubtitleLine] = []
    prev_end_cs = 0

    for line_idx, line_words in enumerate(lines):
        raw_start = max(cutoff, line_words[0].start) if cutoff > 0.0 else line_words[0].start
        line_start_cs = max(prev_end_cs, _to_cs(raw_start))
        n_tok = len(line_words)

        raw_end = line_words[-1].end
        line_end_cs = max(line_start_cs + max(6, n_tok), _to_cs(raw_end))
        if line_idx + 1 < len(lines):
            next_line_raw_s = max(cutoff, lines[line_idx + 1][0].start) if cutoff > 0.0 else lines[line_idx + 1][0].start
            next_line_start_cs = _to_cs(next_line_raw_s)
            if next_line_start_cs > line_start_cs + n_tok:
                line_end_cs = min(line_end_cs, next_line_start_cs)
            elif next_line_start_cs > line_start_cs:
                line_end_cs = max(line_start_cs + n_tok, min(line_end_cs, next_line_start_cs))

        line_speaker = next((w.speaker for w in line_words if w.speaker), None)
        full_text = " ".join(w.word for w in line_words)
        editable_lines.append(
            EditableSubtitleLine(
                index=line_idx,
                start=round(_from_cs(line_start_cs), 3),
                end=round(_from_cs(line_end_cs), 3),
                text=full_text,
                speaker=line_speaker,
            )
        )

        # Emit strictly monotonic, non-overlapping steps per active word
        cursor_cs = line_start_cs
        for active_i, active_w in enumerate(line_words):
            remaining_after = n_tok - active_i - 1
            step_start_cs = cursor_cs
            if remaining_after == 0:
                step_end_cs = max(step_start_cs + 1, line_end_cs)
            else:
                raw_next_s = max(cutoff, line_words[active_i + 1].start) if cutoff > 0.0 else line_words[active_i + 1].start
                max_end_here = max(step_start_cs + 1, line_end_cs - remaining_after)
                step_end_cs = max(step_start_cs + 1, min(max_end_here, _to_cs(raw_next_s)))
            cursor_cs = step_end_cs
            prev_end_cs = max(prev_end_cs, step_end_cs)

            sp_key = active_w.speaker or line_speaker
            active_hl_ass = speaker_ass_map.get(sp_key or "", default_highlight_ass)

            parts: list[str] = []
            for j, tok in enumerate(line_words):
                safe_tok = escape_ass_text(tok.word)
                if j == active_i:
                    parts.append(f"{{\\c{active_hl_ass}\\fscx106\\fscy106}}{safe_tok}{{\\fscx100\\fscy100}}")
                else:
                    parts.append(f"{{\\c{primary_ass}}}{safe_tok}")
            ass_line = " ".join(parts)
            events.append(
                f"Dialogue: 0,{format_ass_time(_from_cs(step_start_cs))},{format_ass_time(_from_cs(step_end_cs))},Default,,0,0,0,,{ass_line}"
            )

    content = header + "\n".join(events) + ("\n" if events else "")
    output_ass_path.write_text(content, encoding="utf-8")
    return editable_lines


def generate_ass_from_edited_lines(
    edited_lines: list[EditableSubtitleLine],
    output_ass_path: Path,
    aspect: AspectRatio,
    style_cfg: SubtitleStyleConfig,
    is_short: bool = True,
    font_path: Optional[Path] = None,
    skip_before_sec: float = 0.0,
    speaker_color_map: Optional[dict[str, str]] = None,
) -> Path:
    """Regenerate ASS file after user edits subtitle lines in Review mode."""
    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    font_family = resolve_truetype_family(font_path, style_cfg.font_name)
    header = _build_ass_header(aspect, style_cfg, font_family, is_short)

    cutoff = float(skip_before_sec or 0.0)
    primary_ass = hex_to_ass_color(style_cfg.primary_color, alpha=0.0)
    default_highlight_ass = hex_to_ass_color(style_cfg.highlight_color, alpha=0.0)
    speaker_ass_map: dict[str, str] = {}
    if style_cfg.speaker_colors_enabled and speaker_color_map:
        for sp_id, hex_col in speaker_color_map.items():
            try:
                speaker_ass_map[sp_id] = hex_to_ass_color(hex_col, alpha=0.0)
            except Exception:
                continue

    events: list[str] = []
    prev_end_cs = 0
    for line in edited_lines:
        if cutoff > 0.0 and float(line.end) <= cutoff + 0.05:
            continue
        txt = line.text.strip()
        if not txt:
            continue
        if style_cfg.uppercase:
            txt = txt.upper()
        tokens = txt.split()
        n_tok = len(tokens)
        eff_start_raw = max(cutoff, float(line.start)) if cutoff > 0.0 else float(line.start)
        eff_start_cs = max(prev_end_cs, _to_cs(eff_start_raw))
        eff_end_cs = max(eff_start_cs + max(10, n_tok), _to_cs(float(line.end)))
        total_cs = eff_end_cs - eff_start_cs
        active_hl_ass = speaker_ass_map.get(line.speaker or "", default_highlight_ass)

        cursor_cs = eff_start_cs
        for active_i in range(n_tok):
            s_cs = cursor_cs
            if active_i == n_tok - 1:
                e_cs = eff_end_cs
            else:
                target_end = eff_start_cs + int(round(((active_i + 1) * total_cs) / n_tok))
                e_cs = max(s_cs + 1, min(eff_end_cs - (n_tok - active_i - 1), target_end))
            cursor_cs = e_cs
            prev_end_cs = max(prev_end_cs, e_cs)

            parts: list[str] = []
            for j, tok in enumerate(tokens):
                safe_tok = escape_ass_text(tok)
                if j == active_i:
                    parts.append(f"{{\\c{active_hl_ass}\\fscx106\\fscy106}}{safe_tok}{{\\fscx100\\fscy100}}")
                else:
                    parts.append(f"{{\\c{primary_ass}}}{safe_tok}")
            events.append(
                f"Dialogue: 0,{format_ass_time(_from_cs(s_cs))},{format_ass_time(_from_cs(e_cs))},Default,,0,0,0,,{' '.join(parts)}"
            )

    content = header + "\n".join(events) + ("\n" if events else "")
    output_ass_path.write_text(content, encoding="utf-8")
    return output_ass_path
