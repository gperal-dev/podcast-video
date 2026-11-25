"""Text-to-Speech (TTS) engine for intro curtains, cached profile outro CTAs, and voice previews.

Supports three selectable engines per podcaster profile:
1. `edge_tts`: Microsoft Edge Neural HD voices (free, high-naturalness Spanish, word timings).
2. `openrouter_audio`: OpenRouter native audio (`openai/gpt-audio-mini` / `openai/gpt-audio`, PCM16 stream).
3. `offline_local`: 100% local offline synthesis using Piper ONNX neural voices (with automatic
   Windows SAPI `System.Speech` offline fallback so offline mode never fails).
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
import wave
from typing import Any, Optional

import httpx

from backend.config import Settings, get_settings
from backend.core.audio_analyzer import get_audio_duration
from backend.models.profile import PodcasterProfile

logger = logging.getLogger(__name__)


TTS_CATALOG: dict[str, dict[str, Any]] = {
    "edge_tts": {
        "id": "edge_tts",
        "label": "Neural HD Gratuita (Edge TTS)",
        "cost_note": "Gratuita · Voces neuronales en español + sincronización exacta por palabra",
        "requires_api_key": False,
        "voices": [
            {"id": "es-ES-AlvaroNeural", "label": "Álvaro (España · Masculina HD)", "gender": "M"},
            {"id": "es-ES-ElviraNeural", "label": "Elvira (España · Femenina HD)", "gender": "F"},
            {"id": "es-MX-JorgeNeural", "label": "Jorge (Latam · Masculina HD)", "gender": "M"},
            {"id": "es-MX-DaliaNeural", "label": "Dalia (Latam · Femenina HD)", "gender": "F"},
            {"id": "es-AR-TomasNeural", "label": "Tomás (Argentina · Masculina HD)", "gender": "M"},
        ],
    },
    "openrouter_audio": {
        "id": "openrouter_audio",
        "label": "OpenRouter Audio (GPT Audio Mini / Ultra Realista)",
        "cost_note": "Usa tu OPENROUTER_API_KEY · Ultra expresiva para radio/podcast (~$0.002/clip)",
        "requires_api_key": True,
        "voices": [
            {"id": "ash", "label": "Ash (Grave · Documental / Podcast)", "gender": "M"},
            {"id": "coral", "label": "Coral (Clara · Presentadora)", "gender": "F"},
            {"id": "sage", "label": "Sage (Equilibrada · Narración)", "gender": "F"},
            {"id": "ballad", "label": "Ballad (Expresiva · Storytelling)", "gender": "M"},
            {"id": "verse", "label": "Verse (Dinámica · Hooks cortos)", "gender": "M"},
            {"id": "alloy", "label": "Alloy (Neutral clásica)", "gender": "N"},
            {"id": "echo", "label": "Echo (Cálida)", "gender": "M"},
            {"id": "shimmer", "label": "Shimmer (Suave)", "gender": "F"},
        ],
    },
    "offline_local": {
        "id": "offline_local",
        "label": "Local 100% Offline (Piper Neural / SAPI)",
        "cost_note": "Gratuita · Sin nube ni API keys · Ideal si trabajas sin conexión",
        "requires_api_key": False,
        "voices": [
            {"id": "es_ES-davefx-medium", "label": "DaveFX (España · Masculina Local)", "gender": "M"},
            {"id": "es_ES-sharvard-medium", "label": "Sharvard (España · Femenina Local)", "gender": "F"},
        ],
    },
}

_PIPER_VOICE_URLS: dict[str, tuple[str, str]] = {
    "es_ES-davefx-medium": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx.json",
    ),
    "es_ES-sharvard-medium": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx.json",
    ),
}


def list_tts_catalog(settings: Optional[Settings] = None) -> dict[str, Any]:
    """Return engine/voice catalog with runtime availability flags."""
    cfg = settings or get_settings()
    has_openrouter = bool(cfg.openrouter_api_key and cfg.openrouter_api_key.strip())
    engines = []
    for engine_id, spec in TTS_CATALOG.items():
        configured = True if not spec["requires_api_key"] else has_openrouter
        engines.append({**spec, "configured": configured})
    return {
        "engines": engines,
        "sample_text_default": "¿Sabías este dato sorprendente? Quédate, porque lo que vas a escuchar cambia todo.",
    }


def _run_coro_sync(coro_factory):
    """Execute an async coroutine safely whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        return asyncio.run(coro_factory())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def _tts_cache_dir(settings: Settings) -> Path:
    d = settings.base_dir / "cache" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_rate_to_piper_length_scale(rate: str) -> float:
    """Convert '+6%' / '-10%' rate string into Piper length_scale (1.0 = normal, <1.0 = faster)."""
    m = re.match(r"^\s*([+-]?\d+)\s*%?\s*$", str(rate or "0"))
    if not m:
        return 1.0
    pct = max(-50, min(60, int(m.group(1))))
    return max(0.65, min(1.5, 1.0 / (1.0 + pct / 100.0)))


