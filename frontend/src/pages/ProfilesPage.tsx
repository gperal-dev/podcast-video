import { useEffect, useState, type DragEvent } from "react";
import {
  CheckCircle2,
  CopyPlus,
  ImagePlus,
  Music,
  Plus,
  Save,
  ShieldAlert,
  Sparkles,
  Trash2,
  Type,
  Volume2,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { SubtitlePreview } from "../components/SubtitlePreview";
import {
  ALL_MEDIA_PROVIDERS,
  INTRO_HOOK_TEMPLATES,
  type AIImagePreviewResponse,
  type AspectRatio,
  type FontItem,
  type ImageDisplayMode,
  type ImagePriority,
  type IntroCurtainBgMode,
  type IntroCurtainConfig,
  type IntroCurtainDisplayMode,
  type IntroCurtainMode,
  type MediaProviderId,
  type OutroAudioMode,
  type OutroCurtainConfig,
  type PodcasterProfile,
  type SystemStatus,
  type TTSEngineId,
  type TTSVoiceCatalog,
  type VisualHookStyle,
} from "../types";

interface ProfilesPageProps {
  profiles: PodcasterProfile[];
  selectedProfileId: string;
  onSelectProfile: (id: string) => void;
  onProfilesChanged: (profiles: PodcasterProfile[]) => void;
  systemStatus: SystemStatus | null;
  onAnnounce: (msg: string) => void;
}

type ProfileTab = "editorial" | "audio" | "images" | "visual" | "curtains";

const DEFAULT_INTRO_CURTAIN: IntroCurtainConfig = {
  enabled: true,
  mode: "auto_if_needed",
  apply_to_shorts: true,
  apply_to_medium: true,
  apply_to_long: true,
  hook_template: "auto",
  custom_hook_prompt: "",
  display_mode: "multi_phrase",
  bg_mode: "blurred_clip_video",
  bg_primary_color: "#0F172A",
  bg_secondary_color: "#1E1B4B",
  bg_overlay_opacity: 0.68,
  accent_color: "#FFD400",
  text_color: "#FFFFFF",
  bg_image_id: null,
  badge_text: "",
  tts_enabled: true,
  tts_engine: "edge_tts",
  tts_voice: "es-ES-AlvaroNeural",
  tts_rate: "+6%",
  openrouter_audio_model: "openai/gpt-audio-mini",
  transition_sfx: true,
  min_sec: 2.2,
  max_sec: 6.2,
};

const DEFAULT_OUTRO_CURTAIN: OutroCurtainConfig = {
  enabled: true,
  apply_to_shorts: true,
  apply_to_medium: true,
  apply_to_long: true,
  duration_sec: 4.5,
  audio_mode: "no_overlap_lcut",
  bg_mode: "blurred_clip_video",
  bg_primary_color: "#090D16",
  bg_secondary_color: "#1E293B",
  bg_overlay_opacity: 0.78,
  accent_color: "#38BDF8",
  text_color: "#FFFFFF",
  bg_image_id: null,
  headline_text: "SIGUE EL PODCAST COMPLETO",
  platform_cta_text: "",
  channel_name_text: "",
  use_profile_outro_cta_logo: true,
  tts_enabled: true,
  tts_text: "",
  custom_audio_filename: null,
  custom_audio_volume_db: -3.0,
  podcast_duck_volume_db: -16.0,
  cached_tts_filename: null,
  cached_tts_hash: null,
};

export function ProfilesPage({
  profiles,
  selectedProfileId,
  onSelectProfile,
  onProfilesChanged,
  systemStatus,
  onAnnounce,
}: ProfilesPageProps) {
  const activeProfile = profiles.find((p) => p.id === selectedProfileId) || profiles[0];
  const [draft, setDraft] = useState<PodcasterProfile | null>(activeProfile || null);
  const [tab, setTab] = useState<ProfileTab>("editorial");
  const [fonts, setFonts] = useState<FontItem[]>([]);
  const [saving, setSaving] = useState(false);

  const [newWord, setNewWord] = useState("");
  const [newBadStock, setNewBadStock] = useState("");
  const [imgDrag, setImgDrag] = useState(false);

  // Pre-production AI Image Studio state
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiAspect, setAiAspect] = useState<AspectRatio>("9:16");
  const [aiGenerating, setAiGenerating] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiPreview, setAiPreview] = useState<AIImagePreviewResponse | null>(null);
  const [aiTitle, setAiTitle] = useState("");
  const [aiUsage, setAiUsage] = useState("");
  const [aiPriority, setAiPriority] = useState<ImagePriority>("alta");
  const [aiMode, setAiMode] = useState<ImageDisplayMode>("fullscreen_zoom");
  const [aiSaving, setAiSaving] = useState(false);

  // Intro/Outro Curtains & TTS preview state
  const [ttsCatalog, setTtsCatalog] = useState<TTSVoiceCatalog | null>(null);
  const [sampleText, setSampleText] = useState("¿Sabías este dato sorprendente? Quédate, porque lo que vas a escuchar cambia todo.");
  const [sampleLoading, setSampleLoading] = useState(false);
  const [sampleUrl, setSampleUrl] = useState<string | null>(null);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [outroTtsLoading, setOutroTtsLoading] = useState(false);
  const [outroAudioPreviewUrl, setOutroAudioPreviewUrl] = useState<string | null>(null);
  const [outroTtsMsg, setOutroTtsMsg] = useState<string | null>(null);
  const [curtainPreviewPane, setCurtainPreviewPane] = useState<"intro" | "outro">("intro");
  const [curtainPreviewAspect, setCurtainPreviewAspect] = useState<"9:16" | "16:9">("9:16");

  useEffect(() => {
    api.getTtsVoices().then((cat) => {
      setTtsCatalog(cat);
      if (cat.sample_text_default) setSampleText(cat.sample_text_default);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (activeProfile) {
      const cloned = structuredClone(activeProfile);
      cloned.intro_curtain = { ...DEFAULT_INTRO_CURTAIN, ...(cloned.intro_curtain || {}) };
      cloned.outro_curtain = { ...DEFAULT_OUTRO_CURTAIN, ...(cloned.outro_curtain || {}) };
      setDraft(cloned);
      setOutroAudioPreviewUrl(
        cloned.outro_curtain.cached_tts_filename
          ? api.profileAudioUrl(cloned.id, cloned.outro_curtain.cached_tts_filename)
          : null,
      );
      api.listFonts(activeProfile.id).then(setFonts).catch(() => {});
    }
  }, [activeProfile?.id, activeProfile?.updated_at]);

  if (!draft) return null;

  const ic = draft.intro_curtain || DEFAULT_INTRO_CURTAIN;
  const oc = draft.outro_curtain || DEFAULT_OUTRO_CURTAIN;
  const updateIntro = (patch: Partial<IntroCurtainConfig>) =>
    setDraft({ ...draft, intro_curtain: { ...ic, ...patch } });
  const updateOutro = (patch: Partial<OutroCurtainConfig>) =>
    setDraft({ ...draft, outro_curtain: { ...oc, ...patch } });

  const saveDraft = async () => {
    setSaving(true);
    try {
      const saved = await api.updateProfile(draft.id, draft);
      onProfilesChanged(profiles.map((p) => (p.id === saved.id ? saved : p)));
      onAnnounce(`Perfil '${saved.name}' guardado.`);
    } finally {
      setSaving(false);
    }
  };

  const handleCreateProfile = async () => {
    const name = window.prompt("Nombre del nuevo perfil de podcast:", "Nuevo Podcast");
    if (!name || !name.trim()) return;
    const base = structuredClone(draft);
    base.id = "";
    base.name = name.trim();
    base.podcast_name = name.trim();
    base.custom_images = [];
    const created = await api.createProfile(base);
    onProfilesChanged([...profiles, created]);
    onSelectProfile(created.id);
    onAnnounce(`Perfil '${created.name}' creado.`);
  };

  const handleDuplicate = async () => {
    const dup = await api.duplicateProfile(draft.id);
    onProfilesChanged([...profiles, dup]);
    onSelectProfile(dup.id);
    onAnnounce(`Perfil duplicado: ${dup.name}`);
  };

  const handleDelete = async () => {
    if (profiles.length <= 1) return;
    if (!window.confirm(`¿Eliminar el perfil '${draft.name}'?`)) return;
    await api.deleteProfile(draft.id);
    const remaining = profiles.filter((p) => p.id !== draft.id);
    onProfilesChanged(remaining);
    if (remaining[0]) onSelectProfile(remaining[0].id);
  };

  const addCensoredWord = () => {
    const clean = newWord.trim();
    if (!clean) return;
    if (!draft.audio.censored_words.includes(clean)) {
      setDraft({
        ...draft,
        audio: { ...draft.audio, censored_words: [...draft.audio.censored_words, clean] },
      });
    }
    setNewWord("");
  };

  const handleUploadImages = async (files: FileList | null) => {
    if (!files) return;
    let latest = draft;
    for (const file of Array.from(files)) {
      latest = await api.uploadProfileImage(draft.id, file, {
        title: file.name.replace(/\.[^/.]+$/, ""),
        usage_instructions: "",
        priority: "alta",
        display_mode: "fullscreen_zoom",
      });
    }
    onProfilesChanged(profiles.map((p) => (p.id === latest.id ? latest : p)));
    setDraft(structuredClone(latest));
    onAnnounce("Imágenes añadidas al perfil.");
  };

  const handleDropImages = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setImgDrag(false);
    handleUploadImages(e.dataTransfer.files);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Perfiles y Personalización</h1>
          <p className="mt-0.5 text-sm text-slate-600">
            Configura criterios editoriales, palabras censuradas, biblioteca de imágenes propias y subtítulos.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Seleccionar perfil para editar"
            value={draft.id}
            onChange={(e) => onSelectProfile(e.target.value)}
            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-semibold"
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={handleCreateProfile}
            className="inline-flex items-center gap-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-800 hover:bg-slate-50"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" /> Nuevo
          </button>
          <button
            type="button"
            onClick={handleDuplicate}
            className="inline-flex items-center gap-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-800 hover:bg-slate-50"
          >
            <CopyPlus className="h-3.5 w-3.5" aria-hidden="true" /> Duplicar
          </button>
          {profiles.length > 1 && (
            <button
              type="button"
              onClick={handleDelete}
              className="inline-flex items-center gap-1 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700 hover:bg-rose-100"
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" /> Eliminar
            </button>
          )}
          <button
            type="button"
            disabled={saving}
            onClick={saveDraft}
            className="inline-flex items-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2 text-sm font-bold text-white shadow hover:bg-indigo-700 disabled:opacity-60"
          >
            <Save className="h-4 w-4" aria-hidden="true" />
            {saving ? "Guardando..." : "Guardar perfil"}
          </button>
        </div>
      </div>

      {/* Single-row accessible tablist */}
      <div className="rounded-2xl border border-slate-200 bg-white shadow-sm">
        <div
          role="tablist"
          aria-label="Secciones de personalización del perfil"
          className="border-b border-slate-200 px-4"
        >
          {(
            [
              ["editorial", "Editorial y Cuotas"],
              ["audio", "Audio y Censura"],
              ["images", `Imágenes Propias (${draft.custom_images.length})`],
              ["visual", "Estilo Visual y Subtítulos"],
              ["curtains", "Cortinilla y Cierre IA"],
            ] as [ProfileTab, string][]
          ).map(([id, label], tabIdx, allTabs) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`tab-${id}`}
              tabIndex={tab === id ? 0 : -1}
              aria-selected={tab === id}
              onClick={() => setTab(id)}
              onKeyDown={(e) => {
                let nextIdx: number | null = null;
                if (e.key === "ArrowRight") nextIdx = (tabIdx + 1) % allTabs.length;
                else if (e.key === "ArrowLeft") nextIdx = (tabIdx - 1 + allTabs.length) % allTabs.length;
                else if (e.key === "Home") nextIdx = 0;
                else if (e.key === "End") nextIdx = allTabs.length - 1;
                if (nextIdx !== null) {
                  e.preventDefault();
                  const nextId = allTabs[nextIdx][0];
                  setTab(nextId);
                  document.getElementById(`tab-${nextId}`)?.focus();
                }
              }}
              className={`px-4 py-3.5 text-sm font-semibold whitespace-nowrap transition ${
                tab === id ? "text-indigo-700" : "text-slate-600 hover:text-slate-900"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Tab 1: Editorial & Quotas */}
        <div role="tabpanel" aria-labelledby="tab-editorial" hidden={tab !== "editorial"}>
          <div className="grid grid-cols-1 gap-6 p-6 lg:grid-cols-2">
            <div className="space-y-4">
              <div>
                <label htmlFor="p-name" className="block text-xs font-semibold text-slate-700">
                  Nombre del perfil
                </label>
                <input
                  id="p-name"
                  type="text"
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label htmlFor="p-pod" className="block text-xs font-semibold text-slate-700">
                    Nombre del podcast
                  </label>
                  <input
                    id="p-pod"
                    type="text"
                    value={draft.podcast_name}
                    onChange={(e) => setDraft({ ...draft, podcast_name: e.target.value })}
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="p-plat" className="block text-xs font-semibold text-slate-700">
                    Plataforma destino principal
                  </label>
                  <input
                    id="p-plat"
                    type="text"
                    value={draft.target_platform}
                    onChange={(e) => setDraft({ ...draft, target_platform: e.target.value })}
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
              </div>
              <div>
                <label htmlFor="p-niche" className="block text-xs font-semibold text-slate-700">
                  Descripción del nicho y tono del programa
                </label>
                <input
                  id="p-niche"
                  type="text"
                  value={draft.niche_description}
                  onChange={(e) => setDraft({ ...draft, niche_description: e.target.value })}
                  className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label htmlFor="p-inst" className="block text-xs font-semibold text-slate-700">
                  ¿Qué consideras viral, gracioso o interesante en este podcast?
                </label>
                <textarea
                  id="p-inst"
                  rows={4}
                  value={draft.editorial_instructions}
                  onChange={(e) => setDraft({ ...draft, editorial_instructions: e.target.value })}
                  className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
            </div>

            <div className="space-y-4">
              <h2 className="text-sm font-bold text-slate-800">Cuotas por defecto y ventanas post-corte (segundos)</h2>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="q-cliff" className="block text-xs font-medium text-slate-700">
                    Shorts Cliffhanger
                  </label>
                  <input
                    id="q-cliff"
                    type="number"
                    min={0}
                    value={draft.quotas.shorts_cliffhanger_count}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        quotas: { ...draft.quotas, shorts_cliffhanger_count: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="q-concl" className="block text-xs font-medium text-slate-700">
                    Shorts Conclusivos
                  </label>
                  <input
                    id="q-concl"
                    type="number"
                    min={0}
                    value={draft.quotas.shorts_conclusive_count}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        quotas: { ...draft.quotas, shorts_conclusive_count: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="q-med" className="block text-xs font-medium text-slate-700">
                    Vídeos Medios (5–6 min)
                  </label>
                  <input
                    id="q-med"
                    type="number"
                    min={0}
                    value={draft.quotas.medium_videos_count}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        quotas: { ...draft.quotas, medium_videos_count: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="q-long" className="block text-xs font-medium text-slate-700">
                    Vídeos Largos (12 min)
                  </label>
                  <input
                    id="q-long"
                    type="number"
                    min={0}
                    value={draft.quotas.long_videos_count}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        quotas: { ...draft.quotas, long_videos_count: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                <div>
                  <span className="font-semibold text-slate-700">Shorts (seg)</span>
                  <div className="mt-1 flex gap-1">
                    <input
                      aria-label="Mínimo Shorts segundos"
                      type="number"
                      value={draft.quotas.shorts_window.min_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            shorts_window: { ...draft.quotas.shorts_window, min_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                    <input
                      aria-label="Máximo Shorts segundos"
                      type="number"
                      value={draft.quotas.shorts_window.max_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            shorts_window: { ...draft.quotas.shorts_window, max_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                  </div>
                </div>
                <div>
                  <span className="font-semibold text-slate-700">Medios (seg)</span>
                  <div className="mt-1 flex gap-1">
                    <input
                      aria-label="Mínimo Medios segundos"
                      type="number"
                      value={draft.quotas.medium_window.min_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            medium_window: { ...draft.quotas.medium_window, min_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                    <input
                      aria-label="Máximo Medios segundos"
                      type="number"
                      value={draft.quotas.medium_window.max_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            medium_window: { ...draft.quotas.medium_window, max_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                  </div>
                </div>
                <div>
                  <span className="font-semibold text-slate-700">Largos (seg)</span>
                  <div className="mt-1 flex gap-1">
                    <input
                      aria-label="Mínimo Largos segundos"
                      type="number"
                      value={draft.quotas.long_window.min_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            long_window: { ...draft.quotas.long_window, min_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                    <input
                      aria-label="Máximo Largos segundos"
                      type="number"
                      value={draft.quotas.long_window.max_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          quotas: {
                            ...draft.quotas,
                            long_window: { ...draft.quotas.long_window, max_sec: Number(e.target.value) },
                          },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                  </div>
                </div>
              </div>

              <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={draft.quotas.allow_short_as_teaser}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      quotas: { ...draft.quotas, allow_short_as_teaser: e.target.checked },
                    })
                  }
                  className="h-4 w-4 rounded"
                />
                Permitir que un Short sirva como avance (teaser) de un vídeo medio o largo
              </label>
            </div>
          </div>
        </div>

        {/* Tab 2: Audio & Censorship */}
        <div role="tabpanel" aria-labelledby="tab-audio" hidden={tab !== "audio"}>
          <div className="grid grid-cols-1 gap-6 p-6 lg:grid-cols-2">
            <div className="space-y-4">
              <label className="flex cursor-pointer items-center gap-2.5 text-sm font-semibold text-slate-900">
                <input
                  type="checkbox"
                  checked={draft.audio.remove_silences}
                  onChange={(e) =>
                    setDraft({ ...draft, audio: { ...draft.audio, remove_silences: e.target.checked } })
                  }
                  className="h-4 w-4 rounded"
                />
                Recortar silencios y pausas largas automáticamente
              </label>

              <label className="flex cursor-pointer items-center gap-2.5 text-sm font-semibold text-slate-900">
                <input
                  type="checkbox"
                  checked={draft.audio.level_multi_speaker_voices ?? true}
                  onChange={(e) =>
                    setDraft({ ...draft, audio: { ...draft.audio, level_multi_speaker_voices: e.target.checked } })
                  }
                  className="h-4 w-4 rounded"
                />
                Nivelar volumen automáticamente entre múltiples hablantes (dynaudnorm)
              </label>

              <div className="grid grid-cols-3 gap-3 text-xs">
                <div>
                  <label htmlFor="sil-db" className="font-semibold text-slate-700">
                    Umbral (dB)
                  </label>
                  <input
                    id="sil-db"
                    type="number"
                    value={draft.audio.silence_threshold_db}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        audio: { ...draft.audio, silence_threshold_db: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="sil-min" className="font-semibold text-slate-700">
                    Silencio mín. (s)
                  </label>
                  <input
                    id="sil-min"
                    type="number"
                    step="0.1"
                    value={draft.audio.min_silence_sec}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        audio: { ...draft.audio, min_silence_sec: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="sil-keep" className="font-semibold text-slate-700">
                    Margen natural (s)
                  </label>
                  <input
                    id="sil-keep"
                    type="number"
                    step="0.02"
                    value={draft.audio.keep_silence_sec}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        audio: { ...draft.audio, keep_silence_sec: Number(e.target.value) },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
              </div>

              <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
                  <Music className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                  Música de fondo sutil (opcional)
                </div>
                <p className="mt-1 text-xs text-slate-600">
                  Archivo actual: {draft.audio.bg_music_filename || "Ninguno"}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-3">
                  <input
                    type="file"
                    accept=".mp3,.wav,.m4a,.ogg"
                    aria-label="Subir música de fondo"
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      const updated = await api.uploadBgMusic(draft.id, f);
                      setDraft(structuredClone(updated));
                      onProfilesChanged(profiles.map((p) => (p.id === updated.id ? updated : p)));
                    }}
                    className="text-xs"
                  />
                  <label className="text-xs text-slate-700">
                    Volumen: {draft.audio.bg_music_volume_db} dB
                    <input
                      type="range"
                      min={-42}
                      max={-12}
                      value={draft.audio.bg_music_volume_db}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          audio: { ...draft.audio, bg_music_volume_db: Number(e.target.value) },
                        })
                      }
                      className="ml-2 align-middle"
                    />
                  </label>
                </div>
              </div>
            </div>

            {/* Censored words chip input */}
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
                <ShieldAlert className="h-4 w-4 text-rose-600" aria-hidden="true" />
                Palabras o frases censuradas (corte limpio sin pitidos)
              </div>
              <p className="text-xs text-slate-600">
                Se detectan sin importar mayúsculas ni tildes, se recortan del audio con empalme suave y se eliminan de los subtítulos.
              </p>

              <div className="flex gap-2">
                <input
                  type="text"
                  aria-label="Añadir palabra o frase prohibida"
                  value={newWord}
                  onChange={(e) => setNewWord(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                      e.preventDefault();
                      addCensoredWord();
                    }
                  }}
                  placeholder="Ej. palabrota, marca rival..."
                  className="flex-1 rounded-xl border border-slate-300 px-3 py-2 text-sm"
                />
                <button
                  type="button"
                  onClick={addCensoredWord}
                  className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800"
                >
                  Añadir
                </button>
              </div>

              <div className="flex min-h-24 flex-wrap content-start gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                {draft.audio.censored_words.length === 0 ? (
                  <span className="text-xs text-slate-400">Sin palabras censuradas todavía.</span>
                ) : (
                  draft.audio.censored_words.map((w, idx) => (
                    <span
                      key={w + idx}
                      className="inline-flex items-center gap-1 rounded-full bg-rose-100 px-3 py-1 text-xs font-semibold text-rose-800"
                    >
                      {w}
                      <button
                        type="button"
                        aria-label={`Quitar palabra ${w}`}
                        onClick={() =>
                          setDraft({
                            ...draft,
                            audio: {
                              ...draft.audio,
                              censored_words: draft.audio.censored_words.filter((_, i) => i !== idx),
                            },
                          })
                        }
                        className="rounded-full p-0.5 hover:bg-rose-200"
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    </span>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Tab 3: Custom Images */}
        <div role="tabpanel" aria-labelledby="tab-images" hidden={tab !== "images"}>
          <div className="space-y-4 p-6">
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setImgDrag(true);
              }}
              onDragLeave={() => setImgDrag(false)}
              onDrop={handleDropImages}
              className={`flex flex-col items-center justify-center rounded-2xl border-2 border-dashed p-6 text-center transition ${
                imgDrag ? "border-indigo-500 bg-indigo-50" : "border-slate-300 bg-slate-50"
              }`}
            >
              <ImagePlus className="h-8 w-8 text-indigo-600" aria-hidden="true" />
              <p className="mt-2 text-sm font-semibold text-slate-800">
                Arrastra aquí fotos del presentador, logos o gráficos recurrentes
              </p>
              <label className="mt-3 cursor-pointer rounded-xl bg-indigo-600 px-4 py-2 text-xs font-semibold text-white hover:bg-indigo-700">
                Seleccionar imágenes
                <input
                  type="file"
                  multiple
                  accept=".png,.jpg,.jpeg,.webp"
                  className="sr-only"
                  onChange={(e) => handleUploadImages(e.target.files)}
                />
              </label>
            </div>

            {/* Pre-production AI Image Studio (never auto-invoked during video render) */}
            <div className="rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm font-bold text-indigo-950">
                  <Sparkles className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                  <span>Generar imagen con IA (Pre-producción)</span>
                </div>
                <span className="rounded-full bg-indigo-100 px-2.5 py-0.5 text-[11px] font-semibold text-indigo-800">
                  Solo aquí antes de crear vídeos • No genera IA al renderizar
                </span>
              </div>

              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-12">
                <div className="sm:col-span-7">
                  <label htmlFor="prof-ai-prompt" className="block text-xs font-semibold text-slate-700">
                    Describe la imagen que quieres generar
                  </label>
                  <input
                    id="prof-ai-prompt"
                    type="text"
                    value={aiPrompt}
                    onChange={(e) => setAiPrompt(e.target.value)}
                    placeholder="Ej. Mapa estratégico europeo de 1940 en mesa de madera oscura"
                    className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs"
                  />
                </div>
                <div className="sm:col-span-2">
                  <label htmlFor="prof-ai-aspect" className="block text-xs font-semibold text-slate-700">
                    Formato
                  </label>
                  <select
                    id="prof-ai-aspect"
                    value={aiAspect}
                    onChange={(e) => setAiAspect(e.target.value as AspectRatio)}
                    className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-2.5 py-2 text-xs"
                  >
                    <option value="9:16">9:16 Vertical</option>
                    <option value="16:9">16:9 Horizontal</option>
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
                        if (!aiTitle.trim()) {
                          setAiTitle(aiPrompt.trim().slice(0, 48));
                        }
                      } catch (err) {
                        setAiError(err instanceof Error ? err.message : "Error generando imagen IA");
                      } finally {
                        setAiGenerating(false);
                      }
                    }}
                    className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                  >
                    <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                    {aiGenerating ? "Generando..." : "Generar vista previa"}
                  </button>
                </div>
              </div>

              {aiError && (
                <p role="alert" className="mt-2 text-xs font-medium text-rose-700">
                  {aiError}
                </p>
              )}

              {aiPreview && (
                <div className="mt-4 flex flex-col gap-4 rounded-xl border border-indigo-200 bg-white p-3 sm:flex-row">
                  <img
                    src={aiPreview.preview_url}
                    alt={aiTitle || "Vista previa generada con IA"}
                    className="h-32 w-32 shrink-0 rounded-xl border border-slate-200 object-cover bg-slate-100"
                  />
                  <div className="grid flex-1 grid-cols-1 gap-2 sm:grid-cols-2 text-xs">
                    <div>
                      <label htmlFor="prof-ai-title" className="font-semibold text-slate-700">
                        Título del recurso
                      </label>
                      <input
                        id="prof-ai-title"
                        type="text"
                        value={aiTitle}
                        onChange={(e) => setAiTitle(e.target.value)}
                        className="mt-0.5 w-full rounded-lg border border-slate-300 px-2.5 py-1.5"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label htmlFor="prof-ai-prio" className="font-semibold text-slate-700">
                          Prioridad
                        </label>
                        <select
                          id="prof-ai-prio"
                          value={aiPriority}
                          onChange={(e) => setAiPriority(e.target.value as ImagePriority)}
                          className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5"
                        >
                          <option value="alta">Alta</option>
                          <option value="media">Media</option>
                          <option value="baja">Baja</option>
                        </select>
                      </div>
                      <div>
                        <label htmlFor="prof-ai-mode" className="font-semibold text-slate-700">
                          Modo
                        </label>
                        <select
                          id="prof-ai-mode"
                          value={aiMode}
                          onChange={(e) => setAiMode(e.target.value as ImageDisplayMode)}
                          className="mt-0.5 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5"
                        >
                          <option value="fullscreen_zoom">Pantalla completa</option>
                          <option value="pip_overlay">Recuadro (PiP)</option>
                          <option value="watermark">Marca de agua</option>
                          <option value="outro_cta">Cierre CTA</option>
                        </select>
                      </div>
                    </div>
                    <div className="sm:col-span-2">
                      <label htmlFor="prof-ai-usage" className="font-semibold text-slate-700">
                        Instrucciones de uso para la IA (cuándo insertarla en tus vídeos)
                      </label>
                      <input
                        id="prof-ai-usage"
                        type="text"
                        value={aiUsage}
                        onChange={(e) => setAiUsage(e.target.value)}
                        placeholder="Ej. Mostrar al hablar de la estrategia aérea o apertura del episodio"
                        className="mt-0.5 w-full rounded-lg border border-slate-300 px-2.5 py-1.5"
                      />
                    </div>
                    <div className="flex items-center justify-between sm:col-span-2">
                      <span className="text-[11px] text-slate-500">
                        {aiPreview.width}x{aiPreview.height} • {aiPreview.model_used}
                      </span>
                      <button
                        type="button"
                        disabled={aiSaving}
                        onClick={async () => {
                          setAiSaving(true);
                          try {
                            const updated = await api.addProfileAIImage(draft.id, {
                              preview_filename: aiPreview.preview_filename,
                              title: aiTitle.trim() || "Imagen IA",
                              usage_instructions: aiUsage.trim(),
                              priority: aiPriority,
                              display_mode: aiMode,
                            });
                            setDraft(structuredClone(updated));
                            onProfilesChanged(profiles.map((p) => (p.id === updated.id ? updated : p)));
                            onAnnounce("Imagen IA añadida al perfil.");
                            setAiPreview(null);
                            setAiPrompt("");
                            setAiTitle("");
                            setAiUsage("");
                          } catch (err) {
                            setAiError(err instanceof Error ? err.message : "No se pudo guardar la imagen IA.");
                          } finally {
                            setAiSaving(false);
                          }
                        }}
                        className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                      >
                        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                        {aiSaving ? "Guardando..." : "Añadir al perfil"}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {draft.custom_images.map((img, idx) => (
                <div key={img.id} className="flex gap-3 rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
                  <img
                    src={`/api/profiles/${encodeURIComponent(draft.id)}/images/${encodeURIComponent(img.filename)}`}
                    alt={img.title}
                    className="h-24 w-24 shrink-0 rounded-xl object-cover bg-slate-100"
                  />
                  <div className="flex-1 space-y-2 text-xs">
                    <div className="flex items-center gap-2">
                      <input
                        aria-label="Título de imagen"
                        type="text"
                        value={img.title}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            custom_images: draft.custom_images.map((x, i) =>
                              i === idx ? { ...x, title: e.target.value } : x,
                            ),
                          })
                        }
                        className="flex-1 rounded-lg border border-slate-300 px-2.5 py-1 font-semibold"
                      />
                      <button
                        type="button"
                        aria-label={`Eliminar ${img.title}`}
                        onClick={async () => {
                          const updated = await api.deleteProfileImage(draft.id, img.id);
                          setDraft(structuredClone(updated));
                          onProfilesChanged(profiles.map((p) => (p.id === updated.id ? updated : p)));
                        }}
                        className="rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-700"
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </button>
                    </div>

                    <textarea
                      aria-label="Instrucciones de uso para la IA"
                      rows={2}
                      value={img.usage_instructions}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          custom_images: draft.custom_images.map((x, i) =>
                            i === idx ? { ...x, usage_instructions: e.target.value } : x,
                          ),
                        })
                      }
                      placeholder="Ej. Usar cuando hable en primera persona o cierre el episodio"
                      className="w-full rounded-lg border border-slate-300 px-2.5 py-1"
                    />

                    <div className="grid grid-cols-2 gap-2">
                      <select
                        aria-label="Prioridad"
                        value={img.priority}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            custom_images: draft.custom_images.map((x, i) =>
                              i === idx ? { ...x, priority: e.target.value as ImagePriority } : x,
                            ),
                          })
                        }
                        className="rounded-lg border border-slate-300 bg-white px-2 py-1"
                      >
                        <option value="alta">Prioridad Alta</option>
                        <option value="media">Prioridad Media</option>
                        <option value="baja">Prioridad Baja</option>
                      </select>

                      <select
                        aria-label="Modo de aparición"
                        value={img.display_mode}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            custom_images: draft.custom_images.map((x, i) =>
                              i === idx ? { ...x, display_mode: e.target.value as ImageDisplayMode } : x,
                            ),
                          })
                        }
                        className="rounded-lg border border-slate-300 bg-white px-2 py-1"
                      >
                        <option value="fullscreen_zoom">Pantalla completa + Zoom</option>
                        <option value="pip_overlay">Recuadro flotante (PiP)</option>
                        <option value="watermark">Marca de agua fija</option>
                        <option value="outro_cta">Cierre CTA final</option>
                      </select>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Tab 4: Visual Style & Live Subtitles */}
        <div role="tabpanel" aria-labelledby="tab-visual" hidden={tab !== "visual"}>
          <div className="grid grid-cols-1 gap-6 p-6 lg:grid-cols-12">
            <div className="space-y-4 lg:col-span-7">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label htmlFor="v-tone" className="block text-xs font-semibold text-slate-700">
                    Tono visual deseado
                  </label>
                  <input
                    id="v-tone"
                    type="text"
                    value={draft.visual.visual_tone}
                    onChange={(e) =>
                      setDraft({ ...draft, visual: { ...draft.visual, visual_tone: e.target.value } })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="v-shorts-ar" className="block text-xs font-semibold text-slate-700">
                    Formato vídeos medios
                  </label>
                  <select
                    id="v-shorts-ar"
                    value={draft.visual.medium_aspect}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        visual: { ...draft.visual, medium_aspect: e.target.value as AspectRatio },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
                  >
                    <option value="16:9">16:9 (YouTube / X)</option>
                    <option value="1:1">1:1 (Cuadrado Instagram / X)</option>
                  </select>
                </div>
              </div>

              {/* Font picker & custom TTF upload */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label htmlFor="s-font" className="block text-xs font-semibold text-slate-700">
                    Tipografía de subtítulos
                  </label>
                  <select
                    id="s-font"
                    value={draft.subtitles.font_file}
                    onChange={(e) => {
                      const chosen = fonts.find((f) => f.filename === e.target.value);
                      setDraft({
                        ...draft,
                        subtitles: {
                          ...draft.subtitles,
                          font_file: e.target.value,
                          font_name: chosen?.display_name || e.target.value,
                        },
                      });
                    }}
                    className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
                  >
                    {fonts.map((f) => (
                      <option key={f.filename} value={f.filename}>
                        {f.display_name} ({f.scope === "profile" ? "Propia" : "Incluida"})
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <span className="block text-xs font-semibold text-slate-700">Subir fuente (.ttf / .otf)</span>
                  <label className="mt-1 flex cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-slate-300 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-100">
                    <Type className="h-4 w-4 text-indigo-600" aria-hidden="true" />
                    Cargar tipografía propia
                    <input
                      type="file"
                      accept=".ttf,.otf"
                      className="sr-only"
                      onChange={async (e) => {
                        const f = e.target.files?.[0];
                        if (!f) return;
                        const updated = await api.uploadProfileFont(draft.id, f);
                        setDraft(structuredClone(updated));
                        onProfilesChanged(profiles.map((p) => (p.id === updated.id ? updated : p)));
                        setFonts(await api.listFonts(draft.id));
                        onAnnounce("Tipografía cargada.");
                      }}
                    />
                  </label>
                </div>
              </div>

              {/* Colors & Sizes */}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div>
                  <label htmlFor="c-prim" className="block text-xs font-semibold text-slate-700">
                    Color texto
                  </label>
                  <input
                    id="c-prim"
                    type="color"
                    value={draft.subtitles.primary_color}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, primary_color: e.target.value },
                      })
                    }
                    className="mt-1 h-10 w-full cursor-pointer rounded-lg border border-slate-300 bg-white p-1"
                  />
                </div>
                <div>
                  <label htmlFor="c-high" className="block text-xs font-semibold text-slate-700">
                    Palabra activa
                  </label>
                  <input
                    id="c-high"
                    type="color"
                    value={draft.subtitles.highlight_color}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, highlight_color: e.target.value },
                      })
                    }
                    className="mt-1 h-10 w-full cursor-pointer rounded-lg border border-slate-300 bg-white p-1"
                  />
                </div>
                <div>
                  <label htmlFor="c-out" className="block text-xs font-semibold text-slate-700">
                    Contorno
                  </label>
                  <input
                    id="c-out"
                    type="color"
                    value={draft.subtitles.outline_color}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, outline_color: e.target.value },
                      })
                    }
                    className="mt-1 h-10 w-full cursor-pointer rounded-lg border border-slate-300 bg-white p-1"
                  />
                </div>
                <div>
                  <label htmlFor="s-size" className="block text-xs font-semibold text-slate-700">
                    Tamaño Short ({draft.subtitles.font_size_short})
                  </label>
                  <input
                    id="s-size"
                    type="range"
                    min={40}
                    max={96}
                    value={draft.subtitles.font_size_short}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, font_size_short: Number(e.target.value) },
                      })
                    }
                    className="mt-3 w-full"
                  />
                </div>
              </div>

              <div className="flex flex-wrap gap-4 text-xs font-medium text-slate-700">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={draft.subtitles.uppercase}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, uppercase: e.target.checked },
                      })
                    }
                    className="h-4 w-4 rounded"
                  />
                  MAYÚSCULAS
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={draft.subtitles.box_enabled}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, box_enabled: e.target.checked },
                      })
                    }
                    className="h-4 w-4 rounded"
                  />
                  Caja de fondo semitransparente
                </label>
                <label className="flex items-center gap-2">
                  Palabras/línea Short:
                  <input
                    type="number"
                    min={1}
                    max={8}
                    value={draft.subtitles.words_per_line_short}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, words_per_line_short: Number(e.target.value) },
                      })
                    }
                    className="w-16 rounded-lg border border-slate-300 px-2 py-1"
                  />
                </label>
                <label className="flex items-center gap-2">
                  Palabras/línea Largo:
                  <input
                    type="number"
                    min={2}
                    max={14}
                    value={draft.subtitles.words_per_line_long}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, words_per_line_long: Number(e.target.value) },
                      })
                    }
                    className="w-16 rounded-lg border border-slate-300 px-2 py-1"
                  />
                </label>
              </div>

              <div className="grid grid-cols-2 gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                <div>
                  <span className="font-semibold text-slate-700">Ritmo cambio plano Shorts (s)</span>
                  <div className="mt-1 flex gap-1.5">
                    <input
                      aria-label="Mínimo ritmo Short"
                      type="number"
                      step="0.5"
                      min={1}
                      value={draft.visual.short_cut_min_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, short_cut_min_sec: Number(e.target.value) },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                    <input
                      aria-label="Máximo ritmo Short"
                      type="number"
                      step="0.5"
                      min={1.5}
                      value={draft.visual.short_cut_max_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, short_cut_max_sec: Number(e.target.value) },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                  </div>
                </div>
                <div>
                  <span className="font-semibold text-slate-700">Ritmo cambio plano Largos (s)</span>
                  <div className="mt-1 flex gap-1.5">
                    <input
                      aria-label="Mínimo ritmo Largo"
                      type="number"
                      step="0.5"
                      min={2}
                      value={draft.visual.long_cut_min_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, long_cut_min_sec: Number(e.target.value) },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                    <input
                      aria-label="Máximo ritmo Largo"
                      type="number"
                      step="0.5"
                      min={3}
                      value={draft.visual.long_cut_max_sec}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, long_cut_max_sec: Number(e.target.value) },
                        })
                      }
                      className="w-full rounded border border-slate-300 bg-white px-2 py-1"
                    />
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label htmlFor="s-hook" className="block text-xs font-semibold text-slate-700">
                    Texto fijo auxiliar (opcional)
                  </label>
                  <input
                    id="s-hook"
                    type="text"
                    value={draft.subtitles.hook_prefix_text}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, hook_prefix_text: e.target.value },
                      })
                    }
                    placeholder="Vacío = usa el texto real del gancho hablado"
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="s-cta" className="block text-xs font-semibold text-slate-700">
                    Faldón / cierre CTA final
                  </label>
                  <input
                    id="s-cta"
                    type="text"
                    value={draft.subtitles.cta_text}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, cta_text: e.target.value },
                      })
                    }
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
              </div>

              {/* Full-Screen Visual Hook Configuration */}
              <fieldset className="space-y-3 rounded-xl border border-amber-200 bg-amber-50/40 p-3.5 text-xs">
                <legend className="px-1 font-bold text-slate-800">
                  Gancho Visual Inicial (Pantalla Completa)
                </legend>
                <label className="flex cursor-pointer items-center gap-2 font-semibold text-slate-800">
                  <input
                    type="checkbox"
                    checked={draft.subtitles.visual_hook_enabled ?? true}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        subtitles: { ...draft.subtitles, visual_hook_enabled: e.target.checked },
                      })
                    }
                    className="h-4 w-4 rounded"
                  />
                  Mostrar gancho visual 100% pantalla con lo que dice la voz (sin subtítulos debajo durante esos segundos)
                </label>

                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  {(
                    [
                      ["kinetic_fullscreen", "Cinético dinámico"],
                      ["editorial_card", "Tarjeta editorial"],
                      ["bold_minimal", "Minimal con progreso"],
                    ] as [VisualHookStyle, string][]
                  ).map(([val, lbl]) => (
                    <label
                      key={val}
                      className={`flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-2 font-medium ${
                        (draft.subtitles.visual_hook_style ?? "kinetic_fullscreen") === val
                          ? "border-indigo-500 bg-white text-indigo-900 shadow-xs"
                          : "border-slate-200 bg-white/60 text-slate-700"
                      }`}
                    >
                      <input
                        type="radio"
                        name="visual_hook_style"
                        checked={(draft.subtitles.visual_hook_style ?? "kinetic_fullscreen") === val}
                        onChange={() =>
                          setDraft({
                            ...draft,
                            subtitles: { ...draft.subtitles, visual_hook_style: val },
                          })
                        }
                        className="h-3.5 w-3.5"
                      />
                      <span>{lbl}</span>
                    </label>
                  ))}
                </div>

                <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
                  <label className="block font-semibold text-slate-700">
                    Fondo gancho
                    <input
                      type="color"
                      value={draft.subtitles.visual_hook_bg_color || "#0B0F1A"}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          subtitles: { ...draft.subtitles, visual_hook_bg_color: e.target.value },
                        })
                      }
                      className="mt-1 h-8 w-full cursor-pointer rounded border border-slate-300"
                    />
                  </label>
                  <label className="block font-semibold text-slate-700">
                    Texto gancho
                    <input
                      type="color"
                      value={draft.subtitles.visual_hook_text_color || "#FFFFFF"}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          subtitles: { ...draft.subtitles, visual_hook_text_color: e.target.value },
                        })
                      }
                      className="mt-1 h-8 w-full cursor-pointer rounded border border-slate-300"
                    />
                  </label>
                  <label className="block font-semibold text-slate-700">
                    Acento palabra
                    <input
                      type="color"
                      value={draft.subtitles.visual_hook_accent_color || "#FFD400"}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          subtitles: { ...draft.subtitles, visual_hook_accent_color: e.target.value },
                        })
                      }
                      className="mt-1 h-8 w-full cursor-pointer rounded border border-slate-300"
                    />
                  </label>
                  <label className="block font-semibold text-slate-700">
                    Opacidad ({Math.round((draft.subtitles.visual_hook_bg_opacity ?? 0.72) * 100)}%)
                    <input
                      type="range"
                      min={0.25}
                      max={0.95}
                      step={0.05}
                      value={draft.subtitles.visual_hook_bg_opacity ?? 0.72}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          subtitles: { ...draft.subtitles, visual_hook_bg_opacity: Number(e.target.value) },
                        })
                      }
                      className="mt-2.5 w-full"
                    />
                  </label>
                </div>
              </fieldset>

              {/* Multi-Speaker & V2 Visual Quality Engine */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 rounded-xl border border-slate-200 bg-slate-50 p-3.5 text-xs">
                <div className="space-y-2">
                  <div className="font-bold text-slate-800">Multi-hablante (paleta por voz)</div>
                  <label className="flex cursor-pointer items-center gap-2 font-medium text-slate-700">
                    <input
                      type="checkbox"
                      checked={draft.subtitles.speaker_colors_enabled ?? true}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          subtitles: { ...draft.subtitles, speaker_colors_enabled: e.target.checked },
                        })
                      }
                      className="h-4 w-4 rounded"
                    />
                    Color automático por hablante
                  </label>
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    {(draft.subtitles.speaker_palette || []).map((hex, pIdx) => (
                      <label key={pIdx} className="flex flex-col items-center gap-0.5">
                        <input
                          type="color"
                          aria-label={`Color hablante ${pIdx + 1}`}
                          value={hex}
                          onChange={(e) => {
                            const next = [...(draft.subtitles.speaker_palette || [])];
                            next[pIdx] = e.target.value;
                            setDraft({
                              ...draft,
                              subtitles: { ...draft.subtitles, speaker_palette: next },
                            });
                          }}
                          className="h-6 w-7 cursor-pointer rounded border border-slate-300"
                        />
                        <span className="text-[10px] text-slate-500">V{pIdx + 1}</span>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="font-bold text-slate-800">Control Visual IA & Archivos Libres</div>
                  <label className="flex cursor-pointer items-center gap-2 font-medium text-slate-700">
                    <input
                      type="checkbox"
                      checked={draft.visual.enable_vlm_stock_check ?? true}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, enable_vlm_stock_check: e.target.checked },
                        })
                      }
                      className="h-4 w-4 rounded"
                    />
                    Filtro visual IA (descarta planos irrelevantes)
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 font-medium text-slate-700">
                    <input
                      type="checkbox"
                      checked={draft.visual.enable_public_domain_archives ?? true}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, enable_public_domain_archives: e.target.checked },
                        })
                      }
                      className="h-4 w-4 rounded"
                    />
                    Wikimedia / Archive (solo Dominio Público / CC0)
                  </label>
                  <div className="flex items-center gap-2 pt-0.5">
                    <label className="flex cursor-pointer items-center gap-2 font-medium text-slate-700">
                      <input
                        type="checkbox"
                        checked={draft.visual.enable_smooth_transitions ?? true}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            visual: { ...draft.visual, enable_smooth_transitions: e.target.checked },
                          })
                        }
                        className="h-4 w-4 rounded"
                      />
                      Transiciones suaves (s):
                    </label>
                    <input
                      aria-label="Duración transición en segundos"
                      type="number"
                      min={0}
                      max={0.6}
                      step={0.05}
                      value={draft.visual.transition_sec ?? 0.25}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          visual: { ...draft.visual, transition_sec: Number(e.target.value) },
                        })
                      }
                      className="w-16 rounded border border-slate-300 bg-white px-2 py-0.5"
                    />
                  </div>
                </div>
              </div>

              {/* Selectable Image/Video Sources & Strict License Policy */}
              <fieldset className="space-y-2.5 rounded-xl border border-emerald-200 bg-emerald-50/40 p-3.5 text-xs">
                <legend className="px-1 font-bold text-emerald-950">
                  Fuentes de Imágenes y Vídeos Permitidas (100% Licencias Libres y Gratis)
                </legend>
                <p className="text-[11px] text-slate-600">
                  Elige de dónde puede tomar recursos visuales la IA para este perfil. Todas las fuentes se auditan por archivo y se descarga un certificado <code className="rounded bg-white px-1">.licencias.txt</code> junto a cada vídeo.
                </p>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {ALL_MEDIA_PROVIDERS.map((prov) => {
                    const currentProvs: MediaProviderId[] =
                      draft.visual.allowed_media_providers && draft.visual.allowed_media_providers.length > 0
                        ? draft.visual.allowed_media_providers
                        : ALL_MEDIA_PROVIDERS.map((p) => p.id);
                    const checked = currentProvs.includes(prov.id);
                    return (
                      <label
                        key={prov.id}
                        className={`flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 transition ${
                          checked
                            ? "border-emerald-400 bg-white text-slate-900 shadow-xs"
                            : "border-slate-200 bg-white/50 text-slate-500"
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            let next = e.target.checked
                              ? [...currentProvs, prov.id]
                              : currentProvs.filter((x) => x !== prov.id);
                            if (next.length === 0) {
                              next = ["custom_images"];
                            }
                            setDraft({
                              ...draft,
                              visual: {
                                ...draft.visual,
                                allowed_media_providers: next,
                                enable_public_domain_archives:
                                  next.includes("wikimedia_commons") || next.includes("internet_archive"),
                              },
                            });
                          }}
                          className="mt-0.5 h-4 w-4 rounded"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center justify-between gap-1 font-semibold">
                            <span>{prov.label}</span>
                            <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800">
                              {prov.licenseBadge}
                            </span>
                          </div>
                          <div className="mt-0.5 text-[11px] text-slate-500">{prov.desc}</div>
                        </div>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              {/* Forbidden stock keywords */}
              <div>
                <span className="block text-xs font-semibold text-slate-700">
                  Palabras o estilos prohibidos en búsquedas de stock
                </span>
                <div className="mt-1 flex gap-2">
                  <input
                    type="text"
                    value={newBadStock}
                    onChange={(e) => setNewBadStock(e.target.value)}
                    placeholder="Ej. cartoon, neon"
                    className="flex-1 rounded-xl border border-slate-300 px-3 py-1.5 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      if (!newBadStock.trim()) return;
                      setDraft({
                        ...draft,
                        visual: {
                          ...draft.visual,
                          forbidden_stock_keywords: [...draft.visual.forbidden_stock_keywords, newBadStock.trim()],
                        },
                      });
                      setNewBadStock("");
                    }}
                    className="rounded-xl bg-slate-800 px-3 py-1.5 text-xs font-semibold text-white"
                  >
                    Añadir
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {draft.visual.forbidden_stock_keywords.map((kw, i) => (
                    <span key={kw + i} className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-xs">
                      {kw}
                      <button
                        type="button"
                        aria-label={`Quitar ${kw}`}
                        onClick={() =>
                          setDraft({
                            ...draft,
                            visual: {
                              ...draft.visual,
                              forbidden_stock_keywords: draft.visual.forbidden_stock_keywords.filter((_, idx) => idx !== i),
                            },
                          })
                        }
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            </div>

            <div className="lg:col-span-5">
              <SubtitlePreview
                profileId={draft.id}
                subtitles={draft.subtitles}
                visualTone={draft.visual.visual_tone}
              />
            </div>
          </div>
        </div>

        {/* Tab 5: Intro & Outro Curtains + AI Voices */}
        <div role="tabpanel" aria-labelledby="tab-curtains" hidden={tab !== "curtains"}>
          <div className="grid grid-cols-1 gap-6 p-6 lg:grid-cols-12">
            <div className="space-y-6 lg:col-span-7">
              {/* SECTION A: Voice Engine + Sample Player */}
              <div className="rounded-2xl border border-indigo-200 bg-indigo-50/40 p-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="flex items-center gap-2 text-base font-bold text-slate-900">
                      <Volume2 className="h-4 w-4 text-indigo-700" aria-hidden="true" />
                      Voz IA Compartida (Cortinilla Inicial y Cierre Cacheado)
                    </h3>
                    <p className="mt-0.5 text-xs text-slate-600">
                      Esta voz narra el contexto inicial en cada clip y el aviso de cierre (generado una sola vez para todo el perfil).
                    </p>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                  {(ttsCatalog?.engines || []).map((eng) => {
                    const active = ic.tts_engine === eng.id;
                    return (
                      <button
                        key={eng.id}
                        type="button"
                        onClick={() => {
                          const firstVoice = eng.voices[0]?.id || ic.tts_voice;
                          updateIntro({ tts_engine: eng.id, tts_voice: firstVoice });
                          setSampleUrl(null);
                          setSampleError(null);
                        }}
                        className={`rounded-xl border p-3 text-left transition ${
                          active
                            ? "border-indigo-600 bg-white ring-2 ring-indigo-200"
                            : "border-slate-200 bg-white/80 hover:border-slate-300"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-1">
                          <span className="text-xs font-bold text-slate-900">{eng.label}</span>
                          {!eng.configured && (
                            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
                              Falta API key
                            </span>
                          )}
                        </div>
                        <p className="mt-1 text-[11px] text-slate-500">{eng.cost_note}</p>
                      </button>
                    );
                  })}
                </div>

                <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div className="sm:col-span-2">
                    <label htmlFor="tts-voice-sel" className="block text-xs font-semibold text-slate-700">
                      Locutor / Voz en Español
                    </label>
                    <select
                      id="tts-voice-sel"
                      value={ic.tts_voice}
                      onChange={(e) => updateIntro({ tts_voice: e.target.value })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                    >
                      {(
                        ttsCatalog?.engines.find((e) => e.id === ic.tts_engine)?.voices || [
                          { id: ic.tts_voice, label: ic.tts_voice, gender: "M" },
                        ]
                      ).map((v) => (
                        <option key={v.id} value={v.id}>
                          {v.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="tts-rate-sel" className="block text-xs font-semibold text-slate-700">
                      Velocidad de locución
                    </label>
                    <select
                      id="tts-rate-sel"
                      value={ic.tts_rate}
                      onChange={(e) => updateIntro({ tts_rate: e.target.value })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                    >
                      <option value="-8%">Pausada (-8%)</option>
                      <option value="+0%">Natural (0%)</option>
                      <option value="+6%">Ágil podcast (+6%)</option>
                      <option value="+12%">Muy dinámica (+12%)</option>
                    </select>
                  </div>
                </div>

                {ic.tts_engine === "openrouter_audio" && (
                  <div className="mt-3">
                    <label htmlFor="tts-or-model" className="block text-xs font-semibold text-slate-700">
                      Modelo OpenRouter Audio
                    </label>
                    <select
                      id="tts-or-model"
                      value={ic.openrouter_audio_model}
                      onChange={(e) => updateIntro({ openrouter_audio_model: e.target.value })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                    >
                      <option value="openai/gpt-audio-mini">openai/gpt-audio-mini (Rápido y económico)</option>
                      <option value="openai/gpt-audio">openai/gpt-audio (Máxima calidad)</option>
                    </select>
                  </div>
                )}

                <div className="mt-4 rounded-xl border border-indigo-200 bg-white p-3">
                  <label htmlFor="tts-sample-txt" className="block text-xs font-semibold text-slate-700">
                    Frase de prueba en vivo
                  </label>
                  <div className="mt-1.5 flex flex-col gap-2 sm:flex-row">
                    <input
                      id="tts-sample-txt"
                      type="text"
                      value={sampleText}
                      onChange={(e) => setSampleText(e.target.value)}
                      className="flex-1 rounded-xl border border-slate-300 px-3 py-2 text-xs"
                    />
                    <button
                      type="button"
                      disabled={sampleLoading}
                      onClick={async () => {
                        setSampleLoading(true);
                        setSampleError(null);
                        try {
                          const res = await api.previewTts({
                            text: sampleText,
                            engine: ic.tts_engine,
                            voice: ic.tts_voice,
                            rate: ic.tts_rate,
                            openrouter_audio_model: ic.openrouter_audio_model,
                          });
                          setSampleUrl(`${res.url}?t=${Date.now()}`);
                        } catch (err) {
                          setSampleError(err instanceof Error ? err.message : "Error generando muestra");
                        } finally {
                          setSampleLoading(false);
                        }
                      }}
                      className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      <Volume2 className="h-3.5 w-3.5" aria-hidden="true" />
                      {sampleLoading ? "Sintetizando..." : "Escuchar ejemplo"}
                    </button>
                  </div>
                  {sampleError && (
                    <p role="alert" className="mt-2 text-xs font-medium text-rose-700">
                      {sampleError}
                    </p>
                  )}
                  {sampleUrl && (
                    <audio controls autoPlay src={sampleUrl} className="mt-2.5 w-full" />
                  )}
                </div>
              </div>

              {/* SECTION B: Intro Curtain Configuration */}
              <div className="rounded-2xl border border-slate-200 bg-white p-5 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="text-base font-bold text-slate-900">1. Cortinilla Inicial de Contexto (Gancho IA)</h3>
                    <p className="text-xs text-slate-500">
                      Muestra una pantalla inicial personalizable con narración IA antes de arrancar el audio del podcast (sustituye al gancho tradicional).
                    </p>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 rounded-xl bg-slate-100 px-3 py-1.5 text-xs font-bold text-slate-800">
                    <input
                      type="checkbox"
                      checked={ic.enabled}
                      onChange={(e) => updateIntro({ enabled: e.target.checked })}
                      className="h-4 w-4 rounded"
                    />
                    Activada
                  </label>
                </div>

                <div>
                  <span className="block text-xs font-semibold text-slate-700">Cuándo activar la narración IA inicial</span>
                  <div className="mt-1.5 grid grid-cols-1 gap-2 sm:grid-cols-2 text-xs">
                    {(
                      [
                        [
                          "auto_if_needed",
                          "Solo si falta contexto en los primeros 4-6s (Recomendado)",
                          "Prioriza el podcast puro o un gancho propio y usa IA solo si el arranque no queda claro.",
                        ],
                        [
                          "always",
                          "Siempre al inicio",
                          "Genera siempre una cortinilla breve de contexto antes de entrar en el audio.",
                        ],
                      ] as [IntroCurtainMode, string, string][]
                    ).map(([mVal, title, desc]) => (
                      <label
                        key={mVal}
                        className={`flex cursor-pointer items-start gap-2 rounded-xl border p-2.5 transition ${
                          (ic.mode || "auto_if_needed") === mVal
                            ? "border-indigo-500 bg-indigo-50/70"
                            : "border-slate-200 bg-slate-50/60"
                        }`}
                      >
                        <input
                          type="radio"
                          name="ic_mode"
                          checked={(ic.mode || "auto_if_needed") === mVal}
                          onChange={() => updateIntro({ mode: mVal })}
                          className="mt-0.5 h-3.5 w-3.5"
                        />
                        <span>
                          <span className="block font-bold text-slate-900">{title}</span>
                          <span className="block text-[11px] text-slate-500">{desc}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-4 text-xs font-semibold text-slate-700">
                  <span>Aplicar por defecto en:</span>
                  {(
                    [
                      ["apply_to_shorts", "Shorts"],
                      ["apply_to_medium", "Vídeos Medios"],
                      ["apply_to_long", "Vídeos Largos"],
                    ] as ["apply_to_shorts" | "apply_to_medium" | "apply_to_long", string][]
                  ).map(([k, lbl]) => (
                    <label key={k} className="flex cursor-pointer items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={Boolean(ic[k])}
                        onChange={(e) => updateIntro({ [k]: e.target.checked })}
                        className="h-3.5 w-3.5 rounded"
                      />
                      {lbl}
                    </label>
                  ))}
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label htmlFor="ic-template" className="block text-xs font-semibold text-slate-700">
                      Estilo de arranque / pregunta gancho
                    </label>
                    <select
                      id="ic-template"
                      value={ic.hook_template}
                      onChange={(e) => updateIntro({ hook_template: e.target.value })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                    >
                      {INTRO_HOOK_TEMPLATES.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="ic-badge" className="block text-xs font-semibold text-slate-700">
                      Etiqueta superior opcional
                    </label>
                    <input
                      id="ic-badge"
                      type="text"
                      value={ic.badge_text}
                      onChange={(e) => updateIntro({ badge_text: e.target.value })}
                      placeholder="Auto (ej. ¿LO SABÍAS? / DATO CLAVE)"
                      className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-xs"
                    />
                  </div>
                </div>

                <div>
                  <label htmlFor="ic-custom-prompt" className="block text-xs font-semibold text-slate-700">
                    Instrucción adicional para el guion de cortinilla (opcional)
                  </label>
                  <input
                    id="ic-custom-prompt"
                    type="text"
                    value={ic.custom_hook_prompt}
                    onChange={(e) => updateIntro({ custom_hook_prompt: e.target.value })}
                    placeholder="Ej. Usa tono de misterio histórico y menciona siempre el año o protagonista"
                    className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-xs"
                  />
                </div>

                <div>
                  <span className="block text-xs font-semibold text-slate-700">Modo de presentación en pantalla</span>
                  <div className="mt-1.5 grid grid-cols-1 gap-2 sm:grid-cols-2 text-xs">
                    {(
                      [
                        ["multi_phrase", "En varias frases / pantallas secuenciales", "Cambia de tarjeta limpia siguiendo cada bloque de la frase."],
                        ["single_screen", "Todo en una sola pantalla con karaoke", "Muestra todo el texto centrado resaltando cada palabra."],
                      ] as [IntroCurtainDisplayMode, string, string][]
                    ).map(([m, title, desc]) => (
                      <label
                        key={m}
                        className={`flex cursor-pointer items-start gap-2 rounded-xl border p-3 ${
                          ic.display_mode === m
                            ? "border-indigo-500 bg-indigo-50/50 text-slate-900"
                            : "border-slate-200 bg-white text-slate-600"
                        }`}
                      >
                        <input
                          type="radio"
                          name="ic_display_mode"
                          checked={ic.display_mode === m}
                          onChange={() => updateIntro({ display_mode: m })}
                          className="mt-0.5 h-3.5 w-3.5"
                        />
                        <div>
                          <div className="font-bold">{title}</div>
                          <div className="text-[11px] text-slate-500">{desc}</div>
                        </div>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label htmlFor="ic-bg-mode" className="block text-xs font-semibold text-slate-700">
                      Fondo de la cortinilla inicial
                    </label>
                    <select
                      id="ic-bg-mode"
                      value={ic.bg_mode}
                      onChange={(e) => updateIntro({ bg_mode: e.target.value as IntroCurtainBgMode })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                    >
                      <option value="blurred_clip_video">Vídeo del propio clip difuminado + velo color</option>
                      <option value="solid_gradient">Degradado sólido personalizable</option>
                      <option value="custom_image">Imagen de marca del perfil + velo color</option>
                    </select>
                  </div>
                  {ic.bg_mode === "custom_image" && (
                    <div>
                      <label htmlFor="ic-bg-img" className="block text-xs font-semibold text-slate-700">
                        Imagen de fondo (perfil)
                      </label>
                      <select
                        id="ic-bg-img"
                        value={ic.bg_image_id || ""}
                        onChange={(e) => updateIntro({ bg_image_id: e.target.value || null })}
                        className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs font-medium"
                      >
                        <option value="">-- Degradado si no hay imagen --</option>
                        {draft.custom_images.map((im) => (
                          <option key={im.id} value={im.id}>
                            {im.title} ({im.filename})
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-xs">
                  <div>
                    <label htmlFor="ic-c1" className="block font-semibold text-slate-700">Color fondo 1</label>
                    <input
                      id="ic-c1"
                      type="color"
                      value={ic.bg_primary_color}
                      onChange={(e) => updateIntro({ bg_primary_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="ic-c2" className="block font-semibold text-slate-700">Color fondo 2</label>
                    <input
                      id="ic-c2"
                      type="color"
                      value={ic.bg_secondary_color}
                      onChange={(e) => updateIntro({ bg_secondary_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="ic-acc" className="block font-semibold text-slate-700">Acento</label>
                    <input
                      id="ic-acc"
                      type="color"
                      value={ic.accent_color}
                      onChange={(e) => updateIntro({ accent_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="ic-txtc" className="block font-semibold text-slate-700">Texto</label>
                    <input
                      id="ic-txtc"
                      type="color"
                      value={ic.text_color}
                      onChange={(e) => updateIntro({ text_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-6 text-xs font-semibold text-slate-700">
                  <label className="flex cursor-pointer items-center gap-2">
                    <input
                      type="checkbox"
                      checked={ic.tts_enabled}
                      onChange={(e) => updateIntro({ tts_enabled: e.target.checked })}
                      className="h-4 w-4 rounded"
                    />
                    Narrar gancho con Voz IA (silencia podcast en este tramo)
                  </label>
                  <label className="flex cursor-pointer items-center gap-2">
                    <input
                      type="checkbox"
                      checked={ic.transition_sfx}
                      onChange={(e) => updateIntro({ transition_sfx: e.target.checked })}
                      className="h-4 w-4 rounded"
                    />
                    Efecto sonoro sutil Whoosh al entrar al podcast
                  </label>
                </div>
              </div>

              {/* SECTION C: Outro Curtain Configuration */}
              <div className="rounded-2xl border border-slate-200 bg-white p-5 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="text-base font-bold text-slate-900">2. Cortinilla Final de Cierre (Podcast / iVoox / Canal)</h3>
                    <p className="text-xs text-slate-500">
                      Cierra cada Short o Vídeo invitando al canal completo. El audio de voz IA de cierre se genera 1 sola vez por perfil y se reutiliza en caché.
                    </p>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 rounded-xl bg-slate-100 px-3 py-1.5 text-xs font-bold text-slate-800">
                    <input
                      type="checkbox"
                      checked={oc.enabled}
                      onChange={(e) => updateOutro({ enabled: e.target.checked })}
                      className="h-4 w-4 rounded"
                    />
                    Activada
                  </label>
                </div>

                <div className="flex flex-wrap items-center gap-4 text-xs font-semibold text-slate-700">
                  <span>Aplicar por defecto en:</span>
                  {(
                    [
                      ["apply_to_shorts", "Shorts"],
                      ["apply_to_medium", "Vídeos Medios"],
                      ["apply_to_long", "Vídeos Largos"],
                    ] as ["apply_to_shorts" | "apply_to_medium" | "apply_to_long", string][]
                  ).map(([k, lbl]) => (
                    <label key={k} className="flex cursor-pointer items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={Boolean(oc[k])}
                        onChange={(e) => updateOutro({ [k]: e.target.checked })}
                        className="h-3.5 w-3.5 rounded"
                      />
                      {lbl}
                    </label>
                  ))}
                </div>

                <div>
                  <span className="block text-xs font-semibold text-slate-700">Modo de mezcla de audio en el cierre</span>
                  <div className="mt-1.5 grid grid-cols-1 gap-2 sm:grid-cols-2 text-xs">
                    {(
                      [
                        [
                          "no_overlap_lcut",
                          "Sin pisar la voz (Recomendado · L-Cut limpio)",
                          "El cierre arranca justo cuando termina de hablar el podcast; música opcional en L-cut suave.",
                        ],
                        [
                          "duck_podcast_bg",
                          "Podcast bajando gradualmente de fondo",
                          "El podcast baja progresivamente de volumen mientras aparece la ventana final y entra música/voz IA.",
                        ],
                      ] as [OutroAudioMode, string, string][]
                    ).map(([m, title, desc]) => (
                      <label
                        key={m}
                        className={`flex cursor-pointer items-start gap-2 rounded-xl border p-3 ${
                          oc.audio_mode === m
                            ? "border-sky-500 bg-sky-50/50 text-slate-900"
                            : "border-slate-200 bg-white text-slate-600"
                        }`}
                      >
                        <input
                          type="radio"
                          name="oc_audio_mode"
                          checked={oc.audio_mode === m}
                          onChange={() => updateOutro({ audio_mode: m })}
                          className="mt-0.5 h-3.5 w-3.5"
                        />
                        <div>
                          <div className="font-bold">{title}</div>
                          <div className="text-[11px] text-slate-500">{desc}</div>
                        </div>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label htmlFor="oc-dur" className="block text-xs font-semibold text-slate-700">
                      Duración ventana de cierre: {oc.duration_sec.toFixed(1)}s
                    </label>
                    <input
                      id="oc-dur"
                      type="range"
                      min={2.5}
                      max={10.0}
                      step={0.5}
                      value={oc.duration_sec}
                      onChange={(e) => updateOutro({ duration_sec: Number(e.target.value) })}
                      className="mt-2 w-full"
                    />
                  </div>
                  {oc.audio_mode === "duck_podcast_bg" && (
                    <div>
                      <label htmlFor="oc-duck" className="block text-xs font-semibold text-slate-700">
                        Nivel atenuado del podcast de fondo: {oc.podcast_duck_volume_db} dB
                      </label>
                      <input
                        id="oc-duck"
                        type="range"
                        min={-28}
                        max={-8}
                        step={1}
                        value={oc.podcast_duck_volume_db}
                        onChange={(e) => updateOutro({ podcast_duck_volume_db: Number(e.target.value) })}
                        className="mt-2 w-full"
                      />
                    </div>
                  )}
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 text-xs">
                  <div>
                    <label htmlFor="oc-head" className="block font-semibold text-slate-700">Cabecera cierre</label>
                    <input
                      id="oc-head"
                      type="text"
                      value={oc.headline_text}
                      onChange={(e) => updateOutro({ headline_text: e.target.value })}
                      className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2"
                    />
                  </div>
                  <div>
                    <label htmlFor="oc-chan" className="block font-semibold text-slate-700">Canal (si vacío usa podcast)</label>
                    <input
                      id="oc-chan"
                      type="text"
                      value={oc.channel_name_text}
                      onChange={(e) => updateOutro({ channel_name_text: e.target.value })}
                      placeholder={draft.podcast_name}
                      className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2"
                    />
                  </div>
                  <div>
                    <label htmlFor="oc-plat" className="block font-semibold text-slate-700">Plataforma CTA</label>
                    <input
                      id="oc-plat"
                      type="text"
                      value={oc.platform_cta_text}
                      onChange={(e) => updateOutro({ platform_cta_text: e.target.value })}
                      placeholder={`Escúchalo en ${draft.target_platform}`}
                      className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-xs">
                  <div>
                    <label htmlFor="oc-c1" className="block font-semibold text-slate-700">Color cierre 1</label>
                    <input
                      id="oc-c1"
                      type="color"
                      value={oc.bg_primary_color}
                      onChange={(e) => updateOutro({ bg_primary_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="oc-c2" className="block font-semibold text-slate-700">Color cierre 2</label>
                    <input
                      id="oc-c2"
                      type="color"
                      value={oc.bg_secondary_color}
                      onChange={(e) => updateOutro({ bg_secondary_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="oc-acc" className="block font-semibold text-slate-700">Acento cierre</label>
                    <input
                      id="oc-acc"
                      type="color"
                      value={oc.accent_color}
                      onChange={(e) => updateOutro({ accent_color: e.target.value })}
                      className="mt-1 h-9 w-full cursor-pointer rounded-lg border border-slate-300"
                    />
                  </div>
                  <div>
                    <label htmlFor="oc-bgmode" className="block font-semibold text-slate-700">Fondo</label>
                    <select
                      id="oc-bgmode"
                      value={oc.bg_mode}
                      onChange={(e) => updateOutro({ bg_mode: e.target.value as IntroCurtainBgMode })}
                      className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-2 py-2"
                    >
                      <option value="blurred_clip_video">Clip difuminado</option>
                      <option value="solid_gradient">Degradado</option>
                      <option value="custom_image">Imagen propia</option>
                    </select>
                  </div>
                </div>

                <label className="flex cursor-pointer items-center gap-2 text-xs font-semibold text-slate-700">
                  <input
                    type="checkbox"
                    checked={oc.use_profile_outro_cta_logo}
                    onChange={(e) => updateOutro({ use_profile_outro_cta_logo: e.target.checked })}
                    className="h-4 w-4 rounded"
                  />
                  Mostrar imagen de marca con modo «Cierre CTA» subida en Imágenes Propias
                </label>

                {/* Custom outro music vs One-time cached AI Voice CTA */}
                <div className="rounded-xl border border-sky-200 bg-sky-50/40 p-4 space-y-3">
                  <div className="text-xs font-bold text-slate-900">
                    Audio del Cierre (Música personalizada O locución IA generada 1 vez por perfil)
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-white px-3 py-2 text-xs font-semibold text-slate-800 ring-1 ring-slate-300 hover:bg-slate-50">
                      <Music className="h-3.5 w-3.5 text-sky-700" aria-hidden="true" />
                      {oc.custom_audio_filename ? `Cambiar audio propio (${oc.custom_audio_filename})` : "Subir música / jingle de cierre (.mp3/.wav)"}
                      <input
                        type="file"
                        accept=".mp3,.wav,.m4a,.ogg"
                        className="sr-only"
                        onChange={async (e) => {
                          const f = e.target.files?.[0];
                          if (!f) return;
                          const saved = await api.uploadOutroAudio(draft.id, f);
                          onProfilesChanged(profiles.map((p) => (p.id === saved.id ? saved : p)));
                          onAnnounce("Audio propio de cierre guardado.");
                        }}
                      />
                    </label>
                    {oc.custom_audio_filename && (
                      <button
                        type="button"
                        onClick={async () => {
                          const saved = await api.deleteOutroAudio(draft.id);
                          onProfilesChanged(profiles.map((p) => (p.id === saved.id ? saved : p)));
                          onAnnounce("Audio propio de cierre eliminado; volverá a usar locución IA en caché.");
                        }}
                        className="inline-flex items-center gap-1 rounded-xl bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700 ring-1 ring-rose-200 hover:bg-rose-100"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" /> Quitar música y usar Voz IA
                      </button>
                    )}
                  </div>

                  {oc.custom_audio_filename ? (
                    <div className="space-y-2">
                      <audio
                        controls
                        src={api.profileAudioUrl(draft.id, oc.custom_audio_filename)}
                        className="w-full"
                      />
                      <div className="text-xs text-slate-600">
                        Volumen música cierre: {oc.custom_audio_volume_db} dB (al tener audio propio cargado se usa este en vez de la voz IA).
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-2.5">
                      <label className="flex cursor-pointer items-center gap-2 text-xs font-semibold text-slate-800">
                        <input
                          type="checkbox"
                          checked={oc.tts_enabled}
                          onChange={(e) => updateOutro({ tts_enabled: e.target.checked })}
                          className="h-4 w-4 rounded"
                        />
                        Usar la misma Voz IA para locutar el cierre (se genera 1 sola vez y se guarda en caché)
                      </label>

                      {oc.tts_enabled && (
                        <>
                          <div>
                            <label htmlFor="oc-tts-txt" className="block text-xs font-semibold text-slate-700">
                              Texto del cierre IA (si lo dejas vacío se genera automáticamente según tu perfil)
                            </label>
                            <textarea
                              id="oc-tts-txt"
                              rows={2}
                              value={oc.tts_text}
                              onChange={(e) => updateOutro({ tts_text: e.target.value })}
                              placeholder={`Puedes seguir escuchando este podcast completo en ${oc.platform_cta_text || draft.target_platform || "iVoox"}, en el canal ${oc.channel_name_text || draft.podcast_name}.`}
                              className="mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs"
                            />
                          </div>

                          <div className="flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              disabled={outroTtsLoading}
                              onClick={async () => {
                                setOutroTtsLoading(true);
                                setOutroTtsMsg(null);
                                try {
                                  const res = await api.previewProfileOutroTts(draft.id, {
                                    force: false,
                                    tts_text: oc.tts_text,
                                    tts_engine: ic.tts_engine,
                                    tts_voice: ic.tts_voice,
                                    tts_rate: ic.tts_rate,
                                    openrouter_audio_model: ic.openrouter_audio_model,
                                  });
                                  onProfilesChanged(
                                    profiles.map((p) => (p.id === res.profile.id ? res.profile : p)),
                                  );
                                  setOutroAudioPreviewUrl(`${res.url}?t=${Date.now()}`);
                                  setOutroTtsMsg(
                                    res.cached
                                      ? `Reutilizado desde caché (${res.duration_sec.toFixed(1)}s): «${res.text_used}»`
                                      : `Generado y guardado en caché (${res.duration_sec.toFixed(1)}s): «${res.text_used}»`,
                                  );
                                } catch (err) {
                                  setOutroTtsMsg(
                                    err instanceof Error ? err.message : "Error generando audio de cierre IA",
                                  );
                                } finally {
                                  setOutroTtsLoading(false);
                                }
                              }}
                              className="inline-flex items-center gap-1.5 rounded-xl bg-sky-700 px-3.5 py-2 text-xs font-semibold text-white hover:bg-sky-800 disabled:opacity-50"
                            >
                              <Volume2 className="h-3.5 w-3.5" aria-hidden="true" />
                              {outroTtsLoading ? "Generando cierre..." : "Generar / Escuchar cierre IA cacheado"}
                            </button>

                            {oc.cached_tts_filename && (
                              <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-[11px] font-bold text-emerald-800">
                                Cacheado listo ({oc.cached_tts_filename})
                              </span>
                            )}
                          </div>
                          {outroTtsMsg && <p className="text-[11px] font-medium text-sky-900">{outroTtsMsg}</p>}
                          {outroAudioPreviewUrl && (
                            <audio controls src={outroAudioPreviewUrl} className="w-full" />
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Live Preview Panel */}
            <div className="lg:col-span-5">
              <div className="sticky top-6 rounded-2xl border border-slate-200 bg-slate-950 p-4 text-white shadow-lg">
                <div className="flex items-center justify-between gap-2 pb-3">
                  <div className="flex rounded-xl bg-slate-800 p-1 text-xs font-semibold">
                    <button
                      type="button"
                      onClick={() => setCurtainPreviewPane("intro")}
                      className={`rounded-lg px-3 py-1 transition ${
                        curtainPreviewPane === "intro" ? "bg-indigo-600 text-white" : "text-slate-300"
                      }`}
                    >
                      Cortinilla Inicial
                    </button>
                    <button
                      type="button"
                      onClick={() => setCurtainPreviewPane("outro")}
                      className={`rounded-lg px-3 py-1 transition ${
                        curtainPreviewPane === "outro" ? "bg-sky-600 text-white" : "text-slate-300"
                      }`}
                    >
                      Cierre Final
                    </button>
                  </div>
                  <div className="flex rounded-xl bg-slate-800 p-1 text-[11px] font-semibold">
                    <button
                      type="button"
                      onClick={() => setCurtainPreviewAspect("9:16")}
                      className={`rounded-lg px-2 py-1 ${curtainPreviewAspect === "9:16" ? "bg-slate-600 text-white" : "text-slate-400"}`}
                    >
                      9:16
                    </button>
                    <button
                      type="button"
                      onClick={() => setCurtainPreviewAspect("16:9")}
                      className={`rounded-lg px-2 py-1 ${curtainPreviewAspect === "16:9" ? "bg-slate-600 text-white" : "text-slate-400"}`}
                    >
                      16:9
                    </button>
                  </div>
                </div>

                {curtainPreviewPane === "intro" ? (
                  <div
                    className={`mx-auto flex flex-col items-center justify-center overflow-hidden rounded-2xl border border-slate-700 p-5 text-center ${
                      curtainPreviewAspect === "9:16" ? "aspect-[9/16] max-w-[270px]" : "aspect-video w-full"
                    }`}
                    style={{
                      background: `linear-gradient(180deg, ${ic.bg_primary_color}, ${ic.bg_secondary_color})`,
                    }}
                  >
                    <div className="relative w-full rounded-2xl border-2 bg-slate-950/80 px-4 py-6 shadow-xl" style={{ borderColor: ic.accent_color }}>
                      <span
                        className="absolute -top-3 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full px-3 py-0.5 text-[10px] font-extrabold text-slate-950"
                        style={{ backgroundColor: ic.accent_color }}
                      >
                        {(ic.badge_text || "¿LO SABÍAS?").toUpperCase()}
                      </span>
                      <div className="mt-2 font-black leading-snug tracking-tight" style={{ color: ic.text_color, fontSize: curtainPreviewAspect === "9:16" ? "1.05rem" : "1rem" }}>
                        {ic.display_mode === "multi_phrase"
                          ? `${ic.hook_template === "auto" ? "¿Sabías este dato sobre" : ic.hook_template.replace("...", "")} el episodio de hoy?`
                          : sampleText}
                      </div>
                      {ic.display_mode === "multi_phrase" && (
                        <div className="mt-4 flex justify-center gap-1.5">
                          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: ic.accent_color }} />
                          <span className="h-2 w-2 rounded-full bg-white/30" />
                          <span className="h-2 w-2 rounded-full bg-white/30" />
                        </div>
                      )}
                    </div>
                    <p className="mt-3 text-[11px] text-slate-300">
                      {ic.tts_enabled ? `Voz IA (${ic.tts_voice}) · Podcast en silencio` : "Cortinilla visual sin voz IA"}
                    </p>
                  </div>
                ) : (
                  <div
                    className={`mx-auto flex flex-col items-center justify-center overflow-hidden rounded-2xl border border-slate-700 p-5 text-center ${
                      curtainPreviewAspect === "9:16" ? "aspect-[9/16] max-w-[270px]" : "aspect-video w-full"
                    }`}
                    style={{
                      background: `linear-gradient(180deg, ${oc.bg_primary_color}, ${oc.bg_secondary_color})`,
                    }}
                  >
                    <div className="w-full rounded-2xl border-2 bg-slate-950/85 px-4 py-5 shadow-xl" style={{ borderColor: oc.accent_color }}>
                      <div className="text-[10px] font-extrabold uppercase tracking-wider" style={{ color: oc.accent_color }}>
                        {oc.headline_text || "SIGUE EL PODCAST COMPLETO"}
                      </div>
                      <div className="mt-2 text-lg font-black leading-tight" style={{ color: oc.text_color }}>
                        {oc.channel_name_text || draft.podcast_name}
                      </div>
                      <div
                        className="mt-3 inline-block rounded-full px-3.5 py-1 text-[11px] font-bold text-slate-950"
                        style={{ backgroundColor: oc.accent_color }}
                      >
                        {oc.platform_cta_text || `Escúchalo en ${draft.target_platform}`}
                      </div>
                    </div>
                    <p className="mt-3 text-[11px] text-slate-300">
                      {oc.audio_mode === "duck_podcast_bg"
                        ? "Podcast baja gradualmente de fondo"
                        : "L-Cut limpio sin pisar la última frase"}
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Discreet Environment Status Footer */}
      {systemStatus && (
        <div className="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
          <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">
            Estado de herramientas y configuración (.env)
          </h2>
          <div className="mt-2.5 flex flex-wrap gap-3 text-xs">
            {[
              ["OpenRouter IA", systemStatus.openrouter_configured],
              ["Pexels Stock", systemStatus.pexels_configured],
              ["Pixabay Stock", systemStatus.pixabay_configured],
              ["FFmpeg + libass", systemStatus.ffmpeg_ok && systemStatus.libass_ok],
            ].map(([label, ok]) => (
              <span
                key={String(label)}
                className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 font-semibold ${
                  ok ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-800"
                }`}
              >
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                {label}: {ok ? "OK" : "Falta"}
              </span>
            ))}
            <span className="rounded-full bg-slate-100 px-3 py-1 font-medium text-slate-700">
              Encoder: {systemStatus.selected_encoder}
            </span>
            <span className="rounded-full bg-slate-100 px-3 py-1 font-medium text-slate-700">
              Whisper: {systemStatus.whisper_draft_model}/{systemStatus.whisper_precision_model} ({systemStatus.whisper_device})
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
