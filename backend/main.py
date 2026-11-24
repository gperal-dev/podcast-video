"""FastAPI application entrypoint serving API, SSE progress streams, and compiled React SPA."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes_jobs import router as jobs_router
from backend.api.routes_media import router as media_router
from backend.api.routes_profiles import router as profiles_router
from backend.config import get_settings
from backend.core.diagnostics import get_quick_system_status, run_full_doctor
from backend.core.pipeline import JobManager
from backend.core.watcher import FolderWatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    jm = JobManager(settings)
    jm.recover_orphaned_jobs()
    watcher = FolderWatcher(jm, settings)
    app.state.job_manager = jm
    app.state.watcher = watcher
    try:
        yield
    finally:
        watcher.stop()


app = FastAPI(
    title="PodcastForge Studio",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/system/status")
def system_status() -> dict:
    return get_quick_system_status(get_settings())


@app.get("/api/system/doctor")
def system_doctor(test_apis: bool = False) -> dict:
    return run_full_doctor(get_settings(), test_apis=test_apis)


app.include_router(profiles_router)
app.include_router(jobs_router)
app.include_router(media_router)


# Serve built Vite React frontend if compiled
_settings = get_settings()
_dist_dir = _settings.frontend_dist_dir
if _dist_dir.exists() and (_dist_dir / "assets").exists():
    app.mount("/assets", StaticFiles(directory=_dist_dir / "assets"), name="frontend-assets")


@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Endpoint no encontrado.")
    dist = get_settings().frontend_dist_dir
    if dist.exists():
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        index_html = dist / "index.html"
        if index_html.exists():
            return FileResponse(index_html)
    return {
        "app": "PodcastForge Studio",
        "status": "backend_ready",
        "message": "Frontend no compilado aún. Ejecuta 'npm run build' en frontend/.",
    }
