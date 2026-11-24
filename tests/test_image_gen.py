"""Unit tests for pre-production OpenRouter AI image generation."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.config import get_settings
from backend.core.image_gen import generate_preview_image


def _make_png_data_uri(width: int = 32, height: int = 48) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 80, 160)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def test_generate_preview_image_requires_api_key(tmp_path: Path) -> None:
    cfg = get_settings().model_copy(
        update={"openrouter_api_key": "", "processing_dir": tmp_path / "proc"}
    )
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        generate_preview_image("Barco histórico en el mar", aspect_ratio="16:9", settings=cfg)


def test_generate_preview_image_base64_and_400_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = get_settings().model_copy(
        update={
            "openrouter_api_key": "sk-or-test-key",
            "openrouter_model_image": "google/gemini-2.5-flash-image",
            "processing_dir": tmp_path / "proc",
        }
    )

    calls: list[dict[str, Any]] = []
    data_uri = _make_png_data_uri(64, 112)

    class FakeResponse:
        def __init__(self, status_code: int, body: dict[str, Any], text: str = "") -> None:
            self.status_code = status_code
            self._body = body
            self.text = text

        def json(self) -> dict[str, Any]:
            return self._body

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> FakeResponse:
        calls.append(json)
        if len(calls) == 1:
            assert "image_config" in json
            return FakeResponse(400, {}, text="image_config not supported")
        assert "image_config" not in json
        return FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "images": [{"type": "image_url", "image_url": {"url": data_uri}}],
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("backend.core.image_gen.httpx.post", fake_post)

    result = generate_preview_image("Submarino emergiendo en niebla", aspect_ratio="9:16", settings=cfg)
    assert len(calls) == 2
    saved_path = tmp_path / "proc" / "_ai_previews" / result["preview_filename"]
    assert saved_path.exists()
    assert result["preview_url"] == f"/api/ai/previews/{result['preview_filename']}"
    assert result["width"] == 64
    assert result["height"] == 112
    assert result["aspect_ratio"] == "9:16"
    assert result["model_used"] == "google/gemini-2.5-flash-image"
