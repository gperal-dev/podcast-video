import { useEffect, useState } from "react";
import { Activity, Film, Mic2, Sliders, Sparkles } from "lucide-react";
import { api } from "./api/client";
import { GalleryPage } from "./pages/GalleryPage";
import { JobsMonitorPage } from "./pages/JobsMonitorPage";
import { NewProductionPage } from "./pages/NewProductionPage";
import { ProfilesPage } from "./pages/ProfilesPage";
import type { JobState, PodcasterProfile, SystemStatus, WatcherStatus } from "./types";

type Section = "new" | "monitor" | "gallery" | "profiles";

export function App() {
  const [section, setSection] = useState<Section>("new");
  const [profiles, setProfiles] = useState<PodcasterProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>("ejemplo_podcast");
  const [jobs, setJobs] = useState<JobState[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [watcher, setWatcher] = useState<WatcherStatus>({
    running: false,
    profile_id: null,
    input_dir: "input",
    processed_count: 0,
  });
  const [toast, setToast] = useState<string>("");

  const announce = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(""), 3500);
  };

  useEffect(() => {
    Promise.all([
      api.listProfiles(),
      api.listJobs(),
      api.getSystemStatus(),
      api.getWatcher(),
    ])
      .then(([pList, jList, sys, w]) => {
        setProfiles(pList);
        if (pList.length > 0 && !pList.some((p) => p.id === selectedProfileId)) {
          setSelectedProfileId(pList[0].id);
        }
        setJobs(jList);
        if (jList[0]) setSelectedJobId(jList[0].job_id);
        setSystemStatus(sys);
        setWatcher(w);
      })
      .catch(() => {});
  }, []);

  const handleJobCreated = (job: JobState) => {
    setJobs((prev) => [job, ...prev.filter((j) => j.job_id !== job.job_id)]);
    setSelectedJobId(job.job_id);
    setSection("monitor");
  };

  const handleJobUpdated = (job: JobState) => {
    setJobs((prev) => {
      const idx = prev.findIndex((j) => j.job_id === job.job_id);
      if (idx === -1) return [job, ...prev];
      const next = [...prev];
      next[idx] = job;
      return next;
    });
  };

  const runningCount = jobs.filter((j) => j.status === "running" || j.status === "awaiting_review").length;

  const navItems: { id: Section; label: string; icon: typeof Sparkles; badge?: number }[] = [
    { id: "new", label: "Nueva Producción", icon: Sparkles },
    { id: "monitor", label: "Monitor y Revisión", icon: Activity, badge: runningCount || undefined },
    { id: "gallery", label: "Galería de Vídeos", icon: Film },
    { id: "profiles", label: "Perfiles y Estilo", icon: Sliders },
  ];

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 md:flex-row">
      {/* Sidebar */}
      <aside className="w-full shrink-0 bg-slate-900 text-white md:w-64">
        <div className="flex items-center gap-3 border-b border-slate-800 px-5 py-5">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600 shadow-lg shadow-indigo-600/30">
            <Mic2 className="h-5 w-5 text-white" aria-hidden="true" />
          </div>
          <div>
            <div className="text-base font-extrabold tracking-tight">PodcastForge</div>
            <div className="text-[11px] font-medium text-slate-400">Studio Local</div>
          </div>
        </div>

        <nav aria-label="Navegación principal" className="flex gap-1 overflow-x-auto p-3 md:flex-col">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = section === item.id;
            return (
              <button
                key={item.id}
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => setSection(item.id)}
                className={`flex items-center justify-between gap-2.5 rounded-xl px-3.5 py-2.5 text-sm font-semibold whitespace-nowrap transition ${
                  active
                    ? "bg-indigo-600 text-white shadow"
                    : "text-slate-300 hover:bg-slate-800 hover:text-white"
                }`}
              >
                <span className="flex items-center gap-2.5">
                  <Icon className="h-4 w-4" aria-hidden="true" />
                  {item.label}
                </span>
                {item.badge !== undefined && (
                  <span className="rounded-full bg-amber-400 px-2 py-0.5 text-[11px] font-extrabold text-slate-950">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="hidden px-5 py-4 text-xs text-slate-400 md:block">
          {systemStatus && (
            <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-3 space-y-1">
              <div className="font-semibold text-slate-200">Estado Local</div>
              <div>FFmpeg: {systemStatus.ffmpeg_ok ? "OK" : "Falta"}</div>
              <div>Whisper: {systemStatus.whisper_device}</div>
              <div>OpenRouter: {systemStatus.openrouter_configured ? "Conectado" : "Falta clave"}</div>
            </div>
          )}
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 p-5 sm:p-8">
        <div className="mx-auto max-w-6xl">
          {section === "new" && (
            <NewProductionPage
              profiles={profiles}
              selectedProfileId={selectedProfileId}
              onSelectProfile={setSelectedProfileId}
              watcher={watcher}
              onWatcherChange={setWatcher}
              onJobCreated={handleJobCreated}
              onAnnounce={announce}
            />
          )}

          {section === "monitor" && (
            <JobsMonitorPage
              jobs={jobs}
              selectedJobId={selectedJobId}
              onSelectJob={setSelectedJobId}
              onJobUpdated={handleJobUpdated}
              profiles={profiles}
              onOpenGallery={() => setSection("gallery")}
              onAnnounce={announce}
            />
          )}

          {section === "gallery" && <GalleryPage onAnnounce={announce} />}

          {section === "profiles" && (
            <ProfilesPage
              profiles={profiles}
              selectedProfileId={selectedProfileId}
              onSelectProfile={setSelectedProfileId}
              onProfilesChanged={setProfiles}
              systemStatus={systemStatus}
              onAnnounce={announce}
            />
          )}
        </div>
      </main>

      {/* Centralized polite live region */}
      <div
        role="status"
        aria-live="polite"
        className={`fixed right-5 bottom-5 z-50 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white shadow-xl transition ${
          toast ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        {toast}
      </div>
    </div>
  );
}

export default App;
