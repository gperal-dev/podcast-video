"""API routes for gallery listing, secure Range-enabled media streaming, downloads, and output folder opening."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import get_settings


router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("/gallery")
def list_gallery(
    episode: Optional[str] = Query(None),
    clip_type: Optional[str] = Query(None),
) -> list[dict[str, Any]]:
    """Scan output/ directory for rendered videos and sidecar metadata."""
    settings = get_settings()
    out_root = settings.output_dir.resolve()
    items: list[dict[str, Any]] = []
    if not out_root.exists():
        return items

    for mp4_path in sorted(out_root.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        rel = mp4_path.relative_to(out_root).as_posix()
        meta_path = mp4_path.with_suffix(".json")
        thumb_path = mp4_path.with_suffix(".jpg")
        sidecar: dict[str, Any] = {}
        if meta_path.exists():
            try:
                sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        ep_name = sidecar.get("episode_name") or mp4_path.parent.name
        c_type = sidecar.get("clip_type") or "short_conclusive"
        if episode and episode.lower() not in ep_name.lower() and episode.lower() not in mp4_path.parent.name.lower():
            continue
        if clip_type and clip_type != "all" and c_type != clip_type:
            continue

        thumb_rel = thumb_path.relative_to(out_root).as_posix() if thumb_path.exists() else None
        lic_path = mp4_path.with_suffix(".licencias.txt")
        lic_rel = lic_path.relative_to(out_root).as_posix() if lic_path.exists() else None
        items.append(
            {
                "id": sidecar.get("clip_id") or mp4_path.stem,
                "job_id": sidecar.get("job_id"),
                "folder": mp4_path.parent.name,
                "episode_name": ep_name,
                "clip_type": c_type,
                "aspect_ratio": sidecar.get("aspect_ratio", "9:16"),
                "score": int(sidecar.get("score", 80)),
                "duration_sec": float(sidecar.get("duration_sec", 0.0)),
                "size_bytes": mp4_path.stat().st_size,
                "video_filename": mp4_path.name,
                "video_url": f"/api/media/output/{rel}",
                "thumbnail_url": f"/api/media/output/{thumb_rel}" if thumb_rel else None,
                "license_manifest_url": f"/api/media/output/{lic_rel}?download=1" if lic_rel else None,
                "summary": sidecar.get("summary", ""),
                "metadata": sidecar.get("metadata", {}),
                "narrative_archetype": sidecar.get("narrative_archetype", "mini_historia"),
                "speakers_detected": sidecar.get("speakers_detected", []),
                "speaker_color_map": sidecar.get("speaker_color_map", {}),
                "visual_hook_enabled": sidecar.get("visual_hook_enabled"),
                "visual_credits_audit": sidecar.get("visual_credits_audit", []),
                "license_warnings": sidecar.get("license_warnings", []),
                "standalone_packaging": sidecar.get("standalone_packaging", {}),
                "created_at": sidecar.get("created_at", ""),
            }
        )

    items.sort(key=lambda x: (x["folder"], -x["score"]))
    return items


@router.api_route("/output/{subpath:path}", methods=["GET", "HEAD"])
def stream_output_file(subpath: str, download: bool = False) -> FileResponse:
    settings = get_settings()
    root = settings.output_dir.resolve()
    target = (root / subpath).resolve()
    if not target.is_relative_to(root) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Archivo multimedia no encontrado.")

    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
    return FileResponse(target, headers=headers)


@router.api_route("/processing/{job_id}/source-audio", methods=["GET", "HEAD"])
def stream_source_audio(job_id: str) -> FileResponse:
    """Stream the source episode audio for Semi-Auto segment previewing."""
    settings = get_settings()
    job_file = (settings.processing_dir / job_id / "job.json").resolve()
    if not job_file.is_relative_to(settings.processing_dir.resolve()) or not job_file.exists():
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    job_data = json.loads(job_file.read_text(encoding="utf-8"))
    src_path = Path(str(job_data.get("source_audio_path") or ""))
    if not src_path.exists() or not src_path.is_file():
        raise HTTPException(status_code=404, detail="Audio fuente no encontrado.")
    return FileResponse(src_path)


@router.api_route("/processing/{job_id}/{subpath:path}", methods=["GET", "HEAD"])
def stream_processing_file(job_id: str, subpath: str, download: bool = False) -> FileResponse:
    """Safely serve audio previews, thumbnails, and license manifests inside processing/<job_id>/."""
    settings = get_settings()
    root = (settings.processing_dir / job_id).resolve()
    target = (root / subpath).resolve()
    if not target.is_relative_to(root) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Recurso de procesamiento no encontrado.")
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
    return FileResponse(target, headers=headers)


@router.post("/open-output")
def open_output_folder() -> dict[str, str]:
    settings = get_settings()
    out_dir = settings.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(out_dir))  # type: ignore[attr-defined]
        elif os.uname().sysname == "Darwin":
            subprocess.Popen(["open", str(out_dir)])
        else:
            subprocess.Popen(["xdg-open", str(out_dir)])
        return {"status": "opened", "path": str(out_dir)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo abrir la carpeta: {exc}") from exc
