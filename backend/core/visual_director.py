"""Visual director v2: Visual Bible coherence, zero gap-fillers, organic pause-snapped pacing, and PD archive support."""

from __future__ import annotations

import json
from typing import Any, Optional

from backend.core.openrouter_client import OpenRouterClient
from backend.models.job import (
    ClipCandidate,
    StoryboardPlan,
    StoryboardShot,
    WordTimestamp,
)
from backend.models.profile import CustomImage, DEFAULT_MEDIA_PROVIDERS, PodcasterProfile


def _group_words_timed_chunks(
    words: list[WordTimestamp],
    chunk_sec: float = 4.5,
) -> list[dict[str, Any]]:
    """Group mapped words into spoken thought chunks aligned to natural pauses/punctuation."""
    if not words:
        return []
    chunks: list[dict[str, Any]] = []
    cur_words: list[WordTimestamp] = []
    cur_start = words[0].start

    for idx, w in enumerate(words):
        if not cur_words:
            cur_start = w.start
        cur_words.append(w)
        dur = w.end - cur_start
        next_gap = (words[idx + 1].start - w.end) if idx + 1 < len(words) else 1.0
        ends_sentence = w.word.strip().endswith((".", "?", "!"))
        if (dur >= chunk_sec and (ends_sentence or next_gap >= 0.20)) or dur >= chunk_sec * 1.45:
            chunks.append(
                {
                    "start": round(cur_start, 2),
                    "end": round(w.end, 2),
                    "text": " ".join(x.word for x in cur_words),
                }
            )
            cur_words = []

    if cur_words:
        chunks.append(
            {
                "start": round(cur_start, 2),
                "end": round(cur_words[-1].end, 2),
                "text": " ".join(x.word for x in cur_words),
            }
        )
    return chunks


def _extract_spoken_context_for_shot(
    words: list[WordTimestamp],
    start: float,
    end: float,
    fallback: str,
) -> str:
    tokens = [w.word for w in words if w.end >= start - 0.3 and w.start <= end + 0.3]
    text = " ".join(tokens).strip()
    return text or fallback


def _snap_to_pause_boundary(
    t: float,
    words: list[WordTimestamp],
    tolerance: float = 0.45,
) -> float:
    """Snap a visual cut slightly to the nearest spoken sentence/pause boundary for organic rhythm."""
    if len(words) < 2:
        return round(t, 2)
    best_t = t
    best_dist = tolerance
    for i in range(len(words) - 1):
        boundary = (words[i].end + words[i + 1].start) * 0.5
        gap = words[i + 1].start - words[i].end
        ends_sent = words[i].word.strip().endswith((".", "?", "!", ","))
        if gap >= 0.15 or ends_sent:
            dist = abs(boundary - t)
            if dist < best_dist:
                best_dist = dist
                best_t = boundary
    return round(best_t, 2)


