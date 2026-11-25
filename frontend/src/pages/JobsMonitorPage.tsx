import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Check,
  Coins,
  Download,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  Square,
} from "lucide-react";
import { api, clipTypeLabel, formatDuration } from "../api/client";
import { TopicSelectionPanel } from "../components/TopicSelectionPanel";
import type { ClipCandidate, JobState, PodcasterProfile } from "../types";

interface JobsMonitorPageProps {
  jobs: JobState[];
  selectedJobId: string | null;
  onSelectJob: (id: string) => void;
  onJobUpdated: (job: JobState) => void;
  profiles: PodcasterProfile[];
  onOpenGallery: () => void;
  onAnnounce: (msg: string) => void;
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  queued: { label: "En cola", cls: "bg-slate-100 text-slate-700" },
  running: { label: "En proceso", cls: "bg-indigo-100 text-indigo-800" },
  awaiting_topic_selection: { label: "Elegir temas (Semi-Auto)", cls: "bg-indigo-100 text-indigo-900" },
  awaiting_review: { label: "Esperando revisión", cls: "bg-amber-100 text-amber-900" },
  completed: { label: "Completado", cls: "bg-emerald-100 text-emerald-800" },
  failed: { label: "Interrumpido", cls: "bg-rose-100 text-rose-800" },
  cancelled: { label: "Cancelado", cls: "bg-slate-200 text-slate-700" },
};

