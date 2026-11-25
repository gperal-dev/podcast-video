import { useState, useRef, type DragEvent, type FormEvent } from "react";
import {
  UploadCloud,
  Sparkles,
  ImagePlus,
  Trash2,
  FolderSearch,
  Play,
  Square,
  CheckCircle2,
  AlertCircle,
  FileAudio,
} from "lucide-react";
import { api } from "../api/client";
import {
  ALL_MEDIA_PROVIDERS,
  type AIImagePreviewResponse,
  type AspectRatio,
  type ExecutionMode,
  type ImageDisplayMode,
  type ImagePriority,
  type JobState,
  type MediaProviderId,
  type PodcasterProfile,
  type VisualHookStyle,
  type WatcherStatus,
} from "../types";

interface EpisodeImageDraft {
  id: string;
  file: File;
  previewUrl: string;
  title: string;
  usage_instructions: string;
  priority: ImagePriority;
  display_mode: ImageDisplayMode;
}

interface EpisodeAIImageDraft {
  id: string;
  preview_filename: string;
  previewUrl: string;
  title: string;
  usage_instructions: string;
  priority: ImagePriority;
  display_mode: ImageDisplayMode;
}

interface NewProductionPageProps {
  profiles: PodcasterProfile[];
  selectedProfileId: string;
  onSelectProfile: (id: string) => void;
  watcher: WatcherStatus;
  onWatcherChange: (w: WatcherStatus) => void;
  onJobCreated: (job: JobState) => void;
  onAnnounce: (msg: string) => void;
}

