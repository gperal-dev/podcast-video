"""Resumable 5-phase podcast-to-video pipeline orchestrator with checkpoints and SSE broadcasting."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from backend.config import Settings, get_settings
from backend.core.audio_analyzer import (
    compute_rms_energy_blocks,
    detect_non_speech_intervals,
    detect_silences,
    get_audio_duration,
    merge_non_speech_with_silences,
)
from backend.core.audio_editor import (
    build_edl_for_spans,
    render_clean_audio,
    snap_spans_to_word_edges,
    trim_pass2_edge_orphans,
    verify_edl_no_duplicates,
)
from backend.core.diarizer import (
    assign_speaker_colors,
    diarize_clip_words,
    diarize_draft_sentences,
)
from backend.core.editorial_engine import (
    _PROMO_AD_PATTERNS,
    _is_clean_sentence_end,
    _is_dead_opening,
    _opening_needs_context_bridge,
    _split_hook_into_phrases,
    _strip_accents_lower,
    format_output_filename,
    generate_final_clip_metadata,
    generate_intro_curtain_script,
    make_slug,
    propose_clip_from_block,
    refine_clip_glossary_from_pass2,
    run_editorial_selection,
)
from backend.core.openrouter_client import CostTracker, OpenRouterClient
from backend.core.profile_store import ProfileStore
from backend.core.stock_fetcher import StockFetcher, write_clip_license_manifest
from backend.core.subtitle_engine import (
    generate_ass_from_edited_lines,
    generate_ass_from_words,
)
from backend.core.transcriber import (
    apply_glossary_to_words,
    compute_audio_draft_cache_key,
    load_cached_draft,
    save_cached_draft,
    transcribe_draft,
    transcribe_spans_precision,
    unload_whisper_models,
)
from backend.core.tts_engine import (
    get_or_create_profile_outro_tts,
    synthesize_speech,
)
from backend.core.video_renderer import compute_visual_hook_segment, render_final_video
from backend.core.visual_director import generate_storyboard
from backend.models.job import (
    ClipCandidate,
    EditableSubtitleLine,
    ExecutionMode,
    JobState,
    PhaseName,
    PhaseProgress,
    SentenceSegment,
    WordTimestamp,
)
from backend.models.profile import CustomImage, DEFAULT_MEDIA_PROVIDERS, MediaProviderId, PodcasterProfile


def _initial_phases() -> list[PhaseProgress]:
    return [
        PhaseProgress(phase="transcription", label="1. Transcripción local"),
        PhaseProgress(phase="editorial", label="2. Análisis editorial IA"),
        PhaseProgress(phase="audio_edit", label="3. Corte de silencios y censura"),
        PhaseProgress(phase="visual_direction", label="4. Dirección visual y stock"),
        PhaseProgress(phase="rendering", label="5. Renderizado de vídeos"),
    ]


def _resolve_curtain_enabled(
    override_val: Optional[bool],
    cfg_obj: Any,
    clip_type: str,
) -> bool:
    """Resolve whether intro or outro curtain applies to a specific clip type."""
    if override_val is not None:
        return bool(override_val)
    if not bool(getattr(cfg_obj, "enabled", False)):
        return False
    if str(clip_type).startswith("short"):
        return bool(getattr(cfg_obj, "apply_to_shorts", True))
    if clip_type == "medium":
        return bool(getattr(cfg_obj, "apply_to_medium", True))
    if clip_type == "long":
        return bool(getattr(cfg_obj, "apply_to_long", True))
    return True


def _resolve_clip_intro_curtain_enabled(
    override_val: Optional[bool],
    cfg_obj: Any,
    clip: ClipCandidate,
    mapped_words: Optional[list[WordTimestamp]] = None,
    visual_bible: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Resolve whether AI intro curtain applies based on overrides, profile mode, and opening clarity."""
    if override_val is not None:
        return bool(override_val), "anulación manual"
    if not _resolve_curtain_enabled(None, cfg_obj, clip.clip_type):
        return False, "desactivada en perfil"
    mode = str(getattr(cfg_obj, "mode", "auto_if_needed") or "auto_if_needed").strip().lower()
    if mode == "always":
        return True, "modo siempre activo"
    if clip.intro_curtain_enabled is True:
        return True, "validada en dirección editorial"
    if clip.intro_curtain_enabled is False:
        return False, "aprobada con voz propia del podcast"
    pkg = clip.standalone_packaging or {}
    if bool(pkg.get("needs_ai_intro_curtain", False)):
        return True, "arranque necesita contexto IA"
    if mapped_words and _opening_needs_context_bridge(mapped_words, visual_bible):
        return True, "pronombres huérfanos en primeros 4-6s"
    return False, "el podcast ya sitúa el contexto en los primeros 4-6s"


def _synthesize_curtain_tts_with_fallback(
    text: str,
    out_wav: Path,
    profile: PodcasterProfile,
    settings: Settings,
    llm: Optional[OpenRouterClient] = None,
) -> tuple[dict[str, Any], Optional[str]]:
    """Try configured TTS engine and fall back cleanly to Edge TTS -> Offline Local on failure."""
    ic = profile.intro_curtain
    attempts: list[tuple[str, str]] = [(ic.tts_engine, ic.tts_voice)]
    if ic.tts_engine != "edge_tts":
        attempts.append(("edge_tts", "es-ES-AlvaroNeural"))
    if ic.tts_engine != "offline_local":
        attempts.append(("offline_local", "es_ES-davefx-medium"))

    last_err = ""
    for idx, (eng, voice) in enumerate(attempts):
        try:
            res = synthesize_speech(
                text=text,
                out_wav_path=out_wav,
                engine=eng,
                voice=voice,
                rate=ic.tts_rate,
                openrouter_audio_model=ic.openrouter_audio_model,
                target_lufs=float(profile.audio.target_lufs),
                settings=settings,
                openrouter_client=llm,
                trim_silence=True,
                max_duration_sec=float(ic.max_sec) if ic.max_sec else None,
            )
            warn = (
                f"Motor TTS '{ic.tts_engine}' falló ({last_err}); usado fallback '{eng}'."
                if idx > 0 and last_err
                else None
            )
            return res, warn
        except Exception as exc:
            last_err = str(exc)
    raise RuntimeError(f"Todos los motores TTS fallaron: {last_err}")


def _resolve_profile_outro_curtain_audio(
    profile: PodcasterProfile,
    store: ProfileStore,
    settings: Settings,
    llm: Optional[OpenRouterClient] = None,
) -> tuple[Optional[Path], float, str]:
    """Return `(outro_audio_path, duration_sec, status_note)` reusing cached profile AI TTS."""
    oc = profile.outro_curtain
    target_dur = float(oc.duration_sec)
    if oc.custom_audio_filename:
        custom_path = store.audio_dir(profile.id) / oc.custom_audio_filename
        if custom_path.exists() and custom_path.stat().st_size > 1024:
            return custom_path, target_dur, f"música/audio propio ({oc.custom_audio_filename})"

    if oc.tts_enabled:
        try:
            wav_path, tts_dur, _, cache_hit = get_or_create_profile_outro_tts(
                profile=profile,
                store=store,
                settings=settings,
                openrouter_client=llm,
            )
            eff_dur = max(target_dur, min(14.0, round(tts_dur + 0.55, 2)))
            note = "voz IA cacheada del perfil" if cache_hit else "voz IA generada 1ª vez y cacheada en perfil"
            return wav_path, eff_dur, note
        except Exception as exc:
            return None, target_dur, f"solo visual (fallback por error TTS cierre: {exc})"

    return None, target_dur, "solo cortinilla visual"


class JobCancelledError(RuntimeError):
    """Raised cooperatively when a job is cancelled by the user."""


