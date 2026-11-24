import type {
  AIImagePreviewResponse,
  AspectRatio,
  FontItem,
  GalleryItem,
  ImageDisplayMode,
  ImagePriority,
  JobState,
  OutroTTSPreviewResponse,
  PodcasterProfile,
  SystemStatus,
  TTSPreviewResponse,
  TTSVoiceCatalog,
  WatcherStatus,
} from "../types";

async function jsonReq<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let msg = `Error HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err?.detail) msg = String(err.detail);
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

export const api = {
  getSystemStatus: () => jsonReq<SystemStatus>("/api/system/status"),

  listProfiles: () => jsonReq<PodcasterProfile[]>("/api/profiles"),
  createProfile: (profile: PodcasterProfile) =>
    jsonReq<PodcasterProfile>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    }),
  updateProfile: (profileId: string, profile: PodcasterProfile) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    }),
  duplicateProfile: (profileId: string, newName?: string) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/duplicate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: newName }),
    }),
  deleteProfile: (profileId: string) =>
    jsonReq<{ status: string }>(`/api/profiles/${profileId}`, { method: "DELETE" }),

  uploadProfileImage: async (
    profileId: string,
    file: File,
    meta: { title: string; usage_instructions: string; priority: ImagePriority; display_mode: ImageDisplayMode },
  ): Promise<PodcasterProfile> => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("title", meta.title);
    fd.append("usage_instructions", meta.usage_instructions);
    fd.append("priority", meta.priority);
    fd.append("display_mode", meta.display_mode);
    return jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/images`, { method: "POST", body: fd });
  },

  updateProfileImage: (
    profileId: string,
    imageId: string,
    meta: Partial<{ title: string; usage_instructions: string; priority: ImagePriority; display_mode: ImageDisplayMode }>,
  ) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/images/${imageId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meta),
    }),

  deleteProfileImage: (profileId: string, imageId: string) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/images/${imageId}`, { method: "DELETE" }),

  generateAIImage: (prompt: string, aspect_ratio: AspectRatio = "9:16") =>
    jsonReq<AIImagePreviewResponse>("/api/ai/generate-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, aspect_ratio }),
    }),

  addProfileAIImage: (
    profileId: string,
    payload: {
      preview_filename: string;
      title: string;
      usage_instructions: string;
      priority: ImagePriority;
      display_mode: ImageDisplayMode;
    },
  ) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/images/from-ai`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  listFonts: (profileId?: string) =>
    jsonReq<FontItem[]>(profileId ? `/api/fonts?profile_id=${encodeURIComponent(profileId)}` : "/api/fonts"),

  uploadProfileFont: (profileId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/fonts`, { method: "POST", body: fd });
  },

  uploadBgMusic: (profileId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/bg-music`, { method: "POST", body: fd });
  },

  getTtsVoices: () => jsonReq<TTSVoiceCatalog>("/api/tts/voices"),
  previewTts: (payload: {
    text: string;
    engine: string;
    voice: string;
    rate: string;
    openrouter_audio_model: string;
  }) =>
    jsonReq<TTSPreviewResponse>("/api/tts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  uploadOutroAudio: (profileId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/outro-audio`, { method: "POST", body: fd });
  },
  deleteOutroAudio: (profileId: string) =>
    jsonReq<PodcasterProfile>(`/api/profiles/${profileId}/outro-audio`, { method: "DELETE" }),
  previewProfileOutroTts: (
    profileId: string,
    payload: {
      force?: boolean;
      tts_text?: string;
      tts_engine?: string;
      tts_voice?: string;
      tts_rate?: string;
      openrouter_audio_model?: string;
    } = {},
  ) =>
    jsonReq<OutroTTSPreviewResponse>(`/api/profiles/${profileId}/outro-tts-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  profileAudioUrl: (profileId: string, filename: string) =>
    `/api/profiles/${encodeURIComponent(profileId)}/audio/${encodeURIComponent(filename)}`,

  listJobs: () => jsonReq<JobState[]>("/api/jobs"),
  getJob: (jobId: string) => jsonReq<JobState>(`/api/jobs/${jobId}`),

  uploadAndStartJob: (
    formData: FormData,
    onProgress: (pct: number) => void,
  ): Promise<JobState> =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) {
          onProgress(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as JobState);
          } catch {
            reject(new Error("Respuesta inesperada del servidor."));
          }
        } else {
          try {
            const err = JSON.parse(xhr.responseText);
            reject(new Error(err?.detail || `Error ${xhr.status}`));
          } catch {
            reject(new Error(`Error HTTP ${xhr.status}`));
          }
        }
      };
      xhr.onerror = () => reject(new Error("Error de red subiendo el archivo."));
      xhr.send(formData);
    }),

  cancelJob: (jobId: string) => jsonReq<JobState>(`/api/jobs/${jobId}/cancel`, { method: "POST" }),
  resumeJob: (jobId: string) => jsonReq<JobState>(`/api/jobs/${jobId}/resume`, { method: "POST" }),
  saveReviewEdits: (jobId: string, clips: unknown[]) =>
    jsonReq<JobState>(`/api/jobs/${jobId}/review`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clips }),
    }),
  approveJob: (jobId: string) => jsonReq<JobState>(`/api/jobs/${jobId}/approve`, { method: "POST" }),

  getWatcher: () => jsonReq<WatcherStatus>("/api/watcher"),
  startWatcher: (profileId: string) =>
    jsonReq<WatcherStatus>("/api/watcher/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: profileId, process_existing: false }),
    }),
  stopWatcher: () => jsonReq<WatcherStatus>("/api/watcher/stop", { method: "POST" }),

  getGallery: (episode?: string, clipType?: string) => {
    const q = new URLSearchParams();
    if (episode) q.set("episode", episode);
    if (clipType && clipType !== "all") q.set("clip_type", clipType);
    const qs = q.toString();
    return jsonReq<GalleryItem[]>(`/api/media/gallery${qs ? `?${qs}` : ""}`);
  },
  openOutputFolder: () => jsonReq<{ status: string; path: string }>("/api/media/open-output", { method: "POST" }),
};

export function clipTypeLabel(t: string): string {
  switch (t) {
    case "short_cliffhanger":
      return "Short Cliffhanger (Intriga)";
    case "short_conclusive":
      return "Short Conclusivo (Cerrado)";
    case "medium":
      return "Vídeo Medio (5–6 min)";
    case "long":
      return "Vídeo Largo (12 min)";
    default:
      return t;
  }
}

export function formatDuration(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(rem).padStart(2, "0")}`;
  return `${m}:${String(rem).padStart(2, "0")}`;
}
