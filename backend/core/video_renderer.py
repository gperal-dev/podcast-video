"""FFmpeg video rendering pipeline: 9:16/16:9/1:1, Ken Burns, PiP, watermark, CTA, ASS burn-in, HW accel."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from backend.config import Settings, get_settings
from backend.core.audio_analyzer import get_audio_duration
from backend.core.diagnostics import detect_ffmpeg_capabilities
from backend.core.openrouter_client import OpenRouterClient
from backend.core.profile_store import ProfileStore
from backend.core.transcriber import DANGLING_END_WORDS, _norm_last_word
from backend.core.tts_engine import get_or_create_whoosh_sfx, resolve_default_outro_tts_text
from backend.models.job import ClipCandidate, StoryboardShot, WordTimestamp
from backend.models.profile import AspectRatio, CustomImage, PodcasterProfile


def resolution_for_aspect(aspect: AspectRatio) -> tuple[int, int]:
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "1:1":
        return 1080, 1080
    return 1920, 1080


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path cleanly for FFmpeg filter arguments (Windows drive letters, colons & quotes)."""
    raw = path.resolve().as_posix()
    # Escape backslashes, colons after Windows drive letter, and single quotes inside filter expressions
    return raw.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _low_priority_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)}
    return {}


def _encoder_args(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "23"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]


def _run_ffmpeg_with_fallback(
    ffmpeg_path: Path,
    base_args: list[str],
    output_args: list[str],
    preferred_encoder: str,
    threads: int,
    timeout_sec: int = 180,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    expected_duration_sec: float = 0.0,
    progress_range: tuple[float, float] = (0.0, 100.0),
    progress_label: str = "Renderizando vídeo...",
) -> None:
    """Execute FFmpeg encode with preferred HW encoder, optional real progress streaming, and libx264 fallback."""
    encoders_to_try = [preferred_encoder]
    if preferred_encoder != "libx264":
        encoders_to_try.append("libx264")

    last_err = ""
    use_pipe = progress_cb is not None and expected_duration_sec > 0.5
    p_lo, p_hi = progress_range

    for enc in encoders_to_try:
        cmd = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            str(threads),
            *base_args,
            *_encoder_args(enc),
            "-pix_fmt",
            "yuv420p",
            *output_args,
        ]
        if not use_pipe:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(30, int(timeout_sec)),
                    **_low_priority_kwargs(),
                    check=False,
                )
                if proc.returncode == 0:
                    return
                last_err = proc.stderr.strip()
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"FFmpeg agotó el tiempo límite ({timeout_sec}s) con codificador {enc}.") from exc
            continue

        # Stream `-progress pipe:1` while collecting stderr for accurate live progress bars
        pipe_cmd = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-threads",
            str(threads),
            *base_args,
            *_encoder_args(enc),
            "-pix_fmt",
            "yuv420p",
            *output_args,
        ]
        proc_p = subprocess.Popen(
            pipe_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_low_priority_kwargs(),
        )
        t_start = time.monotonic()
        last_emit = 0.0
        max_wait = max(30, int(timeout_sec))
        try:
            assert proc_p.stdout is not None
            for raw_line in proc_p.stdout:
                if time.monotonic() - t_start > max_wait:
                    proc_p.kill()
                    raise RuntimeError(f"FFmpeg agotó el tiempo límite ({timeout_sec}s) con codificador {enc}.")
                line = raw_line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    val_str = line.split("=", 1)[1].strip()
                    try:
                        us = float(val_str)
                    except ValueError:
                        continue
                    cur_s = us / 1_000_000.0
                    frac = max(0.0, min(1.0, cur_s / max(0.5, expected_duration_sec)))
                    now_m = time.monotonic()
                    if now_m - last_emit >= 1.2:
                        last_emit = now_m
                        pct = p_lo + (p_hi - p_lo) * frac
                        if progress_cb is not None:
                            progress_cb(pct, f"{progress_label} ({frac * 100:.0f}%)")
            ret = proc_p.wait(timeout=max(5, int(max_wait - (time.monotonic() - t_start))))
            stderr_txt = proc_p.stderr.read() if proc_p.stderr else ""
            if ret == 0:
                if progress_cb is not None:
                    progress_cb(p_hi, f"{progress_label} (100%)")
                return
            last_err = stderr_txt.strip()
        except subprocess.TimeoutExpired as exc:
            proc_p.kill()
            raise RuntimeError(f"FFmpeg agotó el tiempo límite ({timeout_sec}s) con codificador {enc}.") from exc

    raise RuntimeError(f"FFmpeg falló al renderizar vídeo: {last_err[:500]}")


