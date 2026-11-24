"""Configuration loading, .env parsing, path resolution, and FFmpeg discovery."""

from __future__ import annotations

import os
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent.parent


def _strip_inline_comment(raw: Optional[str]) -> str:
    """Strip inline `# comment` from unquoted .env values."""
    if raw is None:
        return ""
    text = raw.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    # Remove `# ...` preceded by whitespace or at start
    cleaned = re.split(r"\s+#", text, maxsplit=1)[0].strip()
    return cleaned


def _load_clean_env(env_path: Path) -> dict[str, str]:
    """Load .env values safely without leaving inline comments attached."""
    merged: dict[str, str] = {}
    if env_path.exists():
        raw_values = dotenv_values(env_path)
        for key, val in raw_values.items():
            if key:
                merged[key] = _strip_inline_comment(val)
    # Process environment overrides .env if explicitly set and non-empty
    for key in list(merged.keys()) + [
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL_FAST",
        "OPENROUTER_MODEL_EDITORIAL",
        "OPENROUTER_MODEL_EDITORIAL_STRONG",
        "OPENROUTER_MODEL_IMAGE",
        "PEXELS_API_KEY",
        "PIXABAY_API_KEY",
        "WHISPER_DRAFT_MODEL",
        "WHISPER_PRECISION_MODEL",
        "WHISPER_DEVICE",
        "WHISPER_COMPUTE_TYPE",
        "FFMPEG_THREADS",
        "HOST",
        "PORT",
        "FFMPEG_PATH",
        "FFPROBE_PATH",
    ]:
        env_val = os.environ.get(key)
        if env_val is not None and env_val.strip():
            merged[key] = _strip_inline_comment(env_val)
    return merged


def _discover_binary(name: str, explicit_path: str = "") -> Optional[Path]:
    """Locate ffmpeg or ffprobe across env, PATH, local tools, and WinGet packages."""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))

    which_hit = shutil.which(name)
    if which_hit:
        candidates.append(Path(which_hit))

    exe_name = f"{name}.exe" if os.name == "nt" else name
    candidates.append(BASE_DIR / "tools" / "ffmpeg" / "bin" / exe_name)
    candidates.append(BASE_DIR / "tools" / "ffmpeg" / exe_name)

    if os.name == "nt":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        winget_link = local_app_data / "Microsoft" / "WinGet" / "Links" / exe_name
        candidates.append(winget_link)

        winget_pkgs = local_app_data / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            for pkg_dir in sorted(winget_pkgs.glob("Gyan.FFmpeg*"), reverse=True):
                for found in pkg_dir.rglob(exe_name):
                    candidates.append(found)
            for pkg_dir in sorted(winget_pkgs.glob("*ffmpeg*"), reverse=True):
                for found in pkg_dir.rglob(exe_name):
                    candidates.append(found)

        for common in [
            Path("C:/ffmpeg/bin") / exe_name,
            Path("C:/Program Files/ffmpeg/bin") / exe_name,
        ]:
            candidates.append(common)

    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


class Settings(BaseModel):
    """Application settings resolved from .env and filesystem."""

    base_dir: Path = BASE_DIR
    env_file: Path = BASE_DIR / ".env"

    profiles_dir: Path = BASE_DIR / "profiles"
    fonts_dir: Path = BASE_DIR / "profiles" / "_fonts"
    input_dir: Path = BASE_DIR / "input"
    processing_dir: Path = BASE_DIR / "processing"
    cache_stock_dir: Path = BASE_DIR / "cache" / "stock"
    output_dir: Path = BASE_DIR / "output"
    frontend_dist_dir: Path = BASE_DIR / "frontend" / "dist"

    openrouter_api_key: str = ""
    openrouter_model_fast: str = "google/gemini-2.5-flash-lite"
    openrouter_model_editorial: str = "deepseek/deepseek-v3.2"
    openrouter_model_editorial_strong: str = "google/gemini-2.5-flash"
    openrouter_model_image: str = "google/gemini-2.5-flash-image"

    pexels_api_key: str = ""
    pixabay_api_key: str = ""

    whisper_draft_model: str = "base"
    whisper_precision_model: str = "medium"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"

    ffmpeg_threads: int = 4
    host: str = "127.0.0.1"
    port: int = 8000

    ffmpeg_path: Optional[Path] = None
    ffprobe_path: Optional[Path] = None

    def ensure_directories(self) -> None:
        """Create required application directories if missing."""
        for path in [
            self.profiles_dir,
            self.fonts_dir,
            self.input_dir,
            self.processing_dir,
            self.cache_stock_dir,
            self.output_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


def _safe_int(val: str, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def load_settings() -> Settings:
    """Build a fresh Settings instance from .env and discovered tools."""
    env_path = BASE_DIR / ".env"
    values = _load_clean_env(env_path)

    ffmpeg_explicit = values.get("FFMPEG_PATH", "")
    ffprobe_explicit = values.get("FFPROBE_PATH", "")

    ffmpeg_bin = _discover_binary("ffmpeg", ffmpeg_explicit)
    ffprobe_bin = _discover_binary("ffprobe", ffprobe_explicit)
    if ffprobe_bin is None and ffmpeg_bin is not None:
        sibling = ffmpeg_bin.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.exists():
            ffprobe_bin = sibling

    settings = Settings(
        env_file=env_path,
        openrouter_api_key=values.get("OPENROUTER_API_KEY", ""),
        openrouter_model_fast=values.get("OPENROUTER_MODEL_FAST") or "google/gemini-2.5-flash-lite",
        openrouter_model_editorial=values.get("OPENROUTER_MODEL_EDITORIAL") or "deepseek/deepseek-v3.2",
        openrouter_model_editorial_strong=values.get("OPENROUTER_MODEL_EDITORIAL_STRONG") or "google/gemini-2.5-flash",
        openrouter_model_image=values.get("OPENROUTER_MODEL_IMAGE") or "google/gemini-2.5-flash-image",
        pexels_api_key=values.get("PEXELS_API_KEY", ""),
        pixabay_api_key=values.get("PIXABAY_API_KEY", ""),
        whisper_draft_model=values.get("WHISPER_DRAFT_MODEL") or "base",
        whisper_precision_model=values.get("WHISPER_PRECISION_MODEL") or "medium",
        whisper_device=(values.get("WHISPER_DEVICE") or "auto").lower(),
        whisper_compute_type=(values.get("WHISPER_COMPUTE_TYPE") or "int8").lower(),
        ffmpeg_threads=max(1, _safe_int(values.get("FFMPEG_THREADS", "4"), 4)),
        host=values.get("HOST") or "127.0.0.1",
        port=_safe_int(values.get("PORT", "8000"), 8000),
        ffmpeg_path=ffmpeg_bin,
        ffprobe_path=ffprobe_bin,
    )
    settings.ensure_directories()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return load_settings()


def reload_settings() -> Settings:
    """Clear settings cache and reload from disk."""
    get_settings.cache_clear()
    return get_settings()


def update_env_key(key: str, new_value: str, env_path: Optional[Path] = None) -> None:
    """Update a single variable line in .env without modifying keys or comments."""
    target = env_path or (BASE_DIR / ".env")
    if not target.exists():
        return
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$")
    updated = False
    new_lines: list[str] = []
    for line in lines:
        if pattern.match(line.rstrip("\r\n")):
            ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            new_lines.append(f"{key}={new_value}{ending}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={new_value}\n")
    target.write_text("".join(new_lines), encoding="utf-8")
    reload_settings()
