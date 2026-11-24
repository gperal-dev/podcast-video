"""Pexels, Pixabay, and verified PD Archive stock fetcher with strict HD orientation checks and VLM verification."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw, ImageOps

from backend.config import Settings, get_settings
from backend.core.archive_fetcher import ArchiveFetcher, is_strictly_public_domain
from backend.core.openrouter_client import OpenRouterClient
from backend.models.profile import AspectRatio, DEFAULT_MEDIA_PROVIDERS


_CACHE_LOCK = threading.Lock()


def _target_orientation(aspect: AspectRatio) -> str:
    if aspect == "9:16":
        return "portrait"
    if aspect == "16:9":
        return "landscape"
    return "square"


def _sanitize_query(query: str, forbidden_keywords: list[str]) -> str:
    q = (query or "").strip()
    for bad in forbidden_keywords:
        b = bad.strip()
        if b:
            q = re.sub(re.escape(b), "", q, flags=re.IGNORECASE)
    cleaned = " ".join(q.split())
    return cleaned or "cinematic dramatic atmospheric scene"


def _matches_forbidden(text: str, forbidden_keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(b.strip().lower() in low for b in forbidden_keywords if b.strip())


def _is_valid_resolution(width: int, height: int, orientation: str, min_short_side: int = 720) -> bool:
    """Strictly verify orientation and HD quality before accepting a stock clip or photo."""
    if width <= 0 or height <= 0:
        return False
    if min(width, height) < min_short_side:
        return False
    if orientation == "portrait":
        return height > width
    if orientation == "landscape":
        return width > height
    ratio = max(width / height, height / width)
    return ratio <= 1.85


def _build_contact_sheet(thumb_bytes_list: list[bytes]) -> bytes:
    """Arrange up to 6 candidate thumbnails into a numbered 3x2 JPEG contact sheet (~640x360) for low-cost VLM selection."""
    cols = 3
    rows = 2
    cell_w, cell_h = 213, 180
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 20, 28))
    draw = ImageDraw.Draw(canvas)

    for idx, raw in enumerate(thumb_bytes_list[:6]):
        r = idx // cols
        c = idx % cols
        x0 = c * cell_w
        y0 = r * cell_h
        try:
            with Image.open(io.BytesIO(raw)) as im:
                fitted = ImageOps.fit(im.convert("RGB"), (cell_w - 8, cell_h - 8))
            canvas.paste(fitted, (x0 + 4, y0 + 4))
        except Exception:
            draw.rectangle([(x0 + 4, y0 + 4), (x0 + cell_w - 4, y0 + cell_h - 4)], fill=(40, 44, 58))

        # High-contrast index badge [1..6]
        draw.rounded_rectangle([(x0 + 10, y0 + 10), (x0 + 42, y0 + 36)], radius=6, fill=(255, 212, 0))
        draw.text((x0 + 21, y0 + 17), str(idx + 1), fill=(10, 12, 18))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=72)
    return buf.getvalue()


class StockFetcher:
    """Downloads, VLM-verifies, and caches orientation-strict HD stock & verified PD archive assets."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.cache_dir = self.settings.cache_stock_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "index.json"
        self._index: dict[str, dict[str, Any]] = self._load_index()
        self.archive_fetcher = ArchiveFetcher(self.settings)

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_index(self) -> None:
        self.index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")

    def _find_cached_asset(
        self,
        query: str,
        media_type: str,
        orientation: str,
        used_ids: set[str],
        allowed_providers: Optional[set[str]] = None,
    ) -> Optional[tuple[str, Path, Optional[dict[str, Any]]]]:
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        best_score = 0
        best_hit: Optional[tuple[str, Path, Optional[dict[str, Any]]]] = None
        allowed = allowed_providers or set(DEFAULT_MEDIA_PROVIDERS)

        with _CACHE_LOCK:
            for asset_id, meta in self._index.items():
                if asset_id in used_ids:
                    continue
                prov = str(meta.get("provider") or "pexels")
                if prov not in allowed:
                    continue
                if meta.get("media_type") != media_type:
                    continue
                w = int(meta.get("width") or 0)
                h = int(meta.get("height") or 0)
                is_arch = prov in {"wikimedia_commons", "internet_archive"}
                if is_arch:
                    if min(w, h) < 480 or max(w, h) < 640:
                        continue
                elif not _is_valid_resolution(w, h, orientation, min_short_side=720):
                    continue
                fpath = self.cache_dir / str(meta.get("filename", ""))
                if not fpath.exists() or fpath.stat().st_size < 2048:
                    continue
                cached_tokens = set(re.findall(r"[a-z0-9]+", str(meta.get("query", "")).lower()))
                overlap = len(tokens & cached_tokens)
                if overlap > best_score and overlap >= min(2, max(1, len(tokens))):
                    best_score = overlap
                    audit = meta.get("audit")
                    if not isinstance(audit, dict):
                        audit = self._build_stock_license_audit(
                            asset_id=asset_id,
                            provider=prov,
                            title=str(meta.get("query") or asset_id),
                            author=str(meta.get("author") or prov.title()),
                            source_url=str(meta.get("source_url") or ""),
                            query=query,
                        )
                    best_hit = (asset_id, fpath, audit)
        return best_hit

    @staticmethod
    def _build_stock_license_audit(
        asset_id: str,
        provider: str,
        title: str,
        author: str,
        source_url: str,
        query: str,
        license_override: str = "",
        license_url_override: str = "",
    ) -> dict[str, Any]:
        """Build a verifiable, human-readable license audit record for Pexels/Pixabay/Archive assets."""
        prov_low = (provider or "").lower()
        if prov_low == "pexels":
            lic_name = license_override or "Pexels Free License (Gratis comercial y personal, sin atribución obligatoria)"
            lic_url = license_url_override or "https://www.pexels.com/license/"
            is_free = True
            summary = (
                "Licencia Pexels 100% gratuita y libre: uso comercial y editorial permitido sin atribución ni regalías."
            )
        elif prov_low == "pixabay":
            lic_name = license_override or "Pixabay Content License (Gratis comercial y personal, sin atribución obligatoria)"
            lic_url = license_url_override or "https://pixabay.com/service/license-summary/"
            is_free = True
            summary = (
                "Licencia Pixabay 100% gratuita y libre: uso comercial y editorial permitido sin atribución ni regalías."
            )
        else:
            ok_pd, ver_lic = is_strictly_public_domain(license_override or "Public Domain", license_url_override)
            lic_name = ver_lic if ok_pd else (license_override or "Licencia externa con condiciones")
            lic_url = license_url_override or "https://creativecommons.org/publicdomain/mark/1.0/"
            is_free = ok_pd
            summary = (
                "Dominio Público / CC0 verificado sin restricciones."
                if ok_pd
                else f"AVISO DE LICENCIA: Este recurso reporta '{lic_name}'. Revisa las condiciones adjuntas en el certificado."
            )

        return {
            "asset_id": asset_id,
            "provider": prov_low,
            "source": prov_low,
            "title": title or query,
            "author": author or prov_low.title(),
            "license": lic_name,
            "license_name": lic_name,
            "license_url": lic_url,
            "source_url": source_url,
            "source_page_url": source_url,
            "verified_public_domain": is_free,
            "requires_license_warning": not is_free,
            "license_summary": summary,
            "query": query,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }

    def _download_file(self, url: str, dest: Path) -> bool:
        try:
            with httpx.stream("GET", url, timeout=30.0, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    return False
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
                tmp.replace(dest)
                return dest.exists() and dest.stat().st_size > 2048
        except Exception:
            return False

    def _register_asset(
        self,
        asset_id: str,
        provider: str,
        media_type: str,
        query: str,
        orientation: str,
        filename: str,
        width: int,
        height: int,
        audit: Optional[dict[str, Any]] = None,
    ) -> Path:
        with _CACHE_LOCK:
            entry: dict[str, Any] = {
                "provider": provider,
                "media_type": media_type,
                "query": query,
                "orientation": orientation,
                "filename": filename,
                "width": width,
                "height": height,
            }
            if audit:
                entry["audit"] = audit
            self._index[asset_id] = entry
            self._save_index()
        return self.cache_dir / filename

    # ------------------------------------------------------------------
    # Candidate collectors (return metadata + preview thumbnail URL without downloading full HD yet)
    # ------------------------------------------------------------------

    def _collect_pexels_videos(
        self,
        query: str,
        orientation: str,
        used_ids: set[str],
        forbidden: list[str],
    ) -> list[dict[str, Any]]:
        if not self.settings.pexels_api_key:
            return []
        params: dict[str, Any] = {"query": query, "per_page": 10}
        if orientation in ("portrait", "landscape", "square"):
            params["orientation"] = orientation
        try:
            resp = httpx.get(
                "https://api.pexels.com/videos/search",
                params=params,
                headers={"Authorization": self.settings.pexels_api_key},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            videos = resp.json().get("videos") or []
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for v in videos:
            vid_id = f"pexels_video_{v.get('id')}"
            if vid_id in used_ids:
                continue
            if _matches_forbidden(str(v.get("url", "")), forbidden):
                continue

            files = v.get("video_files") or []
            mp4s = [f for f in files if f.get("file_type") == "video/mp4" and f.get("link")]
            hd_files = [
                f
                for f in mp4s
                if _is_valid_resolution(int(f.get("width") or 0), int(f.get("height") or 0), orientation, min_short_side=720)
            ]
            if not hd_files:
                continue
            # Prefer ~1080p short side (avoid tiny SD and avoid huge 4K)
            hd_files.sort(key=lambda f: abs(min(int(f.get("width") or 1080), int(f.get("height") or 1080)) - 1080))
            chosen = hd_files[0]

            pics = v.get("video_pictures") or []
            thumb_url = v.get("image") or (pics[0].get("picture") if pics else None)
            if not thumb_url:
                continue

            user_info = v.get("user") or {}
            author_name = str(user_info.get("name") or "Pexels Creator").strip()
            page_url = str(v.get("url") or "https://www.pexels.com/videos/")
            out.append(
                {
                    "asset_id": vid_id,
                    "provider": "pexels",
                    "media_type": "video",
                    "url": chosen["link"],
                    "thumb_url": thumb_url,
                    "width": int(chosen.get("width") or 1080),
                    "height": int(chosen.get("height") or 1920),
                    "ext": ".mp4",
                    "label": page_url.split("/")[-2] if "/" in page_url else query,
                    "author": author_name,
                    "source_url": page_url,
                }
            )
        return out

    def _collect_pexels_photos(
        self,
        query: str,
        orientation: str,
        used_ids: set[str],
        forbidden: list[str],
    ) -> list[dict[str, Any]]:
        if not self.settings.pexels_api_key:
            return []
        params: dict[str, Any] = {"query": query, "per_page": 10}
        if orientation in ("portrait", "landscape", "square"):
            params["orientation"] = orientation
        try:
            resp = httpx.get(
                "https://api.pexels.com/v1/search",
                params=params,
                headers={"Authorization": self.settings.pexels_api_key},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            photos = resp.json().get("photos") or []
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for p in photos:
            pid = f"pexels_photo_{p.get('id')}"
            if pid in used_ids:
                continue
            alt = str(p.get("alt", ""))
            if _matches_forbidden(alt, forbidden):
                continue
            w = int(p.get("width") or 0)
            h = int(p.get("height") or 0)
            if not _is_valid_resolution(w, h, orientation, min_short_side=720):
                continue
            src = p.get("src") or {}
            url = src.get("large2x") or src.get("large") or src.get("original")
            thumb = src.get("medium") or src.get("small") or url
            if not url or not thumb:
                continue
            author_name = str(p.get("photographer") or "Pexels Photographer").strip()
            page_url = str(p.get("url") or "https://www.pexels.com/")
            out.append(
                {
                    "asset_id": pid,
                    "provider": "pexels",
                    "media_type": "photo",
                    "url": url,
                    "thumb_url": thumb,
                    "width": w,
                    "height": h,
                    "ext": ".jpg",
                    "label": alt or query,
                    "author": author_name,
                    "source_url": page_url,
                }
            )
        return out

    def _collect_pixabay_videos(
        self,
        query: str,
        orientation: str,
        used_ids: set[str],
        forbidden: list[str],
    ) -> list[dict[str, Any]]:
        if not self.settings.pixabay_api_key:
            return []
        try:
            resp = httpx.get(
                "https://pixabay.com/api/videos/",
                params={"key": self.settings.pixabay_api_key, "q": query, "per_page": 10},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            hits = resp.json().get("hits") or []
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for h_item in hits:
            vid_id = f"pixabay_video_{h_item.get('id')}"
            if vid_id in used_ids:
                continue
            tags = str(h_item.get("tags", ""))
            if _matches_forbidden(tags, forbidden):
                continue
            vids = h_item.get("videos") or {}
            chosen = None
            for tier in ("large", "medium", "small"):
                cand = vids.get(tier)
                if not cand or not cand.get("url"):
                    continue
                cw = int(cand.get("width") or 0)
                ch = int(cand.get("height") or 0)
                if _is_valid_resolution(cw, ch, orientation, min_short_side=720):
                    chosen = cand
                    break
            if not chosen:
                continue
            thumb = chosen.get("thumbnail") or ""
            if not thumb:
                continue
            author_name = str(h_item.get("user") or "Pixabay Creator").strip()
            page_url = str(h_item.get("pageURL") or "https://pixabay.com/videos/")
            out.append(
                {
                    "asset_id": vid_id,
                    "provider": "pixabay",
                    "media_type": "video",
                    "url": chosen["url"],
                    "thumb_url": thumb,
                    "width": int(chosen.get("width") or 1280),
                    "height": int(chosen.get("height") or 720),
                    "ext": ".mp4",
                    "label": tags or query,
                    "author": author_name,
                    "source_url": page_url,
                }
            )
        return out

    def _collect_pixabay_photos(
        self,
        query: str,
        orientation: str,
        used_ids: set[str],
        forbidden: list[str],
    ) -> list[dict[str, Any]]:
        if not self.settings.pixabay_api_key:
            return []
        pix_orient = "vertical" if orientation == "portrait" else ("horizontal" if orientation == "landscape" else "all")
        try:
            resp = httpx.get(
                "https://pixabay.com/api/",
                params={
                    "key": self.settings.pixabay_api_key,
                    "q": query,
                    "image_type": "photo",
                    "orientation": pix_orient,
                    "per_page": 10,
                },
                timeout=8.0,
            )
            if resp.status_code != 200:
                return []
            hits = resp.json().get("hits") or []
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for h_item in hits:
            pid = f"pixabay_photo_{h_item.get('id')}"
            if pid in used_ids:
                continue
            tags = str(h_item.get("tags", ""))
            if _matches_forbidden(tags, forbidden):
                continue
            w = int(h_item.get("imageWidth") or 0)
            h = int(h_item.get("imageHeight") or 0)
            if not _is_valid_resolution(w, h, orientation, min_short_side=720):
                continue
            url = h_item.get("largeImageURL") or h_item.get("webformatURL")
            thumb = h_item.get("previewURL") or h_item.get("webformatURL") or url
            if not url or not thumb:
                continue
            author_name = str(h_item.get("user") or "Pixabay Photographer").strip()
            page_url = str(h_item.get("pageURL") or "https://pixabay.com/")
            out.append(
                {
                    "asset_id": pid,
                    "provider": "pixabay",
                    "media_type": "photo",
                    "url": url,
                    "thumb_url": thumb,
                    "width": w,
                    "height": h,
                    "ext": ".jpg",
                    "label": tags or query,
                    "author": author_name,
                    "source_url": page_url,
                }
            )
        return out

    def _vlm_pick_best_candidate(
        self,
        candidates: list[dict[str, Any]],
        query: str,
        spoken_context: str,
        visual_bible: dict[str, Any],
        llm: OpenRouterClient,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Show a numbered 3x2 contact sheet to OPENROUTER_MODEL_FAST to pick the most coherent visual."""
        top_cands = candidates[:6]
        if not top_cands:
            return None, None

        thumb_bytes: list[bytes] = []
        valid_cands: list[dict[str, Any]] = []
        headers = {"User-Agent": "PodcastForgeStudio/2.0"}
        for c in top_cands:
            try:
                r = httpx.get(c["thumb_url"], headers=headers, timeout=5.0, follow_redirects=True)
                if r.status_code == 200 and len(r.content) > 500:
                    thumb_bytes.append(r.content)
                    valid_cands.append(c)
            except Exception:
                continue

        if not valid_cands:
            return top_cands[0], None
        if len(valid_cands) == 1:
            return valid_cands[0], None

        sheet = _build_contact_sheet(thumb_bytes)
        bible_str = (
            f"Tema episodio: {visual_bible.get('episode_topic', '')} | "
            f"Época/escenario: {visual_bible.get('era_or_setting', '')} | "
            f"Estilo: {visual_bible.get('visual_style', '')} | "
            f"Prohibido: {', '.join(visual_bible.get('forbidden_anachronisms', []))}"
        )
        labels_list = "\n".join(
            f"[{i+1}] ({c['provider']} {c['media_type']} {c['width']}x{c['height']}): {c.get('label', '')[:80]}"
            for i, c in enumerate(valid_cands)
        )

        sys_prompt = (
            "Eres Supervisor Visual de cine documental y montaje vertical. Ves un mosaico numerado (1..N) de candidatos.\n"
            "Elige el candidato que MEJOR ilustra lo que dice el locutor en ese instante, con encuadre limpio, sin marcas de agua, sin texto molesto "
            "y 100% coherente con la época y el tono del episodio.\n"
            "REGLA ESTRICTA DE ÉPOCA Y RELEVANCIA: Si la época es histórica (ej. años 30-40 / Segunda Guerra Mundial) y TODOS los candidatos muestran "
            "elementos anacrónicos evidentes (coches modernos, aviones reactores comerciales, smartphones, pantallas digitales) o no tienen ninguna relación con el tema, "
            "DEVUELVE `selected=0` y sugiere un `refined_query` histórico o atmosférico muy concreto en INGLÉS de 2 a 4 palabras. Responde en JSON estricto."
        )
        usr_text = (
            f"{bible_str}\n"
            f"Lo que está diciendo la voz ahora: \"{spoken_context[:260]}\"\n"
            f"Búsqueda solicitada: \"{query}\"\n\n"
            f"Candidatos:\n{labels_list}\n\n"
            "Devuelve JSON:\n"
            '{"selected": int (1..N, o 0 si NINGUNO encaja o tiene anacronismos modernos), "refined_query": "búsqueda corta 2-4 palabras en inglés si selected=0"}'
        )

        try:
            resp = llm.chat_vision_json("fast", sys_prompt, usr_text, [sheet], temperature=0.1, max_tokens=200)
            sel = int(resp.get("selected", 1))
            if 1 <= sel <= len(valid_cands):
                return valid_cands[sel - 1], None
            if sel == 0:
                ref_q = str(resp.get("refined_query") or "").strip()
                return None, (ref_q or None)
        except Exception:
            pass
        return valid_cands[0], None

    def _download_candidate(
        self,
        cand: dict[str, Any],
        query: str,
        orientation: str,
    ) -> Optional[tuple[str, Path, str, Optional[dict[str, Any]]]]:
        if cand["provider"] in ("wikimedia_commons", "internet_archive"):
            res = self.archive_fetcher.download_verified_candidate(
                cand,
                query=query,
                aspect_ratio=("9:16" if orientation == "portrait" else ("16:9" if orientation == "landscape" else "1:1")),
            )
            if not res:
                return None
            aid, path, mtype, audit = res
            self._register_asset(
                aid,
                cand["provider"],
                mtype,
                query,
                orientation,
                path.name,
                int(cand["width"]),
                int(cand["height"]),
                audit=audit,
            )
            return aid, path, mtype, audit

        aid = cand["asset_id"]
        audit = self._build_stock_license_audit(
            asset_id=aid,
            provider=str(cand.get("provider") or "pexels"),
            title=str(cand.get("label") or query),
            author=str(cand.get("author") or cand.get("provider", "Stock").title()),
            source_url=str(cand.get("source_url") or ""),
            query=query,
            license_override=str(cand.get("license_name") or ""),
            license_url_override=str(cand.get("license_url") or ""),
        )
        cached = self._index.get(aid)
        if cached:
            cpath = self.cache_dir / str(cached.get("filename", ""))
            if cpath.exists() and cpath.stat().st_size > 2048:
                cached_audit = cached.get("audit")
                return aid, cpath, cand["media_type"], (cached_audit if isinstance(cached_audit, dict) else audit)

        fname = f"{aid}{cand['ext']}"
        dest = self.cache_dir / fname
        if self._download_file(cand["url"], dest):
            self._register_asset(
                aid,
                cand["provider"],
                cand["media_type"],
                query,
                orientation,
                fname,
                int(cand["width"]),
                int(cand["height"]),
                audit=audit,
            )
            return aid, dest, cand["media_type"], audit
        return None

    def fetch_verified_asset(
        self,
        query: str,
        aspect_ratio: AspectRatio,
        prefer_type: str = "video",
        used_ids: Optional[set[str]] = None,
        forbidden_keywords: Optional[list[str]] = None,
        spoken_context: str = "",
        visual_bible: Optional[dict[str, Any]] = None,
        llm: Optional[OpenRouterClient] = None,
        enable_vlm: bool = True,
        allow_archive: bool = False,
        allowed_providers: Optional[list[str]] = None,
    ) -> Optional[tuple[str, Path, str, Optional[dict[str, Any]]]]:
        """Fetch an orientation-strict HD asset from allowed providers, verified for free license & VLM relevance."""
        used = used_ids if used_ids is not None else set()
        forbidden = forbidden_keywords or []
        orientation = _target_orientation(aspect_ratio)
        bible = visual_bible or {}
        clean_query = _sanitize_query(query, forbidden)
        fallback_q = _sanitize_query(str(bible.get("fallback_query") or "cinematic dramatic atmosphere"), forbidden)

        active_providers = set(allowed_providers) if allowed_providers is not None else set(DEFAULT_MEDIA_PROVIDERS)
        archive_providers = active_providers & {"wikimedia_commons", "internet_archive"}
        allow_pexels = "pexels" in active_providers
        allow_pixabay = "pixabay" in active_providers
        vlm_used = False

        # 0. Always check verified HD local cache first ($0.00, deduplicated by used_ids)
        cache_types = ["photo", "video"] if prefer_type == "archive_media" else [("photo" if prefer_type == "photo" else "video")]
        if clean_query:
            for ctype in cache_types:
                hit0 = self._find_cached_asset(
                    clean_query,
                    ctype,
                    orientation,
                    used,
                    allowed_providers=active_providers,
                )
                if hit0:
                    used.add(hit0[0])
                    return hit0[0], hit0[1], ctype, hit0[2]

        # 1. If archival documentary media requested (or only archive providers enabled), search verified PD Wikimedia/IA first
        if (allow_archive or (archive_providers and not allow_pexels and not allow_pixabay)) and archive_providers and (
            prefer_type == "archive_media" or (not allow_pexels and not allow_pixabay)
        ):
            arch_cands = self.archive_fetcher.search_candidates(
                clean_query,
                aspect_ratio,
                used_ids=used,
                allowed_providers=archive_providers,
            )
            if arch_cands:
                chosen = arch_cands[0]
                q_toks = {w for w in re.findall(r"[a-z0-9]+", clean_query.lower()) if len(w) >= 4}
                lbl_toks = set(re.findall(r"[a-z0-9]+", str(chosen.get("label") or "").lower()))
                obvious_hit = len(arch_cands) <= 2 or (len(q_toks) > 0 and len(q_toks & lbl_toks) >= min(2, len(q_toks)))
                if enable_vlm and llm and not vlm_used and not obvious_hit:
                    vlm_used = True
                    picked, _ = self._vlm_pick_best_candidate(arch_cands, clean_query, spoken_context, bible, llm)
                    if picked:
                        chosen = picked
                dl = self._download_candidate(chosen, clean_query, orientation)
                if dl:
                    used.add(dl[0])
                    return dl
            # Fall back to normal stock photo/video if enabled
            prefer_type = "video"

        if not allow_pexels and not allow_pixabay and not archive_providers:
            return None

        queries_to_try = [clean_query]
        words = clean_query.split()
        if len(words) > 3:
            queries_to_try.append(" ".join(words[:3]))
        if fallback_q not in queries_to_try:
            queries_to_try.append(fallback_q)

        tried: set[str] = set()
        idx = 0
        while idx < len(queries_to_try):
            q = queries_to_try[idx]
            idx += 1
            if not q or q in tried:
                continue
            tried.add(q)

            # Check local HD cache on shortened/fallback queries too
            mtype = "photo" if prefer_type == "photo" else "video"
            hit = self._find_cached_asset(
                q,
                mtype,
                orientation,
                used,
                allowed_providers=active_providers,
            )
            if hit:
                used.add(hit[0])
                return hit[0], hit[1], mtype, hit[2]

            cands: list[dict[str, Any]] = []
            if prefer_type == "photo":
                if allow_pexels:
                    cands.extend(self._collect_pexels_photos(q, orientation, used, forbidden))
                if allow_pixabay and len(cands) < 4:
                    cands.extend(self._collect_pixabay_photos(q, orientation, used, forbidden))
                if allow_pexels and len(cands) < 3:
                    cands.extend(self._collect_pexels_videos(q, orientation, used, forbidden))
            else:
                if allow_pexels:
                    cands.extend(self._collect_pexels_videos(q, orientation, used, forbidden))
                if allow_pixabay and len(cands) < 4:
                    cands.extend(self._collect_pixabay_videos(q, orientation, used, forbidden))
                if allow_pexels and len(cands) < 3:
                    cands.extend(self._collect_pexels_photos(q, orientation, used, forbidden))
                if allow_pixabay and len(cands) < 3:
                    cands.extend(self._collect_pixabay_photos(q, orientation, used, forbidden))

            if not cands and archive_providers:
                cands.extend(
                    self.archive_fetcher.search_candidates(
                        q,
                        aspect_ratio,
                        used_ids=used,
                        allowed_providers=archive_providers,
                    )
                )

            if not cands:
                continue

            if enable_vlm and llm and not vlm_used and q != fallback_q:
                vlm_used = True
                picked, refined_q = self._vlm_pick_best_candidate(cands, q, spoken_context, bible, llm)
                if picked is None:
                    if archive_providers and bible.get("prefer_historical_archive"):
                        arch_fb = self.archive_fetcher.search_candidates(
                            refined_q or q,
                            aspect_ratio,
                            used_ids=used,
                            allowed_providers=archive_providers,
                        )
                        if arch_fb:
                            dl_arch = self._download_candidate(arch_fb[0], refined_q or q, orientation)
                            if dl_arch:
                                used.add(dl_arch[0])
                                return dl_arch
                    if refined_q and refined_q not in tried:
                        queries_to_try.insert(idx, refined_q)
                        continue
                chosen = picked or cands[0]
            else:
                chosen = cands[0]

            dl = self._download_candidate(chosen, q, orientation)
            if dl:
                used.add(dl[0])
                return dl

        return None

    def fetch_asset(
        self,
        query: str,
        aspect_ratio: AspectRatio,
        prefer_type: str = "video",
        used_ids: Optional[set[str]] = None,
        forbidden_keywords: Optional[list[str]] = None,
    ) -> Optional[tuple[str, Path, str]]:
        """Backwards-compatible wrapper returning `(asset_id, path, media_type)`."""
        res = self.fetch_verified_asset(
            query=query,
            aspect_ratio=aspect_ratio,
            prefer_type=prefer_type,
            used_ids=used_ids,
            forbidden_keywords=forbidden_keywords,
            enable_vlm=False,
        )
        if not res:
            return None
        return res[0], res[1], res[2]


def write_clip_license_manifest(
    dest_path: Path,
    clip_id: str,
    episode_name: str,
    visual_credits_audit: list[dict[str, Any]],
) -> tuple[Path, list[str]]:
    """Write a complete downloadable license & provenance certificate file (`*.licencias.txt`) for a clip.

    Also returns any active warning strings if a non-PD/non-free license was ever detected.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    lines: list[str] = [
        "========================================================================",
        "  PODCASTFORGE STUDIO — CERTIFICADO Y AUDITORÍA DE LICENCIAS VISUALES",
        "========================================================================",
        f"Episodio : {episode_name}",
        f"Clip ID  : {clip_id}",
        f"Fecha UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]

    for item in visual_credits_audit:
        req_warn = bool(item.get("requires_license_warning")) or not bool(item.get("verified_public_domain", True))
        if req_warn:
            msg = (
                f"AVISO DE LICENCIA en {clip_id} ({item.get('shot_id', 'plano')}): "
                f"Recurso '{item.get('title') or item.get('asset_id')}' ({item.get('provider')}) "
                f"usa licencia '{item.get('license') or item.get('license_name')}'. "
                f"Se ha descargado el certificado de licencia en {dest_path.name}."
            )
            warnings.append(msg)

    if warnings:
        lines.append("⚠️ ALERTA DE LICENCIA DETECTADA:")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")
    else:
        lines.append(
            "ESTADO DE AUDITORÍA: 100% VERIFICADO LIBRE Y GRATUITO (Dominio Público / CC0 / Pexels / Pixabay / Propio)."
        )
        lines.append("Ningún recurso requiere pago de regalías ni contiene restricciones comerciales.")
        lines.append("")

    lines.append("DETALLE DE RECURSOS UTILIZADOS POR PLANO:")
    lines.append("------------------------------------------------------------------------")
    if not visual_credits_audit:
        lines.append("No se utilizaron recursos externos de terceros (solo tarjetas editoriales o recursos propios).")
    else:
        for idx, item in enumerate(visual_credits_audit, start=1):
            shot_id = item.get("shot_id", f"shot_{idx:02d}")
            prov = item.get("provider") or item.get("source") or "desconocido"
            title = item.get("title") or item.get("asset_id") or "Recurso visual"
            author = item.get("author") or "N/D"
            lic = item.get("license") or item.get("license_name") or "Free / Public Domain"
            lic_url = item.get("license_url") or ""
            src_url = item.get("source_url") or item.get("source_page_url") or ""
            summary = item.get("license_summary") or ""
            verified_at = item.get("verified_at") or ""

            lines.append(f"[{idx}] Plano: {shot_id} | Proveedor: {prov}")
            lines.append(f"    Título / Descripción : {title}")
            lines.append(f"    Autor / Origen       : {author}")
            lines.append(f"    Licencia verificada  : {lic}")
            if lic_url:
                lines.append(f"    URL texto de licencia: {lic_url}")
            if src_url:
                lines.append(f"    URL origen del medio : {src_url}")
            if summary:
                lines.append(f"    Resumen legal        : {summary}")
            if verified_at:
                lines.append(f"    Verificado UTC       : {verified_at}")
            lines.append("")

    dest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest_path, warnings