def _load_pil_font(font_path: Optional[Path], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path and font_path.exists():
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def render_card_png(
    output_png: Path,
    width: int,
    height: int,
    title: str,
    subtitle: str = "",
    font_path: Optional[Path] = None,
    accent_hex: str = "#FFD400",
    bg_image_path: Optional[Path] = None,
) -> Path:
    """Render a clean editorial chapter/quote card or dark fallback frame with Pillow."""
    output_png.parent.mkdir(parents=True, exist_ok=True)
    if bg_image_path and bg_image_path.exists():
        base = ImageOps.fit(ImageOps.exif_transpose(Image.open(bg_image_path)).convert("RGBA"), (width, height))
        overlay = Image.new("RGBA", (width, height), (10, 12, 18, 175))
        img = Image.alpha_composite(base, overlay)
    else:
        img = Image.new("RGBA", (width, height), (14, 16, 24, 255))
        draw_bg = ImageDraw.Draw(img)
        # Subtle editorial radial-like vertical gradient
        for y in range(0, height, 4):
            ratio = y / max(1, height)
            r = int(14 + 14 * (1 - ratio))
            g = int(16 + 18 * (1 - ratio))
            b = int(26 + 26 * (1 - ratio))
            draw_bg.rectangle([(0, y), (width, y + 4)], fill=(r, g, b, 255))

    draw = ImageDraw.Draw(img)
    title_size = max(44, int(width * 0.052))
    sub_size = max(28, int(width * 0.032))
    f_title = _load_pil_font(font_path, title_size)
    f_sub = _load_pil_font(font_path, sub_size)

    wrapped = textwrap.wrap(title.strip(), width=22 if width <= 1080 else 32)
    text_block = "\n".join(wrapped[:5])

    bbox = draw.multiline_textbbox((0, 0), text_block, font=f_title, align="center", spacing=14)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = (width - tw) // 2
    cy = (height - th) // 2 - (40 if subtitle else 0)

    # Accent bar above title
    draw.rounded_rectangle(
        [(width // 2 - 70, cy - 36), (width // 2 + 70, cy - 26)],
        radius=5,
        fill=accent_hex,
    )
    draw.multiline_text((cx, cy), text_block, font=f_title, fill="#FFFFFF", align="center", spacing=14)

    if subtitle.strip():
        sub_wrap = "\n".join(textwrap.wrap(subtitle.strip(), width=34)[:3])
        sb = draw.multiline_textbbox((0, 0), sub_wrap, font=f_sub, align="center", spacing=10)
        sw = sb[2] - sb[0]
        draw.multiline_text(
            ((width - sw) // 2, cy + th + 38),
            sub_wrap,
            font=f_sub,
            fill="#D4D8E2",
            align="center",
            spacing=10,
        )

    img.convert("RGB").save(output_png, "PNG")
    return output_png


def render_overlay_banner_png(
    output_png: Path,
    width: int,
    height: int,
    text: str,
    position: str = "top",
    font_path: Optional[Path] = None,
    bg_rgba: tuple[int, int, int, int] = (12, 14, 20, 215),
    accent_hex: str = "#FFD400",
) -> Path:
    """Create a transparent RGBA overlay PNG for opening hook (top) or ending CTA (bottom)."""
    output_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    fsize = max(32, int(width * 0.036))
    font = _load_pil_font(font_path, fsize)
    wrap_cols = 24 if width <= 1080 else 42
    wrapped = "\n".join(textwrap.wrap(text.strip(), width=wrap_cols)[:3])

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=10)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 38, 22
    box_w = min(width - 90, tw + pad_x * 2)
    box_h = th + pad_y * 2
    bx = (width - box_w) // 2
    if position == "top":
        by = int(height * 0.11) if height > width else int(height * 0.07)
    else:
        by = int(height * 0.72) if height > width else int(height * 0.76)

    draw.rounded_rectangle([(bx, by), (bx + box_w, by + box_h)], radius=22, fill=bg_rgba, outline=accent_hex, width=4)
    tx = (width - tw) // 2
    ty = by + pad_y
    draw.multiline_text((tx, ty), wrapped, font=font, fill="#FFFFFF", align="center", spacing=10)

    canvas.save(output_png, "PNG")
    return output_png


def _prepare_prescaled_image(src_img: Path, dst_png: Path, target_w: int, target_h: int) -> Path:
    """Resize/crop static image cleanly with EXIF correction for smooth jitter-free Ken Burns."""
    img = ImageOps.exif_transpose(Image.open(src_img)).convert("RGB")
    fitted = ImageOps.fit(img, (target_w, target_h), method=Image.Resampling.LANCZOS)
    fitted.save(dst_png, "PNG")
    return dst_png


def _render_single_shot(
    shot: StoryboardShot,
    shot_out: Path,
    width: int,
    height: int,
    ffmpeg_path: Path,
    encoder: str,
    threads: int,
    temp_dir: Path,
    font_path: Optional[Path],
    highlight_color: str,
    custom_image_path: Optional[Path] = None,
    dur_override: Optional[float] = None,
) -> Path:
    """Render one contiguous storyboard shot to a normalized 30fps MP4 segment."""
    dur = max(0.3, round(dur_override if dur_override is not None else (shot.end - shot.start), 3))
    frames = max(9, int(round(dur * 30.0)))
    media_path = Path(shot.local_media_path) if shot.local_media_path else None

    if shot.source_type in ("title_card", "quote_card"):
        card_png = temp_dir / f"{shot.id}_card.png"
        render_card_png(
            card_png,
            width,
            height,
            title=shot.card_title or shot.reason or "Punto clave",
            subtitle=shot.card_subtitle,
            font_path=font_path,
            accent_hex=highlight_color,
        )
        base_args = ["-loop", "1", "-i", str(card_png)]
        vf = (
            f"zoompan=z='min(zoom+0.0007,1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={width}x{height}:fps=30,setsar=1"
        )
        _run_ffmpeg_with_fallback(
            ffmpeg_path,
            base_args,
            ["-vf", vf, "-t", f"{dur:.3f}", "-an", str(shot_out)],
            encoder,
            threads,
        )
        return shot_out

    if shot.source_type == "custom_image" and custom_image_path and custom_image_path.exists():
        if shot.display_mode == "pip_overlay" and media_path and media_path.exists():
            # Background stock video + framed floating user image
            pip_png = temp_dir / f"{shot.id}_pip.png"
            pip_w = int(width * 0.44)
            pip_h = int(pip_w * 0.75)
            pil_img = ImageOps.exif_transpose(Image.open(custom_image_path)).convert("RGBA")
            fitted = ImageOps.fit(pil_img, (pip_w, pip_h), method=Image.Resampling.LANCZOS)
            # Rounded border frame
            framed = Image.new("RGBA", (pip_w + 16, pip_h + 16), (255, 255, 255, 235))
            framed.paste(fitted, (8, 8))
            framed.save(pip_png, "PNG")

            x_pos = "(W-w)/2" if width <= height else "W-w-60"
            y_pos = "int(H*0.14)" if width <= height else "60"
            if media_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                bg_args = ["-loop", "1", "-i", str(media_path), "-loop", "1", "-i", str(pip_png)]
            else:
                bg_args = ["-stream_loop", "-1", "-i", str(media_path), "-loop", "1", "-i", str(pip_png)]
            fc = (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30[bg];"
                f"[bg][1:v]overlay={x_pos}:{y_pos}:shortest=1[vout]"
            )
            _run_ffmpeg_with_fallback(
                ffmpeg_path,
                bg_args,
                ["-filter_complex", fc, "-map", "[vout]", "-t", f"{dur:.3f}", "-an", str(shot_out)],
                encoder,
                threads,
            )
            return shot_out

        # Fullscreen Ken Burns (also used for outro_cta)
        pre_png = temp_dir / f"{shot.id}_pre.png"
        _prepare_prescaled_image(custom_image_path, pre_png, int(width * 1.12), int(height * 1.12))
        vf = (
            f"zoompan=z='min(zoom+0.0008,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={width}x{height}:fps=30,setsar=1"
        )
        _run_ffmpeg_with_fallback(
            ffmpeg_path,
            ["-loop", "1", "-i", str(pre_png)],
            ["-vf", vf, "-t", f"{dur:.3f}", "-an", str(shot_out)],
            encoder,
            threads,
        )
        return shot_out

    # Stock video, stock photo, or verified public domain archive media
    if media_path and media_path.exists():
        if media_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
            pre_png = temp_dir / f"{shot.id}_stock.png"
            _prepare_prescaled_image(media_path, pre_png, int(width * 1.12), int(height * 1.12))
            vf = (
                f"zoompan=z='min(zoom+0.0007,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s={width}x{height}:fps=30,setsar=1"
            )
            _run_ffmpeg_with_fallback(
                ffmpeg_path,
                ["-loop", "1", "-i", str(pre_png)],
                ["-vf", vf, "-t", f"{dur:.3f}", "-an", str(shot_out)],
                encoder,
                threads,
            )
            return shot_out
        else:
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30"
            )
            _run_ffmpeg_with_fallback(
                ffmpeg_path,
                ["-stream_loop", "-1", "-i", str(media_path)],
                ["-vf", vf, "-t", f"{dur:.3f}", "-an", str(shot_out)],
                encoder,
                threads,
            )
            return shot_out

    # Ultimate fallback: clean dark editorial card so rendering never aborts
    fb_png = temp_dir / f"{shot.id}_fallback.png"
    render_card_png(
        fb_png,
        width,
        height,
        title=shot.card_title or "PodcastForge Studio",
        subtitle=shot.reason,
        font_path=font_path,
        accent_hex=highlight_color,
    )
    _run_ffmpeg_with_fallback(
        ffmpeg_path,
        ["-loop", "1", "-i", str(fb_png)],
        ["-vf", f"scale={width}:{height},fps=30,setsar=1", "-t", f"{dur:.3f}", "-an", str(shot_out)],
        encoder,
        threads,
    )
    return shot_out


def _hex_to_rgb(hex_color: str, fallback: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    cleaned = (hex_color or "").strip().lstrip("#")
    if len(cleaned) != 6:
        return fallback
    try:
        return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)
    except ValueError:
        return fallback


def compute_visual_hook_segment(
    mapped_words: list[WordTimestamp],
    max_hook_sec: float = 4.8,
    min_hook_sec: float = 1.6,
) -> tuple[float, str, list[WordTimestamp]]:
    """Select the exact spoken opening segment for the full-screen Visual Hook (complete thought, never dangling word)."""
    surviving = [w for w in mapped_words if not w.censored and w.word.strip()]
    if not surviving:
        return 0.0, "", []

    hook_words: list[WordTimestamp] = []

    for idx, cur in enumerate(surviving):
        hook_words.append(cur)
        next_w = surviving[idx + 1] if idx + 1 < len(surviving) else None
        word_txt = cur.word.strip()
        pause_after = (next_w.start - cur.end) if next_w else 0.0
        cur_norm = _norm_last_word(word_txt)

        # Terminal punctuation (. ? !) closes immediately once we have >= 4 words or min_hook_sec
        if word_txt.endswith((".", "?", "!")) and (len(hook_words) >= 4 or cur.end >= min_hook_sec):
            break
        # Natural breath pause closes once we reached min_hook_sec on a non-dangling word
        if pause_after >= 0.35 and cur.end >= min_hook_sec and cur_norm not in DANGLING_END_WORDS:
            break
        # Comma/colon only closes on longer clauses (>= 7 words, >= 2.3s) and non-dangling words
        if (
            word_txt.endswith((",", ";", ":"))
            and len(hook_words) >= 7
            and cur.end >= 2.3
            and cur_norm not in DANGLING_END_WORDS
        ):
            break
        if len(hook_words) >= 12 or cur.end >= max_hook_sec:
            # Look ahead up to 3 words (<= +1.4s) to finish at punctuation if close, otherwise trim dangling words
            extended = False
            for extra in range(1, 4):
                if idx + extra >= len(surviving):
                    break
                cand_w = surviving[idx + extra]
                if cand_w.end > max_hook_sec + 1.4:
                    break
                cand_txt = cand_w.word.strip()
                if cand_txt.endswith((".", "?", "!")) or (
                    cand_txt.endswith((",", ";", ":")) and _norm_last_word(cand_txt) not in DANGLING_END_WORDS
                ):
                    hook_words.extend(surviving[idx + 1 : idx + extra + 1])
                    extended = True
                    break
            if not extended:
                while len(hook_words) > 3 and _norm_last_word(hook_words[-1].word) in DANGLING_END_WORDS:
                    hook_words.pop()
            break

    last = hook_words[-1]
    next_after = surviving[len(hook_words)] if len(hook_words) < len(surviving) else None
    if next_after and next_after.start > last.end + 0.02:
        hook_end_sec = min(last.end + 0.06, next_after.start - 0.02)
    else:
        hook_end_sec = last.end

    hook_end_sec = round(max(0.25, hook_end_sec), 3)
    spoken = " ".join(w.word.strip() for w in hook_words)
    return hook_end_sec, spoken, hook_words


def _wrap_tokens_to_pixel_lines(
    draw: ImageDraw.ImageDraw,
    tokens: list[tuple[int, str]],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    space_w: int,
) -> list[list[tuple[int, str]]]:
    lines: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_w = 0
    for idx, tok in tokens:
        tb = draw.textbbox((0, 0), tok, font=font)
        tw = tb[2] - tb[0]
        needed = tw if not cur else (cur_w + space_w + tw)
        if cur and needed > max_width:
            lines.append(cur)
            cur = [(idx, tok)]
            cur_w = tw
        else:
            cur.append((idx, tok))
            cur_w = needed
    if cur:
        lines.append(cur)
    return lines


def _render_fullscreen_hook_overlay(
    tmp_dir: Path,
    width: int,
    height: int,
    hook_end_sec: float,
    hook_words: list[WordTimestamp],
    spoken_text: str,
    style_name: str,
    bg_hex: str,
    text_hex: str,
    accent_hex: str,
    bg_opacity: float,
    font_path: Optional[Path],
) -> Optional[Path]:
    """Generate timed full-screen RGBA frames + FFmpeg concat manifest for the opening Visual Hook."""
    if hook_end_sec <= 0.2:
        return None

    active_words = [w for w in hook_words if not w.censored and w.word.strip()]
    if not active_words:
        raw_tokens = [t.strip() for t in spoken_text.split() if t.strip()]
        if not raw_tokens:
            return None
        step = hook_end_sec / len(raw_tokens)
        active_words = [
            WordTimestamp(word=tok, start=i * step, end=(i + 1) * step)
            for i, tok in enumerate(raw_tokens)
        ]

    # Pick font size that wraps cleanly within center vertical/horizontal safe region
    base_size = int(width * (0.070 if width <= height else 0.048))
    if len(active_words) >= 9:
        base_size = int(base_size * 0.86)
    base_size = max(36, min(92, base_size))

    bg_rgb = _hex_to_rgb(bg_hex, (11, 15, 26))
    text_rgb = _hex_to_rgb(text_hex, (255, 255, 255))
    accent_rgb = _hex_to_rgb(accent_hex, (255, 212, 0))
    clamped_op = max(0.0, min(1.0, float(bg_opacity)))

    probe_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe_img)
    display_tokens = [
        (i, w.word.strip().upper() if style_name == "kinetic_fullscreen" else w.word.strip())
        for i, w in enumerate(active_words)
    ]

    font = _load_pil_font(font_path, base_size)
    max_text_w = int(width * 0.80)
    wrapped_lines: list[list[tuple[int, str]]] = []
    for scale in (1.0, 0.88, 0.78, 0.68):
        font = _load_pil_font(font_path, max(28, int(base_size * scale)))
        sb = probe_draw.textbbox((0, 0), " ", font=font)
        space_w = max(8, (sb[2] - sb[0]) or int(font.size * 0.32 if hasattr(font, "size") else 14))
        wrapped_lines = _wrap_tokens_to_pixel_lines(probe_draw, display_tokens, font, max_text_w, space_w)
        if len(wrapped_lines) <= (5 if width <= height else 4):
            break

    sb = probe_draw.textbbox((0, 0), " ", font=font)
    space_w = max(10, (sb[2] - sb[0]) or 16)
    line_h = int((getattr(font, "size", base_size)) * 1.32)
    block_h = len(wrapped_lines) * line_h
    start_y = max(int(height * 0.14), (height - block_h) // 2)

    hook_dir = tmp_dir / "hook_frames"
    hook_dir.mkdir(parents=True, exist_ok=True)

    if style_name == "editorial_card":
        alpha = int(min(0.95, clamped_op + 0.18) * 255)
    else:
        alpha = int(clamped_op * 255)

    # Render one RGBA PNG frame per spoken step plus optional brief pre-speech frame
    frame_entries: list[tuple[Path, float]] = []
    first_start = max(0.0, active_words[0].start)

    def _draw_hook_frame(active_idx: int, png_path: Path) -> None:
        canvas = Image.new("RGBA", (width, height), (*bg_rgb, alpha))
        draw = ImageDraw.Draw(canvas)

        if style_name == "editorial_card":
            pad = int(min(width, height) * 0.055)
            draw.rounded_rectangle(
                [(pad, pad), (width - pad, height - pad)],
                radius=28,
                outline=(*accent_rgb, 210),
                width=4,
            )
            draw.rounded_rectangle(
                [(pad + 14, pad + 14), (width - pad - 14, height - pad - 14)],
                radius=22,
                outline=(*text_rgb, 65),
                width=2,
            )
            bar_w = int(width * 0.16)
            draw.rounded_rectangle(
                [(width // 2 - bar_w // 2, start_y - 46), (width // 2 + bar_w // 2, start_y - 38)],
                radius=4,
                fill=(*accent_rgb, 240),
            )
            draw.rounded_rectangle(
                [(width // 2 - bar_w // 2, start_y + block_h + 34), (width // 2 + bar_w // 2, start_y + block_h + 42)],
                radius=4,
                fill=(*accent_rgb, 240),
            )
        elif style_name == "kinetic_fullscreen":
            bar_w = int(width * 0.18)
            draw.rounded_rectangle(
                [(width // 2 - bar_w // 2, start_y - 44), (width // 2 + bar_w // 2, start_y - 34)],
                radius=5,
                fill=(*accent_rgb, 255),
            )
        else:
            # bold_minimal: clean animated progress bar near bottom center
            bar_w = int(width * 0.36)
            bar_x = (width - bar_w) // 2
            bar_y = min(height - 90, start_y + block_h + 52)
            draw.rounded_rectangle([(bar_x, bar_y), (bar_x + bar_w, bar_y + 8)], radius=4, fill=(*text_rgb, 60))
            frac = 0.08 if active_idx < 0 else (active_idx + 1) / max(1, len(display_tokens))
            fill_w = max(12, int(bar_w * frac))
            draw.rounded_rectangle([(bar_x, bar_y), (bar_x + fill_w, bar_y + 8)], radius=4, fill=(*accent_rgb, 255))

        for line_i, line_items in enumerate(wrapped_lines):
            widths = []
            for _, tok in line_items:
                tb = draw.textbbox((0, 0), tok, font=font)
                widths.append(tb[2] - tb[0])
            total_lw = sum(widths) + space_w * max(0, len(line_items) - 1)
            cur_x = (width - total_lw) // 2
            cur_y = start_y + line_i * line_h

            for (tok_idx, tok), tw in zip(line_items, widths):
                is_cur = tok_idx == active_idx
                is_spoken = active_idx >= 0 and tok_idx <= active_idx
                if is_cur:
                    fill_col = (*accent_rgb, 255)
                elif is_spoken or active_idx < 0:
                    fill_col = (*text_rgb, 255)
                else:
                    fill_col = (*text_rgb, 210)

                # Subtle shadow for crisp readability over video at lower opacity
                draw.text((cur_x + 3, cur_y + 4), tok, font=font, fill=(0, 0, 0, 190))
                draw.text((cur_x, cur_y), tok, font=font, fill=fill_col)
                if is_cur and style_name == "kinetic_fullscreen":
                    draw.rounded_rectangle(
                        [(cur_x, cur_y + int(line_h * 0.80)), (cur_x + tw, cur_y + int(line_h * 0.86))],
                        radius=3,
                        fill=(*accent_rgb, 240),
                    )
                cur_x += tw + space_w

        canvas.save(png_path, "PNG")

    if first_start > 0.08:
        p0 = hook_dir / "hook_pre.png"
        _draw_hook_frame(-1, p0)
        frame_entries.append((p0, first_start))

    cur_cursor = first_start if first_start > 0.08 else 0.0
    for idx, w in enumerate(active_words):
        step_end = (
            max(cur_cursor + 0.05, active_words[idx + 1].start)
            if idx + 1 < len(active_words)
            else max(cur_cursor + 0.05, hook_end_sec)
        )
        step_dur = max(0.04, step_end - cur_cursor)
        p = hook_dir / f"hook_{idx:03d}.png"
        _draw_hook_frame(idx, p)
        frame_entries.append((p, step_dur))
        cur_cursor += step_dur

    if not frame_entries:
        return None

    manifest = hook_dir / "hook_frames.txt"
    lines_out: list[str] = []
    for fpath, fdur in frame_entries:
        lines_out.append(f"file '{fpath.resolve().as_posix()}'")
        lines_out.append(f"duration {fdur:.4f}")
    lines_out.append(f"file '{frame_entries[-1][0].resolve().as_posix()}'")
    manifest.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return manifest


def _join_shots_with_concat(
    shot_files: list[Path],
    joined_mp4: Path,
    ffmpeg_path: Path,
    tmp_dir: Path,
) -> None:
    concat_txt = tmp_dir / "concat.txt"
    concat_txt.write_text(
        "\n".join(f"file '{sf.resolve().as_posix()}'" for sf in shot_files),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_txt),
            "-c",
            "copy",
            str(joined_mp4),
        ],
        capture_output=True,
        text=True,
        **_low_priority_kwargs(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Error uniendo planos: {proc.stderr.strip()}")


def _run_xfade_batch(
    files: list[Path],
    nominal_durs: list[float],
    t_dur: float,
    out_mp4: Path,
    ffmpeg_path: Path,
    encoder: str,
    threads: int,
) -> None:
    """Join <= 8 pre-rendered MP4 segments using smooth xfade with exact cumulative offsets."""
    if len(files) == 1:
        shutil.copy2(files[0], out_mp4)
        return

    inputs: list[str] = []
    filters: list[str] = []
    for idx, f in enumerate(files):
        inputs.extend(["-i", str(f)])
        filters.append(f"[{idx}:v]fps=30,settb=AVTB,format=yuv420p[s{idx}]")

    cur_label = "s0"
    cum_offset = nominal_durs[0]
    for i in range(1, len(files)):
        out_label = f"xf{i}"
        filters.append(
            f"[{cur_label}][s{i}]xfade=transition=fade:duration={t_dur:.3f}:offset={cum_offset:.3f}[{out_label}]"
        )
        cur_label = out_label
        cum_offset += nominal_durs[i]

    fc = ";".join(filters)
    _run_ffmpeg_with_fallback(
        ffmpeg_path,
        inputs,
        ["-filter_complex", fc, "-map", f"[{cur_label}]", "-an", str(out_mp4)],
        encoder,
        threads,
    )


def _join_shots_with_xfade(
    shot_files: list[Path],
    nominal_durs: list[float],
    t_dur: float,
    joined_mp4: Path,
    ffmpeg_path: Path,
    encoder: str,
    threads: int,
    tmp_dir: Path,
) -> None:
    """Join shot MP4 segments in batches via xfade preserving exact total nominal duration."""
    if len(shot_files) <= 8:
        _run_xfade_batch(shot_files, nominal_durs, t_dur, joined_mp4, ffmpeg_path, encoder, threads)
        return

    batch_size = 8
    batch_files: list[Path] = []
    batch_nominal_durs: list[float] = []
    for b_idx in range(0, len(shot_files), batch_size):
        chunk_files = shot_files[b_idx : b_idx + batch_size]
        chunk_durs = nominal_durs[b_idx : b_idx + batch_size]
        b_out = tmp_dir / f"xf_batch_{b_idx // batch_size:02d}.mp4"
        _run_xfade_batch(chunk_files, chunk_durs, t_dur, b_out, ffmpeg_path, encoder, threads)
        batch_files.append(b_out)
        batch_nominal_durs.append(sum(chunk_durs))

    _run_xfade_batch(batch_files, batch_nominal_durs, t_dur, joined_mp4, ffmpeg_path, encoder, threads)


def _score_thumbnail_local(img_path: Path) -> float:
    """Compute local sharpness + exposure + contrast score ($0.00, zero VLM calls)."""
    try:
        import numpy as np

        with Image.open(img_path) as im:
            gray_im = ImageOps.contain(im.convert("L"), (320, 320))
            arr = np.asarray(gray_im, dtype=np.float32)
        if arr.shape[0] < 4 or arr.shape[1] < 4:
            return 0.0
        lap = (
            arr[:-2, 1:-1]
            + arr[2:, 1:-1]
            + arr[1:-1, :-2]
            + arr[1:-1, 2:]
            - 4.0 * arr[1:-1, 1:-1]
        )
        sharpness = float(np.var(lap))
        mean_luma = float(np.mean(arr))
        contrast = float(np.std(arr))
        if mean_luma < 38.0:
            exp_factor = max(0.15, mean_luma / 38.0)
        elif mean_luma > 225.0:
            exp_factor = max(0.15, (255.0 - mean_luma) / 30.0)
        else:
            exp_factor = 1.0 - min(0.25, abs(mean_luma - 130.0) / 400.0)
        sharp_norm = min(1.0, sharpness / 450.0)
        contrast_norm = min(1.0, contrast / 65.0)
        return (sharp_norm * 0.55 + contrast_norm * 0.25 + exp_factor * 0.20) * exp_factor
    except Exception:
        return 0.0


def _select_best_thumbnail(
    output_mp4: Path,
    dur: float,
    hook_end_sec: float,
    thumbnail_jpg: Path,
    ffmpeg_path: Path,
    llm: Optional[OpenRouterClient],
    clip_title: str,
    body_start_sec: float = 0.0,
    body_end_sec: Optional[float] = None,
) -> None:
    """Extract candidate frames inside podcast body and select best thumbnail locally ($0.00)."""
    _ = llm, clip_title
    eff_body_end = min(dur - 0.25, body_end_sec) if body_end_sec and body_end_sec > body_start_sec + 1.0 else dur - 0.35
    safe_start = max(
        0.5,
        min(max(0.5, eff_body_end - 0.6), body_start_sec + hook_end_sec + 0.45),
    )
    avail = max(0.3, eff_body_end - safe_start)
    fractions = (0.15, 0.40, 0.65, 0.85)
    timestamps: list[float] = []
    for frac in fractions:
        t = round(min(max(0.4, safe_start + avail * frac), max(0.4, eff_body_end - 0.1)), 2)
        if not any(abs(t - prev) < 0.25 for prev in timestamps):
            timestamps.append(t)
    if not timestamps:
        timestamps = [round(max(0.5, min(safe_start + avail * 0.4, eff_body_end)), 2)]

    with tempfile.TemporaryDirectory(prefix="pf_thumb_") as th_tmp:
        tdir = Path(th_tmp)
        cand_paths: list[Path] = []
        for idx, t in enumerate(timestamps):
            p = tdir / f"cand_{idx + 1}.jpg"
            try:
                subprocess.run(
                    [
                        str(ffmpeg_path),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{t:.2f}",
                        "-i",
                        str(output_mp4),
                        "-vframes",
                        "1",
                        "-q:v",
                        "2",
                        str(p),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except Exception:
                continue
            if p.exists() and p.stat().st_size > 1000:
                cand_paths.append(p)

        if not cand_paths:
            return

        chosen_path = cand_paths[len(cand_paths) // 2]
        best_score = -1.0
        for cp in cand_paths:
            sc = _score_thumbnail_local(cp)
            if sc > best_score:
                best_score = sc
                chosen_path = cp

        shutil.copy2(chosen_path, thumbnail_jpg)


def _render_gradient_rgba(
    width: int,
    height: int,
    primary_hex: str,
    secondary_hex: str,
    opacity: float,
    full_opaque: bool = False,
) -> Image.Image:
    """Render a smooth vertical gradient RGBA image with optional translucency."""
    c1 = _hex_to_rgb(primary_hex, (15, 23, 42))
    c2 = _hex_to_rgb(secondary_hex, (30, 27, 75))
    alpha = 255 if full_opaque else max(0, min(250, int(round(opacity * 255))))
    strip = Image.new("RGBA", (1, height))
    denom = max(1, height - 1)
    strip_pixels = []
    for y in range(height):
        t = y / denom
        r = int(round(c1[0] + (c2[0] - c1[0]) * t))
        g = int(round(c1[1] + (c2[1] - c1[1]) * t))
        b = int(round(c1[2] + (c2[2] - c1[2]) * t))
        strip_pixels.append((r, g, b, alpha))
    strip.putdata(strip_pixels)
    return strip.resize((width, height), Image.Resampling.BILINEAR)


def _resolve_custom_bg_image_path(
    bg_image_id: Optional[str],
    all_imgs: dict[str, CustomImage],
    profile_id: str,
    store: ProfileStore,
    episode_images_dir: Optional[Path],
) -> Optional[Path]:
    if not bg_image_id:
        return None
    img_obj = all_imgs.get(bg_image_id)
    if img_obj is None:
        return None
    cand = store.resolve_custom_image_path(profile_id, img_obj, episode_images_dir)
    if cand and cand.exists():
        return cand
    return None


def _build_curtain_background_layer(
    inputs: list[str],
    filter_steps: list[str],
    next_idx_ref: list[int],
    tmp_dir: Path,
    tag: str,
    width: int,
    height: int,
    dur_sec: float,
    bg_mode: str,
    primary_hex: str,
    secondary_hex: str,
    overlay_opacity: float,
    bg_video_path: Optional[Path],
    bg_image_path: Optional[Path],
    video_seek_sec: float = 0.0,
) -> str:
    """Append FFmpeg inputs + filters for a blurred clip / custom image / solid gradient background."""
    eff_mode = bg_mode
    if eff_mode == "custom_image" and not (bg_image_path and bg_image_path.exists()):
        eff_mode = "solid_gradient"
    if eff_mode == "blurred_clip_video" and not (bg_video_path and bg_video_path.exists()):
        eff_mode = "solid_gradient"

    out_lbl = f"bg_{tag}"
    if eff_mode == "blurred_clip_video" and bg_video_path:
        tint_png = tmp_dir / f"{tag}_tint.png"
        _render_gradient_rgba(
            width, height, primary_hex, secondary_hex, overlay_opacity, full_opaque=False
        ).save(tint_png, "PNG")

        vid_idx = next_idx_ref[0]
        if video_seek_sec > 0.1:
            inputs.extend(["-stream_loop", "-1", "-ss", f"{video_seek_sec:.3f}", "-i", str(bg_video_path)])
        else:
            inputs.extend(["-stream_loop", "-1", "-i", str(bg_video_path)])
        next_idx_ref[0] += 1

        tint_idx = next_idx_ref[0]
        inputs.extend(["-loop", "1", "-t", f"{dur_sec:.3f}", "-i", str(tint_png)])
        next_idx_ref[0] += 1

        filter_steps.append(
            f"[{vid_idx}:v]trim=duration={dur_sec:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=22,eq=brightness=-0.10:saturation=1.15,"
            f"fps=30,format=rgba[{out_lbl}_v]"
        )
        filter_steps.append(f"[{tint_idx}:v]fps=30,format=rgba[{out_lbl}_t]")
        filter_steps.append(f"[{out_lbl}_v][{out_lbl}_t]overlay=0:0:shortest=1[{out_lbl}]")
        return out_lbl

    if eff_mode == "custom_image" and bg_image_path:
        prescaled = tmp_dir / f"{tag}_custom_bg.png"
        _prepare_prescaled_image(bg_image_path, prescaled, width, height)
        tint_png = tmp_dir / f"{tag}_custom_tint.png"
        _render_gradient_rgba(
            width, height, primary_hex, secondary_hex, overlay_opacity, full_opaque=False
        ).save(tint_png, "PNG")

        img_idx = next_idx_ref[0]
        inputs.extend(["-loop", "1", "-t", f"{dur_sec:.3f}", "-i", str(prescaled)])
        next_idx_ref[0] += 1

        tint_idx = next_idx_ref[0]
        inputs.extend(["-loop", "1", "-t", f"{dur_sec:.3f}", "-i", str(tint_png)])
        next_idx_ref[0] += 1

        filter_steps.append(f"[{img_idx}:v]fps=30,format=rgba[{out_lbl}_i]")
        filter_steps.append(f"[{tint_idx}:v]fps=30,format=rgba[{out_lbl}_t]")
        filter_steps.append(f"[{out_lbl}_i][{out_lbl}_t]overlay=0:0:shortest=1[{out_lbl}]")
        return out_lbl

    solid_png = tmp_dir / f"{tag}_solid_bg.png"
    _render_gradient_rgba(
        width, height, primary_hex, secondary_hex, 1.0, full_opaque=True
    ).save(solid_png, "PNG")
    bg_idx = next_idx_ref[0]
    inputs.extend(["-loop", "1", "-t", f"{dur_sec:.3f}", "-i", str(solid_png)])
    next_idx_ref[0] += 1
    filter_steps.append(f"[{bg_idx}:v]fps=30,format=rgba[{out_lbl}]")
    return out_lbl


def _render_intro_curtain_block(
    clip: ClipCandidate,
    profile: PodcasterProfile,
    joined_mp4: Path,
    tmp_dir: Path,
    width: int,
    height: int,
    cfg: Settings,
    encoder: str,
    font_path: Optional[Path],
    bg_image_path: Optional[Path],
) -> Path:
    """Render the dedicated opening curtain MP4 (muted podcast + optional AI TTS + transition SFX)."""
    ic = profile.intro_curtain
    dur_sec = max(1.0, float(clip.intro_curtain_duration_sec or ic.min_sec))
    cards_dir = tmp_dir / "intro_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    text_rgb = _hex_to_rgb(ic.text_color, (255, 255, 255))
    accent_rgb = _hex_to_rgb(ic.accent_color, (255, 212, 0))
    badge_label = (clip.intro_curtain_badge or ic.badge_text or "¿LO SABÍAS?").strip().upper()

    phrases = [p.strip() for p in (clip.intro_curtain_phrases or []) if p.strip()]
    spoken_full = (clip.intro_curtain_text or "").strip() or " ".join(phrases) or (
        clip.metadata.titles[0] if clip.metadata.titles else clip.summary
    )
    if not phrases:
        phrases = [spoken_full]

    def _draw_card_base(draw: ImageDraw.ImageDraw, card_box: tuple[int, int, int, int]) -> int:
        cx1, cy1, cx2, cy2 = card_box
        draw.rounded_rectangle(
            [(cx1, cy1), (cx2, cy2)],
            radius=32,
            fill=(10, 14, 24, 210),
            outline=(*accent_rgb, 235),
            width=4,
        )
        bar_w = int((cx2 - cx1) * 0.24)
        mid_x = (cx1 + cx2) // 2
        draw.rounded_rectangle(
            [(mid_x - bar_w // 2, cy1 + 18), (mid_x + bar_w // 2, cy1 + 26)],
            radius=4,
            fill=(*accent_rgb, 255),
        )
        if badge_label:
            bf_size = max(24, int(width * (0.029 if width <= height else 0.020)))
            f_badge = _load_pil_font(font_path, bf_size)
            bb = draw.textbbox((0, 0), badge_label, font=f_badge)
            bw = (bb[2] - bb[0]) + 46
            bh = (bb[3] - bb[1]) + 22
            bx = mid_x - bw // 2
            by = cy1 - bh // 2
            draw.rounded_rectangle([(bx, by), (bx + bw, by + bh)], radius=bh // 2, fill=(*accent_rgb, 255))
            draw.text(
                (mid_x - (bb[2] - bb[0]) // 2, by + 9),
                badge_label,
                font=f_badge,
                fill=(10, 14, 24, 255),
            )
            return cy1 + 48
        return cy1 + 36

    frame_entries: list[tuple[Path, float]] = []
    timings = list(clip.intro_curtain_word_timings or [])
    base_size = max(34, min(80, int(width * (0.062 if width <= height else 0.044))))
    if len(spoken_full.split()) >= 12:
        base_size = int(base_size * 0.88)
    font = _load_pil_font(font_path, base_size)
    max_line_w = int(width * (0.76 if width <= height else 0.70))

    if ic.display_mode == "multi_phrase" and len(phrases) > 1:
        # Compute phrase time slices using word timings when aligned, else word count weights
        phrase_durs: list[float] = []
        phrase_wc = [max(1, len(p.split())) for p in phrases]
        total_wc = sum(phrase_wc)
        usable_speech = max(0.9, dur_sec - 0.30)
        if timings and len(timings) >= max(2, int(total_wc * 0.6)):
            w_cursor = 0
            prev_t = 0.0
            for idx_p, wc in enumerate(phrase_wc):
                if idx_p == len(phrase_wc) - 1:
                    end_t = dur_sec
                else:
                    target_wi = min(len(timings) - 1, w_cursor + max(1, round(wc * (len(timings) / total_wc))) - 1)
                    next_s = float(timings[min(len(timings) - 1, target_wi + 1)].get("start", dur_sec))
                    end_t = min(dur_sec - 0.35 * (len(phrase_wc) - 1 - idx_p), max(prev_t + 0.55, next_s))
                    w_cursor = target_wi + 1
                phrase_durs.append(max(0.45, end_t - prev_t))
                prev_t = end_t
        else:
            for idx_p, wc in enumerate(phrase_wc):
                d = usable_speech * (wc / total_wc)
                if idx_p == len(phrase_wc) - 1:
                    d += max(0.25, dur_sec - usable_speech)
                phrase_durs.append(max(0.45, d))

        dummy = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ddraw = ImageDraw.Draw(dummy)
        space_w = int((ddraw.textbbox((0, 0), " ", font=font)[2] or 16) * 1.15)
        line_h = int(base_size * 1.24)

        for idx_p, (ptext, pdur) in enumerate(zip(phrases, phrase_durs)):
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            toks = [(i, t) for i, t in enumerate(ptext.split()) if t]
            wrapped = _wrap_tokens_to_pixel_lines(draw, toks, font, max_line_w, space_w)
            text_h = max(line_h, len(wrapped) * line_h)
            card_h = max(int(height * 0.22), text_h + 150)
            cy1 = (height - card_h) // 2
            cy2 = cy1 + card_h
            cx1 = int(width * (0.07 if width <= height else 0.11))
            cx2 = width - cx1
            _draw_card_base(draw, (cx1, cy1, cx2, cy2))

            ty = cy1 + (card_h - text_h) // 2 - 4
            for li, line_items in enumerate(wrapped):
                line_str = " ".join(t for _, t in line_items)
                lb = draw.textbbox((0, 0), line_str, font=font)
                lw = lb[2] - lb[0]
                lx = (width - lw) // 2
                draw.text((lx + 3, ty + li * line_h + 4), line_str, font=font, fill=(0, 0, 0, 190))
                draw.text((lx, ty + li * line_h), line_str, font=font, fill=(*text_rgb, 255))

            # Progress dots at card bottom
            dot_r = 7
            dot_gap = 26
            dots_w = len(phrases) * (dot_r * 2) + (len(phrases) - 1) * (dot_gap - dot_r * 2)
            dx = (width - dots_w) // 2
            dy = cy2 - 36
            for di in range(len(phrases)):
                fill_c = (*accent_rgb, 255) if di == idx_p else (*text_rgb, 90)
                draw.ellipse([(dx, dy), (dx + dot_r * 2, dy + dot_r * 2)], fill=fill_c)
                dx += dot_gap

            out_png = cards_dir / f"phrase_{idx_p:02d}.png"
            canvas.save(out_png, "PNG")
            frame_entries.append((out_png, pdur))
    else:
        # single_screen with smooth karaoke word highlights
        tokens = [t.strip() for t in spoken_full.split() if t.strip()]
        if not tokens:
            tokens = ["¡Mira esto!"]
        dummy = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ddraw = ImageDraw.Draw(dummy)
        space_w = int((ddraw.textbbox((0, 0), " ", font=font)[2] or 16) * 1.15)
        line_h = int(base_size * 1.24)
        wrapped = _wrap_tokens_to_pixel_lines(ddraw, list(enumerate(tokens)), font, max_line_w, space_w)
        text_h = max(line_h, len(wrapped) * line_h)
        card_h = max(int(height * 0.25), text_h + 150)
        cy1 = (height - card_h) // 2
        cy2 = cy1 + card_h
        cx1 = int(width * (0.07 if width <= height else 0.11))
        cx2 = width - cx1
        ty = cy1 + (card_h - text_h) // 2 + 4

        def _render_single_frame(active_i: int, png_out: Path) -> None:
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            _draw_card_base(draw, (cx1, cy1, cx2, cy2))
            for li, line_items in enumerate(wrapped):
                widths = [draw.textbbox((0, 0), tok, font=font)[2] - draw.textbbox((0, 0), tok, font=font)[0] for _, tok in line_items]
                total_lw = sum(widths) + space_w * max(0, len(line_items) - 1)
                cur_x = (width - total_lw) // 2
                cur_y = ty + li * line_h
                for (tok_idx, tok), tw in zip(line_items, widths):
                    fill_c = (*accent_rgb, 255) if tok_idx == active_i else ((*text_rgb, 255) if tok_idx < active_i or active_i < 0 else (*text_rgb, 200))
                    draw.text((cur_x + 3, cur_y + 4), tok, font=font, fill=(0, 0, 0, 190))
                    draw.text((cur_x, cur_y), tok, font=font, fill=fill_c)
                    cur_x += tw + space_w
            canvas.save(png_out, "PNG")

        if timings:
            cur_t = 0.0
            first_s = float(timings[0].get("start", 0.0))
            if first_s > 0.06:
                p0 = cards_dir / "ss_pre.png"
                _render_single_frame(-1, p0)
                frame_entries.append((p0, min(dur_sec, first_s)))
                cur_t = first_s
            n_t = min(len(tokens), len(timings))
            for wi in range(n_t):
                end_t = float(timings[wi + 1].get("start", dur_sec)) if wi + 1 < n_t else dur_sec
                step_d = max(0.05, end_t - cur_t)
                p = cards_dir / f"ss_{wi:03d}.png"
                _render_single_frame(wi, p)
                frame_entries.append((p, step_d))
                cur_t += step_d
        else:
            p = cards_dir / "ss_static.png"
            _render_single_frame(-1, p)
            frame_entries.append((p, dur_sec))

    manifest = cards_dir / "intro_frames.txt"
    mlines: list[str] = []
    for fp, fd in frame_entries:
        mlines.append(f"file '{fp.resolve().as_posix()}'")
        mlines.append(f"duration {fd:.4f}")
    mlines.append(f"file '{frame_entries[-1][0].resolve().as_posix()}'")
    manifest.write_text("\n".join(mlines) + "\n", encoding="utf-8")

    inputs: list[str] = []
    filter_steps: list[str] = []
    next_idx_ref = [0]
    bg_lbl = _build_curtain_background_layer(
        inputs=inputs,
        filter_steps=filter_steps,
        next_idx_ref=next_idx_ref,
        tmp_dir=tmp_dir,
        tag="intro",
        width=width,
        height=height,
        dur_sec=dur_sec,
        bg_mode=ic.bg_mode,
        primary_hex=ic.bg_primary_color,
        secondary_hex=ic.bg_secondary_color,
        overlay_opacity=ic.bg_overlay_opacity,
        bg_video_path=joined_mp4,
        bg_image_path=bg_image_path,
        video_seek_sec=0.0,
    )

    fg_idx = next_idx_ref[0]
    inputs.extend(["-f", "concat", "-safe", "0", "-i", str(manifest)])
    next_idx_ref[0] += 1
    filter_steps.append(f"[{fg_idx}:v]fps=30,format=rgba[intro_fg]")
    filter_steps.append(f"[{bg_lbl}][intro_fg]overlay=0:0:eof_action=repeat,format=yuv420p[v_intro]")

    has_tts_wav = bool(clip.intro_curtain_audio_path and Path(clip.intro_curtain_audio_path).exists())
    if has_tts_wav:
        tts_idx = next_idx_ref[0]
        inputs.extend(["-i", str(clip.intro_curtain_audio_path)])
        next_idx_ref[0] += 1
        filter_steps.append(
            f"[{tts_idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad=whole_dur={dur_sec:.3f},atrim=0:{dur_sec:.3f},asetpts=PTS-STARTPTS[a_tts]"
        )
    else:
        filter_steps.append(f"anullsrc=r=48000:cl=stereo:d={dur_sec:.3f}[a_tts]")

    a_out_lbl = "a_tts"
    if ic.transition_sfx:
        try:
            whoosh = get_or_create_whoosh_sfx(cfg)
            sfx_idx = next_idx_ref[0]
            inputs.extend(["-i", str(whoosh)])
            next_idx_ref[0] += 1
            delay_ms = max(0, int((dur_sec - 0.34) * 1000))
            filter_steps.append(
                f"[{sfx_idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms},apad=whole_dur={dur_sec:.3f},atrim=0:{dur_sec:.3f},asetpts=PTS-STARTPTS[a_sfx]"
            )
            filter_steps.append("[a_tts][a_sfx]amix=inputs=2:duration=first:normalize=0[a_intro]")
            a_out_lbl = "a_intro"
        except Exception:
            pass

    intro_mp4 = tmp_dir / "intro_part.mp4"
    _run_ffmpeg_with_fallback(
        cfg.ffmpeg_path,
        inputs,
        [
            "-filter_complex",
            ";".join(filter_steps),
            "-map",
            "[v_intro]",
            "-map",
            f"[{a_out_lbl}]",
            "-r",
            "30",
            "-video_track_timescale",
            "30000",
            "-t",
            f"{dur_sec:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(intro_mp4),
        ],
        encoder,
        cfg.ffmpeg_threads,
    )
    return intro_mp4


def _render_outro_window_png(
    out_png: Path,
    profile: PodcasterProfile,
    width: int,
    height: int,
    font_path: Optional[Path],
    outro_logo_path: Optional[Path] = None,
    include_backdrop: bool = False,
) -> Path:
    """Render closing CTA card PNG (optionally composited over tinted gradient backdrop)."""
    oc = profile.outro_curtain
    if include_backdrop:
        canvas = _render_gradient_rgba(
            width,
            height,
            oc.bg_primary_color,
            oc.bg_secondary_color,
            oc.bg_overlay_opacity,
            full_opaque=False,
        )
    else:
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    draw = ImageDraw.Draw(canvas)
    text_rgb = _hex_to_rgb(oc.text_color, (255, 255, 255))
    accent_rgb = _hex_to_rgb(oc.accent_color, (56, 189, 248))

    headline = (oc.headline_text or "SIGUE EL PODCAST COMPLETO").strip().upper()
    channel = (oc.channel_name_text or profile.podcast_name or profile.name).strip()
    platform_txt = (oc.platform_cta_text or f"Escúchalo en {profile.target_platform}").strip()

    f_head = _load_pil_font(font_path, max(24, int(width * (0.032 if width <= height else 0.022))))
    f_chan = _load_pil_font(font_path, max(38, int(width * (0.064 if width <= height else 0.044))))
    f_plat = _load_pil_font(font_path, max(26, int(width * (0.035 if width <= height else 0.024))))

    card_w = int(width * (0.86 if width <= height else 0.72))
    cx1 = (width - card_w) // 2
    cx2 = cx1 + card_w
    mid_x = width // 2

    chan_wrapped = "\n".join(textwrap.wrap(channel, width=18 if width <= height else 28)[:3])
    cb = draw.multiline_textbbox((0, 0), chan_wrapped, font=f_chan, align="center", spacing=10)
    chan_h = cb[3] - cb[1]

    plat_wrapped = "\n".join(textwrap.wrap(platform_txt, width=28 if width <= height else 42)[:2])
    pb = draw.multiline_textbbox((0, 0), plat_wrapped, font=f_plat, align="center", spacing=8)
    plat_w = pb[2] - pb[0]
    plat_h = pb[3] - pb[1]

    logo_size = int(min(width, height) * 0.16) if (oc.use_profile_outro_cta_logo and outro_logo_path and outro_logo_path.exists()) else 0
    logo_block = (logo_size + 28) if logo_size > 0 else 0
    card_h = logo_block + 56 + chan_h + plat_h + 135
    cy1 = max(40, (height - card_h) // 2)
    cy2 = min(height - 40, cy1 + card_h)

    draw.rounded_rectangle(
        [(cx1, cy1), (cx2, cy2)],
        radius=34,
        fill=(8, 12, 22, 224),
        outline=(*accent_rgb, 235),
        width=4,
    )

    cur_y = cy1 + 40
    if logo_size > 0 and outro_logo_path:
        try:
            with Image.open(outro_logo_path) as raw_logo:
                logo_im = ImageOps.fit(
                    ImageOps.exif_transpose(raw_logo).convert("RGBA"),
                    (logo_size, logo_size),
                    method=Image.Resampling.LANCZOS,
                )
            lx = mid_x - logo_size // 2
            canvas.paste(logo_im, (lx, cur_y), logo_im)
            draw.rounded_rectangle([(lx, cur_y), (lx + logo_size, cur_y + logo_size)], radius=18, outline=(*accent_rgb, 230), width=3)
            cur_y += logo_size + 28
        except Exception:
            pass

    hb = draw.textbbox((0, 0), headline, font=f_head)
    draw.text((mid_x - (hb[2] - hb[0]) // 2, cur_y), headline, font=f_head, fill=(*accent_rgb, 255))
    cur_y += (hb[3] - hb[1]) + 22

    cw = cb[2] - cb[0]
    draw.multiline_text((mid_x - cw // 2, cur_y), chan_wrapped, font=f_chan, fill=(*text_rgb, 255), align="center", spacing=10)
    cur_y += chan_h + 34

    pill_w = min(card_w - 64, plat_w + 68)
    pill_h = plat_h + 32
    px1 = mid_x - pill_w // 2
    draw.rounded_rectangle([(px1, cur_y), (px1 + pill_w, cur_y + pill_h)], radius=pill_h // 2, fill=(*accent_rgb, 255))
    draw.multiline_text(
        (mid_x - plat_w // 2, cur_y + 15),
        plat_wrapped,
        font=f_plat,
        fill=(10, 14, 24, 255),
        align="center",
        spacing=8,
    )

    canvas.save(out_png, "PNG")
    return out_png


def render_final_video(
    clip: ClipCandidate,
    profile: PodcasterProfile,
    output_mp4: Path,
    thumbnail_jpg: Path,
    store: ProfileStore,
    episode_images: Optional[list[CustomImage]] = None,
    episode_images_dir: Optional[Path] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    settings: Optional[Settings] = None,
    llm: Optional[OpenRouterClient] = None,
    visual_hook_style_override: Optional[str] = None,
) -> tuple[Path, Path]:
    """Assemble shots, transitions, watermark, full-screen visual hook, CTA, ASS subtitles, and clean audio."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")
    if not clip.clean_wav_path or not Path(clip.clean_wav_path).exists():
        raise RuntimeError(f"Falta audio limpio para {clip.id}")
    if not clip.storyboard or not clip.storyboard.shots:
        raise RuntimeError(f"Falta storyboard para {clip.id}")

    caps = detect_ffmpeg_capabilities(cfg)
    encoder = caps["selected_encoder"]
    width, height = resolution_for_aspect(clip.aspect_ratio)
    font_path = store.resolve_font_path(profile.id, profile.subtitles.font_file)

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_jpg.parent.mkdir(parents=True, exist_ok=True)

    # Resolve image lookup from profile + episode
    all_imgs = {img.id: img for img in [*profile.custom_images, *(episode_images or [])]}
    watermark_img = next(
        (img for img in [*profile.custom_images, *(episode_images or [])] if img.display_mode == "watermark"),
        None,
    )

    with tempfile.TemporaryDirectory(prefix=f"pf_render_{clip.id}_") as tmp_str:
        tmp_dir = Path(tmp_str)
        shots = clip.storyboard.shots
        nominal_durs = [max(0.3, round(sh.end - sh.start, 3)) for sh in shots]

        use_xfade = bool(profile.visual.enable_smooth_transitions and len(shots) > 1)
        t_dur = 0.0
        if use_xfade:
            t_dur = round(min(float(profile.visual.transition_sec), min(nominal_durs) * 0.30), 3)
            if t_dur < 0.08:
                use_xfade = False
                t_dur = 0.0

        def _render_all_shots(add_transition_tail: bool) -> list[Path]:
            rendered: list[Path] = []
            for idx, sh in enumerate(shots):
                custom_path: Optional[Path] = None
                if sh.custom_image_id or sh.custom_image_filename:
                    img_obj = all_imgs.get(sh.custom_image_id or "")
                    if img_obj is not None:
                        custom_path = store.resolve_custom_image_path(profile.id, img_obj, episode_images_dir)
                    elif sh.custom_image_filename:
                        for cand_dir in [episode_images_dir, store.images_dir(profile.id)]:
                            if cand_dir and (cand_dir / sh.custom_image_filename).exists():
                                custom_path = cand_dir / sh.custom_image_filename
                                break

                dur_ov = (
                    nominal_durs[idx] + t_dur
                    if add_transition_tail and idx < len(shots) - 1
                    else nominal_durs[idx]
                )
                seg_path = tmp_dir / f"seg_{idx:03d}_{'xf' if add_transition_tail else 'cat'}.mp4"
                _render_single_shot(
                    shot=sh,
                    shot_out=seg_path,
                    width=width,
                    height=height,
                    ffmpeg_path=cfg.ffmpeg_path,
                    encoder=encoder,
                    threads=cfg.ffmpeg_threads,
                    temp_dir=tmp_dir,
                    font_path=font_path,
                    highlight_color=profile.subtitles.highlight_color,
                    custom_image_path=custom_path,
                    dur_override=dur_ov,
                )
                rendered.append(seg_path)
                if progress_cb:
                    progress_cb(10.0 + (idx + 1) / len(shots) * 55.0, f"Renderizando plano {idx + 1}/{len(shots)}")
            return rendered

        shot_files = _render_all_shots(add_transition_tail=use_xfade)
        if progress_cb:
            progress_cb(68.0, "Concatenando y suavizando transiciones entre planos...")
        joined_mp4 = tmp_dir / "joined.mp4"
        if use_xfade:
            try:
                _join_shots_with_xfade(
                    shot_files=shot_files,
                    nominal_durs=nominal_durs,
                    t_dur=t_dur,
                    joined_mp4=joined_mp4,
                    ffmpeg_path=cfg.ffmpeg_path,
                    encoder=encoder,
                    threads=cfg.ffmpeg_threads,
                    tmp_dir=tmp_dir,
                )
            except Exception:
                # Fallback to clean nominal concat if xfade fails on an edge case
                fallback_shots = _render_all_shots(add_transition_tail=False)
                _join_shots_with_concat(fallback_shots, joined_mp4, cfg.ffmpeg_path, tmp_dir)
        else:
            _join_shots_with_concat(shot_files, joined_mp4, cfg.ffmpeg_path, tmp_dir)

        if progress_cb:
            progress_cb(75.0, "Aplicando subtítulos, gancho visual y mezcla final...")

        # Master composition: overlays (watermark, legacy visual_hook / CTA if no AI curtains) + ASS burn-in
        body_dur = float(clip.edl.edited_duration if clip.edl else clip.storyboard.duration)
        try:
            probed_body = get_audio_duration(Path(clip.clean_wav_path), cfg)
            if probed_body > 0.1:
                body_dur = probed_body
        except Exception:
            pass

        use_intro_curtain = bool(clip.intro_curtain_enabled and float(clip.intro_curtain_duration_sec or 0.0) > 0.2)
        use_outro_curtain = bool(clip.outro_curtain_enabled and float(clip.outro_curtain_duration_sec or 0.0) > 0.2)

        inputs: list[str] = ["-i", str(joined_mp4), "-i", str(clip.clean_wav_path)]
        filter_steps: list[str] = []
        v_cur = "0:v"
        next_idx_ref = [2]

        if watermark_img:
            wm_path = store.resolve_custom_image_path(profile.id, watermark_img, episode_images_dir)
            if wm_path and wm_path.exists():
                wm_i = next_idx_ref[0]
                inputs.extend(["-loop", "1", "-i", str(wm_path)])
                next_idx_ref[0] += 1
                wm_w = max(90, int(width * 0.13))
                filter_steps.append(f"[{wm_i}:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa=0.85[wm]")
                filter_steps.append(f"[{v_cur}][wm]overlay=W-w-36:36:shortest=1[vwm]")
                v_cur = "vwm"

        effective_hook_enabled = (
            False
            if use_intro_curtain
            else (
                clip.visual_hook_enabled
                if clip.visual_hook_enabled is not None
                else profile.subtitles.visual_hook_enabled
            )
        )
        effective_hook_end = float(clip.visual_hook_end_sec or 0.0) if effective_hook_enabled else 0.0
        hook_words: list[WordTimestamp] = []
        hook_spoken = (clip.visual_hook_spoken_text or "").strip()
        if effective_hook_enabled:
            if clip.mapped_words:
                calc_end, calc_text, hook_words = compute_visual_hook_segment(clip.mapped_words)
                if effective_hook_end <= 0.2:
                    effective_hook_end = calc_end
                else:
                    hook_words = [
                        w
                        for w in clip.mapped_words
                        if not w.censored and w.word.strip() and w.start < effective_hook_end - 0.03
                    ]
                if not hook_spoken:
                    hook_spoken = calc_text
            if effective_hook_end > 0.2 and (hook_words or hook_spoken):
                style_name = (
                    visual_hook_style_override
                    or profile.subtitles.visual_hook_style
                    or "kinetic_fullscreen"
                )
                hook_manifest = _render_fullscreen_hook_overlay(
                    tmp_dir=tmp_dir,
                    width=width,
                    height=height,
                    hook_end_sec=effective_hook_end,
                    hook_words=hook_words,
                    spoken_text=hook_spoken,
                    style_name=style_name,
                    bg_hex=profile.subtitles.visual_hook_bg_color,
                    text_hex=profile.subtitles.visual_hook_text_color,
                    accent_hex=profile.subtitles.visual_hook_accent_color,
                    bg_opacity=profile.subtitles.visual_hook_bg_opacity,
                    font_path=font_path,
                )
                if hook_manifest and hook_manifest.exists():
                    hk_i = next_idx_ref[0]
                    inputs.extend(["-f", "concat", "-safe", "0", "-i", str(hook_manifest)])
                    next_idx_ref[0] += 1
                    filter_steps.append(f"[{hk_i}:v]fps=30,format=rgba[vhk_in]")
                    filter_steps.append(
                        f"[{v_cur}][vhk_in]overlay=0:0:eof_action=pass:enable='between(t,0,{effective_hook_end:.3f})'[vhook]"
                    )
                    v_cur = "vhook"

        cta_msg = (
            clip.metadata.cliffhanger_question.strip()
            if clip.clip_type == "short_cliffhanger" and clip.metadata.cliffhanger_question.strip()
            else profile.subtitles.cta_text.strip()
        )
        if cta_msg and body_dur >= 4.0 and not use_outro_curtain:
            cta_png = tmp_dir / "cta_banner.png"
            render_overlay_banner_png(
                cta_png,
                width,
                height,
                text=cta_msg,
                position="bottom",
                font_path=font_path,
                accent_hex=profile.subtitles.highlight_color,
            )
            cta_start = max(effective_hook_end + 0.2 if effective_hook_enabled else 0.0, body_dur - 3.3)
            if cta_start < body_dur - 0.5:
                cta_i = next_idx_ref[0]
                inputs.extend(["-loop", "1", "-i", str(cta_png)])
                next_idx_ref[0] += 1
                filter_steps.append(
                    f"[{v_cur}][{cta_i}:v]overlay=0:0:enable='between(t,{cta_start:.2f},{body_dur:.2f})':shortest=1[vcta]"
                )
                v_cur = "vcta"

        if clip.ass_path and Path(clip.ass_path).exists():
            ass_esc = _escape_ffmpeg_filter_path(Path(clip.ass_path))
            fonts_dir_path = font_path.parent if font_path else cfg.fonts_dir
            fonts_esc = _escape_ffmpeg_filter_path(fonts_dir_path)
            filter_steps.append(f"[{v_cur}]ass=filename='{ass_esc}':fontsdir='{fonts_esc}'[vfinal]")
            v_cur = "vfinal"
        elif v_cur == "0:v":
            filter_steps.append("[0:v]null[vfinal]")
            v_cur = "vfinal"

        if not use_intro_curtain and not use_outro_curtain:
            filter_complex = ";".join(filter_steps)
            _run_ffmpeg_with_fallback(
                cfg.ffmpeg_path,
                inputs,
                [
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    f"[{v_cur}]",
                    "-map",
                    "1:a",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(output_mp4),
                ],
                encoder,
                cfg.ffmpeg_threads,
                timeout_sec=480,
                progress_cb=progress_cb,
                expected_duration_sec=body_dur,
                progress_range=(75.0, 93.0),
                progress_label="Aplicando subtítulos y mezcla final",
            )
            if progress_cb:
                progress_cb(94.0, "Seleccionando mejor miniatura local...")
            _select_best_thumbnail(
                output_mp4=output_mp4,
                dur=body_dur,
                hook_end_sec=effective_hook_end if effective_hook_enabled else 0.0,
                thumbnail_jpg=thumbnail_jpg,
                ffmpeg_path=cfg.ffmpeg_path,
                llm=llm,
                clip_title=clip.metadata.titles[0] if clip.metadata.titles else clip.slug,
                body_start_sec=0.0,
                body_end_sec=body_dur,
            )
        else:
            body_outro_mp4 = (tmp_dir / "body_outro.mp4") if use_intro_curtain else output_mp4
            a_bo_lbl = "a_bo"
            v_bo_lbl = "v_bo"
            total_bo_dur = body_dur

            if not use_outro_curtain:
                filter_steps.append(f"[{v_cur}]trim=0:{body_dur:.3f},setpts=PTS-STARTPTS,fps=30,setsar=1,format=yuv420p[{v_bo_lbl}]")
                filter_steps.append(
                    f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=0:{body_dur:.3f},asetpts=PTS-STARTPTS[{a_bo_lbl}]"
                )
            else:
                oc = profile.outro_curtain
                outro_dur = max(1.5, float(clip.outro_curtain_duration_sec or oc.duration_sec))
                outro_logo_img = next(
                    (img for img in [*profile.custom_images, *(episode_images or [])] if img.display_mode == "outro_cta"),
                    None,
                )
                outro_logo_path = (
                    store.resolve_custom_image_path(profile.id, outro_logo_img, episode_images_dir)
                    if outro_logo_img
                    else None
                )
                has_outro_audio = bool(clip.outro_curtain_audio_path and Path(clip.outro_curtain_audio_path).exists())
                is_custom_outro_music = bool(oc.custom_audio_filename and has_outro_audio)

                if oc.audio_mode == "duck_podcast_bg" and body_dur > 4.5:
                    overlap_sec = min(max(1.4, body_dur * 0.16), max(1.6, outro_dur * 0.55))
                    duck_start = max(0.5, body_dur - overlap_sec)
                    tail_sec = max(1.5, outro_dur - overlap_sec)
                    total_bo_dur = body_dur + tail_sec

                    outro_overlay_png = tmp_dir / "outro_duck_overlay.png"
                    _render_outro_window_png(
                        outro_overlay_png,
                        profile=profile,
                        width=width,
                        height=height,
                        font_path=font_path,
                        outro_logo_path=outro_logo_path,
                        include_backdrop=True,
                    )
                    ov_i = next_idx_ref[0]
                    inputs.extend(["-loop", "1", "-t", f"{total_bo_dur:.3f}", "-i", str(outro_overlay_png)])
                    next_idx_ref[0] += 1

                    filter_steps.append(
                        f"[{v_cur}]trim=0:{body_dur:.3f},setpts=PTS-STARTPTS,"
                        f"tpad=stop_mode=clone:stop_duration={tail_sec:.3f},"
                        f"trim=0:{total_bo_dur:.3f},setpts=PTS-STARTPTS,fps=30,format=rgba[vb_ext]"
                    )
                    filter_steps.append(
                        f"[{ov_i}:v]format=rgba,fade=t=in:st={duck_start:.3f}:d=0.45:alpha=1[ov_card]"
                    )
                    filter_steps.append(
                        f"[vb_ext][ov_card]overlay=0:0:enable='gte(t,{duck_start:.3f})',setsar=1,format=yuv420p[{v_bo_lbl}]"
                    )

                    fade_len = min(0.50, max(0.20, overlap_sec * 0.40))
                    tail_fade_st = max(0.0, overlap_sec - 0.55)
                    tail_fade_d = max(0.25, overlap_sec - tail_fade_st)
                    filter_steps.append(
                        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[pa1][pa2]"
                    )
                    filter_steps.append(f"[pa1]atrim=0:{duck_start:.3f},asetpts=PTS-STARTPTS[ab_hi]")
                    filter_steps.append(
                        f"[pa2]atrim={duck_start:.3f}:{body_dur:.3f},asetpts=PTS-STARTPTS,"
                        f"volume={float(oc.podcast_duck_volume_db):.1f}dB,"
                        f"afade=t=out:st={tail_fade_st:.3f}:d={tail_fade_d:.3f}[ab_lo]"
                    )
                    filter_steps.append(
                        f"[ab_hi][ab_lo]acrossfade=d={fade_len:.3f}:c1=tri:c2=tri,"
                        f"apad=whole_dur={total_bo_dur:.3f},atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[a_pod]"
                    )
                    if has_outro_audio:
                        oa_i = next_idx_ref[0]
                        if is_custom_outro_music:
                            inputs.extend(["-stream_loop", "-1", "-t", f"{outro_dur:.3f}", "-i", str(clip.outro_curtain_audio_path)])
                            vol_part = f"volume={float(oc.custom_audio_volume_db):.1f}dB,"
                            fade_part = (
                                f"afade=t=in:st=0:d=0.45,afade=t=out:st={max(0.2, outro_dur - 0.45):.3f}:d=0.45,"
                            )
                        else:
                            inputs.extend(["-i", str(clip.outro_curtain_audio_path)])
                            vol_part = ""
                            fade_part = ""
                        next_idx_ref[0] += 1
                        delay_ms = int((duck_start + 0.25) * 1000)
                        filter_steps.append(
                            f"[{oa_i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                            f"{vol_part}{fade_part}adelay={delay_ms}|{delay_ms},"
                            f"apad=whole_dur={total_bo_dur:.3f},atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[a_outro]"
                        )
                        filter_steps.append(
                            f"[a_pod][a_outro]amix=inputs=2:duration=first:normalize=0,atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[{a_bo_lbl}]"
                        )
                    else:
                        filter_steps.append(f"[a_pod]anull[{a_bo_lbl}]")
                else:
                    # no_overlap_lcut (clean body -> clean outro cut without voice collisions)
                    total_bo_dur = body_dur + outro_dur
                    outro_bg_img = _resolve_custom_bg_image_path(
                        oc.bg_image_id, all_imgs, profile.id, store, episode_images_dir
                    )
                    bg_out_lbl = _build_curtain_background_layer(
                        inputs=inputs,
                        filter_steps=filter_steps,
                        next_idx_ref=next_idx_ref,
                        tmp_dir=tmp_dir,
                        tag="outro",
                        width=width,
                        height=height,
                        dur_sec=outro_dur,
                        bg_mode=oc.bg_mode,
                        primary_hex=oc.bg_primary_color,
                        secondary_hex=oc.bg_secondary_color,
                        overlay_opacity=oc.bg_overlay_opacity,
                        bg_video_path=joined_mp4,
                        bg_image_path=outro_bg_img,
                        video_seek_sec=max(0.0, body_dur - outro_dur),
                    )
                    outro_card_png = tmp_dir / "outro_card.png"
                    _render_outro_window_png(
                        outro_card_png,
                        profile=profile,
                        width=width,
                        height=height,
                        font_path=font_path,
                        outro_logo_path=outro_logo_path,
                        include_backdrop=False,
                    )
                    oc_i = next_idx_ref[0]
                    inputs.extend(["-loop", "1", "-t", f"{outro_dur:.3f}", "-i", str(outro_card_png)])
                    next_idx_ref[0] += 1

                    filter_steps.append(
                        f"[{oc_i}:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[oc_fg]"
                    )
                    filter_steps.append(
                        f"[{bg_out_lbl}][oc_fg]overlay=0:0:shortest=1,fps=30,setsar=1,format=yuv420p[v_outro_fin]"
                    )
                    filter_steps.append(
                        f"[{v_cur}]trim=0:{body_dur:.3f},setpts=PTS-STARTPTS,fps=30,setsar=1,format=yuv420p[v_bclean]"
                    )
                    filter_steps.append(f"[v_bclean][v_outro_fin]concat=n=2:v=1:a=0[{v_bo_lbl}]")

                    filter_steps.append(
                        f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                        f"atrim=0:{body_dur:.3f},asetpts=PTS-STARTPTS,"
                        f"afade=t=out:st={max(0.0, body_dur - 0.12):.3f}:d=0.12,"
                        f"apad=whole_dur={total_bo_dur:.3f},atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[a_pod]"
                    )
                    if has_outro_audio:
                        oa_i = next_idx_ref[0]
                        if is_custom_outro_music:
                            lcut_pre = min(1.1, max(0.0, body_dur * 0.25))
                            mus_dur = outro_dur + lcut_pre
                            inputs.extend(["-stream_loop", "-1", "-t", f"{mus_dur:.3f}", "-i", str(clip.outro_curtain_audio_path)])
                            delay_ms = max(0, int((body_dur - lcut_pre) * 1000))
                            filter_steps.append(
                                f"[{oa_i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                                f"volume={float(oc.custom_audio_volume_db):.1f}dB,"
                                f"afade=t=in:st=0:d={max(0.25, lcut_pre):.3f},"
                                f"afade=t=out:st={max(0.2, mus_dur - 0.45):.3f}:d=0.45,"
                                f"adelay={delay_ms}|{delay_ms},"
                                f"apad=whole_dur={total_bo_dur:.3f},atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[a_outro]"
                            )
                        else:
                            inputs.extend(["-i", str(clip.outro_curtain_audio_path)])
                            delay_ms = int((body_dur + 0.08) * 1000)
                            filter_steps.append(
                                f"[{oa_i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                                f"adelay={delay_ms}|{delay_ms},"
                                f"apad=whole_dur={total_bo_dur:.3f},atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[a_outro]"
                            )
                        next_idx_ref[0] += 1
                        filter_steps.append(
                            f"[a_pod][a_outro]amix=inputs=2:duration=first:normalize=0,atrim=0:{total_bo_dur:.3f},asetpts=PTS-STARTPTS[{a_bo_lbl}]"
                        )
                    else:
                        filter_steps.append(f"[a_pod]anull[{a_bo_lbl}]")

            bo_out_args = [
                "-filter_complex",
                ";".join(filter_steps),
                "-map",
                f"[{v_bo_lbl}]",
                "-map",
                f"[{a_bo_lbl}]",
                "-r",
                "30",
                "-video_track_timescale",
                "30000",
                "-t",
                f"{total_bo_dur:.3f}",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
            if not use_intro_curtain:
                bo_out_args.extend(["-movflags", "+faststart"])
            bo_out_args.append(str(body_outro_mp4))
            _run_ffmpeg_with_fallback(
                cfg.ffmpeg_path,
                inputs,
                bo_out_args,
                encoder,
                cfg.ffmpeg_threads,
                timeout_sec=480,
                progress_cb=progress_cb,
                expected_duration_sec=total_bo_dur,
                progress_range=(75.0, 86.0 if use_intro_curtain else 93.0),
                progress_label="Aplicando subtítulos, cierre y mezcla final",
            )

            intro_dur_actual = 0.0
            if use_intro_curtain:
                if progress_cb:
                    progress_cb(86.0, "Renderizando cortinilla de contexto IA y ensamblando...")
                ic_bg_img = _resolve_custom_bg_image_path(
                    profile.intro_curtain.bg_image_id, all_imgs, profile.id, store, episode_images_dir
                )
                intro_mp4 = _render_intro_curtain_block(
                    clip=clip,
                    profile=profile,
                    joined_mp4=joined_mp4,
                    tmp_dir=tmp_dir,
                    width=width,
                    height=height,
                    cfg=cfg,
                    encoder=encoder,
                    font_path=font_path,
                    bg_image_path=ic_bg_img,
                )
                intro_dur_actual = float(clip.intro_curtain_duration_sec or profile.intro_curtain.min_sec)
                expected_final_dur = intro_dur_actual + total_bo_dur

                # Try zero-CPU stream-copy concat first (both parts share encoder, 30fps, timescale 30000, 48kHz AAC)
                concat_ib = tmp_dir / "concat_intro_body.txt"
                concat_ib.write_text(
                    f"file '{intro_mp4.resolve().as_posix()}'\nfile '{body_outro_mp4.resolve().as_posix()}'\n",
                    encoding="utf-8",
                )
                copy_ok = False
                copy_proc = subprocess.run(
                    [
                        str(cfg.ffmpeg_path),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat_ib),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(output_mp4),
                    ],
                    capture_output=True,
                    text=True,
                    **_low_priority_kwargs(),
                    check=False,
                )
                if copy_proc.returncode == 0 and output_mp4.exists() and output_mp4.stat().st_size > 4096:
                    try:
                        probed_cp = get_audio_duration(output_mp4, cfg)
                        if abs(probed_cp - expected_final_dur) <= 0.45:
                            copy_ok = True
                    except Exception:
                        pass

                if not copy_ok:
                    concat_fc = (
                        f"[0:v]fps=30,scale={width}:{height},setsar=1,format=yuv420p[iv];"
                        f"[1:v]fps=30,scale={width}:{height},setsar=1,format=yuv420p[bv];"
                        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[ia];"
                        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[ba];"
                        "[iv][ia][bv][ba]concat=n=2:v=1:a=1[vfinal_all][afinal_all]"
                    )
                    _run_ffmpeg_with_fallback(
                        cfg.ffmpeg_path,
                        ["-i", str(intro_mp4), "-i", str(body_outro_mp4)],
                        [
                            "-filter_complex",
                            concat_fc,
                            "-map",
                            "[vfinal_all]",
                            "-map",
                            "[afinal_all]",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            "-movflags",
                            "+faststart",
                            str(output_mp4),
                        ],
                        encoder,
                        cfg.ffmpeg_threads,
                        timeout_sec=480,
                        progress_cb=progress_cb,
                        expected_duration_sec=expected_final_dur,
                        progress_range=(87.0, 93.0),
                        progress_label="Ensamblando cortinilla y vídeo final",
                    )

            final_dur = intro_dur_actual + total_bo_dur
            try:
                probed_fin = get_audio_duration(output_mp4, cfg)
                if probed_fin > 0.5:
                    final_dur = probed_fin
            except Exception:
                pass

            if progress_cb:
                progress_cb(94.0, "Seleccionando mejor miniatura local...")
            _select_best_thumbnail(
                output_mp4=output_mp4,
                dur=final_dur,
                hook_end_sec=effective_hook_end if effective_hook_enabled else 0.0,
                thumbnail_jpg=thumbnail_jpg,
                ffmpeg_path=cfg.ffmpeg_path,
                llm=llm,
                clip_title=clip.metadata.titles[0] if clip.metadata.titles else clip.slug,
                body_start_sec=intro_dur_actual,
                body_end_sec=intro_dur_actual + body_dur,
            )

    if progress_cb:
        progress_cb(100.0, f"Vídeo completado: {output_mp4.name}")

    return output_mp4, thumbnail_jpg
