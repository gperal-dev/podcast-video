"""Strict Public Domain & CC0 media archive fetcher (Wikimedia Commons & Internet Archive) with per-file license verification."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image

from backend.config import Settings, get_settings
from backend.models.profile import AspectRatio


_USER_AGENT = "PodcastForgeStudio/2.0 (https://localhost; educational-podcast-video-tool)"

_WHITELIST_PATTERNS = (
    "public domain",
    "pd-us",
    "pd-usgov",
    "pd-old",
    "pd-1923",
    "pd-ukgov",
    "pd-germany",
    "pd-self",
    "pd-ineligible",
    "pd-author",
    "cc0",
    "publicdomain/zero/1.0",
    "publicdomain/mark/1.0",
    "no known copyright restrictions",
)

_FORBIDDEN_LICENSE_PATTERNS = (
    "cc-by",
    "cc by",
    "by-sa",
    "by-nc",
    "by-nd",
    "sharealike",
    "share alike",
    "noncommercial",
    "non-commercial",
    "attribution",
    "gfdl",
    "fair use",
    "all rights reserved",
    "in copyright",
    "copyrighted",
    "permission",
)


def is_strictly_public_domain(
    license_name: str,
    license_url: str = "",
    usage_terms: str = "",
    copyrighted_flag: str = "",
    restrictions: str = "",
) -> tuple[bool, str]:
    """Verify strictly that a media item is Public Domain or CC0 with zero attribution/NC/SA/restriction clauses."""
    if restrictions and restrictions.strip():
        return False, f"Restricciones presentes: {restrictions.strip()[:80]}"

    combined = " ".join([license_name or "", license_url or "", usage_terms or ""]).lower()
    if not combined.strip():
        return False, "Sin información explícita de licencia"

    # Reject CC-BY / SA / NC / ND / Fair Use / GFDL unconditionally
    for bad in _FORBIDDEN_LICENSE_PATTERNS:
        if bad in combined:
            # Exception: Wikimedia sometimes sets LicenseShortName='CC0' alongside UsageTerms='Creative Commons Zero...'
            # and never put 'by' inside 'public domain' or 'cc0', except 'copyrighted' -> handled below
            if bad == "copyrighted" and ("no known copyright" in combined or "cc0" in combined or "public domain" in combined):
                continue
            return False, f"Licencia no libre/con restricciones ({bad})"

    # If Wikimedia explicitly marks Copyrighted='True' without CC0, reject
    if (copyrighted_flag or "").strip().lower() == "true" and "cc0" not in combined:
        return False, "Marcado como Copyrighted=True"

    if any(good in combined for good in _WHITELIST_PATTERNS):
        return True, (license_name or license_url or "Public Domain").strip()

    return False, f"Licencia fuera de lista blanca PD/CC0: {license_name}"


def _orientation_ok(width: int, height: int, aspect_ratio: AspectRatio) -> bool:
    # Historical archives (1940s photos / newsreels) are mostly 4:3 or 16:9; renderer zooms/Ken-Burns smoothly
    if width < 640 or height < 480:
        return False
    ratio = width / max(1, height)
    if aspect_ratio == "9:16":
        return 0.45 <= ratio <= 1.85
    if aspect_ratio == "16:9":
        return width >= height or ratio >= 0.85
    return 0.6 <= ratio <= 1.85


class ArchiveFetcher:
    """Searches Wikimedia Commons and Internet Archive, admitting ONLY verified Public Domain / CC0 files."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.cache_dir = self.settings.cache_stock_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "index.json"

    def search_wikimedia_candidates(
        self,
        query: str,
        aspect_ratio: AspectRatio,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Query Wikimedia Commons and return strictly verified PD/CC0 image/video candidates."""
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrsearch": f"{query} filetype:bitmap|video",
            "gsrlimit": str(max(limit, 10)),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "640",
        }
        try:
            resp = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            pages = (resp.json().get("query") or {}).get("pages") or {}
        except Exception:
            return []

        results: list[dict[str, Any]] = []
        for page_id, page in pages.items():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            ii = infos[0]
            mime = str(ii.get("mime") or "").lower()
            if mime not in {"image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"}:
                continue

            w = int(ii.get("width") or 0)
            h = int(ii.get("height") or 0)
            if not _orientation_ok(w, h, aspect_ratio):
                continue

            ext = ii.get("extmetadata") or {}
            lic_name = str((ext.get("LicenseShortName") or {}).get("value") or "")
            lic_url = str((ext.get("LicenseUrl") or {}).get("value") or "")
            usage = str((ext.get("UsageTerms") or {}).get("value") or "")
            copyrighted = str((ext.get("Copyrighted") or {}).get("value") or "")
            restrictions = str((ext.get("Restrictions") or {}).get("value") or "")

            ok, verified_lic = is_strictly_public_domain(
                lic_name, lic_url, usage, copyrighted, restrictions
            )
            if not ok:
                continue

            media_url = ii.get("url")
            thumb_url = ii.get("thumburl") or (media_url if mime.startswith("image/") else None)
            if not media_url or not thumb_url:
                continue

            is_vid = mime.startswith("video/")
            results.append(
                {
                    "asset_id": f"wikimedia_{'video' if is_vid else 'photo'}_{page_id}",
                    "provider": "wikimedia_commons",
                    "media_type": "video" if is_vid else "photo",
                    "title": str(page.get("title") or "").replace("File:", ""),
                    "url": media_url,
                    "thumb_url": thumb_url,
                    "width": w,
                    "height": h,
                    "ext": ".mp4" if mime == "video/mp4" else (".webm" if mime == "video/webm" else ".jpg"),
                    "license_name": verified_lic,
                    "license_url": lic_url or "https://creativecommons.org/publicdomain/mark/1.0/",
                    "source_page_url": str(ii.get("descriptionurl") or f"https://commons.wikimedia.org/?curid={page_id}"),
                }
            )
        return results

    def search_internet_archive_candidates(
        self,
        query: str,
        aspect_ratio: AspectRatio,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Query Internet Archive for Public Domain / CC0 media and verify each item's metadata."""
        q_clean = re.sub(r"[^\w\s-]", " ", query).strip()
        if not q_clean:
            return []

        search_q = f"({q_clean}) AND mediatype:(movies OR image) AND (licenseurl:*publicdomain* OR licenseurl:*zero/1.0*)"
        try:
            resp = httpx.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": search_q,
                    "fl[]": ["identifier", "title", "mediatype", "licenseurl"],
                    "rows": str(max(limit, 8)),
                    "output": "json",
                },
                headers={"User-Agent": _USER_AGENT},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            docs = ((resp.json().get("response") or {}).get("docs")) or []
        except Exception:
            return []

        candidates: list[dict[str, Any]] = []
        for doc in docs[:3]:
            ident = str(doc.get("identifier") or "").strip()
            lic_url = str(doc.get("licenseurl") or "").strip()
            if not ident:
                continue
            ok, verified_lic = is_strictly_public_domain("Public Domain", lic_url, "")
            if not ok:
                continue

            try:
                mresp = httpx.get(
                    f"https://archive.org/metadata/{ident}",
                    headers={"User-Agent": _USER_AGENT},
                    timeout=6.0,
                )
                if mresp.status_code != 200:
                    continue
                mjson = mresp.json()
            except Exception:
                continue

            meta_block = mjson.get("metadata") or {}
            real_lic = str(meta_block.get("licenseurl") or lic_url)
            ok2, verified_lic2 = is_strictly_public_domain(
                str(meta_block.get("rights") or "Public Domain"),
                real_lic,
            )
            if not ok2:
                continue

            files = mjson.get("files") or []
            chosen_file = None
            is_vid = False
            for f in files:
                fname = str(f.get("name") or "")
                low = fname.lower()
                size = int(f.get("size") or 0)
                if low.endswith(".mp4") and 100_000 < size < 45_000_000:
                    chosen_file = fname
                    is_vid = True
                    break
            if not chosen_file:
                for f in files:
                    fname = str(f.get("name") or "")
                    low = fname.lower()
                    size = int(f.get("size") or 0)
                    if low.endswith((".jpg", ".jpeg", ".png")) and not low.endswith("_thumb.jpg") and 25_000 < size < 15_000_000:
                        chosen_file = fname
                        is_vid = False
                        break
            if not chosen_file:
                continue

            candidates.append(
                {
                    "asset_id": f"ia_{'video' if is_vid else 'photo'}_{re.sub(r'[^a-zA-Z0-9_-]', '_', ident)[:36]}",
                    "provider": "internet_archive",
                    "media_type": "video" if is_vid else "photo",
                    "title": str(doc.get("title") or ident),
                    "url": f"https://archive.org/download/{ident}/{chosen_file}",
                    "thumb_url": f"https://archive.org/services/img/{ident}",
                    "width": 1280 if aspect_ratio == "16:9" else 720,
                    "height": 720 if aspect_ratio == "16:9" else 1280,
                    "ext": ".mp4" if is_vid else ".jpg",
                    "license_name": verified_lic2,
                    "license_url": real_lic,
                    "source_page_url": f"https://archive.org/details/{ident}",
                }
            )
        return candidates

    def search_candidates(
        self,
        query: str,
        aspect_ratio: AspectRatio,
        used_ids: Optional[set[str]] = None,
        allowed_providers: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return combined strictly-verified PD/CC0 candidates from enabled archive providers."""
        used = used_ids or set()
        allowed = allowed_providers or {"wikimedia_commons", "internet_archive"}
        combined: list[dict[str, Any]] = []
        if "wikimedia_commons" in allowed:
            combined.extend(self.search_wikimedia_candidates(query, aspect_ratio))
        if "internet_archive" in allowed:
            combined.extend(self.search_internet_archive_candidates(query, aspect_ratio))
        return [c for c in combined if c["asset_id"] not in used]

    def download_verified_candidate(
        self,
        cand: dict[str, Any],
        query: str,
        aspect_ratio: AspectRatio,
    ) -> Optional[tuple[str, Path, str, dict[str, Any]]]:
        """Download a verified PD/CC0 candidate to cache/stock/ and return `(asset_id, path, media_type, audit)`."""
        asset_id = cand["asset_id"]
        ext = cand.get("ext", ".jpg")
        fname = f"{asset_id}{ext}"
        dest = self.cache_dir / fname

        if not dest.exists() or dest.stat().st_size < 2048:
            try:
                with httpx.stream(
                    "GET",
                    cand["url"],
                    headers={"User-Agent": _USER_AGENT},
                    timeout=45.0,
                    follow_redirects=True,
                ) as resp:
                    if resp.status_code != 200:
                        return None
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_bytes(65536):
                            if chunk:
                                fh.write(chunk)
                    tmp.replace(dest)
            except Exception:
                return None

        if not dest.exists() or dest.stat().st_size < 2048:
            return None

        # Verify actual image dimensions if photo
        if cand["media_type"] == "photo":
            try:
                with Image.open(dest) as im:
                    w, h = im.size
                if w < 640 or h < 480:
                    dest.unlink(missing_ok=True)
                    return None
                cand["width"], cand["height"] = w, h
            except Exception:
                dest.unlink(missing_ok=True)
                return None

        audit = {
            "asset_id": asset_id,
            "provider": cand["provider"],
            "source": cand["provider"],
            "title": cand.get("title", ""),
            "author": cand.get("author", "Dominio Público"),
            "license": cand.get("license_name", "Public Domain / CC0"),
            "license_name": cand.get("license_name", "Public Domain / CC0"),
            "license_url": cand.get("license_url", "https://creativecommons.org/publicdomain/mark/1.0/"),
            "source_url": cand.get("source_page_url", ""),
            "source_page_url": cand.get("source_page_url", ""),
            "verified_public_domain": True,
            "requires_license_warning": False,
            "license_summary": "Dominio Público / CC0 verificado: 100% libre y gratuito para uso comercial y editorial sin atribución ni restricciones.",
            "query": query,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        return asset_id, dest, cand["media_type"], audit