export function NewProductionPage({
  profiles,
  selectedProfileId,
  onSelectProfile,
  watcher,
  onWatcherChange,
  onJobCreated,
  onAnnounce,
}: NewProductionPageProps) {
  const currentProfile = profiles.find((p) => p.id === selectedProfileId) || profiles[0];

  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [episodeName, setEpisodeName] = useState("");
  const [dragActive, setDragActive] = useState(false);

  const [shortsCliff, setShortsCliff] = useState(currentProfile?.quotas.shorts_cliffhanger_count ?? 3);
  const [shortsConcl, setShortsConcl] = useState(currentProfile?.quotas.shorts_conclusive_count ?? 3);
  const [mediumCount, setMediumCount] = useState(currentProfile?.quotas.medium_videos_count ?? 1);
  const [longCount, setLongCount] = useState(currentProfile?.quotas.long_videos_count ?? 1);
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("auto");
  const [pauseBeforeRender, setPauseBeforeRender] = useState(false);
  const [saveAnalyticsReport, setSaveAnalyticsReport] = useState(false);

  const [epImages, setEpImages] = useState<EpisodeImageDraft[]>([]);
  const [epAiImages, setEpAiImages] = useState<EpisodeAIImageDraft[]>([]);
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiAspect, setAiAspect] = useState<AspectRatio>("9:16");
  const [aiGenerating, setAiGenerating] = useState(false);
  const [aiPreview, setAiPreview] = useState<AIImagePreviewResponse | null>(null);
  const [aiTitle, setAiTitle] = useState("");
  const [aiUsage, setAiUsage] = useState("");
  const [aiMode, setAiMode] = useState<ImageDisplayMode>("fullscreen_zoom");
  const [aiError, setAiError] = useState<string | null>(null);

  const [hookOverride, setHookOverride] = useState<"inherit" | "on" | "off">("inherit");
  const [introCurtainOverride, setIntroCurtainOverride] = useState<"inherit" | "on" | "off">("inherit");
  const [outroCurtainOverride, setOutroCurtainOverride] = useState<"inherit" | "on" | "off">("inherit");
  const [hookStyleOverride, setHookStyleOverride] = useState<VisualHookStyle>(
    currentProfile?.subtitles.visual_hook_style ?? "kinetic_fullscreen",
  );
  const [selectedProviders, setSelectedProviders] = useState<MediaProviderId[]>(
    currentProfile?.visual.allowed_media_providers && currentProfile.visual.allowed_media_providers.length > 0
      ? currentProfile.visual.allowed_media_providers
      : ALL_MEDIA_PROVIDERS.map((p) => p.id),
  );

  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const audioInputRef = useRef<HTMLInputElement>(null);
  const epImgInputRef = useRef<HTMLInputElement>(null);

  const handleProfileChange = (id: string) => {
    onSelectProfile(id);
    const p = profiles.find((x) => x.id === id);
    if (p) {
      setShortsCliff(p.quotas.shorts_cliffhanger_count);
      setShortsConcl(p.quotas.shorts_conclusive_count);
      setMediumCount(p.quotas.medium_videos_count);
      setLongCount(p.quotas.long_videos_count);
      setHookStyleOverride(p.subtitles.visual_hook_style ?? "kinetic_fullscreen");
      setSelectedProviders(
        p.visual.allowed_media_providers && p.visual.allowed_media_providers.length > 0
          ? p.visual.allowed_media_providers
          : ALL_MEDIA_PROVIDERS.map((m) => m.id),
      );
    }
  };

  const acceptAudio = (file: File | undefined) => {
    if (!file) return;
    setErrorMsg(null);
    setAudioFile(file);
    if (!episodeName.trim()) {
      setEpisodeName(file.name.replace(/\.[^/.]+$/, ""));
    }
  };

  const onDropAudio = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragActive(false);
    acceptAudio(e.dataTransfer.files?.[0]);
  };

  const addEpisodeImages = (files: FileList | null) => {
    if (!files) return;
    const next: EpisodeImageDraft[] = [];
    for (const f of Array.from(files)) {
      next.push({
        id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        file: f,
        previewUrl: URL.createObjectURL(f),
        title: f.name.replace(/\.[^/.]+$/, ""),
        usage_instructions: "",
        priority: "alta",
        display_mode: "fullscreen_zoom",
      });
    }
    setEpImages((prev) => [...prev, ...next]);
  };

  const toggleWatcher = async () => {
    try {
      const updated = watcher.running
        ? await api.stopWatcher()
        : await api.startWatcher(selectedProfileId);
      onWatcherChange(updated);
      onAnnounce(updated.running ? "Vigilancia automática activada en carpeta input/." : "Vigilancia automática pausada.");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Error cambiando vigilancia");
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!audioFile) {
      setErrorMsg("Selecciona o arrastra primero el archivo de audio del podcast.");
      return;
    }
    if (!selectedProfileId) {
      setErrorMsg("Elige un perfil de podcaster.");
      return;
    }

    setErrorMsg(null);
    setUploading(true);
    setUploadPct(0);

    try {
      const fd = new FormData();
      fd.append("profile_id", selectedProfileId);
      fd.append("episode_name", episodeName.trim() || audioFile.name.replace(/\.[^/.]+$/, ""));
      fd.append("shorts_cliffhanger", String(shortsCliff));
      fd.append("shorts_conclusive", String(shortsConcl));
      fd.append("medium_count", String(mediumCount));
      fd.append("long_count", String(longCount));
      fd.append("execution_mode", executionMode);
      fd.append("pause_before_render", String(executionMode === "review_before_render" || pauseBeforeRender));
      fd.append(
        "auto_mode",
        String(executionMode === "auto" || (executionMode === "semi_auto" && !pauseBeforeRender)),
      );
      fd.append("save_analytics_report", String(saveAnalyticsReport));
      if (hookOverride !== "inherit") {
        fd.append("visual_hook_enabled", String(hookOverride === "on"));
      }
      if (introCurtainOverride !== "inherit") {
        fd.append("intro_curtain_enabled", String(introCurtainOverride === "on"));
      }
      if (outroCurtainOverride !== "inherit") {
        fd.append("outro_curtain_enabled", String(outroCurtainOverride === "on"));
      }
      if (hookOverride === "on" || (hookOverride === "inherit" && currentProfile?.subtitles.visual_hook_enabled)) {
        fd.append("visual_hook_style", hookStyleOverride);
      }
      fd.append("allowed_media_providers", JSON.stringify(selectedProviders));
      fd.append("audio_file", audioFile);
      fd.append(
        "episode_images_meta",
        JSON.stringify(
          epImages.map((img) => ({
            title: img.title,
            usage_instructions: img.usage_instructions,
            priority: img.priority,
            display_mode: img.display_mode,
          })),
        ),
      );
      for (const img of epImages) {
        fd.append("episode_image_files", img.file);
      }
      if (epAiImages.length > 0) {
        fd.append(
          "episode_ai_images",
          JSON.stringify(
            epAiImages.map((img) => ({
              preview_filename: img.preview_filename,
              title: img.title,
              usage_instructions: img.usage_instructions,
              priority: img.priority,
              display_mode: img.display_mode,
            })),
          ),
        );
      }

      const job = await api.uploadAndStartJob(fd, setUploadPct);
      onAnnounce(`Producción iniciada: ${job.episode_name}`);
      onJobCreated(job);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "No se pudo subir el audio.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Nueva Producción</h1>
          <p className="mt-1 text-sm text-slate-600">
            Sube cualquier episodio de podcast (de 10 minutos a más de 8 horas) y genera tus clips listos para publicar.
          </p>
        </div>

        {/* Folder Watcher control */}
        <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-2.5 shadow-sm">
          <FolderSearch className="h-5 w-5 text-indigo-600" aria-hidden="true" />
          <div className="text-xs">
            <div className="font-semibold text-slate-800">
              Carpeta automática <code className="rounded bg-slate-100 px-1 py-0.5">input/</code>
            </div>
            <div className="text-slate-500">
              {watcher.running ? `Activa (${watcher.profile_id})` : "En pausa"}
            </div>
          </div>
          <button
            type="button"
            onClick={toggleWatcher}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
              watcher.running
                ? "bg-amber-100 text-amber-900 hover:bg-amber-200"
                : "bg-slate-900 text-white hover:bg-slate-800"
            }`}
          >
            {watcher.running ? (
              <>
                <Square className="h-3.5 w-3.5" aria-hidden="true" /> Pausar
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" aria-hidden="true" /> Activar
              </>
            )}
          </button>
        </div>
      </div>

      {errorMsg && (
        <div role="alert" className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{errorMsg}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Left column: Audio Dropzone & Episode Images */}
        <div className="space-y-6 lg:col-span-7">
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <label htmlFor="profile-select" className="block text-sm font-semibold text-slate-800">
              1. Elige tu perfil de podcaster
            </label>
            <select
              id="profile-select"
              name="profile_id"
              value={selectedProfileId}
              onChange={(e) => handleProfileChange(e.target.value)}
              className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-sm font-medium text-slate-900"
            >
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.podcast_name}
                </option>
              ))}
            </select>

            <div className="mt-4">
              <label htmlFor="episode-name" className="block text-sm font-semibold text-slate-800">
                Nombre del episodio (opcional)
              </label>
              <input
                id="episode-name"
                name="episode_name"
                type="text"
                value={episodeName}
                onChange={(e) => setEpisodeName(e.target.value)}
                placeholder="Ej. Episodio 42 — Cómo escalar sin inversores"
                className="mt-1.5 w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm"
              />
            </div>
          </div>

          {/* Drag & Drop Audio */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <span className="block text-sm font-semibold text-slate-800">
              2. Sube el archivo de audio del podcast
            </span>
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragActive(true);
              }}
              onDragLeave={() => setDragActive(false)}
              onDrop={onDropAudio}
              onClick={() => audioInputRef.current?.click()}
              className={`mt-3 flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed px-6 py-10 text-center transition ${
                dragActive
                  ? "border-indigo-500 bg-indigo-50/70"
                  : audioFile
                    ? "border-emerald-400 bg-emerald-50/40"
                    : "border-slate-300 bg-slate-50/60 hover:border-indigo-400 hover:bg-indigo-50/30"
              }`}
            >
              <input
                ref={audioInputRef}
                id="audio-upload"
                type="file"
                accept=".mp3,.wav,.m4a,.flac,.ogg,.aac,.opus"
                className="sr-only"
                onChange={(e) => acceptAudio(e.target.files?.[0])}
              />

              {audioFile ? (
                <>
                  <CheckCircle2 className="h-10 w-10 text-emerald-600" aria-hidden="true" />
                  <p className="mt-2 font-semibold text-slate-900">{audioFile.name}</p>
                  <p className="mt-0.5 text-xs text-slate-600">
                    {(audioFile.size / (1024 * 1024)).toFixed(1)} MB — Haz clic para cambiar de archivo
                  </p>
                </>
              ) : (
                <>
                  <UploadCloud className="h-10 w-10 text-indigo-600" aria-hidden="true" />
                  <p className="mt-2 font-semibold text-slate-900">
                    Arrastra tu podcast aquí o haz clic para seleccionarlo
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    MP3, WAV, M4A, FLAC, OGG o AAC — Transcripción 100% local sin coste
                  </p>
                </>
              )}
            </div>

            {uploading && (
              <div className="mt-4">
                <div className="flex justify-between text-xs font-medium text-slate-700">
                  <span>Subiendo audio al estudio local...</span>
                  <span>{uploadPct}%</span>
                </div>
                <progress value={uploadPct} max={100} className="mt-1 h-2.5 w-full overflow-hidden rounded-full" />
              </div>
            )}
          </div>

          {/* Episode-specific Images */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">
                  Imágenes puntuales de este episodio (opcional)
                </h2>
                <p className="text-xs text-slate-500">
                  Además de las fotos de tu perfil, añade aquí fotos del invitado o gráficos de este capítulo.
                </p>
              </div>
              <button
                type="button"
                onClick={() => epImgInputRef.current?.click()}
                className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
              >
                <ImagePlus className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                Añadir imágenes
              </button>
              <input
                ref={epImgInputRef}
                type="file"
                multiple
                accept=".png,.jpg,.jpeg,.webp"
                className="sr-only"
                onChange={(e) => addEpisodeImages(e.target.files)}
              />
            </div>

            {epImages.length > 0 && (
              <div className="mt-4 space-y-3">
                {epImages.map((img, idx) => (
                  <div key={img.id} className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3 sm:flex-row">
                    <img
                      src={img.previewUrl}
                      alt={img.title}
                      className="h-20 w-20 shrink-0 rounded-lg object-cover"
                    />
                    <div className="grid flex-1 grid-cols-1 gap-2 sm:grid-cols-2">
                      <div>
                        <label htmlFor={`ep-title-${img.id}`} className="text-xs font-medium text-slate-700">
                          Título
                        </label>
                        <input
                          id={`ep-title-${img.id}`}
                          type="text"
                          value={img.title}
                          onChange={(e) =>
                            setEpImages((prev) =>
                              prev.map((x, i) => (i === idx ? { ...x, title: e.target.value } : x)),
                            )
                          }
                          className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
                        />
                      </div>
                      <div>
                        <label htmlFor={`ep-mode-${img.id}`} className="text-xs font-medium text-slate-700">
                          Modo de aparición
                        </label>
                        <select
                          id={`ep-mode-${img.id}`}
                          value={img.display_mode}
                          onChange={(e) =>
                            setEpImages((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, display_mode: e.target.value as ImageDisplayMode } : x,
                              ),
                            )
                          }
                          className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
                        >
                          <option value="fullscreen_zoom">Pantalla completa con zoom</option>
                          <option value="pip_overlay">Recuadro flotante (PiP)</option>
                          <option value="outro_cta">Cierre final (CTA)</option>
                        </select>
                      </div>
                      <div className="sm:col-span-2">
                        <label htmlFor={`ep-use-${img.id}`} className="text-xs font-medium text-slate-700">
                          ¿Cuándo debe usarla la IA?
                        </label>
                        <input
                          id={`ep-use-${img.id}`}
                          type="text"
                          value={img.usage_instructions}
                          onChange={(e) =>
                            setEpImages((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, usage_instructions: e.target.value } : x,
                              ),
                            )
                          }
                          placeholder="Ej. Foto de Laura, usar cuando explique el caso del lanzamiento"
                          className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs"
                        />
                      </div>
                    </div>
                    <button
                      type="button"
                      aria-label={`Eliminar imagen ${img.title}`}
                      onClick={() => setEpImages((prev) => prev.filter((_, i) => i !== idx))}
                      className="self-start rounded-lg p-2 text-slate-500 hover:bg-rose-100 hover:text-rose-700"
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Pre-production AI Image Studio for this episode (generated BEFORE clicking Generate Videos) */}
            <div className="mt-4 rounded-xl border border-indigo-200 bg-indigo-50/40 p-3.5 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 font-bold text-indigo-950">
                  <Sparkles className="h-3.5 w-3.5 text-indigo-600" aria-hidden="true" />
                  Generar imagen con IA para este episodio (pre-producción)
                </span>
                <span className="text-[11px] text-indigo-700">
                  Revísala e indica cuándo usarla antes de iniciar
                </span>
              </div>

              <div className="mt-2.5 grid grid-cols-1 gap-2 sm:grid-cols-12">
                <div className="sm:col-span-6">
                  <label htmlFor="ep-ai-prompt" className="block font-medium text-slate-700">
                    Prompt de imagen
                  </label>
                  <input
                    id="ep-ai-prompt"
                    type="text"
                    value={aiPrompt}
                    onChange={(e) => setAiPrompt(e.target.value)}
                    placeholder="Ej. Caza Spitfire volando entre nubes dramáticas"
                    className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5"
                  />
                </div>
                <div className="sm:col-span-3">
                  <label htmlFor="ep-ai-aspect" className="block font-medium text-slate-700">
                    Orientación
                  </label>
                  <select
                    id="ep-ai-aspect"
                    value={aiAspect}
                    onChange={(e) => setAiAspect(e.target.value as AspectRatio)}
                    className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5"
                  >
                    <option value="9:16">9:16 Short</option>
                    <option value="16:9">16:9 YouTube</option>
                    <option value="1:1">1:1 Cuadrado</option>
                  </select>
                </div>
                <div className="flex items-end sm:col-span-3">
                  <button
                    type="button"
                    disabled={aiGenerating || !aiPrompt.trim()}
                    onClick={async () => {
                      setAiGenerating(true);
                      setAiError(null);
                      try {
                        const res = await api.generateAIImage(aiPrompt.trim(), aiAspect);
                        setAiPreview(res);
                        setAiTitle(aiPrompt.trim().slice(0, 44));
                      } catch (err) {
                        setAiError(err instanceof Error ? err.message : "Error generando imagen IA");
                      } finally {
                        setAiGenerating(false);
                      }
                    }}
                    className="w-full rounded-lg bg-indigo-600 px-3 py-1.5 font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                  >
                    {aiGenerating ? "Generando..." : "Generar preview"}
                  </button>
                </div>
              </div>

              {aiError && (
                <p role="alert" className="mt-2 font-medium text-rose-700">
                  {aiError}
                </p>
              )}

              {aiPreview && (
                <div className="mt-3 flex flex-col gap-3 rounded-lg border border-indigo-200 bg-white p-2.5 sm:flex-row">
                  <img
                    src={aiPreview.preview_url}
                    alt={aiTitle || "Preview IA"}
                    className="h-24 w-24 shrink-0 rounded-lg object-cover bg-slate-100"
                  />
                  <div className="grid flex-1 grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <label htmlFor="ep-ai-title" className="font-medium text-slate-700">
                        Título
                      </label>
                      <input
                        id="ep-ai-title"
                        type="text"
                        value={aiTitle}
                        onChange={(e) => setAiTitle(e.target.value)}
                        className="mt-0.5 w-full rounded-lg border border-slate-300 px-2 py-1"
                      />
                    </div>
                    <div>
                      <label htmlFor="ep-ai-mode" className="font-medium text-slate-700">
                        Aparición
                      </label>
                      <select
                        id="ep-ai-mode"
                        value={aiMode}
                        onChange={(e) => setAiMode(e.target.value as ImageDisplayMode)}
                        className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2 py-1"
                      >
                        <option value="fullscreen_zoom">Pantalla completa</option>
                        <option value="pip_overlay">Recuadro PiP</option>
                        <option value="outro_cta">Cierre CTA</option>
                      </select>
                    </div>
                    <div className="sm:col-span-2">
                      <label htmlFor="ep-ai-use" className="font-medium text-slate-700">
                        ¿Cuándo debe usarla la IA?
                      </label>
                      <div className="mt-0.5 flex gap-2">
                        <input
                          id="ep-ai-use"
                          type="text"
                          value={aiUsage}
                          onChange={(e) => setAiUsage(e.target.value)}
                          placeholder="Ej. Usar al explicar la batalla aérea o como apoyo clave"
                          className="flex-1 rounded-lg border border-slate-300 px-2 py-1"
                        />
                        <button
                          type="button"
                          onClick={() => {
                            setEpAiImages((prev) => [
                              ...prev,
                              {
                                id: `${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
                                preview_filename: aiPreview.preview_filename,
                                previewUrl: aiPreview.preview_url,
                                title: aiTitle.trim() || "Imagen IA",
                                usage_instructions: aiUsage.trim() || "Usar en momentos clave del episodio",
                                priority: "alta",
                                display_mode: aiMode,
                              },
                            ]);
                            setAiPreview(null);
                            setAiPrompt("");
                            setAiTitle("");
                            setAiUsage("");
                          }}
                          className="rounded-lg bg-emerald-600 px-3 py-1 font-semibold text-white hover:bg-emerald-700"
                        >
                          Añadir al episodio
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {epAiImages.length > 0 && (
                <div className="mt-3 space-y-2">
                  {epAiImages.map((img, idx) => (
                    <div key={img.id} className="flex items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-white p-2">
                      <div className="flex items-center gap-2.5 min-w-0">
                        <img src={img.previewUrl} alt={img.title} className="h-10 w-10 rounded object-cover" />
                        <div className="min-w-0">
                          <div className="truncate font-semibold text-slate-900">{img.title} (IA)</div>
                          <div className="truncate text-[11px] text-slate-500">{img.usage_instructions}</div>
                        </div>
                      </div>
                      <button
                        type="button"
                        aria-label={`Quitar imagen IA ${img.title}`}
                        onClick={() => setEpAiImages((prev) => prev.filter((_, i) => i !== idx))}
                        className="rounded-lg p-1.5 text-slate-500 hover:bg-rose-100 hover:text-rose-700"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right column: Quotas & Mode */}
        <div className="space-y-6 lg:col-span-5">
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-800">3. ¿Cuántos vídeos quieres generar?</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Cantidades heredadas del perfil seleccionado. Ajusta libremente para este episodio.
            </p>

            <div className="mt-4 space-y-3">
              <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50/60 px-4 py-3">
                <div>
                  <label htmlFor="qty-cliff" className="block text-sm font-semibold text-slate-900">
                    Shorts con Cliffhanger (intriga)
                  </label>
                  <span className="text-xs text-slate-600">Cortan en el punto álgido dejando pregunta abierta</span>
                </div>
                <input
                  id="qty-cliff"
                  type="number"
                  min={0}
                  max={20}
                  value={shortsCliff}
                  onChange={(e) => setShortsCliff(Math.max(0, Number(e.target.value)))}
                  className="w-20 rounded-xl border border-slate-300 bg-white px-3 py-2 text-center text-sm font-bold"
                />
              </div>

              <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50/60 px-4 py-3">
                <div>
                  <label htmlFor="qty-concl" className="block text-sm font-semibold text-slate-900">
                    Shorts Conclusivos (cerrados)
                  </label>
                  <span className="text-xs text-slate-600">Historia o consejo con remate final satisfactorio</span>
                </div>
                <input
                  id="qty-concl"
                  type="number"
                  min={0}
                  max={20}
                  value={shortsConcl}
                  onChange={(e) => setShortsConcl(Math.max(0, Number(e.target.value)))}
                  className="w-20 rounded-xl border border-slate-300 bg-white px-3 py-2 text-center text-sm font-bold"
                />
              </div>

              <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
                <div>
                  <label htmlFor="qty-med" className="block text-sm font-semibold text-slate-900">
                    Vídeos Medios (5–6 min)
                  </label>
                  <span className="text-xs text-slate-600">Arco temático completo (16:9 o 1:1)</span>
                </div>
                <input
                  id="qty-med"
                  type="number"
                  min={0}
                  max={10}
                  value={mediumCount}
                  onChange={(e) => setMediumCount(Math.max(0, Number(e.target.value)))}
                  className="w-20 rounded-xl border border-slate-300 bg-white px-3 py-2 text-center text-sm font-bold"
                />
              </div>

              <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
                <div>
                  <label htmlFor="qty-long" className="block text-sm font-semibold text-slate-900">
                    Vídeos Largos YouTube (12 min)
                  </label>
                  <span className="text-xs text-slate-600">Introducción, desarrollo completo y cierre con capítulos</span>
                </div>
                <input
                  id="qty-long"
                  type="number"
                  min={0}
                  max={10}
                  value={longCount}
                  onChange={(e) => setLongCount(Math.max(0, Number(e.target.value)))}
                  className="w-20 rounded-xl border border-slate-300 bg-white px-3 py-2 text-center text-sm font-bold"
                />
              </div>
            </div>
          </div>

          <fieldset className="rounded-2xl border border-indigo-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-sm font-semibold text-slate-800">
              Cortinilla Inicial y Cierre IA (Contexto sin fisuras)
            </legend>
            <p className="mt-0.5 text-xs text-slate-500">
              Si activas la cortinilla inicial, se reproduce antes del podcast con su voz IA de contexto y sustituye al gancho tradicional sobre el podcast.
            </p>
            <div className="mt-3 space-y-3 text-xs">
              <div>
                <div className="mb-1.5 font-semibold text-slate-700">Cortinilla inicial con voz IA</div>
                <div className="grid grid-cols-3 gap-2">
                  {(
                    [
                      ["inherit", `Del perfil (${currentProfile?.intro_curtain?.enabled ?? true ? "Sí" : "No"})`],
                      ["on", "Activar en todos"],
                      ["off", "Desactivar"],
                    ] as ["inherit" | "on" | "off", string][]
                  ).map(([val, lbl]) => (
                    <label
                      key={val}
                      className={`flex cursor-pointer items-center justify-center gap-1.5 rounded-xl border px-2 py-2 font-semibold ${
                        introCurtainOverride === val
                          ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                          : "border-slate-200 bg-white text-slate-700"
                      }`}
                    >
                      <input
                        type="radio"
                        name="intro_curtain_override"
                        checked={introCurtainOverride === val}
                        onChange={() => setIntroCurtainOverride(val)}
                        className="h-3.5 w-3.5"
                      />
                      <span>{lbl}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div>
                <div className="mb-1.5 font-semibold text-slate-700">Cortinilla final / Cierre del canal</div>
                <div className="grid grid-cols-3 gap-2">
                  {(
                    [
                      ["inherit", `Del perfil (${currentProfile?.outro_curtain?.enabled ?? true ? "Sí" : "No"})`],
                      ["on", "Activar en todos"],
                      ["off", "Desactivar"],
                    ] as ["inherit" | "on" | "off", string][]
                  ).map(([val, lbl]) => (
                    <label
                      key={val}
                      className={`flex cursor-pointer items-center justify-center gap-1.5 rounded-xl border px-2 py-2 font-semibold ${
                        outroCurtainOverride === val
                          ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                          : "border-slate-200 bg-white text-slate-700"
                      }`}
                    >
                      <input
                        type="radio"
                        name="outro_curtain_override"
                        checked={outroCurtainOverride === val}
                        onChange={() => setOutroCurtainOverride(val)}
                        className="h-3.5 w-3.5"
                      />
                      <span>{lbl}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          </fieldset>

          <fieldset className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-sm font-semibold text-slate-800">
              Gancho Visual Inicial (Pantalla Completa)
            </legend>
            <p className="mt-0.5 text-xs text-slate-500">
              Muestra la frase de apertura exacta de la voz ocupando el 100% de pantalla, sin subtítulos debajo en ese tramo.
            </p>
            <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
              {(
                [
                  ["inherit", `Del perfil (${currentProfile?.subtitles.visual_hook_enabled ? "Sí" : "No"})`],
                  ["on", "Activado"],
                  ["off", "Desactivado"],
                ] as ["inherit" | "on" | "off", string][]
              ).map(([val, lbl]) => (
                <label
                  key={val}
                  className={`flex cursor-pointer items-center justify-center gap-1.5 rounded-xl border px-2.5 py-2 font-semibold ${
                    hookOverride === val
                      ? "border-indigo-500 bg-indigo-50 text-indigo-900"
                      : "border-slate-200 bg-white text-slate-700"
                  }`}
                >
                  <input
                    type="radio"
                    name="hook_override"
                    checked={hookOverride === val}
                    onChange={() => setHookOverride(val)}
                    className="h-3.5 w-3.5"
                  />
                  <span>{lbl}</span>
                </label>
              ))}
            </div>

            {(hookOverride === "on" || (hookOverride === "inherit" && currentProfile?.subtitles.visual_hook_enabled)) && (
              <div className="mt-3">
                <label htmlFor="prod-hook-style" className="block text-xs font-semibold text-slate-700">
                  Aspecto del gancho visual
                </label>
                <select
                  id="prod-hook-style"
                  value={hookStyleOverride}
                  onChange={(e) => setHookStyleOverride(e.target.value as VisualHookStyle)}
                  className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                >
                  <option value="kinetic_fullscreen">Cinético a pantalla completa</option>
                  <option value="editorial_card">Tarjeta editorial enmarcada</option>
                  <option value="bold_minimal">Minimal con barra de progreso</option>
                </select>
              </div>
            )}
          </fieldset>

          <fieldset className="rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-sm font-semibold text-slate-800">
              Fuentes de Imágenes y Vídeos (Licencias 100% Libres y Gratis)
            </legend>
            <p className="mt-0.5 text-xs text-slate-500">
              Selecciona de dónde puede obtener recursos visuales este episodio. Se verifica la licencia de cada archivo y se descarga su certificado <code className="rounded bg-slate-100 px-1">.licencias.txt</code>.
            </p>
            <div className="mt-3 space-y-2 text-xs">
              {ALL_MEDIA_PROVIDERS.map((prov) => {
                const checked = selectedProviders.includes(prov.id);
                return (
                  <label
                    key={prov.id}
                    className={`flex cursor-pointer items-start justify-between gap-2 rounded-xl border px-3 py-2 transition ${
                      checked
                        ? "border-emerald-400 bg-emerald-50/40 text-slate-900"
                        : "border-slate-200 bg-white text-slate-500"
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          let next = e.target.checked
                            ? [...selectedProviders, prov.id]
                            : selectedProviders.filter((x) => x !== prov.id);
                          if (next.length === 0) {
                            next = ["custom_images"];
                          }
                          setSelectedProviders(next);
                        }}
                        className="mt-0.5 h-4 w-4 rounded"
                      />
                      <div>
                        <div className="font-semibold">{prov.label}</div>
                        <div className="text-[11px] text-slate-500">{prov.desc}</div>
                      </div>
                    </div>
                    <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800">
                      {prov.licenseBadge}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>

          <fieldset className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-sm font-semibold text-slate-800">4. Modo de ejecución</legend>
            <div className="mt-2 space-y-2.5">
              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-200 p-3 hover:bg-slate-50">
                <input
                  type="radio"
                  name="exec_mode"
                  checked={executionMode === "auto"}
                  onChange={() => {
                    setExecutionMode("auto");
                    setPauseBeforeRender(false);
                  }}
                  className="mt-1 h-4 w-4"
                />
                <div className="text-sm">
                  <div className="font-semibold text-slate-900">100% Automático (hasta vídeos finales)</div>
                  <div className="text-xs text-slate-500">
                    Transcribe, selecciona, limpia el audio y renderiza todos los MP4 sin pausas.
                  </div>
                </div>
              </label>

              <div
                className={`rounded-xl border p-3 transition ${
                  executionMode === "semi_auto"
                    ? "border-indigo-400 bg-indigo-50/40"
                    : "border-slate-200 hover:bg-slate-50"
                }`}
              >
                <label className="flex cursor-pointer items-start gap-3">
                  <input
                    type="radio"
                    name="exec_mode"
                    checked={executionMode === "semi_auto"}
                    onChange={() => setExecutionMode("semi_auto")}
                    className="mt-1 h-4 w-4 accent-indigo-600"
                  />
                  <div className="text-sm">
                    <div className="font-semibold text-slate-900">
                      Semiautomático — Elegir y rescatar temas tras Fase 2
                    </div>
                    <div className="text-xs text-slate-500">
                      Analiza todo el episodio, te muestra recomendados, descartes rescatables y el mapa completo para que tú elijas qué producir antes de gastar tiempo en Pasada 2, stock y render.
                    </div>
                  </div>
                </label>
                {executionMode === "semi_auto" && (
                  <label className="mt-2.5 ml-7 flex cursor-pointer items-center gap-2 rounded-lg border border-indigo-200 bg-white px-3 py-2 text-xs text-slate-700">
                    <input
                      type="checkbox"
                      checked={pauseBeforeRender}
                      onChange={(e) => setPauseBeforeRender(e.target.checked)}
                      className="h-3.5 w-3.5 accent-indigo-600"
                    />
                    <span>También pausar antes de renderizar (para editar imágenes/subtítulos en Fase 4)</span>
                  </label>
                )}
              </div>

              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-slate-200 p-3 hover:bg-slate-50">
                <input
                  type="radio"
                  name="exec_mode"
                  checked={executionMode === "review_before_render"}
                  onChange={() => {
                    setExecutionMode("review_before_render");
                    setPauseBeforeRender(true);
                  }}
                  className="mt-1 h-4 w-4"
                />
                <div className="text-sm">
                  <div className="font-semibold text-slate-900">Revisar antes de renderizar (Fase 4)</div>
                  <div className="text-xs text-slate-500">
                    Pausa tras elegir clips, audio limpio y guion visual para que puedas cambiar fotos o subtítulos.
                  </div>
                </div>
              </label>

              <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-indigo-200 bg-indigo-50/30 p-3 hover:bg-indigo-50/60">
                <input
                  type="checkbox"
                  checked={saveAnalyticsReport}
                  onChange={(e) => setSaveAnalyticsReport(e.target.checked)}
                  className="mt-1 h-4 w-4 accent-indigo-600"
                />
                <div className="text-sm">
                  <div className="font-semibold text-slate-900">
                    Guardar informe completo de análisis IA (Analytics Studio)
                  </div>
                  <div className="text-xs text-slate-500">
                    Registra tiempos por fase, decisiones del Juez Ciego, ediciones NLE y descartes en{" "}
                    <code className="rounded bg-slate-100 px-1">analytics_studio/</code>.
                  </div>
                </div>
              </label>
            </div>
          </fieldset>

          <button
            type="submit"
            disabled={uploading}
            className="flex w-full items-center justify-center gap-2 rounded-2xl bg-indigo-600 px-6 py-4 text-base font-bold text-white shadow-lg shadow-indigo-600/25 transition hover:bg-indigo-700 disabled:opacity-60"
          >
            {uploading ? (
              <>
                <FileAudio className="h-5 w-5 animate-pulse" aria-hidden="true" />
                Subiendo ({uploadPct}%)...
              </>
            ) : (
              <>
                <Sparkles className="h-5 w-5" aria-hidden="true" />
                Generar Vídeos
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
