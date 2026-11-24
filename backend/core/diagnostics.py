"""System diagnostics: FFmpeg/libass, HW encoders, Whisper runtime, and live API verification."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Optional

import httpx
from openai import OpenAI

from backend.config import Settings, get_settings, update_env_key


FAST_MODEL_FALLBACKS = [
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash",
    "openai/gpt-4.1-nano",
    "openai/gpt-4o-mini",
]

EDITORIAL_MODEL_FALLBACKS = [
    "deepseek/deepseek-v3.2",
    "google/gemini-2.5-flash",
    "deepseek/deepseek-chat-v3-0324",
    "openai/gpt-4.1-mini",
]

EDITORIAL_STRONG_MODEL_FALLBACKS = [
    "google/gemini-2.5-flash",
    "openai/gpt-4.1-mini",
    "deepseek/deepseek-v3.2",
    "openai/gpt-4.1",
]

VISION_MODEL_FALLBACKS = [
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash",
    "openai/gpt-4o-mini",
]


def _run_subprocess(cmd: list[str], timeout: int = 12) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


_FFMPEG_CAPS_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def detect_ffmpeg_capabilities(settings: Settings, use_cache: bool = True) -> dict[str, Any]:
    """Check FFmpeg/FFprobe availability, required filters, and hardware encoders."""
    cache_key = (
        str(settings.ffmpeg_path) if settings.ffmpeg_path else "",
        str(settings.ffprobe_path) if settings.ffprobe_path else "",
    )
    if use_cache and cache_key in _FFMPEG_CAPS_CACHE:
        return dict(_FFMPEG_CAPS_CACHE[cache_key])

    result: dict[str, Any] = {
        "ffmpeg_ok": False,
        "ffprobe_ok": False,
        "ffmpeg_path": str(settings.ffmpeg_path) if settings.ffmpeg_path else None,
        "ffprobe_path": str(settings.ffprobe_path) if settings.ffprobe_path else None,
        "version": None,
        "required_filters": {},
        "hw_encoders_tested": {},
        "selected_encoder": "libx264",
    }

    if not settings.ffmpeg_path or not settings.ffmpeg_path.exists():
        return result

    code, out, _ = _run_subprocess([str(settings.ffmpeg_path), "-version"])
    if code == 0 and out:
        result["ffmpeg_ok"] = True
        result["version"] = out.splitlines()[0].strip()

    if settings.ffprobe_path and settings.ffprobe_path.exists():
        pcode, _, _ = _run_subprocess([str(settings.ffprobe_path), "-version"])
        result["ffprobe_ok"] = pcode == 0

    _, f_out, _ = _run_subprocess([str(settings.ffmpeg_path), "-hide_banner", "-filters"])
    for flt in ("ass", "acrossfade", "loudnorm", "silencedetect", "zoompan"):
        result["required_filters"][flt] = f" {flt} " in f_out or f"\n{flt} " in f_out or f" {flt}\t" in f_out or f" {flt}" in f_out

    # Probe encoders in priority order with an actual tiny encode
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
        cmd = [
            str(settings.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=256x256:d=0.25",
            "-frames:v",
            "5",
            "-c:v",
            enc,
            "-pix_fmt",
            "yuv420p",
            "-f",
            "null",
            "-",
        ]
        try:
            ecode, _, eerr = _run_subprocess(cmd, timeout=10)
            ok = ecode == 0
            result["hw_encoders_tested"][enc] = {"ok": ok, "detail": "OK" if ok else (eerr.strip()[:120] or "error")}
        except Exception as exc:
            result["hw_encoders_tested"][enc] = {"ok": False, "detail": str(exc)[:120]}

    for enc in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
        if result["hw_encoders_tested"].get(enc, {}).get("ok"):
            result["selected_encoder"] = enc
            break

    _FFMPEG_CAPS_CACHE[cache_key] = dict(result)
    return result


def resolve_whisper_runtime(settings: Settings) -> dict[str, Any]:
    """Detect usable Whisper runtime device and compute type safely."""
    requested_device = settings.whisper_device
    requested_compute = settings.whisper_compute_type
    resolved_device = "cpu"
    resolved_compute = requested_compute if requested_compute in {"int8", "float32"} else "int8"
    cuda_devices = 0
    note = "CPU int8 activo"

    try:
        import ctranslate2

        try:
            cuda_devices = ctranslate2.get_cuda_device_count()
        except Exception:
            cuda_devices = 0

        if requested_device in {"auto", "cuda"} and cuda_devices > 0:
            supported = ctranslate2.get_supported_compute_types("cuda")
            if supported:
                resolved_device = "cuda"
                resolved_compute = requested_compute if requested_compute in supported else ("float16" if "float16" in supported else "int8")
                note = f"CUDA disponible ({cuda_devices} GPU, {resolved_compute})"
            else:
                note = "CUDA detectado pero sin soporte de compute_type; fallback a CPU int8"
        elif requested_device == "cuda" and cuda_devices == 0:
            note = "CUDA solicitado pero no hay runtime CUDA usable en ctranslate2; fallback a CPU int8"
    except Exception as exc:
        note = f"Error comprobando ctranslate2: {exc}"

    return {
        "requested_device": requested_device,
        "resolved_device": resolved_device,
        "resolved_compute_type": resolved_compute,
        "cuda_devices": cuda_devices,
        "draft_model": settings.whisper_draft_model,
        "precision_model": settings.whisper_precision_model,
        "note": note,
    }


def _test_openrouter_model(client: OpenAI, model: str) -> tuple[bool, str]:
    """Verify a model returns valid JSON via OpenRouter."""
    messages = [
        {"role": "system", "content": "Responde exclusivamente con un objeto JSON válido."},
        {"role": "user", "content": 'Devuelve {"status":"ok","check":true}'},
    ]
    for use_response_format in (True, False):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0,
            }
            if use_response_format:
                kwargs["response_format"] = {"type": "json_object"}
            if "gemini-2.5-pro" in model.lower():
                kwargs["extra_body"] = {"reasoning": {"max_tokens": 128}}
            resp = client.chat.completions.create(**kwargs)
            content = (resp.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:].strip()
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return True, f"OK ({'json_object' if use_response_format else 'parsed'})"
        except Exception as exc:
            err_str = str(exc)
            if "401" in err_str or "Authentication" in err_str or "Invalid" in err_str and "key" in err_str.lower():
                return False, f"AUTH_ERROR: {err_str[:160]}"
            if use_response_format and ("response_format" in err_str.lower() or "json_object" in err_str.lower()):
                continue
            return False, err_str[:180]
    return False, "No valid JSON returned"


def verify_live_apis(settings: Settings, auto_fix_models: bool = True) -> dict[str, Any]:
    """Verify OpenRouter (with optional fallback auto-fix), Pexels, and Pixabay."""
    out: dict[str, Any] = {
        "openrouter": {
            "configured": bool(settings.openrouter_api_key),
            "ok": False,
            "fast_model": settings.openrouter_model_fast,
            "fast_ok": False,
            "fast_detail": "",
            "editorial_model": settings.openrouter_model_editorial,
            "editorial_ok": False,
            "editorial_detail": "",
            "updated_env": [],
        },
        "pexels": {"configured": bool(settings.pexels_api_key), "ok": False, "detail": ""},
        "pixabay": {"configured": bool(settings.pixabay_api_key), "ok": False, "detail": ""},
    }

    # OpenRouter
    if settings.openrouter_api_key:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
            default_headers={
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "PodcastForge Studio",
            },
            timeout=25.0,
        )

        fast_ok, fast_detail = _test_openrouter_model(client, settings.openrouter_model_fast)
        if not fast_ok and auto_fix_models and not fast_detail.startswith("AUTH_ERROR"):
            for candidate in FAST_MODEL_FALLBACKS:
                if candidate == settings.openrouter_model_fast:
                    continue
                ok, det = _test_openrouter_model(client, candidate)
                if ok:
                    update_env_key("OPENROUTER_MODEL_FAST", candidate, settings.env_file)
                    out["openrouter"]["fast_model"] = candidate
                    out["openrouter"]["updated_env"].append(f"OPENROUTER_MODEL_FAST={candidate}")
                    fast_ok, fast_detail = True, f"Fallback activo ({det})"
                    break
        out["openrouter"]["fast_ok"] = fast_ok
        out["openrouter"]["fast_detail"] = fast_detail

        ed_ok, ed_detail = _test_openrouter_model(client, settings.openrouter_model_editorial)
        if not ed_ok and auto_fix_models and not ed_detail.startswith("AUTH_ERROR"):
            for candidate in EDITORIAL_MODEL_FALLBACKS:
                if candidate == settings.openrouter_model_editorial:
                    continue
                ok, det = _test_openrouter_model(client, candidate)
                if ok:
                    update_env_key("OPENROUTER_MODEL_EDITORIAL", candidate, settings.env_file)
                    out["openrouter"]["editorial_model"] = candidate
                    out["openrouter"]["updated_env"].append(f"OPENROUTER_MODEL_EDITORIAL={candidate}")
                    ed_ok, ed_detail = True, f"Fallback activo ({det})"
                    break
        out["openrouter"]["editorial_ok"] = ed_ok
        out["openrouter"]["editorial_detail"] = ed_detail
        out["openrouter"]["ok"] = fast_ok and ed_ok
    else:
        out["openrouter"]["fast_detail"] = "Falta OPENROUTER_API_KEY en .env"
        out["openrouter"]["editorial_detail"] = "Falta OPENROUTER_API_KEY en .env"

    # Pexels
    if settings.pexels_api_key:
        try:
            r = httpx.get(
                "https://api.pexels.com/videos/search",
                params={"query": "studio microphone", "per_page": 1},
                headers={"Authorization": settings.pexels_api_key},
                timeout=12.0,
            )
            out["pexels"]["ok"] = r.status_code == 200
            out["pexels"]["detail"] = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
        except Exception as exc:
            out["pexels"]["detail"] = str(exc)[:120]
    else:
        out["pexels"]["detail"] = "Falta PEXELS_API_KEY en .env"

    # Pixabay
    if settings.pixabay_api_key:
        try:
            r = httpx.get(
                "https://pixabay.com/api/videos/",
                params={"key": settings.pixabay_api_key, "q": "microphone", "per_page": 3},
                timeout=12.0,
            )
            out["pixabay"]["ok"] = r.status_code == 200
            out["pixabay"]["detail"] = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
        except Exception as exc:
            out["pixabay"]["detail"] = str(exc)[:120]
    else:
        out["pixabay"]["detail"] = "Falta PIXABAY_API_KEY en .env"

    return out


def get_quick_system_status(settings: Optional[Settings] = None) -> dict[str, Any]:
    """Fast non-network status check suitable for frontend status bar."""
    cfg = settings or get_settings()
    ff = detect_ffmpeg_capabilities(cfg)
    wh = resolve_whisper_runtime(cfg)
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "openrouter_configured": bool(cfg.openrouter_api_key),
        "openrouter_model_fast": cfg.openrouter_model_fast,
        "openrouter_model_editorial": cfg.openrouter_model_editorial,
        "openrouter_model_editorial_strong": cfg.openrouter_model_editorial_strong,
        "pexels_configured": bool(cfg.pexels_api_key),
        "pixabay_configured": bool(cfg.pixabay_api_key),
        "ffmpeg_ok": ff["ffmpeg_ok"] and ff["ffprobe_ok"],
        "libass_ok": bool(ff["required_filters"].get("ass")),
        "selected_encoder": ff["selected_encoder"],
        "whisper_device": wh["resolved_device"],
        "whisper_compute_type": wh["resolved_compute_type"],
        "whisper_draft_model": cfg.whisper_draft_model,
        "whisper_precision_model": cfg.whisper_precision_model,
    }


def run_full_doctor(settings: Optional[Settings] = None, test_apis: bool = True) -> dict[str, Any]:
    """Run complete system, hardware, Whisper, and live API diagnostic."""
    cfg = settings or get_settings()
    ffmpeg_info = detect_ffmpeg_capabilities(cfg)
    whisper_info = resolve_whisper_runtime(cfg)
    apis_info = verify_live_apis(cfg, auto_fix_models=True) if test_apis else {}
    fonts = [f.name for f in sorted(cfg.fonts_dir.glob("*.ttf"))] + [
        f.name for f in sorted(cfg.fonts_dir.glob("*.otf"))
    ]
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "workspace_dir": str(cfg.base_dir),
        "ffmpeg": ffmpeg_info,
        "whisper": whisper_info,
        "apis": apis_info,
        "bundled_fonts": fonts,
    }
