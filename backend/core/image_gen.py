"""Pre-production AI image generator via OpenRouter (never invoked during pipeline video rendering)."""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image

from backend.config import Settings, get_settings
from backend.models.profile import AspectRatio


OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

_ORIENTATION_HINTS: dict[AspectRatio, str] = {
    "9:16": "vertical 9:16 portrait composition, tall cinematic framing",
    "16:9": "wide 16:9 horizontal cinematic composition",
    "1:1": "balanced 1:1 square composition",
}


def ai_previews_dir(settings: Optional[Settings] = None) -> Path:
    """Return directory storing pre-production AI image previews before job/profile assignment."""
    cfg = settings or get_settings()
    out = cfg.processing_dir / "_ai_previews"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _extract_image_url_from_choice(data: dict[str, Any]) -> Optional[str]:
    """Extract image data URI or HTTP URL from an OpenRouter chat completion response."""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices, list):
        return None
    msg = choices[0].get("message") or {}

    images = msg.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = (first.get("image_url") or {}).get("url") or first.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()

    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
    return None


def _decode_image_bytes(image_ref: str) -> bytes:
    """Decode base64 data URI or download direct HTTP image URL."""
    if image_ref.startswith("data:"):
        header, _, b64_part = image_ref.partition(",")
        if not b64_part:
            raise ValueError("Respuesta data URI de imagen vacía.")
        return base64.b64decode(b64_part)

    if image_ref.startswith(("http://", "https://")):
        resp = httpx.get(image_ref, timeout=45.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    return base64.b64decode(image_ref)


def generate_preview_image(
    prompt: str,
    aspect_ratio: AspectRatio = "9:16",
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Generate one preview PNG in pre-production using OpenRouter multimodal image models.

    This helper is strictly for interactive user pre-production in Profiles or New Production
    screens and is NEVER called automatically during pipeline rendering.
    """
    cfg = settings or get_settings()
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("El prompt de la imagen no puede estar vacío.")
    if not cfg.openrouter_api_key.strip():
        raise RuntimeError("Falta OPENROUTER_API_KEY en .env para generar imágenes con IA.")

    hint = _ORIENTATION_HINTS.get(aspect_ratio, _ORIENTATION_HINTS["9:16"])
    enriched_prompt = (
        f"{clean_prompt}. Style: authentic cinematic documentary photography or period-accurate editorial illustration, "
        f"{hint}, natural dramatic lighting, crisp depth of field, rich realistic textures, clean uncluttered framing, "
        f"strictly no embedded text, no letters, no captions, no watermarks, no borders."
    )

    candidate_models = [
        cfg.openrouter_model_image.strip(),
        "google/gemini-3.1-flash-lite-image",
        "openai/gpt-5-image-mini",
    ]
    models: list[str] = []
    for m in candidate_models:
        if m and m not in models:
            models.append(m)

    headers = {
        "Authorization": f"Bearer {cfg.openrouter_api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "PodcastForge Studio",
    }

    last_err = "Sin respuesta válida del proveedor de imagen."
    for model in models:
        payload_with_cfg: dict[str, Any] = {
            "model": model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": enriched_prompt}],
            "image_config": {"aspect_ratio": aspect_ratio},
        }
        payload_plain: dict[str, Any] = {
            "model": model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": enriched_prompt}],
        }

        for attempt_idx, payload in enumerate((payload_with_cfg, payload_plain)):
            try:
                resp = httpx.post(OPENROUTER_CHAT_URL, headers=headers, json=payload, timeout=75.0)
            except Exception as exc:
                last_err = str(exc)
                break

            if resp.status_code == 400 and attempt_idx == 0:
                last_err = f"HTTP 400 ({model}): {resp.text[:200]}"
                continue
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code} ({model}): {resp.text[:240]}"
                break

            try:
                body = resp.json()
            except Exception as exc:
                last_err = f"JSON inválido ({model}): {exc}"
                break

            image_ref = _extract_image_url_from_choice(body)
            if not image_ref:
                last_err = f"El modelo {model} no devolvió ninguna imagen en la respuesta."
                break

            try:
                raw_bytes = _decode_image_bytes(image_ref)
                with Image.open(io.BytesIO(raw_bytes)) as img_raw:
                    img = img_raw.convert("RGBA" if "A" in img_raw.mode else "RGB")
                    width, height = img.size
                    out_dir = ai_previews_dir(cfg)
                    fname = f"ai_{uuid.uuid4().hex[:10]}.png"
                    out_path = out_dir / fname
                    img.save(out_path, format="PNG")
            except Exception as exc:
                last_err = f"Imagen inválida devuelta por {model}: {exc}"
                break

            return {
                "preview_filename": fname,
                "preview_url": f"/api/ai/previews/{fname}",
                "model_used": model,
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
            }

    raise RuntimeError(f"No se pudo generar la imagen con IA: {last_err}")
