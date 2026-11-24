"""End-to-end synthetic rendering test verifying audio EDL + ASS subtitles + Ken Burns + FFprobe output."""

from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from backend.config import get_settings
from backend.core.audio_analyzer import detect_silences
from backend.core.audio_editor import build_edl, render_clean_audio
from backend.core.profile_store import ProfileStore
from backend.core.subtitle_engine import generate_ass_from_words
from backend.core.video_renderer import compute_visual_hook_segment, render_final_video
from backend.models.job import (
    ClipCandidate,
    ClipMetadata,
    StoryboardPlan,
    StoryboardShot,
    WordTimestamp,
)
from backend.models.profile import CustomImage, PodcasterProfile


def _create_synthetic_wav(path: Path) -> None:
    sr = 48000
    dur = 8.0
    t = np.linspace(0.0, dur, int(sr * dur), endpoint=False)
    sig = np.zeros_like(t, dtype=np.float32)
    # Burst 1 (0..2.5s), 1.5s silence (2.5..4.0s), Burst 2 (4.0..8.0s)
    sig[(t >= 0.0) & (t < 2.5)] = 0.2 * np.sin(2.0 * math.pi * 440.0 * t[(t >= 0.0) & (t < 2.5)])
    sig[(t >= 4.0) & (t < 8.0)] = 0.2 * np.sin(2.0 * math.pi * 520.0 * t[(t >= 4.0) & (t < 8.0)])
    pcm = np.clip(sig * 32767.0, -32767, 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm]).ravel()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def test_e2e_synthetic_short_video_render(tmp_path: Path) -> None:
    settings = get_settings()
    store = ProfileStore(settings)

    # Create temporary profile folder inside tmp_path via ProfileStore monkeypatch-free setup
    local_settings = settings.model_copy(update={"profiles_dir": tmp_path / "profiles"})
    local_store = ProfileStore(local_settings)
    profile = PodcasterProfile(id="synth_test", name="Synth Test", podcast_name="Synth Pod")
    profile.audio.censored_words = ["secreto"]

    # Create synthetic custom image
    img_dir = local_store.images_dir(profile.id)
    custom_img_file = img_dir / "host.png"
    Image.new("RGB", (900, 1200), (45, 70, 140)).save(custom_img_file, "PNG")
    profile.custom_images.append(
        CustomImage(
            id="img_host",
            filename="host.png",
            title="Host",
            usage_instructions="Presentador",
            priority="alta",
            display_mode="fullscreen_zoom",
        )
    )
    local_store.save_profile(profile)

    src_wav = tmp_path / "podcast.wav"
    _create_synthetic_wav(src_wav)
    silences = detect_silences(src_wav, threshold_db=-38.0, min_duration=0.7, settings=settings)

    words = [
        WordTimestamp(word="Hola", start=0.2, end=0.7),
        WordTimestamp(word="amigos", start=0.8, end=1.4),
        WordTimestamp(word="secreto", start=1.5, end=2.0),
        WordTimestamp(word="escuchen", start=4.2, end=5.0),
        WordTimestamp(word="esto", start=5.1, end=6.0),
        WordTimestamp(word="ahora.", start=6.1, end=7.5),
    ]

    edl, _, mapped_words = build_edl("clip_synth", 0.0, 8.0, words, silences, profile.audio)
    clean_wav = tmp_path / "clean.wav"
    render_clean_audio(src_wav, edl, clean_wav, profile.audio, settings=settings)

    hook_end, hook_spoken, hook_words = compute_visual_hook_segment(mapped_words, max_hook_sec=3.0, min_hook_sec=1.2)
    assert hook_end > 1.2
    assert "Hola" in hook_spoken
    assert len(hook_words) >= 2

    ass_path = tmp_path / "subs.ass"
    font_path = settings.fonts_dir / profile.subtitles.font_file
    generate_ass_from_words(
        mapped_words,
        ass_path,
        "9:16",
        profile.subtitles,
        is_short=True,
        font_path=font_path,
        skip_before_sec=hook_end,
    )
    ass_text = ass_path.read_text(encoding="utf-8")
    assert "AMIGOS" not in ass_text
    assert "ESCUCHEN" in ass_text

    dur = edl.edited_duration
    sb = StoryboardPlan(
        clip_id="clip_synth",
        aspect_ratio="9:16",
        duration=dur,
        shots=[
            StoryboardShot(
                id="shot_01",
                start=0.0,
                end=round(dur * 0.5, 2),
                source_type="custom_image",
                custom_image_id="img_host",
                custom_image_filename="host.png",
                display_mode="fullscreen_zoom",
            ),
            StoryboardShot(
                id="shot_02",
                start=round(dur * 0.5, 2),
                end=round(dur, 2),
                source_type="title_card",
                card_title="Momento clave del podcast",
                card_subtitle="Suscribete para mas",
            ),
        ],
    )

    clip = ClipCandidate(
        id="clip_synth",
        clip_type="short_cliffhanger",
        aspect_ratio="9:16",
        score=95,
        slug="momento_clave",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=0.0,
        orig_end=8.0,
        visual_hook_enabled=True,
        visual_hook_end_sec=hook_end,
        visual_hook_spoken_text=hook_spoken,
        intro_curtain_enabled=False,
        outro_curtain_enabled=False,
        edl=edl,
        mapped_words=mapped_words,
        clean_wav_path=str(clean_wav),
        ass_path=str(ass_path),
        storyboard=sb,
        metadata=ClipMetadata(
            titles=["Momento clave"],
            hook_text="No vas a creer esto",
            cliffhanger_question="¿Qué pasó después?",
        ),
    )

    out_mp4 = tmp_path / "output" / "95_short_cliffhanger_momento_clave.mp4"
    thumb_jpg = tmp_path / "output" / "95_short_cliffhanger_momento_clave.jpg"

    render_final_video(
        clip=clip,
        profile=profile,
        output_mp4=out_mp4,
        thumbnail_jpg=thumb_jpg,
        store=local_store,
        settings=settings,
    )

    assert out_mp4.exists() and out_mp4.stat().st_size > 10_000
    assert thumb_jpg.exists() and thumb_jpg.stat().st_size > 1_000

    # Verify with ffprobe: 1080x1920 h264 + aac + duration matches EDL
    probe = subprocess.run(
        [
            str(settings.ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            str(out_mp4),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    streams = info["streams"]
    v_stream = next(s for s in streams if s["codec_type"] == "video")
    a_stream = next(s for s in streams if s["codec_type"] == "audio")
    assert v_stream["width"] == 1080 and v_stream["height"] == 1920
    assert v_stream["codec_name"] == "h264"
    assert a_stream["codec_name"] == "aac"
    rendered_dur = float(info["format"]["duration"])
    assert abs(rendered_dur - dur) < 0.35


def _write_tone_wav(path: Path, dur_sec: float, freq: float = 440.0) -> None:
    sr = 44100
    t = np.linspace(0.0, dur_sec, int(sr * dur_sec), endpoint=False)
    sig = 0.2 * np.sin(2.0 * math.pi * freq * t)
    pcm = np.clip(sig * 32767.0, -32767, 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm]).ravel()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def test_e2e_synthetic_intro_and_outro_curtains_both_modes(tmp_path: Path) -> None:
    settings = get_settings()
    local_settings = settings.model_copy(update={"profiles_dir": tmp_path / "profiles", "base_dir": tmp_path})
    local_store = ProfileStore(local_settings)
    profile = PodcasterProfile(
        id="curtain_test",
        name="Curtain Test",
        podcast_name="Pod Ciencia",
        target_platform="iVoox",
    )
    img_dir = local_store.images_dir(profile.id)
    logo_file = img_dir / "logo_outro.png"
    Image.new("RGB", (600, 600), (220, 140, 40)).save(logo_file, "PNG")
    profile.custom_images.append(
        CustomImage(
            id="img_logo",
            filename="logo_outro.png",
            title="Logo Pod",
            usage_instructions="Logo cierre",
            priority="alta",
            display_mode="outro_cta",
        )
    )
    local_store.save_profile(profile)

    src_wav = tmp_path / "podcast.wav"
    _create_synthetic_wav(src_wav)
    words = [
        WordTimestamp(word="Bienvenidos", start=0.2, end=0.8),
        WordTimestamp(word="al", start=0.9, end=1.2),
        WordTimestamp(word="experimento", start=1.3, end=2.1),
        WordTimestamp(word="final", start=4.2, end=5.0),
        WordTimestamp(word="completo.", start=5.1, end=6.2),
    ]
    edl, _, mapped_words = build_edl("clip_curtain", 0.0, 8.0, words, [], profile.audio)
    clean_wav = tmp_path / "clean.wav"
    render_clean_audio(src_wav, edl, clean_wav, profile.audio, settings=local_settings)
    body_dur = edl.edited_duration

    ass_path = tmp_path / "subs.ass"
    font_path = settings.fonts_dir / profile.subtitles.font_file
    generate_ass_from_words(
        mapped_words, ass_path, "9:16", profile.subtitles, is_short=True, font_path=font_path, skip_before_sec=0.0
    )

    intro_wav = tmp_path / "intro_tts.wav"
    _write_tone_wav(intro_wav, 2.4, freq=520.0)
    outro_wav = tmp_path / "outro_tts.wav"
    _write_tone_wav(outro_wav, 2.2, freq=330.0)

    sb = StoryboardPlan(
        clip_id="clip_curtain",
        aspect_ratio="9:16",
        duration=body_dur,
        shots=[
            StoryboardShot(
                id="shot_01",
                start=0.0,
                end=round(body_dur, 2),
                source_type="title_card",
                card_title="Experimento de laboratorio",
                card_subtitle="Pod Ciencia",
            )
        ],
    )

    # Mode 1: no_overlap_lcut + multi_phrase + blurred_clip_video
    profile.intro_curtain.display_mode = "multi_phrase"
    profile.intro_curtain.bg_mode = "blurred_clip_video"
    profile.outro_curtain.audio_mode = "no_overlap_lcut"
    out_dur = 3.0
    clip1 = ClipCandidate(
        id="clip_curtain_1",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=92,
        slug="curtain_1",
        start_sentence_id="S1",
        end_sentence_id="S2",
        orig_start=0.0,
        orig_end=8.0,
        visual_hook_enabled=False,
        intro_curtain_enabled=True,
        intro_curtain_text="¿Sabías cómo empezó este gran experimento histórico?",
        intro_curtain_phrases=["¿Sabías cómo empezó", "este gran experimento histórico?"],
        intro_curtain_badge="¿LO SABÍAS?",
        intro_curtain_audio_path=str(intro_wav),
        intro_curtain_duration_sec=2.6,
        outro_curtain_enabled=True,
        outro_curtain_audio_path=str(outro_wav),
        outro_curtain_duration_sec=out_dur,
        edl=edl,
        mapped_words=mapped_words,
        clean_wav_path=str(clean_wav),
        ass_path=str(ass_path),
        storyboard=sb,
        metadata=ClipMetadata(titles=["Experimento 1"]),
    )
    out1 = tmp_path / "out_lcut.mp4"
    th1 = tmp_path / "th_lcut.jpg"
    render_final_video(clip1, profile, out1, th1, local_store, settings=local_settings)
    assert out1.exists() and th1.exists()

    pr1 = subprocess.run(
        [str(settings.ffprobe_path), "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(out1)],
        capture_output=True, text=True, check=True,
    )
    inf1 = json.loads(pr1.stdout)
    assert {s["codec_type"] for s in inf1["streams"]} >= {"video", "audio"}
    d1 = float(inf1["format"]["duration"])
    assert abs(d1 - (2.6 + body_dur + out_dur)) < 0.55

    # Mode 2: duck_podcast_bg + single_screen + solid_gradient
    profile.intro_curtain.display_mode = "single_screen"
    profile.intro_curtain.bg_mode = "solid_gradient"
    profile.outro_curtain.audio_mode = "duck_podcast_bg"
    out2 = tmp_path / "out_duck.mp4"
    th2 = tmp_path / "th_duck.jpg"
    render_final_video(clip1, profile, out2, th2, local_store, settings=local_settings)
    assert out2.exists() and th2.exists()

    pr2 = subprocess.run(
        [str(settings.ffprobe_path), "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(out2)],
        capture_output=True, text=True, check=True,
    )
    inf2 = json.loads(pr2.stdout)
    assert {s["codec_type"] for s in inf2["streams"]} >= {"video", "audio"}
    d2 = float(inf2["format"]["duration"])
    assert d2 > 2.6 + body_dur + 0.8
