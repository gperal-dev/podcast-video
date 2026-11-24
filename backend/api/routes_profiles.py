"""API routes for Podcaster Profiles, custom images, custom fonts, and background music."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import get_settings
from backend.core.image_gen import ai_previews_dir, generate_preview_image
from backend.core.profile_store import ProfileStore, slugify
from backend.core.tts_engine import (
    generate_voice_preview,
    get_or_create_profile_outro_tts,
    list_tts_catalog,
    resolve_default_outro_tts_text,
)
from backend.models.profile import (
    AspectRatio,
    CustomImage,
    ImageDisplayMode,
    ImagePriority,
    PodcasterProfile,
)


router = APIRouter(prefix="/api", tags=["profiles"])


def _store() -> ProfileStore:
    return ProfileStore(get_settings())


class DuplicateRequest(BaseModel):
    new_name: Optional[str] = None


class ImageUpdateRequest(BaseModel):
    title: Optional[str] = None
    usage_instructions: Optional[str] = None
    priority: Optional[ImagePriority] = None
    display_mode: Optional[ImageDisplayMode] = None


class GenerateAIImageRequest(BaseModel):
    prompt: str
    aspect_ratio: AspectRatio = "9:16"


class AddAIImageRequest(BaseModel):
    preview_filename: str
    title: str = ""
    usage_instructions: str = ""
    priority: ImagePriority = "alta"
    display_mode: ImageDisplayMode = "fullscreen_zoom"


class TTSPreviewRequest(BaseModel):
    text: str = ""
    engine: str = "edge_tts"
    voice: str = "es-ES-AlvaroNeural"
    rate: str = "+6%"
    openrouter_audio_model: str = "openai/gpt-audio-mini"


class OutroTTSPreviewRequest(BaseModel):
    force: bool = False
    tts_text: Optional[str] = None
    tts_engine: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_rate: Optional[str] = None
    openrouter_audio_model: Optional[str] = None


@router.get("/profiles", response_model=list[PodcasterProfile])
def list_profiles() -> list[PodcasterProfile]:
    return _store().list_profiles()


@router.post("/profiles", response_model=PodcasterProfile)
def create_profile(payload: PodcasterProfile) -> PodcasterProfile:
    return _store().create_profile(payload)


@router.get("/profiles/{profile_id}", response_model=PodcasterProfile)
def get_profile(profile_id: str) -> PodcasterProfile:
    try:
        return _store().get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/profiles/{profile_id}", response_model=PodcasterProfile)
def update_profile(profile_id: str, payload: PodcasterProfile) -> PodcasterProfile:
    store = _store()
    try:
        store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload.id = profile_id
    return store.save_profile(payload)


@router.post("/profiles/{profile_id}/duplicate", response_model=PodcasterProfile)
def duplicate_profile(profile_id: str, body: Optional[DuplicateRequest] = None) -> PodcasterProfile:
    try:
        return _store().duplicate_profile(profile_id, body.new_name if body else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, str]:
    store = _store()
    if len(store.list_profiles()) <= 1:
        raise HTTPException(status_code=400, detail="Debe existir al menos un perfil activo.")
    try:
        store.delete_profile(profile_id)
        return {"status": "deleted", "profile_id": profile_id}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/images", response_model=PodcasterProfile)
def upload_profile_image(
    profile_id: str,
    file: UploadFile = File(...),
    title: str = Form(""),
    usage_instructions: str = Form(""),
    priority: ImagePriority = Form("alta"),
    display_mode: ImageDisplayMode = Form("fullscreen_zoom"),
) -> PodcasterProfile:
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ext = Path(file.filename or "imagen.png").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="Formato de imagen no soportado (usa PNG, JPG o WEBP).")

    safe_name = f"{slugify(Path(file.filename or 'img').stem)}_{uuid.uuid4().hex[:6]}{ext}"
    dest = store.images_dir(profile_id) / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    img = CustomImage(
        id=f"img_{uuid.uuid4().hex[:8]}",
        filename=safe_name,
        title=title.strip() or Path(file.filename or safe_name).stem,
        usage_instructions=usage_instructions.strip(),
        priority=priority,
        display_mode=display_mode,
    )
    profile.custom_images.append(img)
    return store.save_profile(profile)


@router.put("/profiles/{profile_id}/images/{image_id}", response_model=PodcasterProfile)
def update_profile_image(
    profile_id: str,
    image_id: str,
    body: ImageUpdateRequest,
) -> PodcasterProfile:
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    target = next((i for i in profile.custom_images if i.id == image_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Imagen no encontrada en el perfil.")

    if body.title is not None:
        target.title = body.title.strip() or target.title
    if body.usage_instructions is not None:
        target.usage_instructions = body.usage_instructions
    if body.priority is not None:
        target.priority = body.priority
    if body.display_mode is not None:
        target.display_mode = body.display_mode

    return store.save_profile(profile)


@router.delete("/profiles/{profile_id}/images/{image_id}", response_model=PodcasterProfile)
def delete_profile_image(profile_id: str, image_id: str) -> PodcasterProfile:
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    kept: list[CustomImage] = []
    removed: Optional[CustomImage] = None
    for img in profile.custom_images:
        if img.id == image_id:
            removed = img
        else:
            kept.append(img)

    if removed is None:
        raise HTTPException(status_code=404, detail="Imagen no encontrada.")

    fpath = store.images_dir(profile_id) / removed.filename
    if fpath.exists():
        fpath.unlink()
    profile.custom_images = kept
    return store.save_profile(profile)


@router.get("/profiles/{profile_id}/images/{filename}")
def get_profile_image_file(profile_id: str, filename: str) -> FileResponse:
    store = _store()
    img_dir = store.images_dir(profile_id).resolve()
    target = (img_dir / filename).resolve()
    if not target.is_relative_to(img_dir) or not target.exists():
        raise HTTPException(status_code=404, detail="Archivo de imagen no encontrado.")
    return FileResponse(target)


@router.get("/fonts")
def list_fonts(profile_id: Optional[str] = None) -> list[dict[str, str]]:
    return _store().list_available_fonts(profile_id=profile_id)


@router.get("/fonts/file/{filename}")
def get_font_file(filename: str, profile_id: Optional[str] = None) -> FileResponse:
    store = _store()
    if profile_id:
        pfont = (store.profile_fonts_dir(profile_id) / filename).resolve()
        if pfont.is_relative_to(store.profile_fonts_dir(profile_id).resolve()) and pfont.exists():
            return FileResponse(pfont)
    gfont = (store.fonts_dir / filename).resolve()
    if gfont.is_relative_to(store.fonts_dir.resolve()) and gfont.exists():
        return FileResponse(gfont)
    raise HTTPException(status_code=404, detail="Tipografía no encontrada.")


@router.post("/profiles/{profile_id}/fonts", response_model=PodcasterProfile)
def upload_profile_font(profile_id: str, file: UploadFile = File(...)) -> PodcasterProfile:
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    orig = Path(file.filename or "custom.ttf")
    if orig.suffix.lower() not in {".ttf", ".otf"}:
        raise HTTPException(status_code=400, detail="Solo se permiten fuentes .ttf o .otf.")

    safe_name = f"{slugify(orig.stem)}{orig.suffix.lower()}"
    dest = store.profile_fonts_dir(profile_id) / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    from backend.core.subtitle_engine import resolve_truetype_family

    profile.subtitles.font_file = safe_name
    profile.subtitles.font_name = resolve_truetype_family(dest, orig.stem)
    return store.save_profile(profile)


@router.post("/profiles/{profile_id}/bg-music", response_model=PodcasterProfile)
def upload_bg_music(profile_id: str, file: UploadFile = File(...)) -> PodcasterProfile:
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    orig = Path(file.filename or "music.mp3")
    if orig.suffix.lower() not in {".mp3", ".wav", ".m4a", ".ogg"}:
        raise HTTPException(status_code=400, detail="Usa un archivo de audio MP3, WAV, M4A u OGG.")

    safe_name = f"bgm_{slugify(orig.stem)}{orig.suffix.lower()}"
    dest = store.audio_dir(profile_id) / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    profile.audio.bg_music_filename = safe_name
    return store.save_profile(profile)


@router.post("/ai/generate-image")
def generate_ai_image_preview(body: GenerateAIImageRequest) -> dict[str, object]:
    """Generate a pre-production preview image via OpenRouter (never used during video pipeline)."""
    try:
        return generate_preview_image(
            prompt=body.prompt,
            aspect_ratio=body.aspect_ratio,
            settings=get_settings(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/ai/previews/{filename}")
def get_ai_preview_file(filename: str) -> FileResponse:
    """Safely serve a generated pre-production AI preview image."""
    pdir = ai_previews_dir(get_settings()).resolve()
    target = (pdir / filename).resolve()
    if not target.is_relative_to(pdir) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Imagen IA de vista previa no encontrada.")
    return FileResponse(target)


@router.post("/profiles/{profile_id}/images/from-ai", response_model=PodcasterProfile)
def add_profile_image_from_ai(profile_id: str, body: AddAIImageRequest) -> PodcasterProfile:
    """Attach a verified pre-production AI preview image onto a podcaster profile."""
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    pdir = ai_previews_dir(get_settings()).resolve()
    src = (pdir / body.preview_filename).resolve()
    if not src.is_relative_to(pdir) or not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Archivo de vista previa IA no encontrado.")

    safe_name = f"ai_{slugify(body.title or Path(body.preview_filename).stem)}_{uuid.uuid4().hex[:6]}.png"
    dest = store.images_dir(profile_id) / safe_name
    shutil.copy2(src, dest)

    img = CustomImage(
        id=f"img_{uuid.uuid4().hex[:8]}",
        filename=safe_name,
        title=body.title.strip() or Path(safe_name).stem,
        usage_instructions=body.usage_instructions.strip(),
        priority=body.priority,
        display_mode=body.display_mode,
    )
    profile.custom_images.append(img)
    return store.save_profile(profile)


@router.get("/tts/voices")
def get_tts_voices() -> dict[str, object]:
    """Return all available TTS engines, Spanish voices, and runtime availability."""
    return list_tts_catalog(get_settings())


@router.post("/tts/preview")
def create_tts_preview(body: TTSPreviewRequest) -> dict[str, object]:
    """Synthesize (or reuse cached) voice sample preview WAV for UI playback."""
    try:
        return generate_voice_preview(
            text=body.text,
            engine=body.engine,
            voice=body.voice,
            rate=body.rate,
            openrouter_audio_model=body.openrouter_audio_model,
            settings=get_settings(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tts/previews/{filename}")
def get_tts_preview_file(filename: str) -> FileResponse:
    """Safely serve a cached voice sample preview WAV."""
    pdir = (get_settings().base_dir / "cache" / "tts" / "previews").resolve()
    target = (pdir / filename).resolve()
    if not target.is_relative_to(pdir) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Muestra de voz no encontrada.")
    return FileResponse(target, media_type="audio/wav")


@router.get("/profiles/{profile_id}/audio/{filename}")
def get_profile_audio_file(profile_id: str, filename: str) -> FileResponse:
    """Safely serve audio files stored under a podcaster profile (outro music, cached CTA, BGM)."""
    store = _store()
    adir = store.audio_dir(profile_id).resolve()
    target = (adir / filename).resolve()
    if not target.is_relative_to(adir) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Audio del perfil no encontrado.")
    return FileResponse(target)


@router.post("/profiles/{profile_id}/outro-audio", response_model=PodcasterProfile)
async def upload_profile_outro_audio(profile_id: str, file: UploadFile = File(...)) -> PodcasterProfile:
    """Upload custom closing curtain music/audio for a profile."""
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    orig = Path(file.filename or "outro_music.mp3")
    if orig.suffix.lower() not in {".mp3", ".wav", ".m4a", ".ogg"}:
        raise HTTPException(status_code=400, detail="Usa un archivo MP3, WAV, M4A u OGG.")

    safe_name = f"outro_{slugify(orig.stem)}{orig.suffix.lower()}"
    dest = store.audio_dir(profile_id) / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    profile.outro_curtain.custom_audio_filename = safe_name
    return store.save_profile(profile)


@router.delete("/profiles/{profile_id}/outro-audio", response_model=PodcasterProfile)
def delete_profile_outro_audio(profile_id: str) -> PodcasterProfile:
    """Detach custom closing curtain audio so profile falls back cleanly to cached AI voice CTA."""
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    old_name = profile.outro_curtain.custom_audio_filename
    if old_name:
        old_path = (store.audio_dir(profile_id) / old_name).resolve()
        if old_path.is_relative_to(store.audio_dir(profile_id).resolve()) and old_path.exists():
            try:
                old_path.unlink()
            except Exception:
                pass
    profile.outro_curtain.custom_audio_filename = None
    return store.save_profile(profile)


@router.post("/profiles/{profile_id}/outro-tts-preview")
def preview_profile_outro_tts(
    profile_id: str,
    body: Optional[OutroTTSPreviewRequest] = None,
) -> dict[str, object]:
    """Generate (or return cached) the profile's single persistent AI closing CTA WAV."""
    store = _store()
    try:
        profile = store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    req = body or OutroTTSPreviewRequest()
    if req.tts_text is not None:
        profile.outro_curtain.tts_text = req.tts_text
    if req.tts_engine:
        profile.intro_curtain.tts_engine = req.tts_engine  # type: ignore[assignment]
    if req.tts_voice:
        profile.intro_curtain.tts_voice = req.tts_voice
    if req.tts_rate:
        profile.intro_curtain.tts_rate = req.tts_rate
    if req.openrouter_audio_model:
        profile.intro_curtain.openrouter_audio_model = req.openrouter_audio_model

    try:
        wav_path, dur, h, cache_hit = get_or_create_profile_outro_tts(
            profile=profile,
            store=store,
            settings=get_settings(),
            force_regen=req.force,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "filename": wav_path.name,
        "url": f"/api/profiles/{profile_id}/audio/{wav_path.name}",
        "duration_sec": round(dur, 3),
        "hash": h,
        "cached": cache_hit,
        "text_used": resolve_default_outro_tts_text(profile),
        "profile": profile.model_dump(mode="json"),
    }
