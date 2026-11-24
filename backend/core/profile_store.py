"""Persistent JSON store for Podcaster Profiles and custom assets."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.config import Settings, get_settings
from backend.models.profile import CustomImage, PodcasterProfile


def slugify(text: str) -> str:
    """Create a filesystem-safe lowercase identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip().lower()).strip("_")
    return cleaned or f"perfil_{uuid.uuid4().hex[:6]}"


class ProfileStore:
    """Manages reading, writing, duplicating, and asset management for profiles."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.profiles_dir = self.settings.profiles_dir
        self.fonts_dir = self.settings.fonts_dir
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.fonts_dir.mkdir(parents=True, exist_ok=True)

    def profile_dir(self, profile_id: str) -> Path:
        return self.profiles_dir / profile_id

    def profile_json_path(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "profile.json"

    def images_dir(self, profile_id: str) -> Path:
        path = self.profile_dir(profile_id) / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def profile_fonts_dir(self, profile_id: str) -> Path:
        path = self.profile_dir(profile_id) / "fonts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def audio_dir(self, profile_id: str) -> Path:
        path = self.profile_dir(profile_id) / "audio"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_profiles(self) -> list[PodcasterProfile]:
        profiles: list[PodcasterProfile] = []
        if not self.profiles_dir.exists():
            return profiles
        for entry in sorted(self.profiles_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            pfile = entry / "profile.json"
            if pfile.exists():
                try:
                    data = json.loads(pfile.read_text(encoding="utf-8"))
                    profiles.append(PodcasterProfile.model_validate(data))
                except Exception:
                    continue
        return profiles

    def get_profile(self, profile_id: str) -> PodcasterProfile:
        pfile = self.profile_json_path(profile_id)
        if not pfile.exists():
            raise FileNotFoundError(f"Perfil no encontrado: {profile_id}")
        data = json.loads(pfile.read_text(encoding="utf-8"))
        return PodcasterProfile.model_validate(data)

    def save_profile(self, profile: PodcasterProfile) -> PodcasterProfile:
        pdir = self.profile_dir(profile.id)
        pdir.mkdir(parents=True, exist_ok=True)
        self.images_dir(profile.id)
        self.profile_fonts_dir(profile.id)
        self.audio_dir(profile.id)
        profile.updated_at = datetime.now(timezone.utc).isoformat()
        self.profile_json_path(profile.id).write_text(
            profile.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return profile

    def create_profile(self, profile: PodcasterProfile) -> PodcasterProfile:
        base_id = slugify(profile.id or profile.name)
        candidate_id = base_id
        idx = 2
        while self.profile_json_path(candidate_id).exists():
            candidate_id = f"{base_id}_{idx}"
            idx += 1
        profile.id = candidate_id
        now = datetime.now(timezone.utc).isoformat()
        profile.created_at = now
        profile.updated_at = now
        return self.save_profile(profile)

    def duplicate_profile(self, source_id: str, new_name: Optional[str] = None) -> PodcasterProfile:
        src = self.get_profile(source_id)
        copy_name = new_name or f"{src.name} (Copia)"
        new_id = slugify(copy_name)
        candidate = new_id
        idx = 2
        while self.profile_json_path(candidate).exists():
            candidate = f"{new_id}_{idx}"
            idx += 1

        src_dir = self.profile_dir(source_id)
        dst_dir = self.profile_dir(candidate)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()
        cloned = src.model_copy(
            update={
                "id": candidate,
                "name": copy_name,
                "created_at": now,
                "updated_at": now,
            }
        )
        return self.save_profile(cloned)

    def delete_profile(self, profile_id: str) -> None:
        pdir = self.profile_dir(profile_id)
        if not pdir.exists():
            raise FileNotFoundError(f"Perfil no encontrado: {profile_id}")
        shutil.rmtree(pdir)

    def list_available_fonts(self, profile_id: Optional[str] = None) -> list[dict[str, str]]:
        """List bundled global fonts and optional per-profile custom fonts."""
        seen: dict[str, dict[str, str]] = {}
        dirs = [self.fonts_dir]
        if profile_id and self.profile_dir(profile_id).exists():
            dirs.append(self.profile_fonts_dir(profile_id))

        for folder in dirs:
            if not folder.exists():
                continue
            scope = "profile" if folder != self.fonts_dir else "global"
            for fpath in sorted(folder.iterdir()):
                if fpath.suffix.lower() in {".ttf", ".otf"} and fpath.is_file():
                    display = fpath.stem.replace("-", " ").replace("_", " ")
                    seen[fpath.name] = {
                        "filename": fpath.name,
                        "display_name": display,
                        "scope": scope,
                        "path": str(fpath.resolve()),
                    }
        return list(seen.values())

    def resolve_font_path(self, profile_id: str, font_file: str) -> Optional[Path]:
        """Resolve a font file from profile fonts or shared _fonts directory."""
        for candidate in [
            self.profile_fonts_dir(profile_id) / font_file,
            self.fonts_dir / font_file,
        ]:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
        # Fallback to first bundled font
        for fpath in sorted(self.fonts_dir.glob("*.ttf")):
            return fpath.resolve()
        return None

    def resolve_custom_image_path(
        self,
        profile_id: str,
        image: CustomImage,
        episode_images_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        """Locate an image file either in episode images or profile images."""
        candidates = []
        if episode_images_dir:
            candidates.append(episode_images_dir / image.filename)
        candidates.append(self.images_dir(profile_id) / image.filename)
        for path in candidates:
            if path.exists() and path.is_file():
                return path.resolve()
        return None
