import { useMemo, useState } from "react";
import { Check, CheckCircle2, Compass, Layers, LifeBuoy, Loader2, Plus, Sparkles } from "lucide-react";
import { api, clipTypeLabel, formatDuration } from "../api/client";
import type { ClipCandidate, ClipType, EditorialBlockItem, JobState, RejectedCandidateRecord } from "../types";

interface TopicSelectionPanelProps {
  job: JobState;
  onJobUpdated: (job: JobState) => void;
  onAnnounce: (msg: string) => void;
}

interface SelectedItemDraft {
  candidate_id: string;
  clip_type: ClipType;
  intro_curtain_enabled: boolean;
  title: string;
}

export function TopicSelectionPanel({ job, onJobUpdated, onAnnounce }: TopicSelectionPanelProps) {
  const recommendedList = useMemo<ClipCandidate[]>(() => {
    const merged = [...(job.clips || []), ...(job.candidate_pool || [])];
    const seen = new Set<string>();
    const out: ClipCandidate[] = [];
    for (const c of merged) {
      const k = String(c.candidate_id || c.id);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(c);
    }
    return out;
  }, [job.clips, job.candidate_pool]);

  const rejectedList = useMemo<RejectedCandidateRecord[]>(
    () => job.rejected_candidates || [],
    [job.rejected_candidates],
  );
  const blocksList = useMemo<EditorialBlockItem[]>(
    () => job.editorial_blocks || [],
    [job.editorial_blocks],
  );

  const [tab, setTab] = useState<"recommended" | "rejected" | "blocks">(
    recommendedList.length > 0 ? "recommended" : rejectedList.length > 0 ? "rejected" : "blocks",
  );
  const [selectedMap, setSelectedMap] = useState<Record<string, SelectedItemDraft>>(() => {
    const init: Record<string, SelectedItemDraft> = {};
    for (const c of job.clips || []) {
      const k = String(c.candidate_id || c.id);
      init[k] = {
        candidate_id: k,
        clip_type: c.clip_type,
        intro_curtain_enabled: Boolean(c.intro_curtain_enabled),
        title: c.metadata?.titles?.[0] || c.original_title || c.slug,
      };
    }
    return init;
  });
  const [pauseBeforeRender, setPauseBeforeRender] = useState(Boolean(job.pause_before_render));
  const [submitting, setSubmitting] = useState(false);
  const [proposingBlock, setProposingBlock] = useState<number | null>(null);
  const [proposeTypeByBlock, setProposeTypeByBlock] = useState<Record<number, ClipType>>({});
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const srcAudioUrl = api.sourceAudioUrl(job.job_id);
  const selectedList = Object.values(selectedMap);

  const toggleCandidate = (
    cid: string,
    defaults: { clip_type: ClipType; intro_curtain_enabled: boolean; title: string },
  ) => {
    setSelectedMap((prev) => {
      const next = { ...prev };
      if (next[cid]) {
        delete next[cid];
      } else {
        next[cid] = { candidate_id: cid, ...defaults };
      }
      return next;
    });
  };

  const updateSelected = (cid: string, patch: Partial<SelectedItemDraft>) => {
    setSelectedMap((prev) => {
      const cur = prev[cid];
      if (!cur) return prev;
      return { ...prev, [cid]: { ...cur, ...patch } };
    });
  };

  const handleProposeFromBlock = async (blockIdx: number) => {
    setErrorMsg(null);
    setProposingBlock(blockIdx);
    const chosenType: ClipType = proposeTypeByBlock[blockIdx] || "short_conclusive";
    try {
      const created = (await api.proposeBlock(job.job_id, blockIdx, chosenType)) as unknown as ClipCandidate;
      const refreshed = await api.getJob(job.job_id);
      onJobUpdated(refreshed);
      const cid = String(created.candidate_id || created.id);
      setSelectedMap((prev) => ({
        ...prev,
        [cid]: {
          candidate_id: cid,
          clip_type: created.clip_type || chosenType,
          intro_curtain_enabled: Boolean(created.intro_curtain_enabled),
          title: created.metadata?.titles?.[0] || created.original_title || created.slug || `Bloque B${blockIdx}`,
        },
      }));
      setTab("recommended");
      onAnnounce(`Propuesta generada desde Bloque B${blockIdx} y añadida a tu selección.`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "No se pudo proponer un tema desde ese bloque.");
    } finally {
      setProposingBlock(null);
    }
  };

  const handleConfirm = async () => {
    if (selectedList.length === 0) {
      setErrorMsg("Selecciona o rescata al menos 1 tema para continuar.");
      return;
    }
    setErrorMsg(null);
    setSubmitting(true);
    try {
      const updated = await api.selectTopics(
        job.job_id,
        selectedList.map((s) => ({
          candidate_id: s.candidate_id,
          clip_type: s.clip_type,
          intro_curtain_enabled: s.intro_curtain_enabled,
          title: s.title,
        })),
        pauseBeforeRender,
      );
      onJobUpdated(updated);
      onAnnounce(`Selección confirmada (${selectedList.length} temas). Continuando en Pasada 2...`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Error al confirmar la selección.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="space-y-4 rounded-2xl border-2 border-indigo-400 bg-indigo-50/35 p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-bold text-slate-900">
            <Compass className="h-5 w-5 text-indigo-600" aria-hidden="true" />
            Selección Semiautomática de Temas ({selectedList.length} elegidos)
          </h2>
          <p className="mt-0.5 text-xs text-slate-600">
            Escucha cualquier fragmento del audio original antes de continuar. Solo se transcribirán en Pasada 2, buscarán stock y renderizarán los temas que marques aquí.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setTab("recommended")}
            className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-semibold transition ${
              tab === "recommended"
                ? "bg-indigo-600 text-white shadow-sm"
                : "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
            }`}
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            Recomendados IA ({recommendedList.length})
          </button>
          <button
            type="button"
            onClick={() => setTab("rejected")}
            className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-semibold transition ${
              tab === "rejected"
                ? "bg-amber-600 text-white shadow-sm"
                : "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
            }`}
          >
            <LifeBuoy className="h-3.5 w-3.5" aria-hidden="true" />
            Descartes rescatables ({rejectedList.length})
          </button>
          <button
            type="button"
            onClick={() => setTab("blocks")}
            className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-semibold transition ${
              tab === "blocks"
                ? "bg-slate-900 text-white shadow-sm"
                : "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
            }`}
          >
            <Layers className="h-3.5 w-3.5" aria-hidden="true" />
            Mapa completo ({blocksList.length})
          </button>
        </div>
      </div>

      {errorMsg && (
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2 text-xs text-rose-800">
          {errorMsg}
        </div>
      )}

      {tab === "recommended" && (
        <div className="space-y-3">
          {recommendedList.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-xs text-slate-500">
              No hay clips recomendados todavía. Revisa «Descartes rescatables» o propón un tema desde el «Mapa completo».
            </div>
          ) : (
            recommendedList.map((clip) => {
              const cid = String(clip.candidate_id || clip.id);
              const sel = selectedMap[cid];
              const isChecked = Boolean(sel);
              const pkg = clip.standalone_packaging || {};
              const segStart = Math.max(0, Math.floor(clip.orig_start));
              const segEnd = Math.max(segStart + 1, Math.ceil(clip.orig_end));
              return (
                <div
                  key={cid}
                  className={`rounded-xl border bg-white p-4 transition ${
                    isChecked ? "border-indigo-400 ring-1 ring-indigo-200" : "border-slate-200 opacity-85"
                  }`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <label className="flex flex-1 cursor-pointer items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() =>
                          toggleCandidate(cid, {
                            clip_type: clip.clip_type,
                            intro_curtain_enabled: Boolean(clip.intro_curtain_enabled),
                            title: clip.metadata?.titles?.[0] || clip.original_title || clip.slug,
                          })
                        }
                        className="mt-1 h-4 w-4 accent-indigo-600"
                      />
                      <div className="flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-bold text-slate-900">
                            {sel?.title || clip.metadata?.titles?.[0] || clip.slug}
                          </span>
                          <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] font-semibold text-indigo-800">
                            {clipTypeLabel(sel?.clip_type || clip.clip_type)}
                          </span>
                          <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-semibold text-emerald-800">
                            Score {clip.score}
                          </span>
                          {clip.user_rescued && (
                            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-800">
                              Rescatado/Propuesto
                            </span>
                          )}
                          <span className="text-[11px] text-slate-500">
                            {formatDuration(clip.orig_start)} – {formatDuration(clip.orig_end)} (~
                            {Math.round(clip.estimated_post_cut_sec)}s)
                          </span>
                        </div>
                        <p className="text-xs text-slate-600">{clip.summary}</p>
                        {pkg.who_identified && (
                          <div className="text-[11px] text-slate-500">
                            Protagonista: <strong>{pkg.who_identified}</strong> • Comp={pkg.comprehension_score ?? "-"} •
                            Ctx={pkg.context_clarity ?? "-"} • Res={pkg.resolution_score ?? "-"}
                          </div>
                        )}
                      </div>
                    </label>

                    {isChecked && (
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <select
                          aria-label="Formato de clip"
                          value={sel.clip_type}
                          onChange={(e) => updateSelected(cid, { clip_type: e.target.value as ClipType })}
                          className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs"
                        >
                          <option value="short_conclusive">Short Conclusivo</option>
                          <option value="short_cliffhanger">Short Cliffhanger</option>
                          <option value="medium">Vídeo Medio (5–6m)</option>
                          <option value="long">Vídeo Largo (12m)</option>
                        </select>
                        <label className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-slate-200 px-2 py-1">
                          <input
                            type="checkbox"
                            checked={sel.intro_curtain_enabled}
                            onChange={(e) => updateSelected(cid, { intro_curtain_enabled: e.target.checked })}
                            className="h-3.5 w-3.5 accent-indigo-600"
                          />
                          <span>Cortinilla IA</span>
                        </label>
                      </div>
                    )}
                  </div>

                  <div className="mt-2.5 flex items-center gap-3">
                    <audio
                      controls
                      preload="none"
                      src={`${srcAudioUrl}#t=${segStart},${segEnd}`}
                      className="h-8 w-full max-w-md"
                    />
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}

      {tab === "rejected" && (
        <div className="space-y-3">
          {rejectedList.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-xs text-slate-500">
              El Juez Ciego no descartó ningún candidato en esta ejecución.
            </div>
          ) : (
            rejectedList.map((rej) => {
              const cid = String(rej.candidate_id);
              const isSelected = Boolean(selectedMap[cid]);
              const segStart = Math.max(0, Math.floor(rej.orig_start));
              const segEnd = Math.max(segStart + 1, Math.ceil(rej.orig_end));
              return (
                <div
                  key={cid}
                  className={`rounded-xl border bg-white p-4 transition ${
                    isSelected ? "border-amber-400 ring-1 ring-amber-200" : "border-slate-200"
                  }`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-bold text-slate-900">{rej.title}</span>
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-700">
                          {clipTypeLabel(rej.clip_type)}
                        </span>
                        <span className="text-[11px] text-slate-500">
                          {formatDuration(rej.orig_start)} – {formatDuration(rej.orig_end)}
                        </span>
                      </div>
                      <div className="text-xs text-rose-700">
                        <strong>Motivo descarte:</strong> {rej.reason}
                      </div>
                      <div className="text-[11px] text-slate-500">
                        Comp={rej.comprehension_score ?? "-"} • Ctx={rej.context_clarity ?? "-"} • Limpieza=
                        {rej.clean_boundaries ?? "-"} • Res={rej.resolution_score ?? "-"}
                        {rej.story_interest_score ? ` • Interés=${rej.story_interest_score}` : ""}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        toggleCandidate(cid, {
                          clip_type: rej.clip_type,
                          intro_curtain_enabled: true,
                          title: rej.title,
                        })
                      }
                      className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-semibold transition ${
                        isSelected
                          ? "bg-emerald-600 text-white hover:bg-emerald-700"
                          : "border border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100"
                      }`}
                    >
                      {isSelected ? (
                        <>
                          <Check className="h-3.5 w-3.5" aria-hidden="true" /> Rescatado
                        </>
                      ) : (
                        <>
                          <LifeBuoy className="h-3.5 w-3.5" aria-hidden="true" /> Rescatar con Cortinilla IA
                        </>
                      )}
                    </button>
                  </div>
                  <div className="mt-2.5">
                    <audio
                      controls
                      preload="none"
                      src={`${srcAudioUrl}#t=${segStart},${segEnd}`}
                      className="h-8 w-full max-w-md"
                    />
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}

      {tab === "blocks" && (
        <div className="max-h-[32rem] space-y-2.5 overflow-y-auto pr-1">
          {blocksList.map((blk) => {
            const bi = Number(blk.block_index);
            const stSec = Math.max(0, Math.floor(Number(blk.start || 0)));
            const enSec = Math.max(stSec + 5, Math.ceil(Number(blk.end || stSec + 60)));
            const cType: ClipType = proposeTypeByBlock[bi] || "short_conclusive";
            const busy = proposingBlock === bi;
            return (
              <div
                key={bi}
                className={`rounded-xl border p-3.5 text-xs ${
                  blk.is_ad_or_promo ? "border-rose-200 bg-rose-50/40" : "border-slate-200 bg-white"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-[11px] font-bold text-white">
                        B{bi}
                      </span>
                      <span className="font-bold text-slate-900">{blk.tema || `Bloque ${bi}`}</span>
                      {blk.interes_general != null && (
                        <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] font-semibold text-indigo-800">
                          Interés {blk.interes_general}/100
                        </span>
                      )}
                      {blk.is_ad_or_promo && (
                        <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-800">
                          Promo / Meta
                        </span>
                      )}
                      <span className="text-[11px] text-slate-500">
                        {formatDuration(stSec)} – {formatDuration(enSec)}
                      </span>
                    </div>
                    {blk.resumen_breve && <p className="text-slate-600">{blk.resumen_breve}</p>}
                  </div>

                  <div className="flex items-center gap-2">
                    <select
                      aria-label={`Formato a proponer para B${bi}`}
                      value={cType}
                      onChange={(e) =>
                        setProposeTypeByBlock((prev) => ({ ...prev, [bi]: e.target.value as ClipType }))
                      }
                      className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs"
                    >
                      <option value="short_conclusive">Short Conclusivo</option>
                      <option value="short_cliffhanger">Short Cliffhanger</option>
                      <option value="medium">Vídeo Medio</option>
                    </select>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleProposeFromBlock(bi)}
                      className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      {busy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                      ) : (
                        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                      Proponer y añadir
                    </button>
                  </div>
                </div>
                <div className="mt-2">
                  <audio
                    controls
                    preload="none"
                    src={`${srcAudioUrl}#t=${stSec},${enSec}`}
                    className="h-7 w-full max-w-md"
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-indigo-200 pt-3">
        <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-700">
          <input
            type="checkbox"
            checked={pauseBeforeRender}
            onChange={(e) => setPauseBeforeRender(e.target.checked)}
            className="h-4 w-4 accent-indigo-600"
          />
          <span>Pausar también antes de renderizar (para editar imágenes/subtítulos en Fase 4)</span>
        </label>

        <button
          type="button"
          disabled={submitting || selectedList.length === 0}
          onClick={handleConfirm}
          className="inline-flex items-center gap-1.5 rounded-xl bg-indigo-600 px-5 py-2.5 text-xs font-bold text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
        >
          {submitting ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
          )}
          Confirmar selección y continuar ({selectedList.length})
        </button>
      </div>
    </section>
  );
}