def _normalize_storyboard_coverage(
    shots: list[StoryboardShot],
    duration: float,
    aspect_ratio: str,
    words: list[WordTimestamp],
    fallback_query: str,
    min_shot_sec: float,
    outro_image: Optional[CustomImage] = None,
    cta_title: str = "",
) -> list[StoryboardShot]:
    """Ensure seamless contiguous coverage [0, duration] with ZERO artificial gap-filler micro-shots."""
    dur = max(1.0, float(duration))
    ordered = sorted(shots, key=lambda s: (float(s.start), float(s.end)))

    cleaned: list[StoryboardShot] = []
    cursor = 0.0

    for sh in ordered:
        s = max(cursor, float(sh.start))
        e = min(dur, float(sh.end))

        # Absorb any gap into the previous shot (or pull current shot start back) — NEVER insert podcast filler!
        if s > cursor + 0.02:
            if cleaned:
                cleaned[-1] = cleaned[-1].model_copy(update={"end": round(s, 2)})
                cursor = s
            else:
                s = 0.0

        # Snap cut cleanly toward nearest speech pause
        if e < dur - 1.0:
            snapped = _snap_to_pause_boundary(e, words, tolerance=0.45)
            if snapped > cursor + min_shot_sec * 0.75 and snapped < dur - 0.8:
                e = snapped

        # Merge ultra-short shots into previous shot so there are no jarring flashes
        if (e - cursor) < max(2.2, min_shot_sec * 0.65) and cleaned and e < dur - 0.5:
            cleaned[-1] = cleaned[-1].model_copy(update={"end": round(e, 2)})
            cursor = e
            continue

        if e <= cursor + 0.4:
            continue

        cleaned.append(
            sh.model_copy(
                update={
                    "id": f"shot_{len(cleaned) + 1:02d}",
                    "start": round(cursor, 2),
                    "end": round(e, 2),
                }
            )
        )
        cursor = e

    if not cleaned:
        pace = 4.5 if aspect_ratio == "9:16" else 9.0
        t = 0.0
        while t < dur - 0.1:
            nxt = min(dur, t + pace)
            if dur - nxt < 2.2:
                nxt = dur
            cleaned.append(
                StoryboardShot(
                    id=f"shot_{len(cleaned) + 1:02d}",
                    start=round(t, 2),
                    end=round(nxt, 2),
                    source_type="stock_video",
                    stock_query=fallback_q(fallback_query),
                    reason="Plano continuo atmosférico",
                )
            )
            t = nxt
    elif cursor < dur - 0.02:
        cleaned[-1] = cleaned[-1].model_copy(update={"end": round(dur, 2)})

    # Ensure outro_cta custom image appears cleanly in final ~3s if configured
    if outro_image is not None and dur >= 6.0:
        outro_len = min(3.0, dur * 0.25)
        cut_pt = round(dur - outro_len, 2)
        trimmed: list[StoryboardShot] = []
        for sh in cleaned:
            if sh.start >= cut_pt:
                continue
            if sh.end > cut_pt:
                sh = sh.model_copy(update={"end": cut_pt})
            if sh.end - sh.start >= 0.8:
                trimmed.append(sh)
        if trimmed and trimmed[-1].end < cut_pt:
            trimmed[-1] = trimmed[-1].model_copy(update={"end": cut_pt})
        trimmed.append(
            StoryboardShot(
                id=f"shot_{len(trimmed) + 1:02d}",
                start=cut_pt,
                end=round(dur, 2),
                source_type="custom_image",
                custom_image_id=outro_image.id,
                custom_image_filename=outro_image.filename,
                display_mode="outro_cta",
                card_title=cta_title,
                reason=f"Cierre CTA con imagen {outro_image.title}",
            )
        )
        cleaned = trimmed

    for idx, sh in enumerate(cleaned, start=1):
        sh.id = f"shot_{idx:02d}"
    if cleaned:
        cleaned[0].start = 0.0
        cleaned[-1].end = round(dur, 2)
        if len(cleaned) > 1 and cleaned[0].source_type == "custom_image":
            max_open = 5.0 if aspect_ratio == "9:16" else 7.0
            if cleaned[0].end - cleaned[0].start > max_open and cleaned[1].end > max_open + 1.0:
                cleaned[0] = cleaned[0].model_copy(update={"end": round(max_open, 2)})
                cleaned[1] = cleaned[1].model_copy(update={"start": round(max_open, 2)})
    return cleaned


def fallback_q(q: str) -> str:
    return (q or "").strip() or "cinematic atmospheric scene"


