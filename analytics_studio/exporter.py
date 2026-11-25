"""Exports complete AI telemetry, timeline X-ray, QA anomalies, and cost/timing reports."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.core.editorial_engine import _PROMO_AD_PATTERNS, apply_glossary_to_sentences
from backend.models.job import SentenceSegment

PHASE_ORDER = ["transcription", "editorial", "audio_edit", "visual_direction", "rendering"]
PHASE_LABELS = {
    "transcription": "Fase 1 · Transcripción y Escaneo",
    "editorial": "Fase 2 · Dirección Editorial + Juez Ciego",
    "audio_edit": "Fase 3 · Pasada 2 Whisper + EDL + TTS",
    "visual_direction": "Fase 4 · Storyboard + Stock VLM + ASS",
    "rendering": "Fase 5 · Render FFmpeg Final",
}

_TRAILING_TRUNCATION_WORDS = frozenset(
    {"de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "en", "con", "por", "para", "y", "o", "a", "que", "al"}
)
_HESITATION_OPENERS = (
    "como la cosa estaba",
    "bueno pues",
    "y bueno",
    "pero bueno",
    "pues bueno",
    "a ver ",
    "o sea ",
)


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _reconstruct_phase_timings(
    job: dict[str, Any],
    telemetry_events: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], float, dict[str, float]]:
    """Compute seconds spent in each of the 5 phases and per-clip render time."""
    events = telemetry_events or []
    phase_starts: dict[str, float] = {}
    phase_ends: dict[str, float] = {}
    active_run_ph: set[str] = set()

    for ev in events:
        if ev.get("type") != "phase_timing":
            continue
        ph = str(ev.get("phase") or "")
        ts = _parse_iso(ev.get("ts"))
        if not ph or ts is None:
            continue
        st = str(ev.get("status") or "")
        if st == "running" and ph not in active_run_ph:
            phase_starts[ph] = ts
            phase_ends.pop(ph, None)
            active_run_ph.add(ph)
        elif ph not in phase_starts:
            phase_starts[ph] = ts
        if st in ("completed", "failed", "skipped"):
            phase_ends[ph] = ts
            active_run_ph.discard(ph)

    logs = job.get("logs") or []
    t_created = _parse_iso(job.get("created_at"))
    t_updated = _parse_iso(job.get("updated_at"))
    if logs:
        first_log_t = _parse_iso(logs[0].get("ts"))
        if first_log_t is not None and (t_created is None or first_log_t < t_created):
            t_created = first_log_t
        last_log_t = _parse_iso(logs[-1].get("ts"))
        if last_log_t is not None and (t_updated is None or last_log_t > t_updated):
            t_updated = last_log_t

    log_markers: dict[str, float] = {}
    clip_render_starts: dict[str, float] = {}
    clip_render_durations: dict[str, float] = {}

    for entry in logs:
        msg = str(entry.get("message") or "")
        ts = _parse_iso(entry.get("ts"))
        if ts is None:
            continue
        if "Iniciando trabajo '" in msg:
            log_markers["run_start"] = ts
        elif "Fase 1 completada" in msg or "Fase 1 acelerada" in msg:
            log_markers["p1_end"] = ts
        elif "Fase 2 completada" in msg:
            log_markers["p2_end"] = ts
        elif "Pasada 2 + EDL" in msg and ("p3_start" not in log_markers or ts > log_markers.get("p2_end", 0.0)):
            log_markers.setdefault("p3_start", ts)
        elif "Fase 3 completada" in msg or "EDL y audios limpios listos" in msg or "Generando storyboards" in msg:
            log_markers["p3_end"] = ts
        elif "Fase 4 completada" in msg:
            log_markers["p4_end"] = ts
        elif "Fase 5 iniciada" in msg:
            log_markers["p5_start"] = ts
        elif "¡Producción finalizada!" in msg:
            log_markers["p5_end"] = ts

        if "] Plano " in msg or "storyboard" in msg.lower() or "Director Visual" in msg:
            if "p4_start" not in log_markers or (log_markers.get("p3_end") and ts >= log_markers["p3_end"]):
                log_markers.setdefault("p4_start", ts)

        m_ren_start = re.search(r"Renderizando MP4 final:\s*([^\s.]+\.mp4)", msg)
        if m_ren_start:
            clip_render_starts[m_ren_start.group(1)] = ts
        m_ren_done = re.search(r"Renderizado completado:\s*([^\s.]+\.mp4)", msg)
        if m_ren_done:
            fn = m_ren_done.group(1)
            if fn in clip_render_starts:
                clip_render_durations[fn] = round(max(0.0, ts - clip_render_starts[fn]), 2)

    if not phase_starts and t_created is not None:
        p1_start = log_markers.get("run_start") or t_created
        p1_end = log_markers.get("p1_end")
        p2_start = p1_end
        p2_end = log_markers.get("p2_end")
        p3_start = p2_end or log_markers.get("p3_start")
        p3_end = log_markers.get("p3_end") or log_markers.get("p4_start")
        p4_start = p3_end or log_markers.get("p4_start")
        p4_end = log_markers.get("p4_end") or log_markers.get("p5_start")
        p5_start = log_markers.get("p5_start") or p4_end
        p5_end = log_markers.get("p5_end") or (t_updated if job.get("status") == "completed" else None)

        bounds = {
            "transcription": (p1_start, p1_end),
            "editorial": (p2_start, p2_end),
            "audio_edit": (p3_start, p3_end),
            "visual_direction": (p4_start, p4_end),
            "rendering": (p5_start, p5_end),
        }
        for ph, (s_t, e_t) in bounds.items():
            if s_t is not None and e_t is not None and e_t >= s_t:
                phase_starts[ph] = s_t
                phase_ends[ph] = e_t

    phases_status = {p.get("phase"): p for p in (job.get("phases") or [])}
    out_phases: list[dict[str, Any]] = []
    known_sum = 0.0
    for ph in PHASE_ORDER:
        st_obj = phases_status.get(ph) or {}
        dur: Optional[float] = None
        if st_obj.get("duration_sec") is not None:
            try:
                dur = round(max(0.0, float(st_obj["duration_sec"])), 2)
            except Exception:
                dur = None
        if dur is None:
            ph_s_iso = _parse_iso(st_obj.get("started_at"))
            ph_e_iso = _parse_iso(st_obj.get("completed_at"))
            if ph_s_iso is not None and ph_e_iso is not None and ph_e_iso >= ph_s_iso:
                dur = round(max(0.0, ph_e_iso - ph_s_iso), 2)
        if dur is None:
            s_t = phase_starts.get(ph)
            e_t = phase_ends.get(ph)
            if s_t is not None and e_t is not None and e_t >= s_t:
                dur = round(max(0.0, e_t - s_t), 2)
        if dur is not None:
            known_sum += dur
        out_phases.append(
            {
                "phase": ph,
                "label": PHASE_LABELS.get(ph, ph),
                "status": st_obj.get("status", "unknown"),
                "duration_sec": dur,
                "detail": st_obj.get("detail", ""),
            }
        )

    wall_sec = round(max(0.0, (t_updated - t_created)), 2) if (t_created and t_updated and t_updated >= t_created) else 0.0
    total_sec = round(known_sum, 2) if (known_sum > 0 and (wall_sec == 0.0 or known_sum <= wall_sec * 1.15)) else wall_sec
    denom = total_sec if total_sec > 0 else (known_sum if known_sum > 0 else 1.0)
    for item in out_phases:
        d = item["duration_sec"]
        item["pct_of_total"] = round((d / denom) * 100.0, 1) if d is not None else 0.0

    return out_phases, total_sec, clip_render_durations


def _extract_llm_telemetry(
    job: dict[str, Any],
    telemetry_events: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract per-call LLM metrics and fallbacks/errors."""
    calls: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    events = telemetry_events or []

    for ev in events:
        if ev.get("type") == "llm_call":
            calls.append(ev)
        elif ev.get("type") == "llm_fallback":
            fallbacks.append(ev)

    if not calls and not fallbacks:
        for entry in (job.get("logs") or []):
            msg = str(entry.get("message") or "")
            ts = entry.get("ts")
            m_call = re.match(
                r"^LLM\s+([^:]+):\s*([\d,]+)\s+in\s*/\s*([\d,]+)\s+out\s*\(\$([\d.]+)\)",
                msg,
            )
            if m_call:
                calls.append(
                    {
                        "type": "llm_call",
                        "model": m_call.group(1).strip(),
                        "prompt_tokens": int(m_call.group(2).replace(",", "")),
                        "completion_tokens": int(m_call.group(3).replace(",", "")),
                        "cost_usd": float(m_call.group(4)),
                        "duration_sec": None,
                        "ts": ts,
                    }
                )
            elif msg.startswith("Fallback OpenRouter"):
                m_fb = re.match(
                    r"^Fallback OpenRouter \(([^)]+)\):\s*(\S+)\s+falló\s*\((.+)\),\s*probando\s+(\S+)",
                    msg,
                    flags=re.DOTALL,
                )
                if m_fb:
                    fallbacks.append(
                        {
                            "type": "llm_fallback",
                            "role": m_fb.group(1),
                            "from_model": m_fb.group(2),
                            "error": m_fb.group(3).strip()[:240],
                            "to_model": m_fb.group(4),
                            "ts": ts,
                        }
                    )
                else:
                    fallbacks.append({"type": "llm_fallback", "error": msg[:240], "ts": ts})

    return calls, fallbacks