def _parse_rate_to_sapi_rate(rate: str) -> int:
    """Convert '+6%' / '-10%' rate string into SAPI rate (-10..10)."""
    m = re.match(r"^\s*([+-]?\d+)\s*%?\s*$", str(rate or "0"))
    if not m:
        return 1
    pct = max(-50, min(80, int(m.group(1))))
    return max(-5, min(5, round(pct / 12.0)))


def _synthesize_edge_tts(text: str, voice: str, rate: str, raw_mp3_path: Path) -> list[dict[str, Any]]:
    """Synthesize speech using Microsoft Edge neural voices and capture word boundaries."""
    import edge_tts

    valid_voices = {v["id"] for v in TTS_CATALOG["edge_tts"]["voices"]}
    chosen_voice = voice if voice in valid_voices else "es-ES-AlvaroNeural"
    safe_rate = rate if re.match(r"^[+-]\d+%$", str(rate or "").strip()) else "+6%"

    async def _run() -> list[dict[str, Any]]:
        comm = edge_tts.Communicate(
            text=text,
            voice=chosen_voice,
            rate=safe_rate,
            boundary="WordBoundary",
        )
        timings: list[dict[str, Any]] = []
        with raw_mp3_path.open("wb") as out_f:
            async for chunk in comm.stream():
                ctype = chunk.get("type")
                if ctype == "audio":
                    out_f.write(chunk["data"])
                elif ctype == "WordBoundary":
                    start_s = float(chunk.get("offset", 0)) / 10_000_000.0
                    dur_s = float(chunk.get("duration", 0)) / 10_000_000.0
                    w_text = str(chunk.get("text", "")).strip()
                    if w_text:
                        timings.append(
                            {
                                "word": w_text,
                                "start": round(start_s, 3),
                                "end": round(start_s + max(0.06, dur_s), 3),
                            }
                        )
        return timings

    timings = _run_coro_sync(_run)
    if not raw_mp3_path.exists() or raw_mp3_path.stat().st_size < 512:
        raise RuntimeError("Edge TTS no devolvió audio válido.")
    return timings


