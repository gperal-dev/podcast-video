"""Standalone local FastAPI server for PodcastForge Analytics Studio."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from analytics_studio.exporter import export_job_report, import_all_historical_jobs

_NOTES_LOCK = threading.Lock()


class NotePayload(BaseModel):
    job_id: str
    clip_id: str
    tags: list[str] = Field(default_factory=list)
    note: str = ""


def _safe_filename(name: str) -> str:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido.")
    return name


def create_analytics_app(base_dir: Optional[Path] = None) -> FastAPI:
    root = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    studio_dir = root / "analytics_studio"
    data_dir = studio_dir / "data"
    static_dir = Path(__file__).resolve().parent / "static"
    data_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="PodcastForge Analytics Studio", version="1.0.0")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _notes_path() -> Path:
        return data_dir / "user_notes.json"

    def _read_notes() -> dict[str, Any]:
        p = _notes_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @app.get("/")
    def serve_index() -> FileResponse:
        idx_html = static_dir / "index.html"
        if not idx_html.exists():
            raise HTTPException(status_code=404, detail="index.html no encontrado.")
        return FileResponse(idx_html)

    @app.get("/api/jobs")
    def list_reports(refresh: bool = Query(False)) -> JSONResponse:
        idx_file = data_dir / "index.json"
        if refresh or not idx_file.exists():
            items = import_all_historical_jobs(root)
            return JSONResponse(items)
        try:
            return JSONResponse(json.loads(idx_file.read_text(encoding="utf-8")))
        except Exception:
            return JSONResponse(import_all_historical_jobs(root))

    @app.get("/api/jobs/{job_id}")
    def get_report(job_id: str, refresh: bool = Query(False)) -> JSONResponse:
        _safe_filename(job_id)
        rep_path = data_dir / job_id / "report.json"
        if refresh or not rep_path.exists():
            try:
                rep = export_job_report(job_id, root)
                return JSONResponse(rep)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(json.loads(rep_path.read_text(encoding="utf-8")))

    @app.get("/api/notes")
    def get_all_notes(job_id: Optional[str] = Query(None)) -> JSONResponse:
        with _NOTES_LOCK:
            notes = _read_notes()
        if job_id:
            return JSONResponse(notes.get(job_id, {}))
        return JSONResponse(notes)

    @app.post("/api/notes")
    def save_note(payload: NotePayload) -> JSONResponse:
        _safe_filename(payload.job_id)
        key_clip = _safe_filename(payload.clip_id)
        with _NOTES_LOCK:
            notes = _read_notes()
            job_notes = notes.setdefault(payload.job_id, {})
            job_notes[key_clip] = {
                "tags": [str(t).strip() for t in payload.tags if str(t).strip()][:12],
                "note": str(payload.note or "").strip()[:2000],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = _notes_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_notes_path())
            saved = job_notes[key_clip]
        return JSONResponse({"ok": True, "job_id": payload.job_id, "clip_id": key_clip, "entry": saved})

    def _resolve_report(job_id: str) -> dict[str, Any]:
        _safe_filename(job_id)
        rep_path = data_dir / job_id / "report.json"
        if rep_path.exists():
            return json.loads(rep_path.read_text(encoding="utf-8"))
        try:
            return export_job_report(job_id, root)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/media/audio/{job_id}")
    def serve_source_audio(job_id: str) -> FileResponse:
        rep = _resolve_report(job_id)
        audio_p = Path(rep.get("source_audio_path") or "")
        if not audio_p.exists() or not audio_p.is_file():
            raise HTTPException(status_code=404, detail="Audio original no encontrado en disco.")
        return FileResponse(audio_p)

    @app.get("/media/video/{job_id}/{filename}")
    def serve_clip_video(job_id: str, filename: str) -> FileResponse:
        fn = _safe_filename(filename)
        rep = _resolve_report(job_id)
        out_dir = Path(rep.get("output_dir") or "")
        vid_p = (out_dir / fn).resolve()
        if not out_dir.exists() or vid_p.parent != out_dir.resolve() or not vid_p.exists():
            raise HTTPException(status_code=404, detail=f"Vídeo no encontrado: {fn}")
        return FileResponse(vid_p, media_type="video/mp4")

    @app.get("/media/thumb/{job_id}/{filename}")
    def serve_clip_thumb(job_id: str, filename: str) -> FileResponse:
        fn = _safe_filename(filename)
        rep = _resolve_report(job_id)
        out_dir = Path(rep.get("output_dir") or "")
        th_p = (out_dir / fn).resolve()
        if not out_dir.exists() or th_p.parent != out_dir.resolve() or not th_p.exists():
            raise HTTPException(status_code=404, detail=f"Miniatura no encontrada: {fn}")
        return FileResponse(th_p, media_type="image/jpeg")

    return app


app = create_analytics_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ANALYTICS_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