def _reconstruct_judge_funnel(
    job: dict[str, Any],
    telemetry_events: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build Blind Judge candidate cards (evaluated, repaired, escalated, approved, discarded)."""
    events = telemetry_events or []
    candidates_by_key: dict[str, dict[str, Any]] = {}
    cid_to_key: dict[str, str] = {}
    title_to_key: dict[str, str] = {}
    order: list[str] = []
    overview: dict[str, Any] = {}
    topic_blocks: list[dict[str, Any]] = []

    def _get_card(title: str, clip_type: str = "", candidate_id: str = "") -> dict[str, Any]:
        cid = str(candidate_id or "").strip()
        t_clean = str(title or "").strip() or "(Sin título)"
        key: Optional[str] = None
        if cid and cid in cid_to_key:
            key = cid_to_key[cid]
        elif t_clean in title_to_key:
            key = title_to_key[t_clean]
        if key is None:
            key = cid or t_clean
            candidates_by_key[key] = {
                "candidate_id": cid or None,
                "title": t_clean,
                "clip_type": clip_type,
                "est_sec": None,
                "initial_eval": None,
                "repairs": [],
                "post_repair_evals": [],
                "escalated_to_strong": False,
                "outcome": "unknown",
                "outcome_detail": "",
                "intro_curtain_text": "",
            }
            order.append(key)
        else:
            card_obj = candidates_by_key[key]
            if cid and not card_obj.get("candidate_id"):
                card_obj["candidate_id"] = cid
            if clip_type and not card_obj["clip_type"]:
                card_obj["clip_type"] = clip_type
            if t_clean != "(Sin título)" and card_obj["title"] == "(Sin título)":
                card_obj["title"] = t_clean
        if cid:
            cid_to_key[cid] = key
        if t_clean != "(Sin título)":
            title_to_key[t_clean] = key
        return candidates_by_key[key]

    has_structured_editorial = any(
        ev.get("type") in ("judge_audit", "nle_repair", "candidate_outcome") for ev in events
    )
    if has_structured_editorial:
        for ev in events:
            t = ev.get("type")
            ev_cid = str(ev.get("candidate_id") or "")
            if t == "editorial_overview":
                overview = {
                    "blacklisted_count": ev.get("blacklisted_count", 0),
                    "music_breaks_count": ev.get("music_breaks_count", 0),
                    "block_map_count": ev.get("block_map_count", 0),
                    "planned_angles": ev.get("planned_angles") or [],
                }
            elif t == "judge_audit":
                card = _get_card(str(ev.get("clip_title") or ""), str(ev.get("clip_type") or ""), candidate_id=ev_cid)
                if ev.get("stage") == "initial":
                    card["initial_eval"] = ev
                else:
                    card["post_repair_evals"].append(ev)
                    if "strong" in str(ev.get("stage") or ""):
                        card["escalated_to_strong"] = True
            elif t == "nle_repair":
                card = _get_card(str(ev.get("clip_title") or ""), candidate_id=ev_cid)
                card["repairs"].append(ev)
                if ev.get("tier") == "editorial_strong":
                    card["escalated_to_strong"] = True
            elif t == "candidate_outcome":
                card = _get_card(str(ev.get("clip_title") or ""), str(ev.get("clip_type") or ""), candidate_id=ev_cid)
                card["outcome"] = ev.get("audit_action") or ("approved" if ev.get("accepted") else "discarded")
                card["outcome_detail"] = ev.get("reason") or ""
                card["intro_curtain_text"] = ev.get("intro_curtain_text") or ""

    # Always parse logs too so older runs and log-only outcomes (best_effort, topic dedup) merge cleanly
    for entry in (job.get("logs") or []):
        msg = str(entry.get("message") or "")
        if "Bloqueados editorialmente" in msg and "blacklisted_count" not in overview:
            m_b = re.search(r"Bloqueados editorialmente (\d+)", msg)
            if m_b:
                overview["blacklisted_count"] = int(m_b.group(1))
        if "Planificador temático detectó" in msg and not overview.get("planned_angles"):
            parts = msg.split(":", 1)[-1].strip().split("|")
            overview["planned_angles"] = [
                {"angle_id": p.strip().split(":")[0].strip(), "theme": p.strip()}
                for p in parts
                if p.strip()
            ]
        if "Filtro temático descarta" in msg:
            m_td = re.search(r"Filtro temático descarta '([^']+)' \((.+)\)\.", msg)
            if m_td:
                topic_blocks.append({"title": m_td.group(1), "detail": m_td.group(2)})

        if has_structured_editorial:
            continue

        m_ev = re.match(r"^Juez Ciego evaluando '(.+?)' \(([^,]+),\s*~(\d+)s\)\.\.\.$", msg)
        if m_ev:
            card = _get_card(m_ev.group(1), m_ev.group(2))
            card["est_sec"] = int(m_ev.group(3))
            continue

        m_rep_req = re.match(r"^Juez Ciego solicita reparación de guion para '(.+?)' \((.+)\)\.", msg)
        if m_rep_req:
            card = _get_card(m_rep_req.group(1))
            card["initial_eval"] = _parse_kv_summary(m_rep_req.group(2))
            continue

        m_esc = re.match(r"^Escalando reparación quirúrgica de '(.+?)' a modelo experto", msg)
        if m_esc:
            card = _get_card(m_esc.group(1))
            card["escalated_to_strong"] = True
            continue

        m_post = re.match(r"^Juez Ciego post-reparación \(([^)]+)\) '(.+?)':\s*(.+)\.$", msg)
        if m_post:
            card = _get_card(m_post.group(2))
            parsed_kv = _parse_kv_summary(m_post.group(3))
            parsed_kv["tier"] = m_post.group(1)
            card["post_repair_evals"].append(parsed_kv)
            if "strong" in m_post.group(1):
                card["escalated_to_strong"] = True
            continue

        m_app = re.match(r"^Juez Ciego APRUEBA '(.+?)'\s+(.+)$", msg)
        if m_app:
            card = _get_card(m_app.group(1))
            rest = m_app.group(2)
            card["outcome"] = "approve" if "100% voz del podcast" in rest else "approve_needs_intro"
            card["outcome_detail"] = rest
            m_q = re.search(r'"(.+)"$', rest)
            if m_q:
                card["intro_curtain_text"] = m_q.group(1)
            continue

        m_rej = re.match(r"^Juez Ciego RECHAZA '(.+?)'\s+incluso con cortinilla IA \((.+)\)\.$", msg)
        if m_rej:
            card = _get_card(m_rej.group(1))
            card["outcome"] = "rejected_with_intro"
            card["outcome_detail"] = m_rej.group(2)
            continue

        m_dis = re.match(r"^Juez Ciego DESCARTA '(.+?)':\s*(.+)$", msg)
        if m_dis:
            card = _get_card(m_dis.group(1))
            card["outcome"] = "discarded"
            card["outcome_detail"] = m_dis.group(2)
            continue

    # Merge any final clip outcomes using candidate_id -> original_title -> final title (prevents Phase 3 title duplicates)
    for c in (job.get("clips") or []):
        cid_cand = str(c.get("candidate_id") or "").strip()
        orig_t = str(c.get("original_title") or "").strip()
        t0 = str((c.get("metadata") or {}).get("titles", [None])[0] or c.get("slug") or c.get("id") or "")
        pkg = c.get("standalone_packaging") or {}
        act = pkg.get("audit_action") or "selected"
        existing_key = (
            cid_to_key.get(cid_cand)
            or title_to_key.get(orig_t)
            or title_to_key.get(t0)
        )
        if existing_key is not None:
            card = candidates_by_key[existing_key]
            if t0 and t0 != card["title"]:
                card["final_title"] = t0
            if cid_cand:
                cid_to_key[cid_cand] = existing_key
                card["candidate_id"] = cid_cand
            if c.get("intro_curtain_text") and not card["intro_curtain_text"]:
                card["intro_curtain_text"] = c.get("intro_curtain_text")
            if act in ("best_effort_needs_intro", "fallback", "salvage_needs_intro") and card["outcome"] in (
                "unknown",
                "rejected_with_intro",
                "discarded",
            ):
                card["outcome"] = act
                card["outcome_detail"] = pkg.get("audit_issue_fixed") or c.get("summary") or ""
            continue
        if act in ("best_effort_needs_intro", "fallback", "salvage_needs_intro") or True:
            card = _get_card(orig_t or t0, str(c.get("clip_type") or ""), candidate_id=cid_cand)
            if t0 and t0 != card["title"]:
                card["final_title"] = t0
            if card["outcome"] in ("unknown", "rejected_with_intro", "discarded"):
                card["outcome"] = act
                card["outcome_detail"] = pkg.get("audit_issue_fixed") or c.get("summary") or ""
                card["intro_curtain_text"] = c.get("intro_curtain_text") or card["intro_curtain_text"]
                if card["initial_eval"] is None and pkg:
                    card["initial_eval"] = {
                        "comprehension_score": pkg.get("comprehension_score"),
                        "hook_strength": pkg.get("hook_strength"),
                        "context_clarity": pkg.get("context_clarity"),
                        "clean_boundaries": pkg.get("clean_boundaries"),
                        "resolution_score": pkg.get("resolution_score"),
                        "who": pkg.get("who_identified"),
                        "what_and_context": pkg.get("what_and_context"),
                    }

    cards = [candidates_by_key[k] for k in order]
    funnel = {
        "total_candidates_evaluated": len(cards),
        "approved_voice_only": sum(1 for x in cards if x["outcome"] == "approve"),
        "approved_with_intro": sum(
            1
            for x in cards
            if "needs_intro" in str(x["outcome"]) or ("approve" in str(x["outcome"]) and x["outcome"] != "approve")
        ),
        "rescued_best_effort": sum(
            1 for x in cards if x["outcome"] in ("best_effort_needs_intro", "fallback", "salvage_needs_intro", "orig_needs_intro")
        ),
        "repaired_attempts": sum(1 for x in cards if x["repairs"] or x["post_repair_evals"]),
        "escalated_strong": sum(1 for x in cards if x["escalated_to_strong"]),
        "discarded_or_rejected": sum(1 for x in cards if x["outcome"] in ("discard", "discarded", "rejected_with_intro")),
        "topic_dedup_blocked": len(topic_blocks),
        "overview": overview,
        "topic_blocks": topic_blocks,
    }
    return funnel, cards


def _parse_kv_summary(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Capture key=value pairs safely where value might contain commas inside who/what
    for key in (
        "who", "comp", "ctx", "bnd", "res",
        "start_ok", "end_ok", "splices_ok", "res_ok", "intro_ok", "spoiler",
    ):
        m = re.search(rf"\b{key}=([^,]+(?:\([^)]*\))?)", text)
        if not m:
            continue
        val: Any = m.group(1).strip()
        if val in ("True", "False"):
            val = val == "True"
        elif re.fullmatch(r"-?\d+", val):
            val = int(val)
        mapped = {
            "comp": "comprehension_score",
            "ctx": "context_clarity",
            "bnd": "clean_boundaries",
            "res": "resolution_score",
            "start_ok": "clean_start",
            "end_ok": "clean_end",
            "splices_ok": "splices_coherent",
            "res_ok": "resolution_answered",
            "intro_ok": "passed_with_intro_only",
            "spoiler": "has_spoiler",
        }.get(key, key)
        out[mapped] = val
    return out


def _detect_promo_sentence_ids(
    sentences: list[dict[str, Any]],
    editorial_map: dict[str, Any],
) -> set[str]:
    promo_ids: set[str] = set()
    id_list = [str(s.get("id") or "") for s in sentences]
    for s in sentences:
        txt = str(s.get("text") or "").lower()
        if any(pat.search(txt) for pat in _PROMO_AD_PATTERNS):
            promo_ids.add(str(s.get("id") or ""))
    for blk in (editorial_map.get("blocks") or []):
        for rng in (blk.get("excluded_ranges") or []):
            sid = str(rng.get("start_sentence_id") or "")
            eid = str(rng.get("end_sentence_id") or "")
            if sid in id_list and eid in id_list:
                i0, i1 = id_list.index(sid), id_list.index(eid)
                for k in range(min(i0, i1), max(i0, i1) + 1):
                    promo_ids.add(id_list[k])
    return promo_ids


def _build_glossary_comparisons(
    sentences_raw: list[dict[str, Any]],
    glossary: dict[str, str],
) -> list[dict[str, Any]]:
    if not glossary:
        return []
    seg_objs = [
        SentenceSegment(
            id=str(s.get("id") or f"S{i+1:04d}"),
            text=str(s.get("text") or ""),
            start=float(s.get("start") or 0.0),
            end=float(s.get("end") or 0.0),
            words=[],
        )
        for i, s in enumerate(sentences_raw)
    ]
    fixed_segs = apply_glossary_to_sentences(seg_objs, glossary)
    rows: list[dict[str, Any]] = []
    for wrong, right in glossary.items():
        examples: list[dict[str, Any]] = []
        for orig_s, fixed_s in zip(seg_objs, fixed_segs):
            if orig_s.text != fixed_s.text and right.lower() in fixed_s.text.lower():
                examples.append(
                    {
                        "sentence_id": orig_s.id,
                        "start": round(orig_s.start, 2),
                        "before": orig_s.text,
                        "after": fixed_s.text,
                    }
                )
                if len(examples) >= 3:
                    break
        rows.append({"raw_term": wrong, "corrected_term": right, "examples": examples})
    return rows


def export_job_report(
    job_id: str,
    base_dir: Path,
    telemetry_events: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build and write analytics_studio/data/<job_id>/report.json cleanly from job artifacts."""
    proc_dir = base_dir / "processing" / job_id
    job_path = proc_dir / "job.json"
    if not job_path.exists():
        raise FileNotFoundError(f"No existe job.json para {job_id}")

    job = json.loads(job_path.read_text(encoding="utf-8"))
    draft_path = proc_dir / "draft_transcript.json"
    draft_data = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {}
    if isinstance(draft_data, list):
        draft_data = {"sentences": draft_data}
    ed_map_path = proc_dir / "editorial_map.json"
    editorial_map = json.loads(ed_map_path.read_text(encoding="utf-8")) if ed_map_path.exists() else {}
    if isinstance(editorial_map, list):
        editorial_map = {"blocks": editorial_map}

    if telemetry_events is None:
        ev_jsonl_path = proc_dir / "events.jsonl"
        if ev_jsonl_path.exists():
            loaded_evs: list[dict[str, Any]] = []
            for raw_l in ev_jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
                s_l = raw_l.strip()
                if not s_l:
                    continue
                try:
                    loaded_evs.append(json.loads(s_l))
                except Exception:
                    continue
            if loaded_evs:
                telemetry_events = loaded_evs

    max_short_window_sec = 90.0
    prof_id = str(job.get("profile_id") or "")
    if prof_id:
        prof_path = base_dir / "profiles" / prof_id / "profile.json"
        if prof_path.exists():
            try:
                prof_data = json.loads(prof_path.read_text(encoding="utf-8"))
                max_short_window_sec = float(
                    ((prof_data.get("quotas") or {}).get("shorts_window") or {}).get("max_sec") or 90.0
                )
            except Exception:
                pass

    phase_timings, total_duration_sec, clip_render_durs = _reconstruct_phase_timings(job, telemetry_events)
    llm_calls, llm_fallbacks = _extract_llm_telemetry(job, telemetry_events)
    judge_funnel, judge_cards = _reconstruct_judge_funnel(job, telemetry_events)

    raw_sentences = draft_data.get("sentences") or []
    promo_ids = _detect_promo_sentence_ids(raw_sentences, editorial_map)
    glossary = job.get("episode_glossary") or editorial_map.get("episode_glossary") or {}
    visual_bible = job.get("visual_bible") or editorial_map.get("visual_bible") or {}
    glossary_rows = _build_glossary_comparisons(raw_sentences, glossary)

    forbidden_anachronisms = [str(x).lower().strip() for x in (visual_bible.get("forbidden_anachronisms") or []) if str(x).strip()]

    out_dir_str = job.get("output_dir") or ""
    out_dir = Path(out_dir_str) if out_dir_str else None
    src_audio = Path(job.get("source_audio_path") or "")

    # Map sentence id -> index for finding excised spans inside multi-span clips
    sid_to_idx = {str(s.get("id") or ""): i for i, s in enumerate(raw_sentences)}

    clip_spans_timeline: list[dict[str, Any]] = []
    excised_gaps_timeline: list[dict[str, Any]] = []
    sentence_clip_map: dict[str, list[str]] = {}
    clips_out: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    if job.get("status") == "failed":
        anomalies.append(
            {
                "severity": "error",
                "clip_id": None,
                "code": "job_failed",
                "title": "El trabajo terminó en error",
                "detail": str(job.get("error_message") or "Fallo durante la ejecución."),
            }
        )
    elif job.get("status") == "cancelled":
        anomalies.append(
            {
                "severity": "warning",
                "clip_id": None,
                "code": "job_cancelled",
                "title": "Trabajo cancelado",
                "detail": "La ejecución fue cancelada antes de completarse.",
            }
        )

    for c in (job.get("clips") or []):
        cid = str(c.get("id") or "")
        ctype = str(c.get("clip_type") or "")
        meta = c.get("metadata") or {}
        titles = meta.get("titles") or []
        title0 = str(titles[0]) if titles else str(c.get("slug") or cid)
        pkg = c.get("standalone_packaging") or {}
        spans = c.get("narrative_spans") or []

        for sp in spans:
            clip_spans_timeline.append(
                {
                    "clip_id": cid,
                    "clip_title": title0,
                    "clip_type": ctype,
                    "role": sp.get("role", "core"),
                    "start_sentence_id": sp.get("start_sentence_id"),
                    "end_sentence_id": sp.get("end_sentence_id"),
                    "orig_start": round(float(sp.get("orig_start") or 0.0), 2),
                    "orig_end": round(float(sp.get("orig_end") or 0.0), 2),
                    "duration_sec": round(float(sp.get("orig_end") or 0.0) - float(sp.get("orig_start") or 0.0), 2),
                }
            )
            si = sid_to_idx.get(str(sp.get("start_sentence_id") or ""))
            ei = sid_to_idx.get(str(sp.get("end_sentence_id") or ""))
            if si is not None and ei is not None:
                for k in range(min(si, ei), max(si, ei) + 1):
                    sid_k = str(raw_sentences[k].get("id") or "")
                    sentence_clip_map.setdefault(sid_k, []).append(cid)

        # Detect gaps pruned out between consecutive spans of the same clip
        sorted_spans = sorted(spans, key=lambda x: float(x.get("orig_start") or 0.0))
        for si_idx in range(len(sorted_spans) - 1):
            e_prev = float(sorted_spans[si_idx].get("orig_end") or 0.0)
            s_next = float(sorted_spans[si_idx + 1].get("orig_start") or 0.0)
            if s_next - e_prev > 1.5:
                excised_gaps_timeline.append(
                    {
                        "clip_id": cid,
                        "clip_title": title0,
                        "orig_start": round(e_prev, 2),
                        "orig_end": round(s_next, 2),
                        "duration_sec": round(s_next - e_prev, 2),
                    }
                )

        mapped_words = c.get("mapped_words") or []
        spoken_body = " ".join(str(w.get("word") or "") for w in mapped_words).strip()
        residual_gaps: list[dict[str, Any]] = []
        for wi in range(len(mapped_words) - 1):
            w0, w1 = mapped_words[wi], mapped_words[wi + 1]
            g = float(w1.get("start") or 0.0) - float(w0.get("end") or 0.0)
            if g > 3.0:
                ctx_before = " ".join(str(x.get("word") or "") for x in mapped_words[max(0, wi - 5) : wi + 1])
                ctx_after = " ".join(str(x.get("word") or "") for x in mapped_words[wi + 1 : wi + 7])
                residual_gaps.append(
                    {
                        "edited_start": round(float(w0.get("end") or 0.0), 2),
                        "edited_end": round(float(w1.get("start") or 0.0), 2),
                        "gap_sec": round(g, 2),
                        "before": ctx_before,
                        "after": ctx_after,
                    }
                )
                anomalies.append(
                    {
                        "severity": "error" if g >= 8.0 else "warning",
                        "clip_id": cid,
                        "code": "residual_silence_gap",
                        "title": f"Hueco mudo residual de {g:.2f}s en {cid}",
                        "detail": f"Silencio de {g:.2f}s entre \"{ctx_before}\" y \"{ctx_after}\".",
                    }
                )

        # Check hook_text truncation
        hook_txt = str(meta.get("hook_text") or "").strip()
        if hook_txt:
            last_w = re.sub(r"[^a-záéíóúüñ0-9]", "", hook_txt.split()[-1].lower())
            if last_w in _TRAILING_TRUNCATION_WORDS:
                anomalies.append(
                    {
                        "severity": "warning",
                        "clip_id": cid,
                        "code": "truncated_hook_text",
                        "title": f"Gancho en pantalla truncado en {cid}",
                        "detail": f"hook_text termina en artículo/preposición: \"{hook_txt}\".",
                    }
                )

        # Check intro curtain length (>6.2s) and phrases parity
        ic_dur = float(c.get("intro_curtain_duration_sec") or 0.0)
        if c.get("intro_curtain_enabled") and ic_dur > 6.2:
            anomalies.append(
                {
                    "severity": "warning",
                    "clip_id": cid,
                    "code": "intro_too_long",
                    "title": f"Cortinilla IA larga ({ic_dur:.2f}s > 6.2s) en {cid}",
                    "detail": f"Texto ({len((c.get('intro_curtain_text') or '').split())} palabras): \"{c.get('intro_curtain_text')}\"",
                }
            )
        if c.get("intro_curtain_enabled") and c.get("intro_curtain_phrases"):
            norm_hook = re.sub(r"[^a-z0-9áéíóúüñ]", "", str(c.get("intro_curtain_text") or "").lower())
            norm_ph = re.sub(
                r"[^a-z0-9áéíóúüñ]",
                "",
                " ".join(str(x) for x in (c.get("intro_curtain_phrases") or [])).lower(),
            )
            if norm_hook and norm_ph and norm_hook != norm_ph:
                anomalies.append(
                    {
                        "severity": "warning",
                        "clip_id": cid,
                        "code": "intro_phrases_mismatch",
                        "title": f"Desajuste entre locución y rótulos de cortinilla en {cid}",
                        "detail": f"Hook: \"{c.get('intro_curtain_text')}\" | Phrases: {c.get('intro_curtain_phrases')}",
                    }
                )

        # Check Shorts exceeding max_sec window
        edl_raw = c.get("edl") or {}
        outro_dur_est = float(c.get("outro_curtain_duration_sec") or 0.0) if c.get("outro_curtain_enabled") else 0.0
        total_clip_dur = (ic_dur if c.get("intro_curtain_enabled") else 0.0) + float(edl_raw.get("edited_duration") or 0.0) + outro_dur_est
        if ctype.startswith("short") and total_clip_dur > max_short_window_sec + 0.5:
            anomalies.append(
                {
                    "severity": "warning",
                    "clip_id": cid,
                    "code": "short_exceeds_max_sec",
                    "title": f"Short supera el límite de {max_short_window_sec:.0f}s ({total_clip_dur:.1f}s) en {cid}",
                    "detail": f"intro={ic_dur:.1f}s + cuerpo={float(edl_raw.get('edited_duration') or 0.0):.1f}s + cierre={outro_dur_est:.1f}s.",
                }
            )

        # Check weak opening on spoken body
        if spoken_body:
            low_open = spoken_body.lower()
            first_char = spoken_body[0]
            if first_char.islower() or any(low_open.startswith(h) for h in _HESITATION_OPENERS):
                anomalies.append(
                    {
                        "severity": "warning",
                        "clip_id": cid,
                        "code": "weak_spoken_opening",
                        "title": f"Inicio del audio del podcast débil o cortado en {cid}",
                        "detail": f"Tras la cortinilla empieza con: \"{' '.join(spoken_body.split()[:12])}...\"",
                    }
                )

        sb = c.get("storyboard") or {}
        shots_out: list[dict[str, Any]] = []
        for sh in (sb.get("shots") or []):
            q_low = str(sh.get("stock_query") or "").lower()
            anach = [a for a in forbidden_anachronisms if a in q_low]
            shots_out.append(
                {
                    "id": sh.get("id"),
                    "start": round(float(sh.get("start") or 0.0), 2),
                    "end": round(float(sh.get("end") or 0.0), 2),
                    "source_type": sh.get("source_type"),
                    "stock_query": sh.get("stock_query") or "",
                    "reason": sh.get("reason") or "",
                    "anachronism_flag": ", ".join(anach),
                }
            )

        video_path = Path(c.get("rendered_video_path") or "") if c.get("rendered_video_path") else None
        thumb_path = Path(c.get("thumbnail_path") or "") if c.get("thumbnail_path") else None
        if not video_path and out_dir and c.get("output_filename"):
            cand_v = out_dir / str(c.get("output_filename"))
            if cand_v.exists():
                video_path = cand_v
        video_filename = video_path.name if video_path and video_path.exists() else (video_path.name if video_path else None)
        thumb_filename = thumb_path.name if thumb_path and thumb_path.exists() else None
        if video_filename and not thumb_filename and out_dir:
            cand_t = out_dir / (Path(video_filename).stem + ".jpg")
            if cand_t.exists():
                thumb_filename = cand_t.name

        edl = c.get("edl") or {}
        edl_orig_dur = float(edl.get("orig_duration") or 0.0)
        edl_edit_dur = float(edl.get("edited_duration") or 0.0)
        tempo_ratio = (edl_edit_dur / edl_orig_dur) if (edl_orig_dur > 0.0 and edl_edit_dur > 0.0) else 1.0

        pb_spans = list(spans)
        splices_out: list[dict[str, Any]] = []
        cum_video_sec = ic_dur if c.get("intro_curtain_enabled") else 0.0
        for sp_i in range(len(pb_spans) - 1):
            sp_a = pb_spans[sp_i]
            sp_b = pb_spans[sp_i + 1]
            a_orig_s = float(sp_a.get("orig_start") or 0.0)
            a_orig_e = float(sp_a.get("orig_end") or 0.0)
            b_orig_s = float(sp_b.get("orig_start") or 0.0)
            b_orig_e = float(sp_b.get("orig_end") or 0.0)
            cum_video_sec += max(0.0, (a_orig_e - a_orig_s) * tempo_ratio)

            ei_a = sid_to_idx.get(str(sp_a.get("end_sentence_id") or ""))
            si_b = sid_to_idx.get(str(sp_b.get("start_sentence_id") or ""))
            before_txt = str(raw_sentences[ei_a].get("text") or "") if ei_a is not None else ""
            after_txt = str(raw_sentences[si_b].get("text") or "") if si_b is not None else ""
            skipped_cnt = 0
            skipped_preview = ""
            if ei_a is not None and si_b is not None and si_b > ei_a + 1:
                skipped_slice = raw_sentences[ei_a + 1 : si_b]
                skipped_cnt = len(skipped_slice)
                skipped_preview = " ".join(str(x.get("text") or "") for x in skipped_slice[:3]).strip()
                if len(skipped_preview) > 220:
                    skipped_preview = skipped_preview[:217] + "..."

            splices_out.append(
                {
                    "index": sp_i + 1,
                    "from_role": str(sp_a.get("role") or "core"),
                    "to_role": str(sp_b.get("role") or "core"),
                    "from_span": f"{sp_a.get('start_sentence_id', '')}..{sp_a.get('end_sentence_id', '')}",
                    "to_span": f"{sp_b.get('start_sentence_id', '')}..{sp_b.get('end_sentence_id', '')}",
                    "orig_end_prev": round(a_orig_e, 2),
                    "orig_start_next": round(b_orig_s, 2),
                    "cut_out_sec": round(b_orig_s - a_orig_e, 2),
                    "cut_out_sentences": skipped_cnt,
                    "cut_out_preview": skipped_preview,
                    "before_text": before_txt,
                    "after_text": after_txt,
                    "video_time_sec": round(cum_video_sec, 2),
                }
            )

        clips_out.append(
            {
                "id": cid,
                "clip_type": ctype,
                "aspect_ratio": c.get("aspect_ratio"),
                "score": c.get("score"),
                "enabled": bool(c.get("enabled", True)),
                "title": title0,
                "all_titles": titles,
                "hook_text": hook_txt,
                "cliffhanger_question": meta.get("cliffhanger_question") or "",
                "summary": c.get("summary") or "",
                "chapters": meta.get("chapters") or [],
                "angle_id": c.get("angle_id") or "",
                "angle_theme": c.get("angle_theme") or "",
                "standalone_packaging": pkg,
                "intro_curtain": {
                    "enabled": bool(c.get("intro_curtain_enabled")),
                    "text": c.get("intro_curtain_text") or "",
                    "phrases": c.get("intro_curtain_phrases") or [],
                    "badge": c.get("intro_curtain_badge") or "",
                    "duration_sec": round(ic_dur, 2),
                    "word_count": len((c.get("intro_curtain_text") or "").split()),
                },
                "outro_curtain_duration_sec": round(float(c.get("outro_curtain_duration_sec") or 0.0), 2),
                "spans": sorted_spans,
                "splices": splices_out,
                "edl": {
                    "orig_duration": round(edl_orig_dur, 2),
                    "edited_duration": round(edl_edit_dur, 2),
                    "music_gaps_removed_count": int(edl.get("music_gaps_removed_count") or 0),
                    "music_gaps_removed_sec": round(float(edl.get("music_gaps_removed_sec") or 0.0), 2),
                    "censored_hits_count": int(edl.get("censored_hits_count") or 0),
                    "segments_count": len(edl.get("segments") or []),
                    "residual_gaps": residual_gaps,
                },
                "spoken_body": spoken_body,
                "storyboard_shots": shots_out,
                "license_warnings": c.get("license_warnings") or [],
                "video_filename": video_filename,
                "video_exists": bool(video_path and video_path.exists()),
                "thumb_filename": thumb_filename,
                "thumb_exists": bool(thumb_filename and out_dir and (out_dir / thumb_filename).exists()),
                "render_duration_sec": clip_render_durs.get(video_filename or "") if video_filename else None,
            }
        )

    # Check log-level EDL repetitions & partial quotas & LLM fallbacks
    for entry in (job.get("logs") or []):
        msg = str(entry.get("message") or "")
        if "Posible repetición de frase detectada tras corte EDL" in msg:
            anomalies.append(
                {
                    "severity": "warning",
                    "clip_id": None,
                    "code": "edl_phrase_repetition",
                    "title": "Posible repetición de frase tras corte EDL",
                    "detail": msg,
                }
            )
    if llm_fallbacks:
        anomalies.append(
            {
                "severity": "warning",
                "clip_id": None,
                "code": "llm_fallbacks",
                "title": f"{len(llm_fallbacks)} fallback(s) de modelo OpenRouter durante la ejecución",
                "detail": "; ".join(f"{fb.get('from_model', '?')} -> {fb.get('to_model', '?')}" for fb in llm_fallbacks[:4]),
            }
        )
    req_total = (
        int(job.get("requested_shorts_cliffhanger") or 0)
        + int(job.get("requested_shorts_conclusive") or 0)
        + int(job.get("requested_medium") or 0)
        + int(job.get("requested_long") or 0)
    )
    if job.get("status") == "completed" and len(clips_out) < req_total:
        anomalies.append(
            {
                "severity": "warning",
                "clip_id": None,
                "code": "partial_quota",
                "title": f"Cuota parcial entregada ({len(clips_out)}/{req_total})",
                "detail": "; ".join(job.get("warnings") or ["No se cubrió toda la cuota solicitada."]),
            }
        )
    if not anomalies:
        anomalies.append(
            {
                "severity": "ok",
                "clip_id": None,
                "code": "clean_run",
                "title": "Ejecución limpia sin anomalías automáticas detectadas",
                "detail": f"{len(clips_out)}/{req_total} clips completados correctamente.",
            }
        )

    sentences_compact = [
        {
            "id": s.get("id"),
            "start": round(float(s.get("start") or 0.0), 2),
            "end": round(float(s.get("end") or 0.0), 2),
            "text": s.get("text") or "",
            "speaker": s.get("speaker") or "",
            "is_promo": str(s.get("id") or "") in promo_ids,
            "clip_ids": sentence_clip_map.get(str(s.get("id") or ""), []),
        }
        for s in raw_sentences
    ]

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "episode_name": job.get("episode_name") or job_id,
        "profile_id": job.get("profile_id") or "",
        "status": job.get("status") or "unknown",
        "error_message": job.get("error_message"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "audio_duration_sec": round(float(job.get("audio_duration_sec") or 0.0), 2),
        "total_processing_sec": total_duration_sec,
        "source_audio_path": str(src_audio) if src_audio else "",
        "source_audio_exists": bool(src_audio and src_audio.exists()),
        "output_dir": str(out_dir) if out_dir else "",
        "quotas": {
            "requested": {
                "short_cliffhanger": int(job.get("requested_shorts_cliffhanger") or 0),
                "short_conclusive": int(job.get("requested_shorts_conclusive") or 0),
                "medium": int(job.get("requested_medium") or 0),
                "long": int(job.get("requested_long") or 0),
                "total": req_total,
            },
            "delivered": len(clips_out),
        },
        "cost": job.get("cost") or {},
        "phase_timings": phase_timings,
        "llm_calls": llm_calls,
        "llm_fallbacks": llm_fallbacks,
        "anomalies": anomalies,
        "judge_funnel": judge_funnel,
        "judge_candidates": judge_cards,
        "visual_bible": visual_bible,
        "glossary": glossary_rows,
        "timeline": {
            "sentences": sentences_compact,
            "editorial_blocks": editorial_map.get("blocks") or [],
            "silences": draft_data.get("silences") or [],
            "music_intervals": draft_data.get("music_intervals") or [],
            "clip_spans": clip_spans_timeline,
            "excised_gaps": excised_gaps_timeline,
        },
        "clips": clips_out,
        "logs": job.get("logs") or [],
    }

    report_dir = base_dir / "analytics_studio" / "data" / job_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def import_all_historical_jobs(base_dir: Path) -> list[dict[str, Any]]:
    """Scan processing/ and rebuild report.json + analytics_studio/data/index.json for all jobs."""
    proc_root = base_dir / "processing"
    summaries: list[dict[str, Any]] = []
    if proc_root.exists():
        for d in sorted(proc_root.iterdir(), reverse=True):
            if not d.is_dir() or not (d / "job.json").exists():
                continue
            try:
                rep = export_job_report(d.name, base_dir)
                cost = rep.get("cost") or {}
                err_cnt = sum(1 for a in rep["anomalies"] if a["severity"] == "error")
                warn_cnt = sum(1 for a in rep["anomalies"] if a["severity"] == "warning")
                summaries.append(
                    {
                        "job_id": rep["job_id"],
                        "episode_name": rep["episode_name"],
                        "profile_id": rep["profile_id"],
                        "status": rep["status"],
                        "created_at": rep["created_at"],
                        "audio_duration_sec": rep["audio_duration_sec"],
                        "total_processing_sec": rep["total_processing_sec"],
                        "cost_usd": float(cost.get("cost_usd") or 0.0),
                        "total_tokens": int(cost.get("total_tokens") or 0),
                        "clips_delivered": rep["quotas"]["delivered"],
                        "clips_requested": rep["quotas"]["requested"]["total"],
                        "videos_playable": sum(1 for c in rep["clips"] if c.get("video_exists")),
                        "judge_evaluated": rep["judge_funnel"]["total_candidates_evaluated"],
                        "judge_discarded": rep["judge_funnel"]["discarded_or_rejected"],
                        "phase_durations": {
                            ph["phase"]: ph["duration_sec"] for ph in (rep.get("phase_timings") or [])
                        },
                        "error_count": err_cnt,
                        "warning_count": warn_cnt,
                        "has_transcript": len(rep["timeline"]["sentences"]) > 0,
                    }
                )
            except Exception:
                continue

    summaries.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    idx_dir = base_dir / "analytics_studio" / "data"
    idx_dir.mkdir(parents=True, exist_ok=True)
    (idx_dir / "index.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
    return summaries


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    res = import_all_historical_jobs(root)
    print(f"Exportados {len(res)} informes a {root / 'analytics_studio' / 'data'}")