export function JobsMonitorPage({
  jobs,
  selectedJobId,
  onSelectJob,
  onJobUpdated,
  profiles,
  onOpenGallery,
  onAnnounce,
}: JobsMonitorPageProps) {
  const activeJob = useMemo(
    () => jobs.find((j) => j.job_id === selectedJobId) || jobs[0] || null,
    [jobs, selectedJobId],
  );

  const [reviewDraft, setReviewDraft] = useState<ClipCandidate[]>([]);
  const [savingReview, setSavingReview] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [logFilter, setLogFilter] = useState<"all" | "info" | "warning" | "error" | "llm">("all");
  const [hideHeartbeat, setHideHeartbeat] = useState(true);
  const [logSearch, setLogSearch] = useState("");
  const [downloadingLogs, setDownloadingLogs] = useState(false);
  const logsContainerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (activeJob) {
      setReviewDraft(activeJob.clips);
    }
  }, [activeJob?.job_id, activeJob?.updated_at]);

  useEffect(() => {
    if (!activeJob || (activeJob.status !== "running" && activeJob.status !== "queued")) return;
    const t = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [activeJob?.job_id, activeJob?.status]);

  useEffect(() => {
    const el = logsContainerRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [activeJob?.logs.length]);

  // Live SSE subscription when a job is queued/running
  useEffect(() => {
    if (!activeJob) return;
    if (activeJob.status !== "running" && activeJob.status !== "queued") return;

    const es = new EventSource(`/api/jobs/${activeJob.job_id}/events`);
    es.addEventListener("state", (ev) => {
      try {
        const parsed = JSON.parse((ev as MessageEvent).data) as JobState;
        onJobUpdated(parsed);
      } catch {
        /* ignore */
      }
    });
    return () => es.close();
  }, [activeJob?.job_id, activeJob?.status]);

  const profile = profiles.find((p) => p.id === activeJob?.profile_id);
  const availableImages = useMemo(
    () => [...(profile?.custom_images || []), ...(activeJob?.episode_images || [])],
    [profile, activeJob],
  );

  const handleSaveReview = async (andApprove: boolean) => {
    if (!activeJob) return;
    setSavingReview(true);
    try {
      const payload = reviewDraft.map((c) => ({
        id: c.id,
        enabled: c.enabled,
        visual_hook_enabled: c.visual_hook_enabled,
        intro_curtain_enabled: c.intro_curtain_enabled,
        intro_curtain_text: c.intro_curtain_text ?? "",
        outro_curtain_enabled: c.outro_curtain_enabled,
        titles: c.metadata.titles,
        subtitle_lines: c.subtitle_lines,
        storyboard_shots: c.storyboard?.shots.map((s) => ({
          id: s.id,
          custom_image_id: s.custom_image_id || null,
          stock_query: s.stock_query,
        })),
      }));
      let updated = await api.saveReviewEdits(activeJob.job_id, payload);
      if (andApprove) {
        updated = await api.approveJob(activeJob.job_id);
        onAnnounce("Revisión aprobada. Renderizando vídeos finales...");
      } else {
        onAnnounce("Cambios de revisión guardados.");
      }
      onJobUpdated(updated);
    } finally {
      setSavingReview(false);
    }
  };

  if (!activeJob) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center shadow-sm">
        <Activity className="mx-auto h-10 w-10 text-slate-400" aria-hidden="true" />
        <h1 className="mt-3 text-xl font-bold text-slate-900">Monitor en Vivo y Revisión</h1>
        <p className="mt-1 text-sm text-slate-600">
          Todavía no hay trabajos ejecutados. Sube tu primer podcast desde «Nueva Producción».
        </p>
      </div>
    );
  }

  const badge = STATUS_BADGE[activeJob.status] || STATUS_BADGE.queued;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Monitor en Vivo y Revisión</h1>
          <p className="mt-0.5 text-sm text-slate-600">
            Progreso en tiempo real, costes de OpenRouter y edición previa antes de renderizar.
          </p>
        </div>

        {/* Job selector */}
        <div className="flex items-center gap-2">
          <label htmlFor="job-switcher" className="text-xs font-semibold text-slate-600">
            Trabajo:
          </label>
          <select
            id="job-switcher"
            value={activeJob.job_id}
            onChange={(e) => onSelectJob(e.target.value)}
            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-medium"
          >
            {jobs.map((j) => (
              <option key={j.job_id} value={j.job_id}>
                {j.episode_name} ({STATUS_BADGE[j.status]?.label || j.status})
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Top status & cost summary */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-lg font-bold text-slate-900">{activeJob.episode_name}</h2>
                <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${badge.cls}`}>
                  {badge.label}
                </span>
              </div>
              <p className="mt-0.5 text-xs text-slate-500">
                ID: {activeJob.job_id} • Duración origen: {formatDuration(activeJob.audio_duration_sec)}
              </p>
            </div>

            <div className="flex items-center gap-2">
              {(activeJob.status === "running" ||
                activeJob.status === "queued" ||
                activeJob.status === "awaiting_topic_selection" ||
                activeJob.status === "awaiting_review") && (
                <button
                  type="button"
                  onClick={async () => onJobUpdated(await api.cancelJob(activeJob.job_id))}
                  className="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700 hover:bg-rose-100"
                >
                  <Square className="h-3.5 w-3.5" aria-hidden="true" /> Cancelar
                </button>
              )}
              {(activeJob.status === "failed" || activeJob.status === "cancelled") && (
                <button
                  type="button"
                  onClick={async () => onJobUpdated(await api.resumeJob(activeJob.job_id))}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-indigo-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-indigo-700"
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Reanudar desde checkpoint
                </button>
              )}
              {activeJob.status === "completed" && (
                <button
                  type="button"
                  onClick={onOpenGallery}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-emerald-700"
                >
                  <Play className="h-3.5 w-3.5" aria-hidden="true" /> Ver vídeos en Galería
                </button>
              )}
            </div>
          </div>

          {(activeJob.status === "running" || activeJob.status === "queued") && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-indigo-200 bg-indigo-50/80 px-3.5 py-2.5 text-xs text-indigo-950">
              <div className="flex items-center gap-2.5 font-semibold">
                <span className="relative flex h-2.5 w-2.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-500 opacity-75" />
                  <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-indigo-600" />
                </span>
                <span>Trabajando activamente:</span>
                <span className="font-medium text-indigo-900">
                  {activeJob.active_step || activeJob.current_phase}
                </span>
              </div>
              <span className="text-[11px] font-medium text-indigo-700">
                Último latido hace{" "}
                {Math.max(
                  0,
                  Math.round(
                    (nowMs - Date.parse(activeJob.heartbeat_at || activeJob.updated_at)) / 1000,
                  ),
                )}
                s
              </span>
            </div>
          )}

          <div className="mt-4">
            <div className="flex justify-between text-xs font-semibold text-slate-700">
              <span>Progreso global</span>
              <span>{activeJob.overall_progress_pct.toFixed(0)}%</span>
            </div>
            <progress
              value={activeJob.overall_progress_pct}
              max={100}
              className="mt-1 h-3 w-full overflow-hidden rounded-full"
            />
          </div>

          {/* 5 phases */}
          <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-5">
            {activeJob.phases.map((ph) => {
              const color =
                ph.status === "completed"
                  ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                  : ph.status === "running"
                    ? "border-indigo-400 bg-indigo-50 text-indigo-900"
                    : ph.status === "failed"
                      ? "border-rose-300 bg-rose-50 text-rose-900"
                      : "border-slate-200 bg-slate-50 text-slate-500";
              const barPct = ph.status === "completed" ? 100 : ph.progress_pct;
              const barColor =
                ph.status === "completed"
                  ? "bg-emerald-500"
                  : ph.status === "running"
                    ? "bg-indigo-600"
                    : ph.status === "failed"
                      ? "bg-rose-500"
                      : "bg-slate-300";
              const liveElapsed =
                ph.status === "running" && ph.started_at
                  ? Math.max(0, Math.round((nowMs - Date.parse(ph.started_at)) / 1000))
                  : ph.duration_sec != null
                    ? Math.round(ph.duration_sec)
                    : null;
              return (
                <div key={ph.phase} className={`rounded-xl border p-2.5 text-xs ${color}`}>
                  <div className="flex items-center justify-between gap-1 font-semibold">
                    <span className="truncate">{ph.label}</span>
                    <span className="text-[10px] opacity-80">{barPct.toFixed(0)}%</span>
                  </div>
                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-white/70">
                    <div
                      className={`h-full transition-all duration-300 ${barColor}`}
                      style={{ width: `${Math.min(100, Math.max(0, barPct))}%` }}
                    />
                  </div>
                  <div
                    className="mt-1.5 truncate text-[11px] opacity-85"
                    title={ph.substep || ph.detail}
                  >
                    {ph.status === "completed"
                      ? "Listo (100%)"
                      : ph.substep || ph.detail || `${ph.progress_pct.toFixed(0)}%`}
                  </div>
                  {(liveElapsed != null || (ph.items_total ?? 0) > 0 || (ph.status === "running" && ph.eta_sec)) && (
                    <div className="mt-1 flex flex-wrap items-center justify-between gap-1 text-[10px] opacity-75">
                      <span>{liveElapsed != null ? formatDuration(liveElapsed) : ""}</span>
                      {(ph.items_total ?? 0) > 0 && (
                        <span>
                          {ph.items_done ?? 0}/{ph.items_total}
                        </span>
                      )}
                      {ph.status === "running" && ph.eta_sec && ph.eta_sec > 2 ? (
                        <span>ETA ~{formatDuration(ph.eta_sec)}</span>
                      ) : null}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* OpenRouter Cost card */}
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <Coins className="h-4 w-4 text-amber-500" aria-hidden="true" />
            Coste IA (OpenRouter)
          </div>
          <div className="mt-3 text-3xl font-extrabold text-slate-900">
            ${activeJob.cost.cost_usd.toFixed(4)}
            <span className="ml-2 text-sm font-medium text-slate-500">
              (~{activeJob.cost.cost_eur.toFixed(4)} €)
            </span>
          </div>
          <div className="mt-3 space-y-1 text-xs text-slate-600">
            <div>Llamadas realizadas: {activeJob.cost.total_calls}</div>
            <div>
              Tokens totales: {activeJob.cost.total_tokens.toLocaleString()} (Entrada:{" "}
              {activeJob.cost.prompt_tokens.toLocaleString()} / Salida:{" "}
              {activeJob.cost.completion_tokens.toLocaleString()})
            </div>
            <div className="font-medium text-emerald-700">Transcripción Whisper: $0.00 (100% local)</div>
            <div className="font-medium text-emerald-700">Miniaturas: $0.00 (scoring local)</div>
            {activeJob.cost.by_model && Object.keys(activeJob.cost.by_model).length > 0 && (
              <div className="mt-2 border-t border-slate-100 pt-2">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                  Desglose por modelo
                </div>
                <div className="max-h-28 space-y-1 overflow-y-auto pr-1 text-[11px]">
                  {Object.values(activeJob.cost.by_model).map((rec) => {
                    const avgLat =
                      rec.calls > 0 && rec.total_latency_sec ? rec.total_latency_sec / rec.calls : null;
                    return (
                      <div key={rec.model} className="flex items-center justify-between gap-2 text-slate-700">
                        <span className="truncate font-mono">{rec.model}</span>
                        <span className="shrink-0 font-medium">
                          {rec.calls}x • {rec.total_tokens.toLocaleString()} tok • ${rec.cost_usd.toFixed(4)}
                          {avgLat != null ? ` • ${avgLat.toFixed(1)}s` : ""}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Live Metrics & Judge Funnel Strip */}
      {(activeJob.live_metrics && Object.keys(activeJob.live_metrics).length > 0) && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-xs shadow-sm">
            <div className="text-[11px] font-semibold text-slate-500">Juez Ciego</div>
            <div className="mt-0.5 text-sm font-bold text-slate-900">
              {Number(activeJob.live_metrics.judge_accepted ?? 0)}/{Number(activeJob.live_metrics.judge_evaluated ?? 0)} aprobados
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-xs shadow-sm">
            <div className="text-[11px] font-semibold text-slate-500">Descartados</div>
            <div className="mt-0.5 text-sm font-bold text-amber-700">
              {Number(activeJob.live_metrics.judge_rejected ?? 0)} candidatos
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-xs shadow-sm">
            <div className="text-[11px] font-semibold text-slate-500">Llamadas LLM</div>
            <div className="mt-0.5 text-sm font-bold text-slate-900">
              {activeJob.cost.total_calls} ({Number(activeJob.live_metrics.llm_fallbacks ?? 0)} fallbacks)
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-xs shadow-sm">
            <div className="text-[11px] font-semibold text-slate-500">Glosario episodio</div>
            <div className="mt-0.5 text-sm font-bold text-emerald-700">
              {Object.keys(activeJob.episode_glossary || {}).length} términos
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-xs shadow-sm">
            <div className="text-[11px] font-semibold text-slate-500">Modo</div>
            <div className="mt-0.5 text-sm font-bold text-indigo-700">
              {activeJob.execution_mode || (activeJob.auto_mode ? "auto" : "review")}
            </div>
          </div>
        </div>
      )}

      {/* Warnings or Error */}
      {activeJob.warnings.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs text-amber-900">
          <div className="flex items-center gap-1.5 font-semibold">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" /> Avisos del motor editorial
          </div>
          <ul className="mt-1 list-disc pl-5 space-y-0.5">
            {activeJob.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {activeJob.error_message && (
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <strong>Error:</strong> {activeJob.error_message}
        </div>
      )}

      {/* Semi-Auto Topic Selection Panel */}
      {activeJob.status === "awaiting_topic_selection" && (
        <TopicSelectionPanel job={activeJob} onJobUpdated={onJobUpdated} onAnnounce={onAnnounce} />
      )}

      {/* Review Mode Cards */}
      {activeJob.status === "awaiting_review" && (
        <section className="space-y-4 rounded-2xl border-2 border-amber-300 bg-amber-50/40 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-bold text-slate-900">
                Revisión previa al renderizado ({reviewDraft.filter((c) => c.enabled).length} activos)
              </h2>
              <p className="text-xs text-slate-600">
                Escucha el audio ya limpio (sin silencios ni censura), cambia imágenes del storyboard o corrige subtítulos.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={savingReview}
                onClick={() => handleSaveReview(false)}
                className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-4 py-2 text-xs font-semibold text-slate-800 hover:bg-slate-50"
              >
                <Save className="h-4 w-4" aria-hidden="true" /> Guardar borrador
              </button>
              <button
                type="button"
                disabled={savingReview}
                onClick={() => handleSaveReview(true)}
                className="inline-flex items-center gap-1.5 rounded-xl bg-indigo-600 px-5 py-2.5 text-sm font-bold text-white shadow hover:bg-indigo-700"
              >
                <Sparkles className="h-4 w-4" aria-hidden="true" /> Aprobar y Renderizar
              </button>
            </div>
          </div>

          <div className="space-y-4">
            {reviewDraft.map((clip, cIdx) => (
              <div key={clip.id} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-2 text-sm font-bold text-slate-900">
                      <input
                        type="checkbox"
                        checked={clip.enabled}
                        onChange={(e) =>
                          setReviewDraft((prev) =>
                            prev.map((x, i) => (i === cIdx ? { ...x, enabled: e.target.checked } : x)),
                          )
                        }
                        className="h-4 w-4 rounded"
                      />
                      <span>{clip.metadata.titles[0] || clip.slug}</span>
                    </label>
                    <span className="rounded-full bg-indigo-100 px-2.5 py-0.5 text-xs font-bold text-indigo-800">
                      {clip.score}/100
                    </span>
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-700">
                      {clipTypeLabel(clip.clip_type)} • {clip.aspect_ratio} •{" "}
                      {formatDuration(clip.edl?.edited_duration || clip.estimated_post_cut_sec)}
                    </span>
                    {clip.angle_theme && (
                      <span className="rounded-full bg-violet-50 px-2.5 py-0.5 text-xs font-medium text-violet-800">
                        {clip.angle_id ? `${clip.angle_id}: ` : ""}
                        {clip.angle_theme}
                      </span>
                    )}
                    {clip.edl && (clip.edl.music_gaps_removed_count || 0) > 0 && (
                      <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-800">
                        Música extirpada: {clip.edl.music_gaps_removed_count} (
                        {(clip.edl.music_gaps_removed_sec || 0).toFixed(1)}s)
                      </span>
                    )}
                    {clip.narrative_archetype && (
                      <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700">
                        {clip.narrative_archetype.replace(/_/g, " ")}
                      </span>
                    )}
                    {(clip.speakers_detected || []).map((sp) => (
                      <span
                        key={sp}
                        className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700"
                      >
                        <span
                          className="h-2 w-2 rounded-full"
                          style={{ backgroundColor: clip.speaker_color_map?.[sp] || "#FFD400" }}
                        />
                        {sp}
                      </span>
                    ))}
                  </div>
                  <div className="flex flex-wrap items-center gap-3">
                    <label className="flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-indigo-800">
                      <input
                        type="checkbox"
                        checked={Boolean(clip.intro_curtain_enabled)}
                        onChange={(e) =>
                          setReviewDraft((prev) =>
                            prev.map((x, i) =>
                              i === cIdx
                                ? {
                                    ...x,
                                    intro_curtain_enabled: e.target.checked,
                                    visual_hook_enabled: e.target.checked ? false : x.visual_hook_enabled,
                                  }
                                : x,
                            ),
                          )
                        }
                        className="h-3.5 w-3.5 rounded"
                      />
                      Cortinilla inicial IA
                    </label>
                    <label className="flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-sky-800">
                      <input
                        type="checkbox"
                        checked={Boolean(clip.outro_curtain_enabled)}
                        onChange={(e) =>
                          setReviewDraft((prev) =>
                            prev.map((x, i) => (i === cIdx ? { ...x, outro_curtain_enabled: e.target.checked } : x)),
                          )
                        }
                        className="h-3.5 w-3.5 rounded"
                      />
                      Cierre del canal
                    </label>
                    {!clip.intro_curtain_enabled && (
                      <label className="flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-slate-700">
                        <input
                          type="checkbox"
                          checked={Boolean(clip.visual_hook_enabled)}
                          onChange={(e) =>
                            setReviewDraft((prev) =>
                              prev.map((x, i) => (i === cIdx ? { ...x, visual_hook_enabled: e.target.checked } : x)),
                            )
                          }
                          className="h-3.5 w-3.5 rounded"
                        />
                        Gancho visual tradicional
                      </label>
                    )}
                  </div>
                </div>

                {clip.intro_curtain_enabled && (
                  <div className="mt-2.5 rounded-xl border border-indigo-200 bg-indigo-50/60 p-3">
                    <label
                      htmlFor={`ic-txt-${clip.id}`}
                      className="flex items-center justify-between text-xs font-semibold text-indigo-950"
                    >
                      <span>Guion narrado de la cortinilla inicial IA</span>
                      {clip.intro_curtain_duration_sec ? (
                        <span className="text-[11px] font-medium text-indigo-700">
                          ~{clip.intro_curtain_duration_sec.toFixed(1)}s
                        </span>
                      ) : null}
                    </label>
                    <input
                      id={`ic-txt-${clip.id}`}
                      type="text"
                      value={clip.intro_curtain_text || ""}
                      onChange={(e) =>
                        setReviewDraft((prev) =>
                          prev.map((x, i) => (i === cIdx ? { ...x, intro_curtain_text: e.target.value } : x)),
                        )
                      }
                      placeholder="Ej. ¿Sabías por qué los primeros cazas a reacción..."
                      className="mt-1.5 w-full rounded-lg border border-indigo-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-900"
                    />
                  </div>
                )}

                <p className="mt-2 text-xs text-slate-600">{clip.summary}</p>
                {clip.standalone_packaging && clip.standalone_packaging.standalone_summary && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-lg border border-indigo-100 bg-indigo-50/50 px-2.5 py-1.5 text-[11px] text-indigo-950">
                    <span className="font-bold">Autonomía fuera del podcast:</span>
                    <span>{clip.standalone_packaging.standalone_summary}</span>
                    {clip.standalone_packaging.hook_strength && (
                      <span className="rounded bg-white px-1.5 py-0.5 font-semibold text-indigo-800">
                        Gancho {clip.standalone_packaging.hook_strength}/100 • Contexto{" "}
                        {clip.standalone_packaging.context_clarity}/100
                      </span>
                    )}
                  </div>
                )}

                {clip.license_warnings && clip.license_warnings.length > 0 && (
                  <div
                    role="alert"
                    className="mt-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950"
                  >
                    <div className="flex items-center gap-1.5 font-bold">
                      <AlertTriangle className="h-4 w-4 text-amber-700" aria-hidden="true" />
                      Aviso de licencia detectada — Se ha descargado también el archivo de licencia
                    </div>
                    <ul className="mt-1 list-disc pl-5 text-[11px]">
                      {clip.license_warnings.map((lw, lIdx) => (
                        <li key={lIdx}>{lw}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {clip.visual_credits_audit && clip.visual_credits_audit.length > 0 && (
                  <details className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50/40 px-3 py-1.5 text-xs">
                    <summary className="cursor-pointer font-semibold text-emerald-900">
                      <span className="inline-flex items-center gap-1.5">
                        <ShieldCheck className="h-3.5 w-3.5 text-emerald-700" aria-hidden="true" />
                        Auditoría de Licencias Libres ({clip.visual_credits_audit.length} planos verificados)
                      </span>
                    </summary>
                    <div className="mt-1.5 flex justify-end">
                      <a
                        href={`/api/media/processing/${encodeURIComponent(activeJob.job_id)}/clips/${encodeURIComponent(clip.id)}/licencias.txt?download=1`}
                        className="inline-flex items-center gap-1 rounded-md bg-white px-2.5 py-1 text-[11px] font-semibold text-emerald-900 ring-1 ring-emerald-300 hover:bg-emerald-50"
                      >
                        <Download className="h-3 w-3" aria-hidden="true" /> Descargar certificado .licencias.txt
                      </a>
                    </div>
                    <ul className="mt-1.5 space-y-1 text-[11px] text-slate-700">
                      {clip.visual_credits_audit.map((aud, aIdx) => (
                        <li key={aIdx}>
                          <strong>{aud.shot_id}</strong>: {aud.title} — {aud.license || aud.license_name} ({aud.provider || aud.source})
                        </li>
                      ))}
                    </ul>
                  </details>
                )}

                {clip.preview_audio_path && (
                  <div className="mt-3">
                    <span className="mb-1 block text-xs font-semibold text-slate-700">
                      Audio limpio (EDL sin silencios/censura):
                    </span>
                    <audio
                      controls
                      preload="metadata"
                      src={`/api/media/processing/${encodeURIComponent(activeJob.job_id)}/clips/${encodeURIComponent(clip.id)}/preview.mp3`}
                      className="w-full"
                    />
                  </div>
                )}

                <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
                  {/* Storyboard shots */}
                  <div>
                    <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">
                      Planos e imágenes asignadas
                    </h3>
                    <div className="mt-2 max-h-56 space-y-2 overflow-y-auto pr-1">
                      {clip.storyboard?.shots.map((sh, sIdx) => (
                        <div key={sh.id} className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs">
                          <div className="flex items-center justify-between font-semibold text-slate-700">
                            <span>
                              {sh.start.toFixed(1)}s – {sh.end.toFixed(1)}s ({sh.source_type})
                            </span>
                            <span className="text-[11px] text-slate-500">{sh.reason}</span>
                          </div>
                          {sh.source_type !== "title_card" && sh.source_type !== "quote_card" && (
                            <div className="mt-1.5 flex flex-wrap items-center gap-2">
                              <select
                                aria-label={`Imagen para plano ${sh.id}`}
                                value={sh.custom_image_id || ""}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  setReviewDraft((prev) =>
                                    prev.map((cl, i) => {
                                      if (i !== cIdx || !cl.storyboard) return cl;
                                      const nextShots = cl.storyboard.shots.map((s, j) =>
                                        j === sIdx
                                          ? {
                                              ...s,
                                              source_type: val ? ("custom_image" as const) : ("stock_video" as const),
                                              custom_image_id: val || null,
                                            }
                                          : s,
                                      );
                                      return { ...cl, storyboard: { ...cl.storyboard, shots: nextShots } };
                                    }),
                                  );
                                }}
                                className="rounded border border-slate-300 bg-white px-2 py-1 text-xs"
                              >
                                <option value="">Usar stock ({sh.stock_query || "auto"})</option>
                                {availableImages.map((img) => (
                                  <option key={img.id} value={img.id}>
                                    Imagen propia: {img.title}
                                  </option>
                                ))}
                              </select>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Subtitle lines quick editor */}
                  <div>
                    <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">
                      Editor rápido de subtítulos
                    </h3>
                    <div className="mt-2 max-h-56 space-y-1.5 overflow-y-auto pr-1">
                      {clip.subtitle_lines.map((ln, lIdx) => (
                        <div key={ln.index} className="flex items-center gap-2">
                          <span className="w-14 shrink-0 text-[11px] text-slate-400">
                            {ln.start.toFixed(1)}s
                          </span>
                          <input
                            type="text"
                            aria-label={`Línea de subtítulo ${ln.index + 1}`}
                            value={ln.text}
                            onChange={(e) => {
                              const val = e.target.value;
                              setReviewDraft((prev) =>
                                prev.map((cl, i) =>
                                  i === cIdx
                                    ? {
                                        ...cl,
                                        subtitle_lines: cl.subtitle_lines.map((line, j) =>
                                          j === lIdx ? { ...line, text: val } : line,
                                        ),
                                      }
                                    : cl,
                                ),
                              );
                            }}
                            className="w-full rounded-lg border border-slate-300 px-2.5 py-1 text-xs"
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Live SSE Log Console */}
      <div className="rounded-2xl border border-slate-800 bg-slate-950 p-4 text-slate-100 shadow-sm">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-slate-300">Registro de actividad en tiempo real</span>
            {activeJob.status === "completed" && (
              <span className="inline-flex items-center gap-1 text-emerald-400">
                <Check className="h-3.5 w-3.5" aria-hidden="true" /> Todo terminado
              </span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {(
              [
                ["all", "Todos"],
                ["llm", "LLM"],
                ["warning", "Avisos"],
                ["error", "Errores"],
              ] as const
            ).map(([fk, fl]) => (
              <button
                key={fk}
                type="button"
                onClick={() => setLogFilter(fk)}
                className={`rounded-md px-2 py-1 text-[11px] font-semibold transition ${
                  logFilter === fk
                    ? "bg-indigo-600 text-white"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                }`}
              >
                {fl}
              </button>
            ))}
            <label className="ml-1 inline-flex cursor-pointer items-center gap-1 rounded-md bg-slate-800 px-2 py-1 text-[11px] text-slate-300">
              <input
                type="checkbox"
                checked={hideHeartbeat}
                onChange={(e) => setHideHeartbeat(e.target.checked)}
                className="h-3 w-3 accent-indigo-500"
              />
              <span>Ocultar latidos</span>
            </label>
            <input
              type="search"
              placeholder="Filtrar texto..."
              value={logSearch}
              onChange={(e) => setLogSearch(e.target.value)}
              className="w-36 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[11px] text-slate-200 placeholder-slate-500"
            />
            <button
              type="button"
              disabled={downloadingLogs}
              onClick={async () => {
                setDownloadingLogs(true);
                try {
                  const res = await api.getJobLogs(activeJob.job_id);
                  const blob = new Blob([JSON.stringify(res.events, null, 2)], { type: "application/json" });
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = `${activeJob.job_id}_logs.json`;
                  a.click();
                  URL.revokeObjectURL(url);
                } finally {
                  setDownloadingLogs(false);
                }
              }}
              className="inline-flex items-center gap-1 rounded-md bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-200 hover:bg-slate-700"
            >
              <Download className="h-3 w-3" aria-hidden="true" /> Log completo
            </button>
          </div>
        </div>
        <div ref={logsContainerRef} className="max-h-80 space-y-1 overflow-y-auto font-mono text-xs">
          {activeJob.logs
            .filter((log) => {
              if (hideHeartbeat && log.message.startsWith("[Latido]")) return false;
              if (logFilter === "llm" && !log.message.startsWith("LLM ")) return false;
              if (logFilter === "warning" && log.level !== "warning") return false;
              if (logFilter === "error" && log.level !== "error") return false;
              if (logFilter === "info" && log.level !== "info") return false;
              if (logSearch.trim() && !log.message.toLowerCase().includes(logSearch.trim().toLowerCase())) {
                return false;
              }
              return true;
            })
            .slice(-300)
            .map((log, i) => (
              <div
                key={i}
                className={
                  log.level === "error"
                    ? "text-rose-400"
                    : log.level === "warning"
                      ? "text-amber-300"
                      : log.message.startsWith("[Latido]")
                        ? "text-cyan-300"
                        : log.message.startsWith("LLM ")
                          ? "text-emerald-300"
                          : "text-slate-200"
                }
              >
                <span className="mr-2 text-slate-500">{log.ts.slice(11, 19)}</span>
                {log.message}
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