def generate_storyboard(
    clip: ClipCandidate,
    profile: PodcasterProfile,
    episode_images: list[CustomImage],
    llm: OpenRouterClient,
    visual_bible: Optional[dict[str, Any]] = None,
    allowed_providers: Optional[list[str]] = None,
) -> StoryboardPlan:
    """Ask OPENROUTER_MODEL_FAST to build a cohesive, organic StoryboardPlan guided by Visual Bible."""
    duration = clip.edl.edited_duration if clip.edl else max(1.0, clip.orig_end - clip.orig_start)
    active_provs = set(
        allowed_providers
        if allowed_providers is not None
        else (profile.visual.allowed_media_providers or list(DEFAULT_MEDIA_PROVIDERS))
    )
    allow_custom = "custom_images" in active_provs
    allow_stock = bool(active_provs & {"pexels", "pixabay"})
    allow_archive_provs = bool(active_provs & {"wikimedia_commons", "internet_archive"})

    all_images = [*profile.custom_images, *episode_images] if allow_custom else []
    image_lookup = {img.id: img for img in all_images}

    story_images = [img for img in all_images if img.display_mode not in ("watermark", "outro_cta")]
    outro_images = [img for img in all_images if img.display_mode == "outro_cta"]
    outro_img = outro_images[0] if outro_images else None

    is_short = clip.clip_type.startswith("short")
    raw_min = profile.visual.short_cut_min_sec if is_short else profile.visual.long_cut_min_sec
    pace_min = max(3.5, min(raw_min, 5.0)) if is_short else max(6.0, raw_min)
    pace_max = profile.visual.short_cut_max_sec if is_short else profile.visual.long_cut_max_sec
    pace_max = max(pace_min + 1.2, pace_max)
    max_shots = max(6, int(duration / max(3.8, pace_min)) + 2) if is_short else max(10, int(duration / max(6.0, pace_min)) + 3)

    chunks = _group_words_timed_chunks(
        clip.mapped_words,
        chunk_sec=(pace_min + pace_max) * 0.5,
    )
    if not chunks:
        chunks = [{"start": 0.0, "end": round(duration, 2), "text": clip.summary or "Narración principal"}]

    bible = visual_bible or {}
    fallback_query = str(bible.get("fallback_query") or "cinematic atmospheric light")
    allow_archive = bool(
        allow_archive_provs
        and (
            (profile.visual.enable_public_domain_archives and bible.get("prefer_historical_archive"))
            or not allow_stock
        )
    )

    img_manifest_lines = [
        f"- id='{img.id}' | título='{img.title}' | prioridad={img.priority} | "
        f"modo={img.display_mode} | cuándo usar: {img.usage_instructions or 'cuando encaje temáticamente'}"
        for img in story_images
    ]
    img_manifest = "\n".join(img_manifest_lines) if img_manifest_lines else "Ninguna imagen de historia disponible."

    forbidden = list(profile.visual.forbidden_stock_keywords) + list(bible.get("forbidden_anachronisms") or [])
    forbidden_str = ", ".join(dict.fromkeys(str(x) for x in forbidden if x)) or "ninguna"

    if allow_archive and not allow_stock:
        archive_rule = (
            "- Los proveedores de stock general están desactivados: usa 'archive_media' (o 'custom_image' / tarjetas) para todos los planos externos."
        )
    elif allow_archive:
        archive_rule = (
            "- En temas históricos, PRIORIZA 'archive_media' (hasta 50-55% de los planos) siempre que la voz nombre "
            "aviones históricos, vehículos, máquinas, ubicaciones o personas reales de la época (evita usar stock moderno en esos planos)."
        )
    else:
        archive_rule = "- Usa 'stock_video' principalmente (>= 75% de los planos) y 'stock_photo' solo puntualmente."
    card_instruction = (
        "En vídeos medios/largos, intercala cada 60-90s una 'title_card' o 'quote_card' (4s) para evitar fatiga visual."
        if not is_short
        else "En Shorts evita tarjetas intermedias lentas y mantén fluidez orgánica en vídeo."
    )

    sys_prompt = (
        "Eres Director de Fotografía y Montador Visual de documentales y reels virales de alta retención. "
        "Generas un storyboard continuo en JSON con clave 'shots' cubriendo exactamente desde 0.0 hasta la duración total "
        "SIN huecos, sin cortes bruscos y con máxima variedad visual entre planos."
    )
    usr_prompt = (
        f"Duración exacta del audio editado: {duration:.2f} segundos.\n"
        f"Formato: {clip.aspect_ratio} | Tipo: {clip.clip_type}\n"
        f"BIBLIA VISUAL DEL EPISODIO:\n{json.dumps(bible, ensure_ascii=False)}\n"
        f"Ritmo orgánico de cambio de plano: cada {pace_min:.1f}s a {pace_max:.1f}s (cambia en pausas o cambios de idea; MÁXIMO {max_shots} planos en total).\n"
        f"Elementos PROHIBIDOS / ANACRONISMOS: {forbidden_str}\n"
        f"{card_instruction}\n\n"
        f"IMÁGENES PROPIAS DEL USUARIO (PRIORÍZALAS SIEMPRE QUE COINCIDA EL TEXTO):\n{img_manifest}\n\n"
        "REGLAS CRÍTICAS PARA QUERIES Y ARCO VISUAL:\n"
        "- Escribe `stock_query` SIEMPRE en INGLÉS, MUY CONCRETA Y CORTA (2 a 4 palabras) sobre ESCENAS REALES FILMABLES o FOTOS HISTÓRICAS EXISTENTES "
        "(aviones, fábricas, motores, planos generales de ciudad/época, documentos, cabinas, retratos de trabajo). "
        "PROHIBIDO describir escenas ficticias o acciones rebuscadas (NUNCA pongas 'woman engineer writing at desk 1940' ni nombres mal transcritos; usa 'aircraft engine workshop', 'Spitfire fighter plane', '1940s aviation blueprint').\n"
        "- PROHIBIDO repetir la misma `stock_query` en dos planos consecutivos: alterna planos generales, planos detalle, maquinaria, lugares y archivo histórico.\n"
        "- PROHIBIDO pedir conceptos abstractos como 'concept of choice', 'abstract flaws' o 'futuristic network' si el tema es histórico o humano.\n"
        "- PROHIBIDO ilustrar frases hechas, modismos o metáforas de forma literal (ej. si dice 'se repara con tela de saco y aguja de coser' hablando de un avión, muestra un hangar o un avión histórico, NUNCA una máquina de coser ni modistas).\n"
        f"{archive_rule}\n"
        "- Cada shot debe cubrir de forma contigua el intervalo anterior (sin dejar segundos vacíos entre shots).\n"
        "- Campos por shot: start (float), end (float), source_type ('custom_image'|'stock_video'|'stock_photo'|'archive_media'|'title_card'|'quote_card'), "
        "custom_image_id (si aplica), stock_query (2-4 palabras concretas en inglés), card_title (si tarjeta), reason (qué dice la voz en ese instante).\n\n"
        "GUION EDITADO CON TIEMPOS:\n"
        + "\n".join(f"[{c['start']:.2f}s - {c['end']:.2f}s] {c['text']}" for c in chunks[:180])
    )

    parsed_shots: list[StoryboardShot] = []
    archive_count = 0
    max_archive = len(chunks) + 10 if (allow_archive and not allow_stock) else max(2, int(len(chunks) * 0.55))

    try:
        resp = llm.chat_json("fast", sys_prompt, usr_prompt, temperature=0.22, max_tokens=8192)
        raw_shots = resp.get("shots") or resp.get("items") or []
        for idx, item in enumerate(raw_shots, start=1):
            if not isinstance(item, dict):
                continue
            stype = str(item.get("source_type", "stock_video")).strip()
            if stype not in {"custom_image", "stock_video", "stock_photo", "archive_media", "title_card", "quote_card"}:
                stype = "stock_video"
            if stype == "archive_media":
                if not allow_archive or archive_count >= max_archive:
                    stype = "stock_video" if allow_stock else ("custom_image" if story_images else "title_card")
                else:
                    archive_count += 1
            elif stype in {"stock_video", "stock_photo"} and not allow_stock:
                if allow_archive:
                    stype = "archive_media"
                    archive_count += 1
                elif story_images:
                    stype = "custom_image"
                else:
                    stype = "title_card"

            cid = item.get("custom_image_id")
            c_img = image_lookup.get(str(cid)) if cid else None
            if stype == "custom_image" and c_img is None:
                if story_images and not allow_stock and not allow_archive:
                    c_img = story_images[(idx - 1) % len(story_images)]
                else:
                    stype = "stock_video" if allow_stock else ("archive_media" if allow_archive else "title_card")

            s_t = float(item.get("start", 0.0))
            e_t = float(item.get("end", 0.0))
            query = str(item.get("stock_query") or "").strip()
            if stype in {"stock_video", "stock_photo", "archive_media"} and not query:
                query = fallback_query

            spoken_ctx = _extract_spoken_context_for_shot(clip.mapped_words, s_t, e_t, str(item.get("reason") or ""))
            parsed_shots.append(
                StoryboardShot(
                    id=f"shot_{idx:02d}",
                    start=s_t,
                    end=e_t,
                    source_type=stype,  # type: ignore[arg-type]
                    custom_image_id=c_img.id if c_img else None,
                    custom_image_filename=c_img.filename if c_img else None,
                    display_mode=c_img.display_mode if c_img else "fullscreen_zoom",
                    stock_query=query,
                    card_title=str(item.get("card_title") or "").strip(),
                    card_subtitle=str(item.get("card_subtitle") or "").strip(),
                    reason=spoken_ctx[:180],
                )
            )
    except Exception:
        parsed_shots = []

    normalized = _normalize_storyboard_coverage(
        parsed_shots,
        duration=duration,
        aspect_ratio=clip.aspect_ratio,
        words=clip.mapped_words,
        fallback_query=fallback_query,
        min_shot_sec=pace_min,
        outro_image=outro_img,
        cta_title=profile.subtitles.cta_text,
    )
    return StoryboardPlan(
        clip_id=clip.id,
        aspect_ratio=clip.aspect_ratio,
        duration=round(duration, 2),
        shots=normalized,
    )