class JobManager:
    """Manages job persistence, execution threads, cancellation, review edits, and SSE subscribers."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.store = ProfileStore(self.settings)
        self._lock = threading.Lock()
        self._save_lock = threading.RLock()
        self._last_activity: dict[str, float] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._active_threads: dict[str, threading.Thread] = {}
        self._active_run_ids: dict[str, str] = {}
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = {}
        self._telemetry_sinks: dict[str, Callable[[dict[str, Any]], None]] = {}

    def job_dir(self, job_id: str) -> Path:
        path = self.settings.processing_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_json_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def _events_jsonl_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "events.jsonl"

    def _append_event(self, job_id: str, ev: dict[str, Any]) -> None:
        try:
            with self._save_lock:
                epath = self._events_jsonl_path(job_id)
                line = json.dumps(ev, ensure_ascii=False)
                with epath.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _sse_state_payload(job: JobState) -> dict[str, Any]:
        """Build a compact SSE snapshot omitting heavy word arrays and capping streamed logs."""
        data = job.model_dump(mode="json")
        for clip_d in data.get("clips") or []:
            if isinstance(clip_d, dict):
                clip_d["precision_words"] = []
                clip_d["mapped_words"] = []
        for pool_key in ("candidate_pool",):
            for clip_d in data.get(pool_key) or []:
                if isinstance(clip_d, dict):
                    clip_d["precision_words"] = []
                    clip_d["mapped_words"] = []
        if isinstance(data.get("logs"), list) and len(data["logs"]) > 250:
            data["logs"] = data["logs"][-250:]
        return {"type": "state", "job": data}

    def episode_images_dir(self, job_id: str) -> Path:
        path = self.job_dir(job_id) / "episode_images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save_job(self, job: JobState) -> None:
        with self._save_lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            job.updated_at = now_iso
            job.heartbeat_at = now_iso
            target = self.job_json_path(job.job_id)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(target)
            payload = self._sse_state_payload(job)
        self._notify_subscribers(job.job_id, payload)

    def load_job(self, job_id: str) -> JobState:
        p = self.settings.processing_dir / job_id / "job.json"
        if not p.exists():
            raise FileNotFoundError(f"Trabajo no encontrado: {job_id}")
        return JobState.model_validate_json(p.read_text(encoding="utf-8"))

    def list_jobs(self) -> list[JobState]:
        jobs: list[JobState] = []
        if not self.settings.processing_dir.exists():
            return jobs
        for d in sorted(self.settings.processing_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            jf = d / "job.json"
            if jf.exists():
                try:
                    jobs.append(JobState.model_validate_json(jf.read_text(encoding="utf-8")))
                except Exception:
                    continue
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def subscribe(self, job_id: str, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.setdefault(job_id, []).append((loop, q))
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subs = self._subscribers.get(job_id, [])
            self._subscribers[job_id] = [(lp, qq) for lp, qq in subs if qq is not q]

    def _notify_subscribers(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(job_id, []))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put_drop_oldest, q, event)
            except RuntimeError:
                pass

    @staticmethod
    def _put_drop_oldest(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def create_job(
        self,
        profile_id: str,
        source_audio_path: Path,
        episode_name: Optional[str] = None,
        shorts_cliffhanger: Optional[int] = None,
        shorts_conclusive: Optional[int] = None,
        medium_count: Optional[int] = None,
        long_count: Optional[int] = None,
        auto_mode: bool = True,
        execution_mode: Optional[ExecutionMode] = None,
        pause_before_render: bool = False,
        episode_images: Optional[list[CustomImage]] = None,
        visual_hook_enabled: Optional[bool] = None,
        visual_hook_style: Optional[str] = None,
        allowed_media_providers: Optional[list[MediaProviderId]] = None,
        intro_curtain_enabled: Optional[bool] = None,
        outro_curtain_enabled: Optional[bool] = None,
        output_dir_override: Optional[str] = None,
        save_analytics_report: bool = False,
    ) -> JobState:
        profile = self.store.get_profile(profile_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_id = f"job_{ts}_{uuid.uuid4().hex[:6]}"
        ep_title = episode_name or source_audio_path.stem
        resolved_mode: ExecutionMode = execution_mode or ("auto" if auto_mode else "review_before_render")
        eff_pause_render = bool(pause_before_render) or (resolved_mode == "review_before_render")
        eff_auto_mode = (resolved_mode == "auto") or (resolved_mode == "semi_auto" and not eff_pause_render)

        job = JobState(
            job_id=job_id,
            profile_id=profile.id,
            episode_name=ep_title,
            source_audio_path=str(source_audio_path.resolve()),
            auto_mode=eff_auto_mode,
            execution_mode=resolved_mode,
            pause_before_render=eff_pause_render,
            requested_shorts_cliffhanger=(
                profile.quotas.shorts_cliffhanger_count if shorts_cliffhanger is None else shorts_cliffhanger
            ),
            requested_shorts_conclusive=(
                profile.quotas.shorts_conclusive_count if shorts_conclusive is None else shorts_conclusive
            ),
            requested_medium=(
                profile.quotas.medium_videos_count if medium_count is None else medium_count
            ),
            requested_long=(
                profile.quotas.long_videos_count if long_count is None else long_count
            ),
            episode_images=episode_images or [],
            allowed_media_providers=(
                allowed_media_providers
                if allowed_media_providers is not None
                else list(profile.visual.allowed_media_providers or DEFAULT_MEDIA_PROVIDERS)
            ),
            visual_hook_enabled=visual_hook_enabled,
            visual_hook_style=visual_hook_style,
            intro_curtain_enabled=intro_curtain_enabled,
            outro_curtain_enabled=outro_curtain_enabled,
            output_dir_override=output_dir_override,
            save_analytics_report=save_analytics_report,
            phases=_initial_phases(),
        )
        self._save_job(job)
        return job

    def start_job_async(
        self,
        job_id: str,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        existing_to_join: Optional[threading.Thread] = None
        with self._lock:
            existing = self._active_threads.get(job_id)
            if existing and existing.is_alive():
                old_ev = self._cancel_flags.get(job_id)
                if old_ev is None or not old_ev.is_set():
                    return
                existing_to_join = existing

        if existing_to_join is not None:
            existing_to_join.join(timeout=3.0)

        with self._lock:
            existing = self._active_threads.get(job_id)
            if existing and existing.is_alive():
                old_ev = self._cancel_flags.get(job_id)
                if old_ev is None or not old_ev.is_set():
                    return
            cancel_ev = threading.Event()
            run_id = uuid.uuid4().hex
            self._cancel_flags[job_id] = cancel_ev
            self._active_run_ids[job_id] = run_id

            t = threading.Thread(
                target=self._run_job_thread,
                args=(job_id, cancel_ev, log_cb, run_id),
                daemon=True,
                name=f"pipeline-{job_id}",
            )
            self._active_threads[job_id] = t
            t.start()

    def run_job_sync(
        self,
        job_id: str,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> JobState:
        cancel_ev = threading.Event()
        run_id = uuid.uuid4().hex
        with self._lock:
            self._cancel_flags[job_id] = cancel_ev
            self._active_run_ids[job_id] = run_id
        self._run_job_thread(job_id, cancel_ev, log_cb, run_id=run_id)
        return self.load_job(job_id)

    def recover_orphaned_jobs(self) -> int:
        """Mark stale running/queued jobs without an active thread as failed so they can resume cleanly."""
        recovered = 0
        for job in self.list_jobs():
            if job.status not in ("running", "queued"):
                continue
            with self._lock:
                th = self._active_threads.get(job.job_id)
                is_live = bool(th and th.is_alive())
            if is_live:
                continue
            job.status = "failed"
            job.error_message = (
                "Proceso interrumpido por reinicio del servidor. "
                "Pulsa 'Reanudar desde checkpoint' para continuar."
            )
            for ph in job.phases:
                if ph.status == "running":
                    ph.status = "failed"
                    ph.detail = "Interrumpido por reinicio del servidor"
            self._append_log(
                job,
                "Trabajo detectado como interrumpido tras reinicio del servidor. Listo para reanudar desde checkpoint.",
                level="warning",
            )
            self._save_job(job)
            recovered += 1
        return recovered

    def cancel_job(self, job_id: str) -> JobState:
        with self._lock:
            ev = self._cancel_flags.get(job_id)
            if ev:
                ev.set()
        job = self.load_job(job_id)
        if job.status in ("queued", "running", "awaiting_review", "awaiting_topic_selection"):
            job.status = "cancelled"
            self._append_log(job, "Trabajo cancelado por el usuario.", level="warning")
            self._save_job(job)
        return job

    def update_review_edits(self, job_id: str, clips_payload: list[dict[str, Any]]) -> JobState:
        """Apply user changes made in Review mode (enable/disable, shot swaps, subtitle edits)."""
        job = self.load_job(job_id)
        profile = self.store.get_profile(job.profile_id)
        font_path = self.store.resolve_font_path(profile.id, profile.subtitles.font_file)
        ep_dir = self.episode_images_dir(job.job_id)

        clip_map = {c.id: c for c in job.clips}
        for upd in clips_payload:
            cid = upd.get("id")
            if cid not in clip_map:
                continue
            clip = clip_map[cid]
            if "enabled" in upd:
                clip.enabled = bool(upd["enabled"])

            cdir = self.job_dir(job.job_id) / "clips" / clip.id
            cdir.mkdir(parents=True, exist_ok=True)
            hook_toggled = False

            intro_changed = False
            if "intro_curtain_enabled" in upd:
                new_ic = bool(upd["intro_curtain_enabled"])
                if new_ic != bool(clip.intro_curtain_enabled):
                    intro_changed = True
                    hook_toggled = True
                clip.intro_curtain_enabled = new_ic
                if new_ic:
                    clip.visual_hook_enabled = False
                    clip.visual_hook_end_sec = 0.0
                    clip.visual_hook_spoken_text = ""

            if "intro_curtain_text" in upd:
                new_txt = str(upd["intro_curtain_text"] or "").strip()
                if new_txt != (clip.intro_curtain_text or "").strip():
                    clip.intro_curtain_text = new_txt
                    clip.intro_curtain_phrases = _split_hook_into_phrases(new_txt, max_phrases=3)
                    intro_changed = True

            if clip.intro_curtain_enabled:
                if not clip.intro_curtain_text.strip() and clip.mapped_words:
                    clip.intro_curtain_text = str(
                        clip.standalone_packaging.get("standalone_summary")
                        or clip.summary
                        or (clip.metadata.titles[0] if clip.metadata.titles else clip.slug)
                    ).strip()
                    clip.intro_curtain_phrases = _split_hook_into_phrases(clip.intro_curtain_text, max_phrases=4)
                    intro_changed = True
                if not clip.intro_curtain_badge:
                    clip.intro_curtain_badge = (profile.intro_curtain.badge_text or "").strip() or "¿LO SABÍAS?"
                if intro_changed or not clip.intro_curtain_duration_sec:
                    if profile.intro_curtain.tts_enabled and clip.intro_curtain_text.strip():
                        ic_wav = cdir / "intro_curtain.wav"
                        tts_res, _ = _synthesize_curtain_tts_with_fallback(
                            clip.intro_curtain_text,
                            ic_wav,
                            profile,
                            self.settings,
                            llm=None,
                        )
                        clip.intro_curtain_audio_path = tts_res["wav_path"]
                        clip.intro_curtain_duration_sec = round(
                            min(
                                float(profile.intro_curtain.max_sec),
                                max(float(profile.intro_curtain.min_sec), float(tts_res["duration_sec"]) + 0.25),
                            ),
                            3,
                        )
                        clip.intro_curtain_word_timings = tts_res["word_timings"]
                    else:
                        clip.intro_curtain_audio_path = None
                        clip.intro_curtain_duration_sec = min(
                            float(profile.intro_curtain.max_sec),
                            max(float(profile.intro_curtain.min_sec), 2.8),
                        )
                        clip.intro_curtain_word_timings = []
            elif intro_changed:
                clip.intro_curtain_audio_path = None
                clip.intro_curtain_duration_sec = 0.0
                clip.intro_curtain_word_timings = []

            if "outro_curtain_enabled" in upd:
                new_oc = bool(upd["outro_curtain_enabled"])
                clip.outro_curtain_enabled = new_oc
                if new_oc:
                    oc_path, oc_dur, _ = _resolve_profile_outro_curtain_audio(profile, self.store, self.settings)
                    clip.outro_curtain_audio_path = str(oc_path) if oc_path else None
                    clip.outro_curtain_duration_sec = round(oc_dur, 3)
                else:
                    clip.outro_curtain_audio_path = None
                    clip.outro_curtain_duration_sec = 0.0

            if "visual_hook_enabled" in upd and not clip.intro_curtain_enabled:
                new_hook_val = bool(upd["visual_hook_enabled"])
                if new_hook_val != bool(clip.visual_hook_enabled):
                    hook_toggled = True
                clip.visual_hook_enabled = new_hook_val
                if new_hook_val and clip.mapped_words:
                    h_end, h_txt, _ = compute_visual_hook_segment(clip.mapped_words)
                    clip.visual_hook_end_sec = h_end
                    clip.visual_hook_spoken_text = h_txt
                elif not new_hook_val:
                    clip.visual_hook_end_sec = 0.0
                    clip.visual_hook_spoken_text = ""

            if "titles" in upd and isinstance(upd["titles"], list):
                clip.metadata.titles = [str(t) for t in upd["titles"] if str(t).strip()] or clip.metadata.titles
                clip.output_filename = format_output_filename(clip.score, clip.clip_type, clip.metadata.titles[0])

            if "storyboard_shots" in upd and clip.storyboard and isinstance(upd["storyboard_shots"], list):
                all_imgs = {img.id: img for img in [*profile.custom_images, *job.episode_images]}
                shot_map = {s.id: s for s in clip.storyboard.shots}
                for s_upd in upd["storyboard_shots"]:
                    sid = s_upd.get("id")
                    if sid not in shot_map:
                        continue
                    sh = shot_map[sid]
                    if "custom_image_id" in s_upd:
                        new_cid = s_upd["custom_image_id"]
                        if new_cid and new_cid in all_imgs:
                            img = all_imgs[new_cid]
                            sh.source_type = "custom_image"
                            sh.custom_image_id = img.id
                            sh.custom_image_filename = img.filename
                            sh.display_mode = img.display_mode
                        elif new_cid is None and s_upd.get("stock_query"):
                            sh.source_type = "stock_video"
                            sh.custom_image_id = None
                            sh.custom_image_filename = None
                            sh.stock_query = str(s_upd["stock_query"])
                            sh.local_media_path = None
                    if "stock_query" in s_upd and sh.source_type in ("stock_video", "stock_photo", "archive_media"):
                        new_q = str(s_upd["stock_query"]).strip()
                        if new_q and new_q != sh.stock_query:
                            sh.stock_query = new_q
                            sh.local_media_path = None

            eff_hook_on = (
                False
                if clip.intro_curtain_enabled
                else (
                    clip.visual_hook_enabled
                    if clip.visual_hook_enabled is not None
                    else profile.subtitles.visual_hook_enabled
                )
            )
            skip_sec = float(clip.visual_hook_end_sec or 0.0) if eff_hook_on else 0.0

            if "subtitle_lines" in upd and isinstance(upd["subtitle_lines"], list):
                new_lines = [EditableSubtitleLine.model_validate(item) for item in upd["subtitle_lines"]]
                clip.subtitle_lines = new_lines
                if clip.ass_path:
                    generate_ass_from_edited_lines(
                        new_lines,
                        Path(clip.ass_path),
                        clip.aspect_ratio,
                        profile.subtitles,
                        is_short=clip.clip_type.startswith("short"),
                        font_path=font_path,
                        skip_before_sec=skip_sec,
                        speaker_color_map=clip.speaker_color_map,
                    )
            elif hook_toggled and clip.ass_path and clip.mapped_words:
                clip.subtitle_lines = generate_ass_from_words(
                    words=clip.mapped_words,
                    output_ass_path=Path(clip.ass_path),
                    aspect=clip.aspect_ratio,
                    style_cfg=profile.subtitles,
                    is_short=clip.clip_type.startswith("short"),
                    font_path=font_path,
                    skip_before_sec=skip_sec,
                    speaker_color_map=clip.speaker_color_map,
                )

        # Ensure any swapped stock queries have cached local media ready
        fetcher = StockFetcher(self.settings)
        active_provs = list(
            job.allowed_media_providers
            if job.allowed_media_providers is not None
            else (profile.visual.allowed_media_providers or DEFAULT_MEDIA_PROVIDERS)
        )
        for clip in job.clips:
            if not clip.storyboard:
                continue
            used = {sh.stock_asset_id for sh in clip.storyboard.shots if sh.stock_asset_id}
            for sh in clip.storyboard.shots:
                if sh.source_type in ("stock_video", "stock_photo", "archive_media") and (
                    not sh.local_media_path or not Path(sh.local_media_path).exists()
                ):
                    pref = "archive_media" if sh.source_type == "archive_media" else ("photo" if sh.source_type == "stock_photo" else "video")
                    hit = fetcher.fetch_verified_asset(
                        query=sh.stock_query,
                        aspect_ratio=clip.aspect_ratio,
                        prefer_type=pref,
                        used_ids=used,
                        forbidden_keywords=profile.visual.forbidden_stock_keywords,
                        spoken_context=sh.reason,
                        visual_bible=job.visual_bible,
                        llm=None,
                        enable_vlm=False,
                        allow_archive=(
                            sh.source_type == "archive_media"
                            and (
                                profile.visual.enable_public_domain_archives
                                or bool(set(active_provs) & {"wikimedia_commons", "internet_archive"})
                            )
                        ),
                        allowed_providers=active_provs,
                    )
                    if hit:
                        asset_id, lpath, actual_t, audit_info = hit
                        sh.stock_asset_id = asset_id
                        sh.local_media_path = str(lpath)
                        if actual_t.startswith("archive") or (audit_info and audit_info.get("provider") in ("wikimedia_commons", "internet_archive")):
                            sh.source_type = "archive_media"
                        else:
                            sh.source_type = "stock_photo" if actual_t == "photo" else "stock_video"
                        if audit_info:
                            clip.visual_credits_audit = [
                                a for a in clip.visual_credits_audit if a.get("shot_id") != sh.id
                            ] + [{"shot_id": sh.id, **audit_info}]
                            if audit_info.get("requires_license_warning"):
                                w_msg = (
                                    f"AVISO DE LICENCIA en {clip.id} ({sh.id}): '{audit_info.get('title')}' usa licencia "
                                    f"'{audit_info.get('license')}'. Se descargará el certificado de licencia adjunto."
                                )
                                if w_msg not in clip.license_warnings:
                                    clip.license_warnings.append(w_msg)
                                if w_msg not in job.license_warnings:
                                    job.license_warnings.append(w_msg)
                                if w_msg not in job.warnings:
                                    job.warnings.append(w_msg)

        self._append_log(job, "Cambios de revisión guardados correctamente.")
        self._save_job(job)
        return job

    def approve_and_resume(self, job_id: str) -> JobState:
        """Approve a paused Review-mode job and start Phase 5 rendering."""
        job = self.load_job(job_id)
        job.auto_mode = True
        job.status = "queued"
        self._append_log(job, "Guion aprobado por el usuario. Iniciando renderizado final...")
        self._save_job(job)
        self.start_job_async(job_id)
        return job

    def confirm_topic_selection(
        self,
        job_id: str,
        selections: list[dict[str, Any]],
        pause_before_render: Optional[bool] = None,
    ) -> JobState:
        """Confirm user-picked/rescued topics after Phase 2 in Semi-Auto mode and resume Phases 3-5."""
        job = self.load_job(job_id)
        if job.status != "awaiting_topic_selection":
            p2 = next((ph for ph in job.phases if ph.phase == "editorial"), None)
            if p2 is None or p2.status != "completed":
                raise ValueError(f"El trabajo '{job_id}' aún no está listo para seleccionar temas (estado={job.status}).")

        profile = self.store.get_profile(job.profile_id)
        if not selections:
            raise ValueError("Debes seleccionar al menos 1 tema/clip para continuar.")

        pool_by_cid: dict[str, ClipCandidate] = {}
        pool_by_id: dict[str, ClipCandidate] = {}
        for c in [*(job.clips or []), *(job.candidate_pool or [])]:
            if c.candidate_id:
                pool_by_cid[c.candidate_id] = c
            pool_by_id[c.id] = c

        rejected_by_cid: dict[str, ClipCandidate] = {}
        for rr in (job.rejected_candidates or []):
            cand_raw = rr.get("candidate") if isinstance(rr, dict) else None
            if isinstance(cand_raw, dict):
                try:
                    rc = ClipCandidate.model_validate(cand_raw)
                    if rc.candidate_id:
                        rejected_by_cid[rc.candidate_id] = rc
                    if rr.get("candidate_id"):
                        rejected_by_cid[str(rr["candidate_id"])] = rc
                except Exception:
                    continue

        eff_hook_default = (
            job.visual_hook_enabled
            if job.visual_hook_enabled is not None
            else profile.subtitles.visual_hook_enabled
        )
        final_clips: list[ClipCandidate] = []
        type_counters: dict[str, int] = {}

        for sel in selections:
            cid = str(sel.get("candidate_id") or "").strip()
            clip_id = str(sel.get("id") or "").strip()
            picked: Optional[ClipCandidate] = (
                pool_by_cid.get(cid)
                or rejected_by_cid.get(cid)
                or pool_by_id.get(clip_id)
            )
            if picked is None:
                continue
            c_obj = picked.model_copy(deep=True)
            if cid and cid in rejected_by_cid and cid not in pool_by_cid:
                c_obj.user_rescued = True
            new_type = str(sel.get("clip_type") or c_obj.clip_type).strip()
            if new_type in ("short_cliffhanger", "short_conclusive", "medium", "long"):
                c_obj.clip_type = new_type  # type: ignore[assignment]
                if new_type.startswith("short"):
                    c_obj.aspect_ratio = profile.visual.shorts_aspect
                elif new_type == "medium":
                    c_obj.aspect_ratio = profile.visual.medium_aspect
                else:
                    c_obj.aspect_ratio = profile.visual.long_aspect
            if "intro_curtain_enabled" in sel and sel["intro_curtain_enabled"] is not None:
                c_obj.intro_curtain_enabled = bool(sel["intro_curtain_enabled"])
            elif c_obj.intro_curtain_enabled is None:
                init_ic, _ = _resolve_clip_intro_curtain_enabled(
                    job.intro_curtain_enabled,
                    profile.intro_curtain,
                    c_obj,
                    visual_bible=job.visual_bible,
                )
                c_obj.intro_curtain_enabled = init_ic
            if c_obj.outro_curtain_enabled is None:
                c_obj.outro_curtain_enabled = _resolve_curtain_enabled(
                    job.outro_curtain_enabled, profile.outro_curtain, c_obj.clip_type
                )
            if c_obj.intro_curtain_enabled:
                c_obj.visual_hook_enabled = False
            elif c_obj.visual_hook_enabled is None:
                c_obj.visual_hook_enabled = eff_hook_default
            custom_title = str(sel.get("title") or "").strip()
            if custom_title:
                if not c_obj.original_title and c_obj.metadata.titles:
                    c_obj.original_title = c_obj.metadata.titles[0]
                rest_titles = [t for t in (c_obj.metadata.titles or []) if t != custom_title]
                c_obj.metadata.titles = [custom_title, *rest_titles][:3]

            type_counters[c_obj.clip_type] = type_counters.get(c_obj.clip_type, 0) + 1
            idx_num = type_counters[c_obj.clip_type]
            c_obj.id = f"{c_obj.clip_type}_{idx_num:02d}_{make_slug(c_obj.slug or 'clip')[:16]}"
            c_obj.enabled = True
            # Reset downstream per-clip artifacts if re-confirming
            c_obj.edl = None
            c_obj.storyboard = None
            c_obj.subtitle_lines = []
            c_obj.ass_path = None
            c_obj.render_completed = False
            final_clips.append(c_obj)

        if not final_clips:
            raise ValueError("Ninguno de los candidatos seleccionados se encontró en el catálogo del episodio.")

        if pause_before_render is not None:
            job.pause_before_render = bool(pause_before_render)
        job.auto_mode = not bool(job.pause_before_render)
        job.topics_confirmed = True
        job.clips = final_clips
        job.status = "queued"
        self._append_log(
            job,
            f"Selección Semiautomática confirmada ({len(final_clips)} temas elegidos, "
            f"pausa_pre_render={'sí' if job.pause_before_render else 'no'}). Reanudando en Fase 3...",
        )
        self._save_job(job)
        self.start_job_async(job_id)
        return job

    def propose_topic_from_block(
        self,
        job_id: str,
        block_index: int,
        clip_type: str = "short_conclusive",
    ) -> dict[str, Any]:
        """Use AI on demand to frame a ClipCandidate from a specific episode block in Semi-Auto mode."""
        job = self.load_job(job_id)
        profile = self.store.get_profile(job.profile_id)
        draft_path = self.job_dir(job_id) / "draft_transcript.json"
        if not draft_path.exists():
            raise FileNotFoundError("No existe draft_transcript.json para este trabajo.")
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        sentences = [SentenceSegment.model_validate(s) for s in draft_data.get("sentences", [])]
        energy_blocks = draft_data.get("energy_blocks", [])
        silences = [tuple(x) for x in draft_data.get("silences", [])]

        if clip_type not in ("short_cliffhanger", "short_conclusive", "medium", "long"):
            clip_type = "short_conclusive"

        cost_tracker = CostTracker(initial=job.cost)
        llm = OpenRouterClient(
            self.settings,
            cost_tracker=cost_tracker,
            call_log_cb=lambda m: self._append_log(job, m),
            telemetry_cb=lambda ev: self._append_event(job_id, ev),
        )
        cand = propose_clip_from_block(
            block_index=block_index,
            clip_type=clip_type,  # type: ignore[arg-type]
            sentences=sentences,
            energy_blocks=energy_blocks,
            silence_intervals=silences,  # type: ignore[arg-type]
            profile=profile,
            llm=llm,
            visual_bible=job.visual_bible,
            block_map=job.editorial_blocks,
        )
        job.cost = cost_tracker.snapshot()
        job.candidate_pool.append(cand)
        self._append_log(
            job,
            f"Nuevo tema propuesto desde Bloque B{block_index} ({clip_type}): '{cand.original_title or cand.slug}'.",
        )
        self._save_job(job)
        return cand.model_dump(mode="json")

    def get_full_logs(self, job_id: str) -> list[dict[str, Any]]:
        """Return complete chronological event/log history from events.jsonl or job.logs fallback."""
        epath = self._events_jsonl_path(job_id)
        if epath.exists():
            out: list[dict[str, Any]] = []
            for raw in epath.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            if out:
                return out
        job = self.load_job(job_id)
        return list(job.logs or [])

    def _append_log(
        self,
        job: JobState,
        message: str,
        level: str = "info",
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        is_hb = bool(message and message.startswith("[Latido]"))
        with self._save_lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            entry = {
                "ts": now_iso,
                "level": level,
                "message": message,
            }
            if is_hb and job.logs and str(job.logs[-1].get("message") or "").startswith("[Latido]"):
                job.logs[-1] = entry
            else:
                job.logs.append(entry)
                if len(job.logs) > 1500:
                    job.logs = job.logs[-1500:]
            job.heartbeat_at = now_iso
            if message and not is_hb:
                job.active_step = message[:160]
            self._last_activity[job.job_id] = time.monotonic()
        if not is_hb:
            self._append_event(job.job_id, {"type": "log", "ts": now_iso, "level": level, "message": message})
        if log_cb:
            log_cb(message)

    def _set_phase(
        self,
        job: JobState,
        phase: PhaseName,
        status: str,
        pct: float,
        detail: str = "",
        substep: str = "",
        items_done: Optional[int] = None,
        items_total: Optional[int] = None,
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        clamped_pct = round(max(0.0, min(100.0, pct)), 1)
        with self._save_lock:
            job.current_phase = phase
            job.heartbeat_at = now_iso
            eff_step = substep or detail
            if eff_step:
                job.active_step = eff_step[:160]
            self._last_activity[job.job_id] = time.monotonic()
            for ph in job.phases:
                if ph.phase == phase:
                    ph.status = status  # type: ignore[assignment]
                    ph.progress_pct = clamped_pct
                    if detail:
                        ph.detail = detail
                    if substep:
                        ph.substep = substep
                    elif detail:
                        ph.substep = detail
                    if items_done is not None:
                        ph.items_done = int(items_done)
                    if items_total is not None:
                        ph.items_total = int(items_total)
                    if status == "running" and not ph.started_at:
                        ph.started_at = now_iso
                        ph.completed_at = None
                    if status in ("completed", "failed", "skipped"):
                        if not ph.started_at:
                            ph.started_at = now_iso
                        ph.completed_at = now_iso
                    if ph.started_at:
                        try:
                            s_dt = datetime.fromisoformat(ph.started_at)
                            if s_dt.tzinfo is None:
                                s_dt = s_dt.replace(tzinfo=timezone.utc)
                            elapsed = max(0.0, (now_dt - s_dt).total_seconds())
                            ph.duration_sec = round(elapsed, 2)
                            if status == "running" and 5.0 <= clamped_pct < 100.0 and elapsed >= 1.0:
                                ph.eta_sec = round(elapsed * (100.0 - clamped_pct) / clamped_pct, 1)
                            elif status in ("completed", "failed", "skipped"):
                                ph.eta_sec = 0.0
                        except Exception:
                            pass
                    break
            weights = {
                "transcription": 0.22,
                "editorial": 0.16,
                "audio_edit": 0.22,
                "visual_direction": 0.15,
                "rendering": 0.25,
            }
            total = 0.0
            for ph in job.phases:
                w = weights.get(ph.phase, 0.2)
                p_val = 100.0 if ph.status in ("completed", "skipped") else ph.progress_pct
                total += w * p_val
            job.overall_progress_pct = round(min(100.0, total), 1)
        phase_ev = {
            "type": "phase_timing",
            "phase": phase,
            "status": status,
            "pct": clamped_pct,
            "detail": detail,
            "substep": substep or detail,
            "ts": now_iso,
        }
        sink = self._telemetry_sinks.get(job.job_id)
        if sink is not None:
            try:
                sink(phase_ev)
            except Exception:
                pass
        else:
            self._append_event(job.job_id, phase_ev)

    def _run_heartbeat_loop(
        self,
        job: JobState,
        stop_ev: threading.Event,
        cancel_ev: threading.Event,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        started_t = time.monotonic()
        while not stop_ev.wait(12.0):
            if cancel_ev.is_set():
                break
            now_m = time.monotonic()
            with self._save_lock:
                last_act = self._last_activity.get(job.job_id, started_t)
                if now_m - last_act >= 25.0:
                    elapsed_s = int(round(now_m - started_t))
                    step_txt = job.active_step or "procesando..."
                    self._append_log(
                        job,
                        f"[Latido] Sigue trabajando en [{job.current_phase}]: {step_txt} ({elapsed_s}s)",
                        log_cb=log_cb,
                    )
                try:
                    self._save_job(job)
                except Exception:
                    pass

    def _check_cancel(self, cancel_ev: threading.Event) -> None:
        if cancel_ev.is_set():
            raise JobCancelledError("Trabajo cancelado por el usuario.")

    def _run_job_thread(
        self,
        job_id: str,
        cancel_ev: threading.Event,
        log_cb: Optional[Callable[[str], None]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        job = self.load_job(job_id)
        profile = self.store.get_profile(job.profile_id)
        jdir = self.job_dir(job_id)
        ep_images_dir = self.episode_images_dir(job_id)
        src_audio = Path(job.source_audio_path)

        cost_tracker = CostTracker(initial=job.cost)
        llm: Optional[OpenRouterClient] = None
        last_llm_save = [0.0]
        telemetry_events: list[dict[str, Any]] = []
        telemetry_lock = threading.Lock()

        def _update_live_metrics(ev: dict[str, Any]) -> None:
            ev_type = str(ev.get("type") or "")
            with self._save_lock:
                lm = dict(job.live_metrics or {})
                lm.setdefault("judge_evaluated", 0)
                lm.setdefault("judge_accepted", 0)
                lm.setdefault("judge_rejected", 0)
                lm.setdefault("llm_calls", 0)
                lm.setdefault("llm_fallbacks", 0)
                lm.setdefault("qa_alerts", 0)
                if ev_type == "candidate_outcome":
                    lm["judge_evaluated"] = int(lm["judge_evaluated"]) + 1
                    if ev.get("accepted"):
                        lm["judge_accepted"] = int(lm["judge_accepted"]) + 1
                    else:
                        lm["judge_rejected"] = int(lm["judge_rejected"]) + 1
                elif ev_type == "llm_call":
                    lm["llm_calls"] = int(lm["llm_calls"]) + 1
                    if ev.get("fallback_used"):
                        lm["llm_fallbacks"] = int(lm["llm_fallbacks"]) + 1
                elif ev_type == "qa_alert":
                    lm["qa_alerts"] = int(lm["qa_alerts"]) + 1
                lm["updated_at"] = datetime.now(timezone.utc).isoformat()
                job.live_metrics = lm

        def record_telemetry(ev: dict[str, Any]) -> None:
            self._append_event(job_id, ev)
            _update_live_metrics(ev)
            with telemetry_lock:
                telemetry_events.append(ev)

        self._telemetry_sinks[job_id] = record_telemetry

        def sync_cost() -> None:
            cost_snap = cost_tracker.snapshot()
            with self._save_lock:
                job.cost = cost_snap

        def get_llm() -> OpenRouterClient:
            nonlocal llm
            if llm is None:
                def _on_llm_call(info_msg: str) -> None:
                    sync_cost()
                    self._append_log(job, info_msg, log_cb=log_cb)
                    now_m = time.monotonic()
                    if now_m - last_llm_save[0] >= 3.5:
                        last_llm_save[0] = now_m
                        try:
                            self._save_job(job)
                        except Exception:
                            pass

                llm = OpenRouterClient(
                    self.settings,
                    cost_tracker=cost_tracker,
                    cancel_check=lambda: self._check_cancel(cancel_ev),
                    call_log_cb=_on_llm_call,
                    telemetry_cb=record_telemetry,
                )
            return llm

        hb_stop = threading.Event()
        hb_thread: Optional[threading.Thread] = None

        try:
            job.status = "running"
            job.error_message = None
            self._append_log(job, f"Iniciando trabajo '{job.episode_name}' con perfil '{profile.name}'.", log_cb=log_cb)
            self._save_job(job)

            hb_thread = threading.Thread(
                target=self._run_heartbeat_loop,
                args=(job, hb_stop, cancel_ev, log_cb),
                daemon=True,
                name=f"pf_hb_{job_id}",
            )
            hb_thread.start()

            # ------------------------------------------------------------------
            # PHASE 1: Local Pass 1 Transcription + RMS Energy + Silence & Music VAD Scan
            # ------------------------------------------------------------------
            self._check_cancel(cancel_ev)
            draft_path = jdir / "draft_transcript.json"
            p1 = next(ph for ph in job.phases if ph.phase == "transcription")
            episode_speakers: list[str] = []
            non_speech_intervals: list[tuple[float, float]] = []
            music_intervals: list[tuple[float, float]] = []

            draft_cache_key = compute_audio_draft_cache_key(
                src_audio,
                language=profile.language or None,
                remove_silences=profile.audio.remove_silences,
                silence_threshold_db=profile.audio.silence_threshold_db,
                min_silence_sec=profile.audio.min_silence_sec,
                settings=self.settings,
            )
            cached_draft = (
                None
                if (p1.status == "completed" and draft_path.exists())
                else load_cached_draft(draft_cache_key, self.settings)
            )

            if p1.status == "completed" and draft_path.exists():
                draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
                sentences = [SentenceSegment.model_validate(s) for s in draft_data["sentences"]]
                energy_blocks = draft_data.get("energy_blocks", [])
                silences = [tuple(x) for x in draft_data.get("silences", [])]
                non_speech_intervals = [tuple(x) for x in draft_data.get("non_speech_intervals", [])]
                music_intervals = [tuple(x) for x in draft_data.get("music_intervals", [])]
                episode_speakers = list(
                    draft_data.get("speakers")
                    or dict.fromkeys(s.speaker for s in sentences if s.speaker)
                )
                self._append_log(job, f"Checkpoint Fase 1 reutilizado ({len(sentences)} frases).", log_cb=log_cb)
            elif cached_draft is not None and cached_draft.get("sentences"):
                self._set_phase(job, "transcription", "running", 15.0, "Reutilizando transcripción y análisis acústico cacheados...")
                job.audio_duration_sec = round(float(cached_draft.get("duration") or get_audio_duration(src_audio, self.settings)), 2)
                job.detected_language = str(cached_draft.get("language") or profile.language or "es")
                sentences = [SentenceSegment.model_validate(s) for s in cached_draft["sentences"]]
                energy_blocks = cached_draft.get("energy_blocks", [])
                silences = [tuple(x) for x in cached_draft.get("silences", [])]
                non_speech_intervals = [tuple(x) for x in cached_draft.get("non_speech_intervals", [])]
                music_intervals = [tuple(x) for x in cached_draft.get("music_intervals", [])]
                episode_speakers = list(
                    cached_draft.get("speakers")
                    or dict.fromkeys(s.speaker for s in sentences if s.speaker)
                )
                draft_path.write_text(json.dumps(cached_draft, indent=2), encoding="utf-8")
                sp_note = "1 voz" if len(episode_speakers) <= 1 else f"{len(episode_speakers)} hablantes detectados"
                self._set_phase(
                    job,
                    "transcription",
                    "completed",
                    100.0,
                    f"Caché Fase 1 reutilizada ({len(sentences)} cláusulas, {sp_note})",
                )
                self._append_log(
                    job,
                    f"Fase 1 acelerada desde caché local ({len(sentences)} cláusulas, {job.audio_duration_sec / 60:.1f} min, {sp_note}).",
                    log_cb=log_cb,
                )
                self._save_job(job)
            else:
                self._set_phase(job, "transcription", "running", 2.0, "Analizando duración y energía acústica...")
                self._save_job(job)

                job.audio_duration_sec = round(get_audio_duration(src_audio, self.settings), 2)
                block_sec = 120.0 if job.audio_duration_sec < 1500.0 else 300.0
                energy_blocks = compute_rms_energy_blocks(src_audio, block_sec=block_sec, settings=self.settings)

                silences = []
                if profile.audio.remove_silences:
                    self._set_phase(job, "transcription", "running", 8.0, "Detectando silencios en el audio...")
                    self._save_job(job)
                    silences = detect_silences(
                        src_audio,
                        threshold_db=profile.audio.silence_threshold_db,
                        min_duration=profile.audio.min_silence_sec,
                        settings=self.settings,
                    )
                    self._set_phase(job, "transcription", "running", 12.0, "Detectando música e interludios sin voz (Silero VAD)...")
                    self._save_job(job)
                    try:
                        non_speech_intervals, music_intervals = detect_non_speech_intervals(
                            src_audio,
                            min_duration=max(0.55, float(profile.audio.min_silence_sec)),
                            progress_cb=lambda pct, msg: self._set_phase(
                                job, "transcription", "running", 12.0 + (pct * 0.03), msg
                            ),
                            settings=self.settings,
                        )
                    except Exception as vad_exc:
                        non_speech_intervals, music_intervals = [], []
                        self._append_log(
                            job,
                            f"Detector Silero VAD omitido (usando solo silencios acústicos): {vad_exc}",
                            level="warning",
                            log_cb=log_cb,
                        )
                    mus_sec = sum(e - s for s, e in music_intervals)
                    self._append_log(
                        job,
                        f"Escaneo acústico: {len(silences)} silencios | {len(non_speech_intervals)} huecos sin voz | "
                        f"{len(music_intervals)} interludios musicales ({mus_sec:.1f}s).",
                        log_cb=log_cb,
                    )

                self._check_cancel(cancel_ev)
                last_save = [0.0]

                def on_whisper_pct(pct: float, msg: str) -> None:
                    self._check_cancel(cancel_ev)
                    scaled = 15.0 + (pct * 0.85)
                    self._set_phase(job, "transcription", "running", scaled, msg)
                    now_t = datetime.now().timestamp()
                    if now_t - last_save[0] >= 2.0 or pct >= 99.0:
                        last_save[0] = now_t
                        self._save_job(job)

                sentences, detected_lang = transcribe_draft(
                    src_audio,
                    language=profile.language or None,
                    progress_cb=on_whisper_pct,
                    settings=self.settings,
                )
                job.detected_language = detected_lang

                self._set_phase(job, "transcription", "running", 95.0, "Identificando voces y hablantes en local...")
                self._save_job(job)
                sentences, detected_speakers = diarize_draft_sentences(src_audio, sentences, settings=self.settings)
                episode_speakers = list(detected_speakers)

                draft_payload = {
                    "language": detected_lang,
                    "duration": job.audio_duration_sec,
                    "speakers": detected_speakers,
                    "sentences": [s.model_dump() for s in sentences],
                    "energy_blocks": energy_blocks,
                    "silences": silences,
                    "non_speech_intervals": non_speech_intervals,
                    "music_intervals": music_intervals,
                }
                draft_path.write_text(
                    json.dumps(draft_payload, indent=2),
                    encoding="utf-8",
                )
                save_cached_draft(draft_cache_key, draft_payload, self.settings)
                sp_note = "1 voz" if len(detected_speakers) <= 1 else f"{len(detected_speakers)} hablantes detectados"
                self._set_phase(
                    job,
                    "transcription",
                    "completed",
                    100.0,
                    f"{len(sentences)} cláusulas transcritas ({sp_note})",
                )
                self._append_log(
                    job,
                    f"Fase 1 completada: {len(sentences)} cláusulas ({job.audio_duration_sec / 60:.1f} min, idioma={detected_lang}, {sp_note}).",
                    log_cb=log_cb,
                )
                self._save_job(job)

            # ------------------------------------------------------------------
            # PHASE 2: Adaptive 2-Level Editorial Engine (OpenRouter)
            # ------------------------------------------------------------------
            self._check_cancel(cancel_ev)
            p2 = next(ph for ph in job.phases if ph.phase == "editorial")
            if p2.status == "completed" and (job.clips or (job.execution_mode == "semi_auto" and (job.candidate_pool or job.rejected_candidates or job.editorial_blocks))):
                self._append_log(job, f"Checkpoint Fase 2 reutilizado ({len(job.clips)} clips).", log_cb=log_cb)
            else:
                self._set_phase(job, "editorial", "running", 5.0, "Analizando estructura editorial y sub-ángulos con IA...")
                self._save_job(job)

                def on_ed_pct(pct: float, msg: str) -> None:
                    self._check_cancel(cancel_ev)
                    sync_cost()
                    self._set_phase(job, "editorial", "running", pct, msg, substep=msg)
                    self._save_job(job)

                intro_for_shorts = _resolve_curtain_enabled(
                    job.intro_curtain_enabled, profile.intro_curtain, "short_conclusive"
                )
                outro_for_shorts = _resolve_curtain_enabled(
                    job.outro_curtain_enabled, profile.outro_curtain, "short_conclusive"
                )
                intro_mode = str(getattr(profile.intro_curtain, "mode", "auto_if_needed") or "auto_if_needed").lower()
                intro_reserve = (
                    (min(5.2, float(profile.intro_curtain.max_sec)) if intro_mode == "always" else min(4.8, float(profile.intro_curtain.max_sec)))
                    if intro_for_shorts
                    else 0.0
                )
                outro_reserve = max(float(profile.outro_curtain.duration_sec), 5.5) if outro_for_shorts else 0.0
                shorts_reserve = intro_reserve + outro_reserve

                unified_gap_refs = merge_non_speech_with_silences(silences, non_speech_intervals)  # type: ignore[arg-type]
                catalog_out: dict[str, Any] = {}
                selected_clips, ed_warnings, block_map, glossary, visual_bible = run_editorial_selection(
                    sentences=sentences,
                    energy_blocks=energy_blocks,
                    silence_intervals=silences,  # type: ignore[arg-type]
                    profile=profile,
                    llm=get_llm(),
                    req_cliffhanger=job.requested_shorts_cliffhanger,
                    req_conclusive=job.requested_shorts_conclusive,
                    req_medium=job.requested_medium,
                    req_long=job.requested_long,
                    progress_cb=on_ed_pct,
                    shorts_reserve_sec=shorts_reserve,
                    non_speech_intervals=unified_gap_refs,
                    log_cb=lambda m: self._append_log(job, m, log_cb=log_cb),
                    telemetry_cb=record_telemetry,
                    catalog_out=catalog_out,
                )
                sync_cost()
                for w in ed_warnings:
                    if w not in job.warnings:
                        job.warnings.append(w)
                        self._append_log(job, f"Aviso editorial: {w}", level="warning", log_cb=log_cb)

                job.episode_glossary = glossary
                job.visual_bible = visual_bible
                job.editorial_blocks = list(block_map or [])
                job.rejected_candidates = list(catalog_out.get("rejected_candidates") or [])
                eff_hook_default = (
                    job.visual_hook_enabled
                    if job.visual_hook_enabled is not None
                    else profile.subtitles.visual_hook_enabled
                )
                for c in selected_clips:
                    if not c.original_title and c.metadata.titles:
                        c.original_title = c.metadata.titles[0]
                    if c.intro_curtain_enabled is None:
                        init_ic, _ = _resolve_clip_intro_curtain_enabled(
                            job.intro_curtain_enabled,
                            profile.intro_curtain,
                            c,
                            visual_bible=visual_bible,
                        )
                        c.intro_curtain_enabled = init_ic
                    if c.outro_curtain_enabled is None:
                        c.outro_curtain_enabled = _resolve_curtain_enabled(
                            job.outro_curtain_enabled, profile.outro_curtain, c.clip_type
                        )
                    if c.intro_curtain_enabled:
                        c.visual_hook_enabled = False
                    elif c.visual_hook_enabled is None:
                        c.visual_hook_enabled = eff_hook_default
                    pkg_info = c.standalone_packaging or {}
                    self._append_log(
                        job,
                        f"[{c.id}] Ángulo={c.angle_id or '-'} ({c.angle_theme or c.summary[:55]}) | "
                        f"auditor={pkg_info.get('audit_action', 'pass')} | intro_IA={'sí' if c.intro_curtain_enabled else 'no (voz podcast)'}",
                        log_cb=log_cb,
                    )

                job.clips = selected_clips
                job.candidate_pool = list(catalog_out.get("candidate_pool") or selected_clips)
                (jdir / "editorial_map.json").write_text(
                    json.dumps(
                        {
                            "blocks": block_map,
                            "episode_glossary": glossary,
                            "visual_bible": visual_bible,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                self._set_phase(job, "editorial", "completed", 100.0, f"{len(job.clips)} clips seleccionados")
                self._append_log(
                    job,
                    f"Fase 2 completada: {len(job.clips)} piezas elegidas | {len(job.rejected_candidates)} descartes rescatables | "
                    f"glosario={len(glossary)} términos (coste acumulado: ${job.cost.cost_usd:.4f}).",
                    log_cb=log_cb,
                )
                self._save_job(job)

            if job.execution_mode == "semi_auto" and not job.topics_confirmed:
                job.status = "awaiting_topic_selection"
                self._append_log(
                    job,
                    f"Pausa en Modo Semiautomático: {len(job.clips)} recomendados, "
                    f"{len(job.rejected_candidates)} descartes rescatables y {len(job.editorial_blocks)} bloques del episodio listos para tu selección.",
                    log_cb=log_cb,
                )
                self._save_job(job)
                return

            if not job.clips:
                raise RuntimeError("No se pudo seleccionar ningún clip válido para este audio.")

            # ------------------------------------------------------------------
            # PHASE 3: Pass 2 Precision Whisper + EDL Silences & Word Censorship
            # ------------------------------------------------------------------
            self._check_cancel(cancel_ev)
            p3 = next(ph for ph in job.phases if ph.phase == "audio_edit")
            if p3.status != "completed":
                self._set_phase(job, "audio_edit", "running", 2.0, "Preparando transcripción de precisión...")
                self._save_job(job)

                bg_music_path: Optional[Path] = None
                if profile.audio.bg_music_filename:
                    cand_bgm = self.store.audio_dir(profile.id) / profile.audio.bg_music_filename
                    if cand_bgm.exists():
                        bg_music_path = cand_bgm

                cached_outro_path: Optional[Path] = None
                cached_outro_dur = float(profile.outro_curtain.duration_sec)
                needs_outro_any = any(
                    _resolve_curtain_enabled(
                        c.outro_curtain_enabled if c.outro_curtain_enabled is not None else job.outro_curtain_enabled,
                        profile.outro_curtain,
                        c.clip_type,
                    )
                    for c in job.clips
                )
                if needs_outro_any:
                    cached_outro_path, cached_outro_dur, outro_note = _resolve_profile_outro_curtain_audio(
                        profile=profile,
                        store=self.store,
                        settings=self.settings,
                        llm=get_llm(),
                    )
                    sync_cost()
                    self._append_log(
                        job,
                        f"Cortinilla de cierre ({cached_outro_dur:.1f}s): {outro_note}.",
                        log_cb=log_cb,
                    )

                proper_nouns_list = [
                    str(x).strip()
                    for x in ((job.visual_bible or {}).get("proper_nouns") or list((job.episode_glossary or {}).values()))
                    if str(x).strip() and len(str(x).strip()) <= 36
                ][:10]
                proper_nouns_prompt = (", ".join(proper_nouns_list))[:180] if proper_nouns_list else None
                phase3_used_intros: list[str] = [
                    c.intro_curtain_text.strip()
                    for c in job.clips
                    if (c.intro_curtain_text or "").strip()
                ]

                total_c = len(job.clips)
                for idx, clip in enumerate(job.clips):
                    self._check_cancel(cancel_ev)
                    cdir = jdir / "clips" / clip.id
                    cdir.mkdir(parents=True, exist_ok=True)
                    clean_wav = cdir / "clean.wav"
                    preview_mp3 = cdir / "preview.mp3"

                    if clip.edl and clean_wav.exists() and preview_mp3.exists():
                        continue

                    self._set_phase(
                        job,
                        "audio_edit",
                        "running",
                        (idx / total_c) * 100.0,
                        f"Pasada 2 + EDL en {clip.id} ({idx + 1}/{total_c})...",
                        substep=f"Transcribiendo Pasada 2 ({clip.id})",
                        items_done=idx,
                        items_total=total_c,
                    )
                    self._save_job(job)

                    spans_list = (
                        [(float(sp.orig_start), float(sp.orig_end)) for sp in clip.narrative_spans]
                        if clip.narrative_spans
                        else [(float(clip.orig_start), float(clip.orig_end))]
                    )
                    last_p3_save = [0.0]

                    def on_p2_pct(pct_w: float, msg_w: str) -> None:
                        self._check_cancel(cancel_ev)
                        overall_p3 = ((idx + (pct_w / 100.0) * 0.85) / max(1, total_c)) * 100.0
                        self._set_phase(
                            job,
                            "audio_edit",
                            "running",
                            overall_p3,
                            f"Pasada 2 + EDL en {clip.id} ({idx + 1}/{total_c})...",
                            substep=f"[{clip.id}] {msg_w}",
                            items_done=idx,
                            items_total=total_c,
                        )
                        now_m = time.monotonic()
                        if now_m - last_p3_save[0] >= 3.0:
                            last_p3_save[0] = now_m
                            try:
                                self._save_job(job)
                            except Exception:
                                pass

                    words = transcribe_spans_precision(
                        src_audio,
                        spans_list,
                        language=job.detected_language or profile.language or None,
                        initial_prompt=proper_nouns_prompt,
                        work_dir=cdir,
                        settings=self.settings,
                        reference_sentences=sentences,
                        progress_cb=on_p2_pct,
                    )
                    try:
                        p2_fixes = refine_clip_glossary_from_pass2(
                            words=words,
                            visual_bible=job.visual_bible,
                            existing_glossary=job.episode_glossary,
                            llm=get_llm(),
                        )
                        sync_cost()
                        if p2_fixes:
                            job.episode_glossary = {**job.episode_glossary, **p2_fixes}
                            self._append_log(
                                job,
                                f"[{clip.id}] Glosario Pasada 2 +{len(p2_fixes)} correcciones: "
                                + ", ".join(f"{k}->{v}" for k, v in list(p2_fixes.items())[:5]),
                                log_cb=log_cb,
                            )
                    except Exception:
                        pass
                    words = apply_glossary_to_words(words, job.episode_glossary)
                    clip.precision_words = words

                    spans_list = snap_spans_to_word_edges(spans_list, words)
                    spans_list = trim_pass2_edge_orphans(spans_list, words)
                    if spans_list:
                        clip.orig_start = min(s for s, _ in spans_list)
                        clip.orig_end = max(e for _, e in spans_list)
                        if clip.narrative_spans and len(clip.narrative_spans) == len(spans_list):
                            for sp_obj, (ns, ne) in zip(clip.narrative_spans, spans_list):
                                sp_obj.orig_start = ns
                                sp_obj.orig_end = ne

                    edl, mapper, mapped_words = build_edl_for_spans(
                        clip.id,
                        spans_list,
                        words,
                        silences,
                        profile.audio,
                        non_speech_intervals=non_speech_intervals,
                        music_intervals=music_intervals,
                    )

                    # Post-cut boundary-safe window adjustment for single-span clips (subtract curtain time on Shorts)
                    intro_on, _ = _resolve_clip_intro_curtain_enabled(
                        job.intro_curtain_enabled,
                        profile.intro_curtain,
                        clip,
                        mapped_words=mapped_words,
                        visual_bible=job.visual_bible,
                    )
                    outro_on = _resolve_curtain_enabled(
                        clip.outro_curtain_enabled if clip.outro_curtain_enabled is not None else job.outro_curtain_enabled,
                        profile.outro_curtain,
                        clip.clip_type,
                    )
                    raw_win = (
                        profile.quotas.shorts_window
                        if clip.clip_type.startswith("short")
                        else (profile.quotas.medium_window if clip.clip_type == "medium" else profile.quotas.long_window)
                    )
                    short_res_s = (
                        (min(5.2, float(profile.intro_curtain.max_sec)) if intro_on else 0.0)
                        + (float(cached_outro_dur) if outro_on else 0.0)
                        if clip.clip_type.startswith("short")
                        else 0.0
                    )
                    win_min_sec = max(12.0, raw_win.min_sec - short_res_s) if short_res_s > 0 else raw_win.min_sec
                    win_max_sec = (
                        max(win_min_sec + 8.0, raw_win.max_sec - short_res_s) if short_res_s > 0 else raw_win.max_sec
                    )
                    if len(spans_list) <= 1 and job.audio_duration_sec * 0.90 >= win_min_sec and sentences:
                        id_to_idx = {s.id: i for i, s in enumerate(sentences)}
                        s_idx = id_to_idx.get(clip.start_sentence_id)
                        e_idx = id_to_idx.get(clip.end_sentence_id)

                        def _is_promo_sent(k: int) -> bool:
                            norm = _strip_accents_lower(sentences[k].text)
                            return any(p.search(norm) for p in _PROMO_AD_PATTERNS)

                        covered_bounds: list[tuple[float, float]] = [(clip.orig_start, clip.orig_end)]
                        attempts = 0
                        while (
                            s_idx is not None
                            and e_idx is not None
                            and attempts < 6
                            and (edl.edited_duration < win_min_sec or edl.edited_duration > win_max_sec + 4.0)
                        ):
                            next_s_idx = s_idx
                            next_e_idx = e_idx
                            changed = False
                            if edl.edited_duration < win_min_sec and e_idx + 1 < len(sentences):
                                cand_e = e_idx + 1
                                # Advance up to +2 sentences to land on a complete sentence end without hitting promos
                                if not _is_promo_sent(cand_e):
                                    if (
                                        not _is_clean_sentence_end(sentences[cand_e].text)
                                        and cand_e + 1 < len(sentences)
                                        and not _is_promo_sent(cand_e + 1)
                                        and _is_clean_sentence_end(sentences[cand_e + 1].text)
                                    ):
                                        cand_e += 1
                                    if _is_clean_sentence_end(sentences[cand_e].text):
                                        next_e_idx = cand_e
                                        changed = True
                            if not changed and edl.edited_duration < win_min_sec and s_idx > 0:
                                cand_s = s_idx - 1
                                if not _is_promo_sent(cand_s):
                                    prev_txt = sentences[cand_s - 1].text if cand_s > 0 else ""
                                    if not _is_dead_opening(sentences[cand_s].text, prev_txt):
                                        next_s_idx = cand_s
                                        changed = True
                            if not changed and edl.edited_duration > win_max_sec + 4.0 and e_idx > s_idx:
                                # Retreat only onto an earlier complete sentence end
                                for back_e in range(e_idx - 1, s_idx - 1, -1):
                                    if _is_clean_sentence_end(sentences[back_e].text):
                                        next_e_idx = back_e
                                        changed = True
                                        break
                            if not changed:
                                break
                            attempts += 1
                            s_idx = next_s_idx
                            e_idx = next_e_idx
                            new_start = sentences[s_idx].start
                            new_end = sentences[e_idx].end
                            min_cov = min(a for a, _ in covered_bounds)
                            max_cov = max(b for _, b in covered_bounds)
                            delta_spans: list[tuple[float, float]] = []
                            if new_start < min_cov - 0.15:
                                delta_spans.append((new_start, min_cov + 0.15))
                            if new_end > max_cov + 0.15:
                                delta_spans.append((max_cov - 0.15, new_end))
                            if delta_spans:
                                new_delta_words = transcribe_spans_precision(
                                    src_audio,
                                    delta_spans,
                                    language=job.detected_language or profile.language or None,
                                    initial_prompt=proper_nouns_prompt,
                                    work_dir=cdir,
                                    settings=self.settings,
                                    reference_sentences=sentences,
                                )
                                new_delta_words = apply_glossary_to_words(new_delta_words, job.episode_glossary)
                                merged_w = list(words)
                                for nw in new_delta_words:
                                    if not any(abs(nw.start - ew.start) < 0.08 for ew in merged_w):
                                        merged_w.append(nw)
                                merged_w.sort(key=lambda w: w.start)
                                words = merged_w
                                covered_bounds.append((new_start, new_end))
                                clip.precision_words = words
                            spans_list = snap_spans_to_word_edges([(new_start, new_end)], words)
                            spans_list = trim_pass2_edge_orphans(spans_list, words)
                            eff_s, eff_e = spans_list[0] if spans_list else (new_start, new_end)
                            clip.start_sentence_id = sentences[s_idx].id
                            clip.end_sentence_id = sentences[e_idx].id
                            clip.orig_start = eff_s
                            clip.orig_end = eff_e
                            if clip.narrative_spans:
                                clip.narrative_spans[0].start_sentence_id = clip.start_sentence_id
                                clip.narrative_spans[0].end_sentence_id = clip.end_sentence_id
                                clip.narrative_spans[0].orig_start = eff_s
                                clip.narrative_spans[0].orig_end = eff_e
                            edl, mapper, mapped_words = build_edl_for_spans(
                                clip.id,
                                spans_list,
                                words,
                                silences,
                                profile.audio,
                                non_speech_intervals=non_speech_intervals,
                                music_intervals=music_intervals,
                            )

                    edl_issues = verify_edl_no_duplicates(edl, mapped_words)
                    for iss in edl_issues:
                        self._append_log(job, f"[{clip.id}] {iss}", level="warning", log_cb=log_cb)

                    max_word_gap = 0.0
                    for wi in range(1, len(mapped_words)):
                        g_w = mapped_words[wi].start - mapped_words[wi - 1].end
                        if g_w > max_word_gap:
                            max_word_gap = g_w
                    self._append_log(
                        job,
                        f"[{clip.id}] EDL: {edl.edited_duration:.1f}s (bruto {edl.orig_duration:.1f}s) | "
                        f"silencios={edl.silences_removed_count} ({edl.silences_removed_sec:.1f}s) | "
                        f"música/no-voz={edl.music_gaps_removed_count} ({edl.music_gaps_removed_sec:.1f}s) | "
                        f"máx hueco={max_word_gap:.2f}s",
                        log_cb=log_cb,
                    )
                    if max_word_gap > 2.0:
                        self._append_log(
                            job,
                            f"[{clip.id}] Aviso: hueco residual de {max_word_gap:.2f}s entre palabras tras EDL.",
                            level="warning",
                            log_cb=log_cb,
                        )

                    render_clean_audio(
                        source_audio=src_audio,
                        edl_plan=edl,
                        output_wav=clean_wav,
                        audio_config=profile.audio,
                        bg_music_path=bg_music_path,
                        preview_mp3_path=preview_mp3,
                        settings=self.settings,
                    )

                    mapped_words, clip_speakers = diarize_clip_words(
                        clean_wav,
                        mapped_words,
                        settings=self.settings,
                        episode_speakers=episode_speakers,
                        draft_sentences=sentences,
                        edl_segments=edl.segments,
                    )
                    clip.edl = edl
                    clip.mapped_words = mapped_words
                    clip.speakers_detected = clip_speakers
                    full_cmap = assign_speaker_colors(
                        episode_speakers if len(episode_speakers) >= 2 else clip_speakers,
                        profile.subtitles.speaker_palette,
                        default_highlight=profile.subtitles.highlight_color,
                    )
                    clip.speaker_color_map = (
                        {sp: full_cmap[sp] for sp in clip_speakers if sp in full_cmap}
                        or assign_speaker_colors(
                            clip_speakers,
                            profile.subtitles.speaker_palette,
                            default_highlight=profile.subtitles.highlight_color,
                        )
                    )

                    if clip.metadata.chapters:
                        clip.metadata.chapters = mapper.map_chapters(clip.metadata.chapters)

                    intro_on, intro_reason = _resolve_clip_intro_curtain_enabled(
                        job.intro_curtain_enabled,
                        profile.intro_curtain,
                        clip,
                        mapped_words=mapped_words,
                        visual_bible=job.visual_bible,
                    )
                    clip.intro_curtain_enabled = bool(intro_on)
                    clip.outro_curtain_enabled = bool(outro_on)

                    if clip.intro_curtain_enabled:
                        clip.visual_hook_enabled = False
                        clip.visual_hook_end_sec = 0.0
                        clip.visual_hook_spoken_text = ""

                        if (clip.intro_curtain_text or "").strip():
                            self._append_log(
                                job,
                                f"[{clip.id}] Cortinilla IA pre-validada en Dirección Editorial: \"{clip.intro_curtain_text}\"",
                                log_cb=log_cb,
                            )
                        else:
                            script = generate_intro_curtain_script(
                                clip,
                                profile,
                                job.visual_bible,
                                get_llm(),
                                already_used_intros=phase3_used_intros,
                            )
                            sync_cost()
                            clip.intro_curtain_text = script["spoken_hook"]
                            clip.intro_curtain_phrases = script["phrases"]
                            clip.intro_curtain_badge = script["badge"]
                            if clip.intro_curtain_text.strip():
                                phase3_used_intros.append(clip.intro_curtain_text.strip())
                            self._append_log(
                                job,
                                f"[{clip.id}] Cortinilla IA ({intro_reason}): \"{clip.intro_curtain_text}\"",
                                log_cb=log_cb,
                            )

                        if profile.intro_curtain.tts_enabled:
                            ic_wav = cdir / "intro_curtain.wav"
                            tts_res, tts_warn = _synthesize_curtain_tts_with_fallback(
                                clip.intro_curtain_text,
                                ic_wav,
                                profile,
                                self.settings,
                                llm=get_llm(),
                            )
                            sync_cost()
                            if tts_warn:
                                self._append_log(job, f"[{clip.id}] {tts_warn}", level="warning", log_cb=log_cb)
                            clip.intro_curtain_audio_path = tts_res["wav_path"]
                            clip.intro_curtain_duration_sec = round(
                                min(
                                    float(profile.intro_curtain.max_sec),
                                    max(float(profile.intro_curtain.min_sec), float(tts_res["duration_sec"]) + 0.25),
                                ),
                                3,
                            )
                            clip.intro_curtain_word_timings = tts_res["word_timings"]
                        else:
                            clip.intro_curtain_audio_path = None
                            clip.intro_curtain_duration_sec = min(
                                float(profile.intro_curtain.max_sec),
                                max(float(profile.intro_curtain.min_sec), 2.8),
                            )
                            clip.intro_curtain_word_timings = []
                    else:
                        clip.intro_curtain_text = ""
                        clip.intro_curtain_phrases = []
                        clip.intro_curtain_badge = ""
                        clip.intro_curtain_audio_path = None
                        clip.intro_curtain_duration_sec = 0.0
                        clip.intro_curtain_word_timings = []
                        self._append_log(
                            job,
                            f"[{clip.id}] Sin cortinilla IA inicial ({intro_reason}).",
                            log_cb=log_cb,
                        )
                        hook_on = (
                            clip.visual_hook_enabled
                            if clip.visual_hook_enabled is not None
                            else profile.subtitles.visual_hook_enabled
                        )
                        clip.visual_hook_enabled = bool(hook_on)
                        if clip.visual_hook_enabled:
                            hook_end, hook_text, _ = compute_visual_hook_segment(clip.mapped_words)
                            clip.visual_hook_end_sec = hook_end
                            clip.visual_hook_spoken_text = hook_text
                        else:
                            clip.visual_hook_end_sec = 0.0
                            clip.visual_hook_spoken_text = ""

                    # Enforce hard Shorts max duration cap (intro + clean body + outro <= raw_win.max_sec)
                    if clip.clip_type.startswith("short"):
                        eff_outro_s = float(cached_outro_dur) if outro_on else 0.0
                        curtain_total_s = float(clip.intro_curtain_duration_sec or 0.0) + eff_outro_s
                        total_final_est = curtain_total_s + float(edl.edited_duration)
                        max_short_s = float(raw_win.max_sec)
                        if total_final_est > max_short_s + 0.05 and len(mapped_words) >= 8:
                            target_body_max = max(12.0, max_short_s - curtain_total_s - 0.35)
                            cut_orig_end: Optional[float] = None
                            # Prefer clean terminal punctuation boundary
                            for mw in reversed(mapped_words):
                                if mw.end <= target_body_max and str(mw.word or "").strip().endswith((".", "?", "!")):
                                    cut_orig_end = float(mw.orig_end)
                                    break
                            if cut_orig_end is None:
                                for mw in reversed(mapped_words):
                                    if mw.end <= target_body_max:
                                        cut_orig_end = float(mw.orig_end)
                                        break
                            if cut_orig_end is not None:
                                trimmed_spans = [
                                    (sp_s, min(sp_e, cut_orig_end + 0.05))
                                    for sp_s, sp_e in spans_list
                                    if sp_s + 1.5 < min(sp_e, cut_orig_end + 0.05)
                                ]
                                if trimmed_spans:
                                    spans_list = trimmed_spans
                                    clip.orig_start = min(s for s, _ in spans_list)
                                    clip.orig_end = max(e for _, e in spans_list)
                                    if clip.narrative_spans:
                                        clip.narrative_spans = [
                                            sp_obj.model_copy(update={"orig_start": ns, "orig_end": ne})
                                            for sp_obj, (ns, ne) in zip(clip.narrative_spans, spans_list)
                                        ]
                                    edl, mapper, mapped_words = build_edl_for_spans(
                                        clip.id,
                                        spans_list,
                                        words,
                                        silences,
                                        profile.audio,
                                        non_speech_intervals=non_speech_intervals,
                                        music_intervals=music_intervals,
                                    )
                                    render_clean_audio(
                                        source_audio=src_audio,
                                        edl_plan=edl,
                                        output_wav=clean_wav,
                                        audio_config=profile.audio,
                                        bg_music_path=bg_music_path,
                                        preview_mp3_path=preview_mp3,
                                        settings=self.settings,
                                    )
                                    mapped_words, clip_speakers = diarize_clip_words(
                                        clean_wav,
                                        mapped_words,
                                        settings=self.settings,
                                        episode_speakers=episode_speakers,
                                        draft_sentences=sentences,
                                        edl_segments=edl.segments,
                                    )
                                    clip.edl = edl
                                    clip.mapped_words = mapped_words
                                    clip.speakers_detected = clip_speakers
                                    self._append_log(
                                        job,
                                        f"[{clip.id}] Recorte límite Short aplicado: {total_final_est:.1f}s -> "
                                        f"{curtain_total_s + edl.edited_duration:.1f}s (máx {max_short_s:.0f}s).",
                                        log_cb=log_cb,
                                    )

                    if not clip.original_title and clip.metadata.titles:
                        clip.original_title = clip.metadata.titles[0]

                    clip.metadata = generate_final_clip_metadata(
                        clip,
                        profile,
                        job.episode_glossary,
                        get_llm(),
                        intro_curtain_text=clip.intro_curtain_text,
                    )
                    sync_cost()
                    if clip.metadata.titles:
                        clip.output_filename = format_output_filename(
                            clip.score,
                            clip.clip_type,
                            clip.metadata.titles[0],
                        )
                    if clip.metadata.chapters and clip.intro_curtain_duration_sec > 0:
                        for ch in clip.metadata.chapters:
                            if ch.edited_time_sec > 0.0:
                                ch.edited_time_sec = round(ch.edited_time_sec + clip.intro_curtain_duration_sec, 2)

                    if clip.outro_curtain_enabled:
                        clip.outro_curtain_audio_path = str(cached_outro_path) if cached_outro_path else None
                        clip.outro_curtain_duration_sec = round(cached_outro_dur, 3)
                    else:
                        clip.outro_curtain_audio_path = None
                        clip.outro_curtain_duration_sec = 0.0

                    clip.clean_wav_path = str(clean_wav)
                    clip.preview_audio_path = str(preview_mp3)
                    self._append_log(
                        job,
                        f"[{clip.id}] Audio limpio: {edl.orig_duration:.1f}s -> {edl.edited_duration:.1f}s "
                        f"({edl.silences_removed_count} silencios, {len(clip_speakers)} hablante(s), "
                        f"cortinilla_intro={clip.intro_curtain_duration_sec:.1f}s, "
                        f"cierre={clip.outro_curtain_duration_sec:.1f}s).",
                        log_cb=log_cb,
                    )
                    self._save_job(job)

                unload_whisper_models()
                self._set_phase(job, "audio_edit", "completed", 100.0, "EDL y audios limpios listos")
                self._save_job(job)

            # ------------------------------------------------------------------
            # PHASE 4: Visual Direction + Stock Fetch/Cache + ASS Subtitles
            # ------------------------------------------------------------------
            self._check_cancel(cancel_ev)
            p4 = next(ph for ph in job.phases if ph.phase == "visual_direction")
            if p4.status != "completed":
                self._set_phase(job, "visual_direction", "running", 2.0, "Generando storyboard y subtítulos...")
                self._save_job(job)

                fetcher = StockFetcher(self.settings)
                font_path = self.store.resolve_font_path(profile.id, profile.subtitles.font_file)
                total_c = len(job.clips)
                active_provs = list(
                    job.allowed_media_providers
                    if job.allowed_media_providers is not None
                    else (profile.visual.allowed_media_providers or DEFAULT_MEDIA_PROVIDERS)
                )

                episode_used_ids: set[str] = set()
                for c_prev in job.clips:
                    if c_prev.storyboard:
                        for s_prev in c_prev.storyboard.shots:
                            if s_prev.stock_asset_id:
                                episode_used_ids.add(s_prev.stock_asset_id)

                for idx, clip in enumerate(job.clips):
                    self._check_cancel(cancel_ev)
                    cdir = jdir / "clips" / clip.id
                    cdir.mkdir(parents=True, exist_ok=True)
                    ass_path = cdir / "subs.ass"

                    if clip.storyboard is None:
                        self._set_phase(
                            job,
                            "visual_direction",
                            "running",
                            (idx / total_c) * 100.0,
                            f"Storyboard IA y stock verificado para {clip.id} ({idx + 1}/{total_c})...",
                        )
                        self._save_job(job)
                        sb = generate_storyboard(
                            clip,
                            profile,
                            job.episode_images,
                            get_llm(),
                            visual_bible=job.visual_bible,
                            allowed_providers=active_provs,
                        )
                        sync_cost()
                        self._append_log(
                            job,
                            f"[{clip.id}] Storyboard generado: {len(sb.shots)} planos visuales ({sb.duration:.1f}s).",
                            log_cb=log_cb,
                        )

                        clip.visual_credits_audit = []
                        clip.license_warnings = []
                        total_shots = max(1, len(sb.shots))
                        used_lock = threading.Lock()
                        pending_stock: list[tuple[int, Any, str, bool]] = []
                        for sh_idx, sh in enumerate(sb.shots, start=1):
                            self._check_cancel(cancel_ev)
                            if sh.source_type == "custom_image":
                                clip.visual_credits_audit.append(
                                    {
                                        "shot_id": sh.id,
                                        "asset_id": sh.custom_image_id or "custom",
                                        "provider": "custom_images",
                                        "source": "custom_images",
                                        "title": sh.custom_image_filename or "Imagen propia del perfil/episodio",
                                        "author": profile.podcast_name,
                                        "license": "Recurso Propio del Usuario (Uso Libre)",
                                        "license_name": "Recurso Propio del Usuario (Uso Libre)",
                                        "license_url": "",
                                        "source_url": "",
                                        "verified_public_domain": True,
                                        "requires_license_warning": False,
                                        "license_summary": "Imagen propia subida o generada en pre-producción por el propietario del podcast.",
                                        "verified_at": datetime.now(timezone.utc).isoformat(),
                                    }
                                )
                            elif sh.source_type in ("stock_video", "stock_photo", "archive_media"):
                                pref = (
                                    "archive_media"
                                    if sh.source_type == "archive_media"
                                    else ("photo" if sh.source_type == "stock_photo" else "video")
                                )
                                allow_arch = bool(
                                    sh.source_type == "archive_media"
                                    and (
                                        profile.visual.enable_public_domain_archives
                                        or bool(set(active_provs) & {"wikimedia_commons", "internet_archive"})
                                    )
                                )
                                pending_stock.append((sh_idx, sh, pref, allow_arch))

                        def _fetch_one_shot(item: tuple[int, Any, str, bool]) -> tuple[int, Any, Optional[tuple[Any, ...]]]:
                            sh_i, sh_obj, pref_t, arch_ok = item
                            self._check_cancel(cancel_ev)
                            with used_lock:
                                used_snap = set(episode_used_ids)
                            hit_res = fetcher.fetch_verified_asset(
                                query=sh_obj.stock_query,
                                aspect_ratio=clip.aspect_ratio,
                                prefer_type=pref_t,
                                used_ids=used_snap,
                                forbidden_keywords=profile.visual.forbidden_stock_keywords,
                                spoken_context=sh_obj.reason,
                                visual_bible=job.visual_bible,
                                llm=get_llm(),
                                enable_vlm=profile.visual.enable_vlm_stock_check,
                                allow_archive=arch_ok,
                                allowed_providers=active_provs,
                            )
                            return sh_i, sh_obj, hit_res

                        done_shots = total_shots - len(pending_stock)
                        if pending_stock:
                            with ThreadPoolExecutor(max_workers=2) as pool:
                                fut_map = {pool.submit(_fetch_one_shot, it): it for it in pending_stock}
                                for fut in as_completed(fut_map):
                                    self._check_cancel(cancel_ev)
                                    sh_idx, sh, hit = fut.result()
                                    done_shots += 1
                                    if hit:
                                        asset_id, lpath, actual_t, audit_info = hit
                                        with used_lock:
                                            if asset_id in episode_used_ids:
                                                # Rare collision across concurrent shots: re-fetch sequentially with updated set
                                                it_orig = fut_map[fut]
                                                retry_hit = fetcher.fetch_verified_asset(
                                                    query=sh.stock_query,
                                                    aspect_ratio=clip.aspect_ratio,
                                                    prefer_type=it_orig[2],
                                                    used_ids=set(episode_used_ids),
                                                    forbidden_keywords=profile.visual.forbidden_stock_keywords,
                                                    spoken_context=sh.reason,
                                                    visual_bible=job.visual_bible,
                                                    llm=get_llm(),
                                                    enable_vlm=profile.visual.enable_vlm_stock_check,
                                                    allow_archive=it_orig[3],
                                                    allowed_providers=active_provs,
                                                )
                                                if retry_hit:
                                                    asset_id, lpath, actual_t, audit_info = retry_hit
                                            episode_used_ids.add(asset_id)
                                        sh.stock_asset_id = asset_id
                                        sh.local_media_path = str(lpath)
                                        if actual_t.startswith("archive") or (
                                            audit_info and audit_info.get("provider") in ("wikimedia_commons", "internet_archive")
                                        ):
                                            sh.source_type = "archive_media"
                                        elif actual_t == "photo":
                                            sh.source_type = "stock_photo"
                                        else:
                                            sh.source_type = "stock_video"
                                        if audit_info:
                                            clip.visual_credits_audit.append({"shot_id": sh.id, **audit_info})
                                        prov_name = (audit_info or {}).get("provider", actual_t)
                                        self._append_log(
                                            job,
                                            f"[{clip.id}] Plano {sh_idx}/{total_shots} ({sh.source_type}): \"{sh.stock_query}\" "
                                            f"-> {prov_name} | ${job.cost.cost_usd:.4f}",
                                            log_cb=log_cb,
                                        )
                                    else:
                                        self._append_log(
                                            job,
                                            f"[{clip.id}] Plano {sh_idx}/{total_shots}: sin coincidencia para \"{sh.stock_query}\" (respaldo visual).",
                                            level="warning",
                                            log_cb=log_cb,
                                        )
                                    sync_cost()
                                    self._set_phase(
                                        job,
                                        "visual_direction",
                                        "running",
                                        ((idx + done_shots / total_shots) / max(1, total_c)) * 100.0,
                                        f"Storyboard y stock para {clip.id} ({idx + 1}/{total_c})...",
                                        substep=f"[{clip.id}] Plano {done_shots}/{total_shots}: \"{sh.stock_query[:45]}\"",
                                        items_done=done_shots,
                                        items_total=total_shots,
                                    )
                                    self._save_job(job)
                        # Write per-clip license audit manifest inside processing/<job_id>/clips/<clip_id>/licencias.txt
                        lic_txt_proc = cdir / "licencias.txt"
                        _, lic_warns = write_clip_license_manifest(
                            dest_path=lic_txt_proc,
                            clip_id=clip.id,
                            episode_name=job.episode_name,
                            visual_credits_audit=clip.visual_credits_audit,
                        )
                        clip.license_manifest_path = str(lic_txt_proc)
                        for lw in lic_warns:
                            if lw not in clip.license_warnings:
                                clip.license_warnings.append(lw)
                            if lw not in job.license_warnings:
                                job.license_warnings.append(lw)
                            if lw not in job.warnings:
                                job.warnings.append(lw)
                                self._append_log(job, lw, level="warning", log_cb=log_cb)
                        clip.storyboard = sb

                    if not ass_path.exists() or not clip.subtitle_lines:
                        hook_on = (
                            False
                            if clip.intro_curtain_enabled
                            else (
                                clip.visual_hook_enabled
                                if clip.visual_hook_enabled is not None
                                else profile.subtitles.visual_hook_enabled
                            )
                        )
                        skip_sec = float(clip.visual_hook_end_sec or 0.0) if hook_on else 0.0
                        lines = generate_ass_from_words(
                            words=clip.mapped_words,
                            output_ass_path=ass_path,
                            aspect=clip.aspect_ratio,
                            style_cfg=profile.subtitles,
                            is_short=clip.clip_type.startswith("short"),
                            font_path=font_path,
                            skip_before_sec=skip_sec,
                            speaker_color_map=clip.speaker_color_map,
                        )
                        clip.subtitle_lines = lines
                    clip.ass_path = str(ass_path)
                    self._save_job(job)

                self._set_phase(job, "visual_direction", "completed", 100.0, "Storyboard y subtítulos listos")
                self._append_log(job, "Fase 4 completada: storyboards, caché stock verificado y subtítulos ASS generados.", log_cb=log_cb)
                self._save_job(job)

            # Pause here if Review Mode is active
            if not job.auto_mode:
                job.status = "awaiting_review"
                self._append_log(
                    job,
                    "Pausa en modo Revisión: revisa los clips, imágenes o subtítulos y pulsa 'Aprobar y Renderizar'.",
                    log_cb=log_cb,
                )
                self._save_job(job)
                return

            # ------------------------------------------------------------------
            # PHASE 5: Final FFmpeg Video Rendering + Thumbnails + Output Sidecar
            # ------------------------------------------------------------------
            self._check_cancel(cancel_ev)
            date_prefix = datetime.now().strftime("%Y-%m-%d")
            ep_slug = make_slug(job.episode_name, max_len=48)
            if job.output_dir_override:
                cand_out = Path(job.output_dir_override)
                out_folder = cand_out if cand_out.is_absolute() else (self.settings.base_dir / cand_out)
            else:
                out_folder = self.settings.output_dir / f"{date_prefix}_{ep_slug}"
            out_folder.mkdir(parents=True, exist_ok=True)
            job.output_dir = str(out_folder)

            enabled_clips = [c for c in job.clips if c.enabled]
            used_output_names: set[str] = {
                Path(c.rendered_video_path).name
                for c in enabled_clips
                if c.render_completed and c.rendered_video_path and Path(c.rendered_video_path).exists()
            }
            self._set_phase(job, "rendering", "running", 1.0, f"Renderizando {len(enabled_clips)} vídeos en {out_folder}...")
            self._append_log(job, f"Fase 5 iniciada: renderizando {len(enabled_clips)} vídeos en {out_folder}.", log_cb=log_cb)
            self._save_job(job)

            for idx, clip in enumerate(enabled_clips):
                self._check_cancel(cancel_ev)
                if clip.render_completed and clip.rendered_video_path and Path(clip.rendered_video_path).exists():
                    continue

                fname = clip.output_filename or format_output_filename(
                    clip.score,
                    clip.clip_type,
                    clip.metadata.titles[0] if clip.metadata.titles else clip.slug,
                )
                if fname in used_output_names:
                    fname_p = Path(fname)
                    fname = f"{fname_p.stem}_{idx + 1:02d}{fname_p.suffix or '.mp4'}"
                used_output_names.add(fname)
                clip.output_filename = fname
                out_mp4 = out_folder / fname
                thumb_jpg = out_mp4.with_suffix(".jpg")
                meta_json = out_mp4.with_suffix(".json")
                lic_txt = out_mp4.with_suffix(".licencias.txt")
                self._append_log(
                    job,
                    f"[{idx + 1}/{len(enabled_clips)}] Renderizando MP4 final: {fname}...",
                    log_cb=log_cb,
                )

                def on_render_pct(pct: float, msg: str) -> None:
                    self._check_cancel(cancel_ev)
                    overall_r = ((idx + pct / 100.0) / max(1, len(enabled_clips))) * 100.0
                    self._set_phase(
                        job,
                        "rendering",
                        "running",
                        overall_r,
                        f"[{idx + 1}/{len(enabled_clips)}] {msg}",
                        substep=f"[{clip.id}] {msg}",
                        items_done=idx,
                        items_total=len(enabled_clips),
                    )
                    with self._save_lock:
                        payload = self._sse_state_payload(job)
                    self._notify_subscribers(job.job_id, payload)

                render_final_video(
                    clip=clip,
                    profile=profile,
                    output_mp4=out_mp4,
                    thumbnail_jpg=thumb_jpg,
                    store=self.store,
                    episode_images=job.episode_images,
                    episode_images_dir=ep_images_dir,
                    progress_cb=on_render_pct,
                    settings=self.settings,
                    llm=get_llm(),
                    visual_hook_style_override=job.visual_hook_style,
                )
                sync_cost()
                clip.rendered_video_path = str(out_mp4)
                clip.thumbnail_path = str(thumb_jpg)
                clip.render_completed = True

                _, out_lic_warns = write_clip_license_manifest(
                    dest_path=lic_txt,
                    clip_id=clip.id,
                    episode_name=job.episode_name,
                    visual_credits_audit=clip.visual_credits_audit,
                )
                clip.license_manifest_path = str(lic_txt)
                for lw in out_lic_warns:
                    if lw not in clip.license_warnings:
                        clip.license_warnings.append(lw)
                    if lw not in job.license_warnings:
                        job.license_warnings.append(lw)

                rendered_dur = get_audio_duration(out_mp4, self.settings) if out_mp4.exists() else (
                    clip.edl.edited_duration if clip.edl else 0.0
                )
                sidecar = {
                    "job_id": job.job_id,
                    "clip_id": clip.id,
                    "profile_id": job.profile_id,
                    "episode_name": job.episode_name,
                    "clip_type": clip.clip_type,
                    "aspect_ratio": clip.aspect_ratio,
                    "score": clip.score,
                    "narrative_archetype": clip.narrative_archetype,
                    "speakers_detected": clip.speakers_detected,
                    "speaker_color_map": clip.speaker_color_map,
                    "visual_hook_enabled": clip.visual_hook_enabled,
                    "visual_hook_end_sec": clip.visual_hook_end_sec,
                    "visual_hook_spoken_text": clip.visual_hook_spoken_text,
                    "intro_curtain_enabled": bool(clip.intro_curtain_enabled),
                    "intro_curtain_text": clip.intro_curtain_text,
                    "intro_curtain_phrases": clip.intro_curtain_phrases,
                    "intro_curtain_duration_sec": clip.intro_curtain_duration_sec,
                    "outro_curtain_enabled": bool(clip.outro_curtain_enabled),
                    "outro_curtain_duration_sec": clip.outro_curtain_duration_sec,
                    "outro_curtain_audio_mode": profile.outro_curtain.audio_mode if clip.outro_curtain_enabled else None,
                    "visual_credits_audit": clip.visual_credits_audit,
                    "license_warnings": clip.license_warnings,
                    "license_manifest_filename": lic_txt.name,
                    "standalone_packaging": clip.standalone_packaging,
                    "duration_sec": round(rendered_dur, 3),
                    "video_filename": out_mp4.name,
                    "thumbnail_filename": thumb_jpg.name,
                    "summary": clip.summary,
                    "metadata": clip.metadata.model_dump(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                meta_json.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")

                self._append_log(
                    job,
                    f"Renderizado completado: {out_mp4.name} ({rendered_dur:.1f}s)",
                    log_cb=log_cb,
                )
                self._save_job(job)

            self._set_phase(job, "rendering", "completed", 100.0, f"{len(enabled_clips)} vídeos exportados")
            job.status = "completed"
            job.overall_progress_pct = 100.0
            self._append_log(
                job,
                f"¡Producción finalizada! Archivos en: {out_folder} | Coste total OpenRouter: ${job.cost.cost_usd:.4f} (~{job.cost.cost_eur:.4f} EUR)",
                log_cb=log_cb,
            )
            self._save_job(job)

        except JobCancelledError:
            with self._lock:
                is_current = run_id is None or self._active_run_ids.get(job_id) == run_id
            if is_current:
                job.status = "cancelled"
                self._append_log(job, "Ejecución detenida por cancelación.", level="warning", log_cb=log_cb)
                self._save_job(job)
        except Exception as exc:
            with self._lock:
                is_current = run_id is None or self._active_run_ids.get(job_id) == run_id
            if is_current:
                job.status = "failed"
                job.error_message = str(exc)
                self._set_phase(job, job.current_phase, "failed", 0.0, str(exc)[:180])
                self._append_log(job, f"Error en fase {job.current_phase}: {exc}", level="error", log_cb=log_cb)
                self._save_job(job)
        finally:
            hb_stop.set()
            if hb_thread is not None:
                hb_thread.join(timeout=2.0)
            self._telemetry_sinks.pop(job_id, None)
            if job.save_analytics_report:
                try:
                    from analytics_studio.exporter import export_job_report

                    with telemetry_lock:
                        ev_copy = list(telemetry_events)
                    export_job_report(job_id, self.settings.base_dir, telemetry_events=ev_copy)
                except Exception:
                    pass
            with self._lock:
                if run_id is None or self._active_run_ids.get(job_id) == run_id:
                    self._active_threads.pop(job_id, None)
                    self._active_run_ids.pop(job_id, None)
                    self._cancel_flags.pop(job_id, None)
