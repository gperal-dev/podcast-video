"""Unit and integration tests for multi-engine TTS, cached profile outro CTA, and transition SFX."""

from __future__ import annotations

from pathlib import Path
import wave

from backend.config import get_settings
from backend.core.profile_store import ProfileStore
from backend.core.tts_engine import (
    get_or_create_profile_outro_tts,
    get_or_create_whoosh_sfx,
    list_tts_catalog,
    resolve_default_outro_tts_text,
    synthesize_speech,
)
from backend.models.profile import PodcasterProfile


def test_tts_catalog_lists_all_three_engines() -> None:
    cat = list_tts_catalog(get_settings())
    ids = {e["id"] for e in cat["engines"]}
    assert ids == {"edge_tts", "openrouter_audio", "offline_local"}
    for eng in cat["engines"]:
        assert len(eng["voices"]) >= 2


def test_edge_tts_synthesis_and_word_timings(tmp_path: Path) -> None:
    settings = get_settings()
    out_wav = tmp_path / "edge_hook.wav"
    res = synthesize_speech(
        text="¿Sabías este dato sorprendente sobre el primer vuelo nocturno?",
        out_wav_path=out_wav,
        engine="edge_tts",
        voice="es-ES-AlvaroNeural",
        rate="+6%",
        settings=settings,
    )
    assert out_wav.exists() and out_wav.stat().st_size > 4096
    assert res["duration_sec"] > 1.2
    assert len(res["word_timings"]) >= 4
    with wave.open(str(out_wav), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 44100


def test_profile_outro_tts_generated_once_and_cached(tmp_path: Path) -> None:
    cfg = get_settings().model_copy(update={"profiles_dir": tmp_path / "profiles", "base_dir": tmp_path})
    store = ProfileStore(cfg)
    profile = PodcasterProfile(
        id="tts_cache_test",
        name="TTS Cache Test",
        podcast_name="Historia Viva",
        target_platform="iVoox",
    )
    # Use edge_tts (fast neural) — 2nd call must be an instant cache hit with identical hash & file
    store.create_profile(profile)
    assert "iVoox" in resolve_default_outro_tts_text(profile)
    assert "Historia Viva" in resolve_default_outro_tts_text(profile)

    p1, dur1, h1, hit1 = get_or_create_profile_outro_tts(profile, store, settings=cfg)
    assert not hit1
    assert p1.exists() and p1.stat().st_size > 4096
    assert dur1 > 1.5

    mtime1 = p1.stat().st_mtime_ns
    p2, dur2, h2, hit2 = get_or_create_profile_outro_tts(profile, store, settings=cfg)
    assert hit2 is True
    assert p2 == p1 and h2 == h1 and abs(dur2 - dur1) < 0.05
    assert p2.stat().st_mtime_ns == mtime1

    # Editing text invalidates hash cleanly
    profile.outro_curtain.tts_text = "Escucha el episodio completo en iVoox."
    p3, _, h3, hit3 = get_or_create_profile_outro_tts(profile, store, settings=cfg)
    assert hit3 is False
    assert h3 != h1
    assert p3.exists()


def test_whoosh_sfx_generation(tmp_path: Path) -> None:
    cfg = get_settings().model_copy(update={"base_dir": tmp_path})
    whoosh = get_or_create_whoosh_sfx(cfg)
    assert whoosh.exists() and whoosh.stat().st_size > 2048


def test_openrouter_audio_records_cost_usd_without_type_error(tmp_path: Path, monkeypatch) -> None:
    import base64
    from types import SimpleNamespace
    import sys

    from backend.core.openrouter_client import CostTracker
    from backend.core.tts_engine import _synthesize_openrouter_audio

    pcm_payload = base64.b64encode(b"\x01\x00" * 1024).decode("ascii")
    chunks = [
        SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=450),
            choices=[SimpleNamespace(delta=SimpleNamespace(audio={"data": pcm_payload}))],
        )
    ]

    class _FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: iter(chunks))
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))

    cfg = get_settings().model_copy(update={"base_dir": tmp_path, "openrouter_api_key": "sk-test-fake"})
    tracker = CostTracker()
    fake_or_client = SimpleNamespace(cost_tracker=tracker)
    out_raw = tmp_path / "openrouter_raw.wav"

    _synthesize_openrouter_audio(
        text="Prueba de narración en audio.",
        voice="alloy",
        openrouter_audio_model="openai/gpt-audio-mini",
        raw_wav_path=out_raw,
        settings=cfg,
        openrouter_client=fake_or_client,
    )

    assert out_raw.exists() and out_raw.stat().st_size > 1024
    assert tracker.metrics.total_calls == 1
    assert tracker.metrics.cost_usd > 0.0

