"""Watchdog monitor on input/ folder for unattended automatic podcast processing."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from backend.config import Settings, get_settings
from backend.core.pipeline import JobManager


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus"}


class _AudioDropHandler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher") -> None:
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher._schedule_candidate(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        if not event.is_directory and dest:
            self.watcher._schedule_candidate(Path(str(dest)))


class FolderWatcher:
    """Watches input/ directory and triggers auto jobs when new audio files finish writing."""

    def __init__(self, job_manager: JobManager, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.job_manager = job_manager
        self._lock = threading.Lock()
        self._observer: Optional[Observer] = None
        self._running = False
        self._profile_id: Optional[str] = None
        self._processed_count = 0
        self._inflight: set[str] = set()
        self._seen_file = self.settings.processing_dir / "watcher_seen.json"

    def _load_seen(self) -> dict[str, dict[str, Any]]:
        if self._seen_file.exists():
            try:
                data = json.loads(self._seen_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_seen(self, data: dict[str, dict[str, Any]]) -> None:
        self._seen_file.parent.mkdir(parents=True, exist_ok=True)
        self._seen_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _snapshot_existing_files(self) -> None:
        """Record pre-existing files on watcher start so old samples aren't auto-retriggered."""
        seen = self._load_seen()
        for fpath in self.settings.input_dir.iterdir():
            if fpath.is_file() and fpath.suffix.lower() in AUDIO_EXTENSIONS:
                if fpath.name not in seen:
                    st = fpath.stat()
                    seen[fpath.name] = {"size": st.st_size, "mtime": st.st_mtime}
        self._save_seen(seen)

    def _schedule_candidate(self, path: Path) -> None:
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return
        with self._lock:
            if not self._running or not self._profile_id:
                return
            key = str(path.resolve())
            if key in self._inflight:
                return
            self._inflight.add(key)

        threading.Thread(
            target=self._wait_and_queue,
            args=(path, self._profile_id),
            daemon=True,
        ).start()

    def _wait_and_queue(self, path: Path, profile_id: str) -> None:
        key = str(path.resolve())
        try:
            prev_size = -1
            stable_ticks = 0
            for _ in range(30):
                if not path.exists():
                    return
                size = path.stat().st_size
                if size > 0 and size == prev_size:
                    stable_ticks += 1
                    if stable_ticks >= 2:
                        break
                else:
                    stable_ticks = 0
                    prev_size = size
                time.sleep(1.0)

            if not path.exists():
                return
            st = path.stat()
            seen = self._load_seen()
            prev = seen.get(path.name)
            if prev and prev.get("size") == st.st_size and abs(float(prev.get("mtime", 0)) - st.st_mtime) < 1.0:
                return

            seen[path.name] = {"size": st.st_size, "mtime": st.st_mtime}
            self._save_seen(seen)

            job = self.job_manager.create_job(
                profile_id=profile_id,
                source_audio_path=path,
                episode_name=path.stem,
                auto_mode=True,
            )
            self.job_manager.start_job_async(job.job_id)
            with self._lock:
                self._processed_count += 1
        finally:
            with self._lock:
                self._inflight.discard(key)

    def start(self, profile_id: str, process_existing: bool = False) -> dict[str, Any]:
        self.job_manager.store.get_profile(profile_id)
        with self._lock:
            if self._running:
                self._profile_id = profile_id
                return self.status()

            self.settings.input_dir.mkdir(parents=True, exist_ok=True)
            if not process_existing:
                self._snapshot_existing_files()

            self._profile_id = profile_id
            obs = Observer()
            obs.schedule(_AudioDropHandler(self), str(self.settings.input_dir), recursive=False)
            obs.start()
            self._observer = obs
            self._running = True
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            obs = self._observer
            self._observer = None
            self._running = False
        if obs is not None:
            obs.stop()
            obs.join(timeout=5.0)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "profile_id": self._profile_id,
            "input_dir": str(self.settings.input_dir),
            "processed_count": self._processed_count,
        }