def _synthesize_openrouter_audio(
    text: str,
    voice: str,
    openrouter_audio_model: str,
    raw_wav_path: Path,
    settings: Settings,
    openrouter_client: Any = None,
) -> None:
    """Synthesize natural podcast host speech via OpenRouter GPT Audio PCM16 streaming."""
    from openai import OpenAI

    api_key = (settings.openrouter_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY no configurada para Voces OpenRouter Audio.")

    valid_voices = {v["id"] for v in TTS_CATALOG["openrouter_audio"]["voices"]}
    chosen_voice = voice if voice in valid_voices else "ash"
    model = (openrouter_audio_model or "openai/gpt-audio-mini").strip()

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    prompt = (
        "Eres un locutor profesional de podcast en español. "
        "Lee EXACTAMENTE en voz alta el siguiente texto palabra por palabra, "
        "con ritmo ágil, claro y natural. No añadas introducciones, explicaciones ni palabras extra:\n\n"
        f"{text}"
    )

    pcm_chunks: list[bytes] = []
    usage_p = 0
    usage_c = 0
    stream = client.chat.completions.create(
        model=model,
        modalities=["text", "audio"],
        audio={"voice": chosen_voice, "format": "pcm16"},
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )
    for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            usage_p = int(getattr(usage, "prompt_tokens", 0) or usage_p)
            usage_c = int(getattr(usage, "completion_tokens", 0) or usage_c)
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        audio_obj = getattr(delta, "audio", None) if delta is not None else None
        if not audio_obj:
            continue
        b64_data = audio_obj.get("data") if isinstance(audio_obj, dict) else getattr(audio_obj, "data", None)
        if b64_data:
            pcm_chunks.append(base64.b64decode(b64_data))

    raw_pcm = b"".join(pcm_chunks)
    if len(raw_pcm) < 1024:
        raise RuntimeError(f"OpenRouter Audio ({model}) no devolvió datos de audio PCM16.")

    with wave.open(str(raw_wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(24000)
        wf.writeframes(raw_pcm)

    if openrouter_client is not None and hasattr(openrouter_client, "cost_tracker"):
        est_cost = (usage_p * 0.6 + usage_c * 20.0) / 1_000_000.0
        openrouter_client.cost_tracker.record(model, usage_p, usage_c, cost_usd=est_cost)


def _ensure_piper_model(voice: str, settings: Settings) -> Optional[tuple[Path, Path]]:
    """Return local (onnx_path, config_path) for Piper, downloading from HuggingFace once if missing."""
    piper_dir = _tts_cache_dir(settings) / "piper"
    piper_dir.mkdir(parents=True, exist_ok=True)
    chosen = voice if voice in _PIPER_VOICE_URLS else "es_ES-davefx-medium"
    onnx_path = piper_dir / f"{chosen}.onnx"
    cfg_path = piper_dir / f"{chosen}.onnx.json"

    if onnx_path.exists() and onnx_path.stat().st_size > 100_000 and cfg_path.exists():
        return onnx_path, cfg_path

    onnx_url, cfg_url = _PIPER_VOICE_URLS[chosen]
    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as http:
            cfg_resp = http.get(cfg_url)
            cfg_resp.raise_for_status()
            cfg_path.write_bytes(cfg_resp.content)

            tmp_onnx = onnx_path.with_suffix(".onnx.tmp")
            with http.stream("GET", onnx_url) as r:
                r.raise_for_status()
                with tmp_onnx.open("wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
            tmp_onnx.replace(onnx_path)
        return onnx_path, cfg_path
    except Exception as exc:
        logger.warning(f"No se pudo descargar modelo Piper ({chosen}): {exc}")
        return None


def _synthesize_windows_sapi(text: str, voice: str, rate: str, raw_wav_path: Path) -> None:
    """Guaranteed 100% offline Windows SpeechSynthesizer fallback."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("Fallback SAPI offline solo está disponible en Windows.")

    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sapi_rate = _parse_rate_to_sapi_rate(rate)
    prefer_female = "sharvard" in voice.lower() or "female" in voice.lower() or "femen" in voice.lower()
    gender_hint = "Female" if prefer_female else "Male"
    out_escaped = str(raw_wav_path.resolve()).replace("'", "''")

    ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $synth.GetInstalledVoices() | Where-Object {{ $_.Enabled }}
$esVoice = $voices | Where-Object {{ $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'es' -and $_.VoiceInfo.Gender -eq '{gender_hint}' }} | Select-Object -First 1
if (-not $esVoice) {{
  $esVoice = $voices | Where-Object {{ $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'es' }} | Select-Object -First 1
}}
if ($esVoice) {{
  $synth.SelectVoice($esVoice.VoiceInfo.Name)
}}
$synth.Rate = {sapi_rate}
$synth.SetOutputToWaveFile('{out_escaped}')
$bytes = [Convert]::FromBase64String('{text_b64}')
$msg = [System.Text.Encoding]::UTF8.GetString($bytes)
$synth.Speak($msg)
$synth.Dispose()
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 or not raw_wav_path.exists() or raw_wav_path.stat().st_size < 1024:
        raise RuntimeError(f"Error SAPI Windows offline: {proc.stderr[-300:]}")


def _synthesize_offline_local(text: str, voice: str, rate: str, raw_wav_path: Path, settings: Settings) -> str:
    """Synthesize 100% locally via Piper ONNX, falling back to Windows SAPI if Piper model is unavailable."""
    try:
        from piper.config import SynthesisConfig
        from piper.voice import PiperVoice

        model_pair = _ensure_piper_model(voice, settings)
        if model_pair is not None:
            onnx_path, cfg_path = model_pair
            pv = PiperVoice.load(onnx_path, config_path=cfg_path, use_cuda=False)
            syn_cfg = SynthesisConfig(length_scale=_parse_rate_to_piper_length_scale(rate))
            with wave.open(str(raw_wav_path), "wb") as wf:
                pv.synthesize_wav(text, wf, syn_config=syn_cfg)
            if raw_wav_path.exists() and raw_wav_path.stat().st_size > 1024:
                return f"piper:{voice}"
    except Exception as exc:
        logger.warning(f"Piper offline falló ({exc}); usando Windows SAPI local.")

    _synthesize_windows_sapi(text, voice, rate, raw_wav_path)
    return "windows_sapi"


def _normalize_tts_wav(
    raw_audio_path: Path,
    final_wav_path: Path,
    target_lufs: float,
    settings: Settings,
    trim_silence: bool = True,
    head_trim_sec: float = 0.0,
    has_word_timings: bool = False,
) -> float:
    """Convert raw TTS output into 44.1kHz stereo PCM WAV normalized to target LUFS."""
    if not settings.ffmpeg_path:
        raise RuntimeError("FFmpeg no encontrado en el sistema.")
    final_wav_path.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    if trim_silence:
        # Remove artificial trailing silence cleanly; preserve word timing origin when has_word_timings=True
        if head_trim_sec > 0.0:
            filters.append(f"atrim=start={head_trim_sec:.3f},asetpts=PTS-STARTPTS")
        elif not has_word_timings:
            filters.append(
                "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-46dB"
            )
        filters.append(
            "areverse,silenceremove=start_periods=1:start_duration=0.04:start_threshold=-46dB,areverse"
        )
    filters.append(f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=9")

    cmd = [
        str(settings.ffmpeg_path),
        "-y",
        "-i",
        str(raw_audio_path),
        "-af",
        ",".join(filters),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(final_wav_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not final_wav_path.exists():
        raise RuntimeError(f"FFmpeg TTS normalize failed: {proc.stderr[-300:]}")
    return get_audio_duration(final_wav_path, settings)


def _build_proportional_word_timings(text: str, duration_sec: float) -> list[dict[str, Any]]:
    """Distribute word timings proportionally by character length when engine has no timestamps."""
    tokens = [t.strip() for t in str(text or "").split() if t.strip()]
    if not tokens or duration_sec <= 0.05:
        return []
    weights = [max(2, len(re.sub(r"\W+", "", t)) + 1) for t in tokens]
    total_w = float(sum(weights))
    usable = max(0.2, duration_sec - 0.08)
    cursor = 0.04
    timings: list[dict[str, Any]] = []
    for tok, w in zip(tokens, weights):
        dur = usable * (w / total_w)
        timings.append(
            {
                "word": tok,
                "start": round(cursor, 3),
                "end": round(cursor + dur, 3),
            }
        )
        cursor += dur
    return timings


def _apply_atempo_wav(wav_path: Path, tempo: float, settings: Settings) -> float:
    """Apply clean tempo speed-up (<= 1.25x) in-place preserving 44.1kHz stereo PCM WAV."""
    if not settings.ffmpeg_path:
        return get_audio_duration(wav_path, settings)
    safe_t = max(1.01, min(1.25, float(tempo)))
    tmp_out = wav_path.with_suffix(".tempo.wav")
    cmd = [
        str(settings.ffmpeg_path),
        "-y",
        "-i",
        str(wav_path),
        "-af",
        f"atempo={safe_t:.4f}",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(tmp_out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 1024:
        tmp_out.replace(wav_path)
    elif tmp_out.exists():
        tmp_out.unlink()
    return get_audio_duration(wav_path, settings)


def synthesize_speech(
    text: str,
    out_wav_path: Path,
    engine: str = "edge_tts",
    voice: str = "es-ES-AlvaroNeural",
    rate: str = "+6%",
    openrouter_audio_model: str = "openai/gpt-audio-mini",
    target_lufs: float = -14.0,
    settings: Optional[Settings] = None,
    openrouter_client: Any = None,
    trim_silence: bool = True,
    max_duration_sec: Optional[float] = None,
) -> dict[str, Any]:
    """Synthesize Spanish speech with the selected engine and normalize to 44.1kHz stereo WAV."""
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean_text:
        raise ValueError("El texto para síntesis TTS está vacío.")

    cfg = settings or get_settings()
    out_wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_wav_path.parent
    raw_mp3 = tmp_dir / f"{out_wav_path.stem}_raw.mp3"
    raw_wav = tmp_dir / f"{out_wav_path.stem}_raw.wav"

    word_timings: list[dict[str, Any]] = []
    engine_used = engine
    voice_used = voice
    head_trim = 0.0

    try:
        if engine == "edge_tts":
            raw_timings = _synthesize_edge_tts(clean_text, voice, rate, raw_mp3)
            if trim_silence and raw_timings:
                first_start = float(raw_timings[0]["start"])
                if first_start > 0.06:
                    head_trim = max(0.0, first_start - 0.04)
            duration_sec = _normalize_tts_wav(
                raw_mp3,
                out_wav_path,
                target_lufs,
                cfg,
                trim_silence=trim_silence,
                head_trim_sec=head_trim,
                has_word_timings=bool(raw_timings),
            )
            if raw_timings:
                for wt in raw_timings:
                    s = max(0.0, round(float(wt["start"]) - head_trim, 3))
                    e = max(s + 0.05, min(duration_sec, round(float(wt["end"]) - head_trim, 3)))
                    word_timings.append({"word": wt["word"], "start": s, "end": e})
        elif engine == "openrouter_audio":
            _synthesize_openrouter_audio(
                clean_text,
                voice,
                openrouter_audio_model,
                raw_wav,
                cfg,
                openrouter_client=openrouter_client,
            )
            duration_sec = _normalize_tts_wav(raw_wav, out_wav_path, target_lufs, cfg, trim_silence=trim_silence)
        elif engine == "offline_local":
            sub_used = _synthesize_offline_local(clean_text, voice, rate, raw_wav, cfg)
            engine_used = sub_used
            duration_sec = _normalize_tts_wav(raw_wav, out_wav_path, target_lufs, cfg, trim_silence=trim_silence)
        else:
            raise ValueError(f"Motor TTS desconocido: {engine}")

        if not word_timings:
            word_timings = _build_proportional_word_timings(clean_text, duration_sec)

        # Enforce hard duration ceiling (e.g. max_sec - 0.35s tail pad) when requested
        if max_duration_sec is not None and max_duration_sec > 1.2:
            target_speech_max = max(1.0, float(max_duration_sec) - 0.35)
            if duration_sec > target_speech_max:
                if engine == "edge_tts" and duration_sec > target_speech_max + 0.55:
                    # Re-synthesize once at brisker natural rate (+16%) before resorting to atempo
                    try:
                        fast_timings = _synthesize_edge_tts(clean_text, voice, "+16%", raw_mp3)
                        fast_head = max(0.0, float(fast_timings[0]["start"]) - 0.04) if (trim_silence and fast_timings and float(fast_timings[0]["start"]) > 0.06) else 0.0
                        fast_dur = _normalize_tts_wav(
                            raw_mp3,
                            out_wav_path,
                            target_lufs,
                            cfg,
                            trim_silence=trim_silence,
                            head_trim_sec=fast_head,
                            has_word_timings=bool(fast_timings),
                        )
                        if fast_dur < duration_sec:
                            duration_sec = fast_dur
                            if fast_timings:
                                word_timings = [
                                    {
                                        "word": wt["word"],
                                        "start": max(0.0, round(float(wt["start"]) - fast_head, 3)),
                                        "end": max(
                                            max(0.0, round(float(wt["start"]) - fast_head, 3)) + 0.05,
                                            min(duration_sec, round(float(wt["end"]) - fast_head, 3)),
                                        ),
                                    }
                                    for wt in fast_timings
                                ]
                    except Exception:
                        pass
                if duration_sec > target_speech_max:
                    ratio = min(1.25, duration_sec / target_speech_max)
                    if ratio > 1.01:
                        old_dur = duration_sec
                        duration_sec = _apply_atempo_wav(out_wav_path, ratio, cfg)
                        scale = duration_sec / max(0.1, old_dur)
                        word_timings = [
                            {
                                "word": wt["word"],
                                "start": round(float(wt["start"]) * scale, 3),
                                "end": round(float(wt["end"]) * scale, 3),
                            }
                            for wt in word_timings
                        ]

        return {
            "wav_path": str(out_wav_path),
            "duration_sec": round(duration_sec, 3),
            "word_timings": word_timings,
            "engine_used": engine_used,
            "voice_used": voice_used,
        }
    finally:
        for tmp in (raw_mp3, raw_wav):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def generate_voice_preview(
    text: str,
    engine: str = "edge_tts",
    voice: str = "es-ES-AlvaroNeural",
    rate: str = "+6%",
    openrouter_audio_model: str = "openai/gpt-audio-mini",
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Generate (or return cached) voice preview WAV under cache/tts/previews/."""
    cfg = settings or get_settings()
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip() or list_tts_catalog(cfg)["sample_text_default"]
    key_payload = json.dumps(
        {
            "engine": engine,
            "voice": voice,
            "rate": rate,
            "model": openrouter_audio_model if engine == "openrouter_audio" else "",
            "text": clean_text,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    h = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:12]
    previews_dir = _tts_cache_dir(cfg) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    filename = f"preview_{engine}_{h}.wav"
    out_wav = previews_dir / filename

    cached = out_wav.exists() and out_wav.stat().st_size > 2048
    if cached:
        dur = get_audio_duration(out_wav, cfg)
        return {
            "filename": filename,
            "url": f"/api/tts/previews/{filename}",
            "duration_sec": round(dur, 3),
            "cached": True,
            "engine_used": engine,
            "voice_used": voice,
        }

    res = synthesize_speech(
        text=clean_text,
        out_wav_path=out_wav,
        engine=engine,
        voice=voice,
        rate=rate,
        openrouter_audio_model=openrouter_audio_model,
        target_lufs=-14.0,
        settings=cfg,
        trim_silence=True,
    )
    return {
        "filename": filename,
        "url": f"/api/tts/previews/{filename}",
        "duration_sec": res["duration_sec"],
        "cached": False,
        "engine_used": res["engine_used"],
        "voice_used": res["voice_used"],
    }


def resolve_default_outro_tts_text(profile: PodcasterProfile) -> str:
    """Resolve the spoken CTA sentence for the closing outro curtain."""
    custom = (profile.outro_curtain.tts_text or "").strip()
    if custom:
        return custom
    platform = (profile.outro_curtain.platform_cta_text or profile.target_platform or "tu plataforma favorita").strip()
    channel = (profile.outro_curtain.channel_name_text or profile.podcast_name or profile.name).strip()
    return f"Puedes seguir escuchando este podcast completo en {platform}, en el canal {channel}."


def compute_outro_tts_hash(profile: PodcasterProfile) -> str:
    """Compute stable SHA-256 fingerprint for profile outro CTA synthesis parameters."""
    ic = profile.intro_curtain
    payload = {
        "engine": ic.tts_engine,
        "voice": ic.tts_voice,
        "rate": ic.tts_rate,
        "model": ic.openrouter_audio_model if ic.tts_engine == "openrouter_audio" else "",
        "text": resolve_default_outro_tts_text(profile),
        "target_lufs": round(float(profile.audio.target_lufs), 1),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def get_or_create_profile_outro_tts(
    profile: PodcasterProfile,
    store: Any,
    settings: Optional[Settings] = None,
    openrouter_client: Any = None,
    force_regen: bool = False,
) -> tuple[Path, float, str, bool]:
    """Generate the profile outro CTA WAV ONCE and reuse on all clips until config changes.

    Returns `(wav_path, duration_sec, hash_12, cache_hit)`.
    """
    cfg = settings or get_settings()
    h = compute_outro_tts_hash(profile)
    audio_dir = store.audio_dir(profile.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    target_filename = f"outro_tts_{h}.wav"
    target_path = audio_dir / target_filename

    if (
        not force_regen
        and profile.outro_curtain.cached_tts_hash == h
        and profile.outro_curtain.cached_tts_filename == target_filename
        and target_path.exists()
        and target_path.stat().st_size > 2048
    ):
        dur = get_audio_duration(target_path, cfg)
        return target_path, dur, h, True

    # Even if profile metadata hadn't been synced yet, check disk for exact hash match
    if not force_regen and target_path.exists() and target_path.stat().st_size > 2048:
        dur = get_audio_duration(target_path, cfg)
        profile.outro_curtain.cached_tts_filename = target_filename
        profile.outro_curtain.cached_tts_hash = h
        store.save_profile(profile)
        return target_path, dur, h, True

    text = resolve_default_outro_tts_text(profile)
    ic = profile.intro_curtain
    res = synthesize_speech(
        text=text,
        out_wav_path=target_path,
        engine=ic.tts_engine,
        voice=ic.tts_voice,
        rate=ic.tts_rate,
        openrouter_audio_model=ic.openrouter_audio_model,
        target_lufs=float(profile.audio.target_lufs),
        settings=cfg,
        openrouter_client=openrouter_client,
        trim_silence=True,
    )

    # Clean old outro_tts_*.wav files for this profile so disk stays tidy
    for old in audio_dir.glob("outro_tts_*.wav"):
        if old.name != target_filename:
            try:
                old.unlink()
            except Exception:
                pass

    profile.outro_curtain.cached_tts_filename = target_filename
    profile.outro_curtain.cached_tts_hash = h
    store.save_profile(profile)
    return target_path, float(res["duration_sec"]), h, False


def get_or_create_whoosh_sfx(settings: Optional[Settings] = None) -> Path:
    """Synthesize a crisp subtle transition whoosh WAV once via FFmpeg."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path:
        raise RuntimeError("FFmpeg no encontrado en el sistema.")
    out_wav = _tts_cache_dir(cfg) / "whoosh_transition.wav"
    if out_wav.exists() and out_wav.stat().st_size > 2048:
        return out_wav

    cmd = [
        str(cfg.ffmpeg_path),
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anoisesrc=d=0.35:c=pink:r=44100:a=0.35",
        "-af",
        "bandpass=f=850:w=600,afade=t=in:st=0:d=0.14,afade=t=out:st=0.14:d=0.21,volume=-11dB,aformat=channel_layouts=stereo",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"Error generando whoosh SFX: {proc.stderr[-250:]}")
    return out_wav
