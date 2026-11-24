"""API routes for podcast upload, job execution, SSE progress stream, review mode, and watcher."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.core.image_gen import ai_previews_dir
from backend.core.pipeline import JobManager
from backend.core.profile_store import slugify
from backend.core.watcher import AUDIO_EXTENSIONS, FolderWatcher
from backend.models.profile import CustomImage, DEFAULT_MEDIA_PROVIDERS, MediaProviderId


router = APIRouter(prefix="/api", tags=["jobs"])


def _jm(request: Request) -> JobManager:
    return request.app.state.job_manager


def _watcher(request: Request) -> FolderWatcher:
    return request.app.state.watcher


class ReviewUpdateRequest(BaseModel):
    clips: list[dict[str, Any]]


class WatcherStartRequest(BaseModel):
    profile_id: str
    process_existing: bool = False


@router.get("/jobs")
def list_jobs(request: Request) -> list[dict[str, Any]]:
    return [j.model_dump(mode="json") for j in _jm(request).list_jobs()]


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _jm(request).load_job(job_id).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs")
async def create_and_start_job(
    request: Request,
    profile_id: str = Form(...),
    episode_name: str = Form(""),
    shorts_cliffhanger: Optional[int] = Form(None),
    shorts_conclusive: Optional[int] = Form(None),
    medium_count: Optional[int] = Form(None),
    long_count: Optional[int] = Form(None),
    auto_mode: bool = Form(True),
    visual_hook_enabled: Optional[bool] = Form(None),
    visual_hook_style: Optional[str] = Form(None),
    allowed_media_providers: Optional[str] = Form(None),
    intro_curtain_enabled: Optional[bool] = Form(None),
    outro_curtain_enabled: Optional[bool] = Form(None),
    save_analytics_report: bool = Form(False),
    audio_file: UploadFile = File(...),
    episode_images_meta: str = Form("[]"),
    episode_image_files: list[UploadFile] = File(default=[]),
    episode_ai_images: str = Form("[]"),
) -> dict[str, Any]:
    jm = _jm(request)
    try:
        jm.store.get_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    orig_audio = Path(audio_file.filename or "podcast.mp3")
    if orig_audio.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado ({orig_audio.suffix}). Usa MP3, WAV, M4A, FLAC, OGG o AAC.",
        )

    parsed_providers: Optional[list[MediaProviderId]] = None
    if allowed_media_providers is not None and allowed_media_providers.strip():
        try:
            raw_p = json.loads(allowed_media_providers)
            if isinstance(raw_p, list):
                valid_set = set(DEFAULT_MEDIA_PROVIDERS)
                filtered_p = [str(x) for x in raw_p if str(x) in valid_set]
                if filtered_p:
                    parsed_providers = filtered_p  # type: ignore[assignment]
        except Exception:
            pass

    # Create job first to get its directory, then stream upload cleanly without holding file in RAM
    temp_job = jm.create_job(
        profile_id=profile_id,
        source_audio_path=jm.settings.input_dir / orig_audio.name,
        episode_name=episode_name.strip() or orig_audio.stem,
        shorts_cliffhanger=shorts_cliffhanger,
        shorts_conclusive=shorts_conclusive,
        medium_count=medium_count,
        long_count=long_count,
        auto_mode=auto_mode,
        episode_images=[],
        visual_hook_enabled=visual_hook_enabled,
        visual_hook_style=visual_hook_style,
        allowed_media_providers=parsed_providers,
        intro_curtain_enabled=intro_curtain_enabled,
        outro_curtain_enabled=outro_curtain_enabled,
        save_analytics_report=save_analytics_report,
    )

    jdir = jm.job_dir(temp_job.job_id)
    audio_dir = jdir / "source_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    saved_audio = audio_dir / f"{slugify(orig_audio.stem)}{orig_audio.suffix.lower()}"

    with saved_audio.open("wb") as out_f:
        while chunk := await audio_file.read(1024 * 1024):
            out_f.write(chunk)

    # Save optional episode-specific images
    try:
        meta_list = json.loads(episode_images_meta or "[]")
    except Exception:
        meta_list = []

    ep_dir = jm.episode_images_dir(temp_job.job_id)
    ep_images: list[CustomImage] = []
    for idx, up_img in enumerate(episode_image_files):
        ext = Path(up_img.filename or "ep.png").suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        safe_img = f"ep_{idx}_{uuid.uuid4().hex[:6]}{ext}"
        dest = ep_dir / safe_img
        with dest.open("wb") as out_i:
            shutil.copyfileobj(up_img.file, out_i)
        m = meta_list[idx] if idx < len(meta_list) and isinstance(meta_list[idx], dict) else {}
        ep_images.append(
            CustomImage(
                id=f"epimg_{uuid.uuid4().hex[:8]}",
                filename=safe_img,
                title=str(m.get("title") or Path(up_img.filename or safe_img).stem),
                usage_instructions=str(m.get("usage_instructions") or "Usar en momentos clave del episodio"),
                priority=m.get("priority", "alta"),
                display_mode=m.get("display_mode", "fullscreen_zoom"),
            )
        )

    # Attach pre-generated AI preview images chosen before job creation
    try:
        ai_list = json.loads(episode_ai_images or "[]")
    except Exception:
        ai_list = []
    pdir = ai_previews_dir(jm.settings).resolve()
    for idx, ai_item in enumerate(ai_list if isinstance(ai_list, list) else []):
        if not isinstance(ai_item, dict):
            continue
        prev_name = str(ai_item.get("preview_filename") or "").strip()
        if not prev_name:
            continue
        src_ai = (pdir / prev_name).resolve()
        if not src_ai.is_relative_to(pdir) or not src_ai.exists() or not src_ai.is_file():
            continue
        safe_ai = f"ep_ai_{idx}_{uuid.uuid4().hex[:6]}.png"
        shutil.copy2(src_ai, ep_dir / safe_ai)
        ep_images.append(
            CustomImage(
                id=f"epimg_{uuid.uuid4().hex[:8]}",
                filename=safe_ai,
                title=str(ai_item.get("title") or Path(prev_name).stem),
                usage_instructions=str(ai_item.get("usage_instructions") or "Usar en momentos clave del episodio"),
                priority=ai_item.get("priority", "alta"),
                display_mode=ai_item.get("display_mode", "fullscreen_zoom"),
            )
        )

    temp_job.source_audio_path = str(saved_audio.resolve())
    temp_job.episode_images = ep_images
    jm._save_job(temp_job)
    jm.start_job_async(temp_job.job_id)
    return temp_job.model_dump(mode="json")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _jm(request).cancel_job(job_id).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str) -> dict[str, Any]:
    jm = _jm(request)
    try:
        job = jm.load_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    job.status = "queued"
    job.error_message = None
    jm._save_job(job)
    jm.start_job_async(job_id)
    return job.model_dump(mode="json")


@router.put("/jobs/{job_id}/review")
def update_review(request: Request, job_id: str, body: ReviewUpdateRequest) -> dict[str, Any]:
    try:
        return _jm(request).update_review_edits(job_id, body.clips).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/approve")
def approve_review(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _jm(request).approve_and_resume(job_id).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/events")
async def stream_job_events(request: Request, job_id: str) -> EventSourceResponse:
    jm = _jm(request)
    try:
        initial = jm.load_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    loop = asyncio.get_running_loop()
    queue = jm.subscribe(job_id, loop)

    async def event_generator():
        try:
            yield {"event": "state", "data": initial.model_dump_json()}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": evt.get("type", "state"), "data": json.dumps(evt.get("job", evt))}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            jm.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())


@router.get("/watcher")
def watcher_status(request: Request) -> dict[str, Any]:
    return _watcher(request).status()


@router.post("/watcher/start")
def watcher_start(request: Request, body: WatcherStartRequest) -> dict[str, Any]:
    try:
        return _watcher(request).start(body.profile_id, process_existing=body.process_existing)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/watcher/stop")
def watcher_stop(request: Request) -> dict[str, Any]:
    return _watcher(request).stop()
