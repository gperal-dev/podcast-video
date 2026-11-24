import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Copy, Check, Download, Film, FolderOpen, Play, ShieldCheck, X } from "lucide-react";
import { api, clipTypeLabel, formatDuration } from "../api/client";
import type { GalleryItem } from "../types";

interface GalleryPageProps {
  onAnnounce: (msg: string) => void;
}

export function GalleryPage({ onAnnounce }: GalleryPageProps) {
  const [items, setItems] = useState<GalleryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [episodeFilter, setEpisodeFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [selectedItem, setSelectedItem] = useState<GalleryItem | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDialogElement>(null);

  const loadGallery = async () => {
    setLoading(true);
    try {
      const data = await api.getGallery();
      setItems(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadGallery();
  }, []);

  // Polyfill fallback for closedby="any" light dismiss when browser lacks HTMLDialogElement.closedBy
  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;
    if ("closedBy" in HTMLDialogElement.prototype) return;

    let pressedOutside = false;
    const isOutside = (e: PointerEvent) => {
      const r = dlg.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    const onDown = (e: PointerEvent) => {
      pressedOutside = isOutside(e);
    };
    const onUp = (e: PointerEvent) => {
      if (pressedOutside && isOutside(e)) {
        dlg.close();
      }
    };
    dlg.addEventListener("pointerdown", onDown);
    dlg.addEventListener("pointerup", onUp);
    return () => {
      dlg.removeEventListener("pointerdown", onDown);
      dlg.removeEventListener("pointerup", onUp);
    };
  }, []);

  const episodes = useMemo(() => {
    return Array.from(new Set(items.map((x) => x.episode_name)));
  }, [items]);

  const filtered = useMemo(() => {
    return items.filter((item) => {
      if (episodeFilter && item.episode_name !== episodeFilter) return false;
      if (typeFilter !== "all" && item.clip_type !== typeFilter) return false;
      return true;
    });
  }, [items, episodeFilter, typeFilter]);

  const openPlayer = (item: GalleryItem) => {
    setSelectedItem(item);
    dialogRef.current?.showModal();
  };

  const closePlayer = () => {
    dialogRef.current?.close();
    setSelectedItem(null);
  };

  const copyText = async (key: string, value: string) => {
    await navigator.clipboard.writeText(value);
    setCopiedKey(key);
    onAnnounce("Texto copiado al portapapeles.");
    window.setTimeout(() => setCopiedKey(null), 1500);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Biblioteca de Resultados</h1>
          <p className="mt-0.5 text-sm text-slate-600">
            Reproduce los vídeos generados, copia sus títulos, descripciones o capítulos y abre la carpeta final.
          </p>
        </div>

        <button
          type="button"
          onClick={async () => {
            await api.openOutputFolder();
            onAnnounce("Carpeta output/ abierta.");
          }}
          className="inline-flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-800 shadow-sm hover:bg-slate-50"
        >
          <FolderOpen className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          Abrir carpeta output/
        </button>
      </div>

      {/* Filters */}
      <search>
        <div className="flex flex-wrap items-end gap-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <div>
            <label htmlFor="filter-ep" className="block text-xs font-semibold text-slate-700">
              Episodio
            </label>
            <select
              id="filter-ep"
              value={episodeFilter}
              onChange={(e) => setEpisodeFilter(e.target.value)}
              className="mt-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              <option value="">Todos los episodios</option>
              {episodes.map((ep) => (
                <option key={ep} value={ep}>
                  {ep}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="filter-type" className="block text-xs font-semibold text-slate-700">
              Formato
            </label>
            <select
              id="filter-type"
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="mt-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              <option value="all">Todos los formatos</option>
              <option value="short_cliffhanger">Shorts Cliffhanger (intriga)</option>
              <option value="short_conclusive">Shorts Conclusivos (cerrados)</option>
              <option value="medium">Vídeos Medios (5–6 min)</option>
              <option value="long">Vídeos Largos (12 min)</option>
            </select>
          </div>

          <button
            type="button"
            onClick={loadGallery}
            className="rounded-xl bg-slate-100 px-3.5 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-200"
          >
            Actualizar lista
          </button>
        </div>
      </search>

      {loading ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center text-sm text-slate-500">
          Cargando vídeos generados...
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center shadow-sm">
          <Film className="mx-auto h-10 w-10 text-slate-400" aria-hidden="true" />
          <p className="mt-2 text-sm font-medium text-slate-700">
            Aún no hay vídeos renderizados con los filtros actuales.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
          {filtered.map((item) => (
            <article
              key={item.video_url}
              className="flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition hover:shadow-md"
            >
              <button
                type="button"
                onClick={() => openPlayer(item)}
                className="group relative aspect-video w-full overflow-hidden bg-slate-900 text-left"
              >
                {item.thumbnail_url ? (
                  <img
                    src={item.thumbnail_url}
                    alt={item.metadata.titles?.[0] || item.video_filename}
                    loading="lazy"
                    className="h-full w-full object-cover opacity-90 transition group-hover:scale-105"
                  />
                ) : (
                  <div className="flex h-full items-center justify-center text-slate-500">
                    <Film className="h-10 w-10" aria-hidden="true" />
                  </div>
                )}
                <div className="absolute inset-0 flex items-center justify-center bg-black/25 transition group-hover:bg-black/40">
                  <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white/95 text-slate-900 shadow-lg">
                    <Play className="ml-0.5 h-5 w-5" aria-hidden="true" />
                  </span>
                </div>
                <span className="absolute top-2.5 left-2.5 rounded-full bg-indigo-600 px-2.5 py-0.5 text-xs font-bold text-white shadow">
                  {item.score}/100
                </span>
                <span className="absolute right-2.5 bottom-2.5 rounded-md bg-black/75 px-2 py-0.5 text-xs font-semibold text-white">
                  {formatDuration(item.duration_sec)} • {item.aspect_ratio}
                </span>
              </button>

              <div className="flex flex-1 flex-col justify-between p-4">
                <div>
                  <div className="flex flex-wrap items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-indigo-700">
                    <span>{clipTypeLabel(item.clip_type)}</span>
                    {item.narrative_archetype && (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium lowercase text-slate-600">
                        {item.narrative_archetype.replace(/_/g, " ")}
                      </span>
                    )}
                    {item.visual_hook_enabled && (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold normal-case text-amber-900">
                        Gancho 100%
                      </span>
                    )}
                  </div>
                  <h2 className="mt-1 line-clamp-2 text-base font-bold text-slate-900">
                    {item.metadata.titles?.[0] || item.video_filename}
                  </h2>
                  <p className="mt-1 line-clamp-2 text-xs text-slate-600">{item.summary}</p>
                  {item.speakers_detected && item.speakers_detected.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {item.speakers_detected.map((sp) => (
                        <span
                          key={sp}
                          className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-700"
                        >
                          <span
                            className="h-2 w-2 rounded-full"
                            style={{ backgroundColor: item.speaker_color_map?.[sp] || "#FFD400" }}
                          />
                          {sp}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3">
                  <span className="truncate text-[11px] text-slate-500">{item.episode_name}</span>
                  <div className="flex items-center gap-2">
                    <a
                      href={`${item.video_url}?download=1`}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden="true" /> Descargar
                    </a>
                    <button
                      type="button"
                      onClick={() => openPlayer(item)}
                      className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
                    >
                      Ver y Copiar
                    </button>
                  </div>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      {/* Native <dialog> video player + publish metadata panel */}
      <dialog
        ref={dialogRef}
        aria-labelledby="video-dialog-title"
        {...({ closedby: "any" } as Record<string, string>)}
        onClose={() => setSelectedItem(null)}
      >
        {selectedItem && (
          <div className="flex max-h-[90vh] w-[min(94vw,62rem)] flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5">
              <div>
                <h2 id="video-dialog-title" className="text-base font-bold text-slate-900">
                  {selectedItem.metadata.titles?.[0] || selectedItem.video_filename}
                </h2>
                <p className="text-xs text-slate-500">
                  {clipTypeLabel(selectedItem.clip_type)} • Puntuación {selectedItem.score}/100
                </p>
              </div>
              <button
                type="button"
                onClick={closePlayer}
                aria-label="Cerrar reproductor"
                className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-900"
              >
                <X className="h-5 w-5" aria-hidden="true" />
              </button>
            </div>

            <div className="grid flex-1 grid-cols-1 gap-5 overflow-y-auto p-5 lg:grid-cols-12">
              <div className="flex flex-col items-center justify-center rounded-xl bg-slate-950 p-3 lg:col-span-6">
                <video
                  key={selectedItem.video_url}
                  controls
                  autoPlay
                  width={selectedItem.aspect_ratio === "9:16" ? 360 : 640}
                  height={selectedItem.aspect_ratio === "9:16" ? 640 : 360}
                  poster={selectedItem.thumbnail_url || undefined}
                  preload="metadata"
                  src={selectedItem.video_url}
                  className="max-h-[65vh] w-auto rounded-lg"
                />
              </div>

              <div className="space-y-4 text-sm lg:col-span-6">
                <div>
                  <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">
                    3 Títulos sugeridos
                  </h3>
                  <div className="mt-1.5 space-y-1.5">
                    {(selectedItem.metadata.titles || []).map((t, idx) => (
                      <div key={idx} className="flex items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                        <span className="font-medium text-slate-800">{t}</span>
                        <button
                          type="button"
                          onClick={() => copyText(`t-${idx}`, t)}
                          className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-white px-2.5 py-1 text-xs font-semibold text-slate-700 ring-1 ring-slate-200 hover:bg-slate-100"
                        >
                          {copiedKey === `t-${idx}` ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : <Copy className="h-3.5 w-3.5" />}
                          Copiar
                        </button>
                      </div>
                    ))}
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">Descripción</h3>
                    <button
                      type="button"
                      onClick={() => copyText("desc", selectedItem.metadata.description || "")}
                      className="inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:underline"
                    >
                      <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar
                    </button>
                  </div>
                  <p className="mt-1 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700 whitespace-pre-wrap">
                    {selectedItem.metadata.description}
                  </p>
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">Hashtags</h3>
                    <button
                      type="button"
                      onClick={() => copyText("tags", (selectedItem.metadata.hashtags || []).join(" "))}
                      className="inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:underline"
                    >
                      <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar
                    </button>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {(selectedItem.metadata.hashtags || []).map((tag, idx) => (
                      <span key={idx} className="rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-800">
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>

                {selectedItem.metadata.chapters && selectedItem.metadata.chapters.length > 0 && (
                  <div>
                    <div className="flex items-center justify-between">
                      <h3 className="text-xs font-bold uppercase tracking-wide text-slate-500">
                        Capítulos (post-edición)
                      </h3>
                      <button
                        type="button"
                        onClick={() =>
                          copyText(
                            "chaps",
                            selectedItem.metadata.chapters
                              .map((c) => `${formatDuration(c.edited_time_sec)} ${c.title}`)
                              .join("\n"),
                          )
                        }
                        className="inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:underline"
                      >
                        <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copiar
                      </button>
                    </div>
                    <ul className="mt-1 space-y-1 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                      {selectedItem.metadata.chapters.map((ch, idx) => (
                        <li key={idx} className="flex gap-2">
                          <span className="font-mono font-bold text-indigo-700">
                            {formatDuration(ch.edited_time_sec)}
                          </span>
                          <span className="text-slate-700">{ch.title}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {selectedItem.standalone_packaging && selectedItem.standalone_packaging.standalone_summary && (
                  <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-3 text-xs text-indigo-950">
                    <div className="font-bold">Auditoría Narrativa (Comprensión en Frío fuera del Podcast)</div>
                    <p className="mt-1 text-slate-700">{selectedItem.standalone_packaging.standalone_summary}</p>
                    {selectedItem.standalone_packaging.hook_strength && (
                      <div className="mt-1.5 flex flex-wrap gap-2 text-[11px] font-semibold text-indigo-800">
                        <span className="rounded bg-white px-2 py-0.5">
                          Gancho: {selectedItem.standalone_packaging.hook_strength}/100
                        </span>
                        <span className="rounded bg-white px-2 py-0.5">
                          Contexto: {selectedItem.standalone_packaging.context_clarity}/100
                        </span>
                        <span className="rounded bg-white px-2 py-0.5">
                          Cierre: {selectedItem.standalone_packaging.resolution_score}/100
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {selectedItem.license_warnings && selectedItem.license_warnings.length > 0 && (
                  <div
                    role="alert"
                    className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950"
                  >
                    <div className="flex items-center gap-1.5 font-bold">
                      <AlertTriangle className="h-4 w-4 text-amber-700" aria-hidden="true" />
                      Aviso de licencia detectada
                    </div>
                    <ul className="mt-1 list-disc pl-5 text-[11px]">
                      {selectedItem.license_warnings.map((lw, i) => (
                        <li key={i}>{lw}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {selectedItem.visual_credits_audit && selectedItem.visual_credits_audit.length > 0 && (
                  <details className="rounded-xl border border-emerald-200 bg-emerald-50/50 p-3 text-xs">
                    <summary className="cursor-pointer font-bold text-emerald-900">
                      <span className="inline-flex items-center gap-1.5">
                        <ShieldCheck className="h-4 w-4 text-emerald-700" aria-hidden="true" />
                        Auditoría de Licencias Libres y Gratis ({selectedItem.visual_credits_audit.length} planos)
                      </span>
                    </summary>
                    {selectedItem.license_manifest_url && (
                      <div className="mt-2 flex justify-end">
                        <a
                          href={selectedItem.license_manifest_url}
                          className="inline-flex items-center gap-1 rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-emerald-900 ring-1 ring-emerald-300 hover:bg-emerald-50"
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden="true" /> Descargar certificado .licencias.txt
                        </a>
                      </div>
                    )}
                    <ul className="mt-2 space-y-2 border-t border-emerald-200 pt-2">
                      {selectedItem.visual_credits_audit.map((aud, idx) => (
                        <li key={idx} className="rounded-lg bg-white p-2 text-[11px] text-slate-700 ring-1 ring-emerald-100">
                          <div className="font-semibold text-slate-900">
                            {aud.shot_id ? `[${aud.shot_id}] ` : ""}
                            {aud.title || "Recurso visual"} ({aud.provider || aud.source || "Libre"})
                          </div>
                          <div className="mt-0.5 text-slate-600">
                            Licencia: <strong>{aud.license || aud.license_name || "Public Domain / CC0 / Free"}</strong>
                            {aud.author ? ` • Autor: ${aud.author}` : ""}
                            {aud.verified_public_domain ? " • Verificado 100% libre y gratis" : ""}
                          </div>
                          {aud.license_url && (
                            <div>
                              <a
                                href={aud.license_url}
                                target="_blank"
                                rel="noreferrer"
                                className="mt-0.5 inline-block truncate max-w-full text-emerald-700 hover:underline"
                              >
                                Texto oficial de licencia: {aud.license_url}
                              </a>
                            </div>
                          )}
                          {(aud.source_url || aud.source_page_url) && (
                            <a
                              href={aud.source_url || aud.source_page_url}
                              target="_blank"
                              rel="noreferrer"
                              className="mt-0.5 inline-block truncate max-w-full text-indigo-600 hover:underline"
                            >
                              {aud.source_url || aud.source_page_url}
                            </a>
                          )}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            </div>
          </div>
        )}
      </dialog>
    </div>
  );
}
