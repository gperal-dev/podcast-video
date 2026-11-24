import { useEffect, useState } from "react";
import { Eye, Smartphone, Monitor, Square, Sparkles, Captions } from "lucide-react";
import type { AspectRatio, SubtitleStyleConfig } from "../types";

interface SubtitlePreviewProps {
  profileId: string;
  subtitles: SubtitleStyleConfig;
  visualTone: string;
}

const SAMPLE_HOOK_WORDS = ["¿POR", "QUÉ", "NINGÚN", "HISTORIADOR", "CONOCÍA", "ESTE", "SECRETO?"];
const SAMPLE_SPEAKER_TURNS: { speakerLabel: string; speakerIdx: number; words: string[] }[] = [
  { speakerLabel: "Voz 1", speakerIdx: 0, words: ["EN", "1940", "CAMBIARON", "LA", "ORDEN"] },
  { speakerLabel: "Voz 2", speakerIdx: 1, words: ["¡ESO", "SALVÓ", "MILES", "DE", "VIDAS!"] },
];

export function SubtitlePreview({ profileId, subtitles, visualTone }: SubtitlePreviewProps) {
  const [aspect, setAspect] = useState<AspectRatio>("9:16");
  const [activeIdx, setActiveIdx] = useState(1);
  const [turnIdx, setTurnIdx] = useState(0);
  const [previewMode, setPreviewMode] = useState<"hook" | "subs">(
    subtitles.visual_hook_enabled ? "hook" : "subs",
  );
  const [showSafeZones, setShowSafeZones] = useState(true);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setActiveIdx((prev) => {
        const next = prev + 1;
        if (previewMode === "subs" && next % 5 === 0) {
          setTurnIdx((t) => (t + 1) % SAMPLE_SPEAKER_TURNS.length);
        }
        return next % 70;
      });
    }, 600);
    return () => window.clearInterval(timer);
  }, [previewMode]);

  const effectiveMode = !subtitles.visual_hook_enabled && previewMode === "hook" ? "subs" : previewMode;

  const fontId = `pf-preview-font-${subtitles.font_file.replace(/[^a-zA-Z0-9]/g, "_")}`;

  useEffect(() => {
    if (!subtitles.font_file) return;
    const styleId = `style-${fontId}`;
    let el = document.getElementById(styleId) as HTMLStyleElement | null;
    if (!el) {
      el = document.createElement("style");
      el.id = styleId;
      document.head.appendChild(el);
    }
    const url = `/api/fonts/file/${encodeURIComponent(subtitles.font_file)}?profile_id=${encodeURIComponent(profileId)}`;
    el.textContent = `@font-face { font-family: "${fontId}"; src: url("${url}"); font-display: swap; }`;
  }, [fontId, profileId, subtitles.font_file]);

  const isShort = aspect === "9:16";
  const wordsCount = isShort ? subtitles.words_per_line_short : subtitles.words_per_line_long;
  const activeTurn = SAMPLE_SPEAKER_TURNS[turnIdx % SAMPLE_SPEAKER_TURNS.length];
  const visibleWords = activeTurn.words.slice(0, Math.min(activeTurn.words.length, Math.max(2, wordsCount)));

  const palette = subtitles.speaker_palette?.length
    ? subtitles.speaker_palette
    : [subtitles.highlight_color, "#38BDF8", "#4ADE80"];
  const activeSpeakerHighlight = subtitles.speaker_colors_enabled
    ? palette[activeTurn.speakerIdx % palette.length]
    : subtitles.highlight_color;

  const rawFontSize = isShort ? subtitles.font_size_short : subtitles.font_size_long;
  const scaledPx = Math.max(13, Math.round(rawFontSize * (isShort ? 0.30 : 0.25)));

  const boxBg = subtitles.box_enabled
    ? `${subtitles.box_color}${Math.round(subtitles.box_opacity * 255)
        .toString(16)
        .padStart(2, "0")}`
    : "transparent";

  const frameClass =
    aspect === "9:16"
      ? "aspect-[9/16] w-64"
      : aspect === "1:1"
        ? "aspect-square w-72"
        : "aspect-video w-full max-w-md";

  const outlineShadow = subtitles.box_enabled
    ? "none"
    : [
        `-2px -2px 0 ${subtitles.outline_color}`,
        `2px -2px 0 ${subtitles.outline_color}`,
        `-2px 2px 0 ${subtitles.outline_color}`,
        `2px 2px 0 ${subtitles.outline_color}`,
        `0 ${Math.max(2, subtitles.shadow_depth * 2)}px 8px rgba(0,0,0,0.85)`,
      ].join(", ");

  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-900 p-4 text-white">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
          <Eye className="h-4 w-4 text-amber-400" aria-hidden="true" />
          <span>Vista previa en vivo</span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {subtitles.visual_hook_enabled && (
            <div className="flex items-center gap-1 rounded-lg bg-slate-800 p-1">
              <button
                type="button"
                onClick={() => setPreviewMode("hook")}
                className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition ${
                  effectiveMode === "hook" ? "bg-amber-500 text-slate-950 font-bold" : "text-slate-300 hover:text-white"
                }`}
              >
                <Sparkles className="h-3 w-3" aria-hidden="true" />
                Gancho (0–3s)
              </button>
              <button
                type="button"
                onClick={() => setPreviewMode("subs")}
                className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition ${
                  effectiveMode === "subs" ? "bg-indigo-600 text-white" : "text-slate-300 hover:text-white"
                }`}
              >
                <Captions className="h-3 w-3" aria-hidden="true" />
                Subtítulos
              </button>
            </div>
          )}

          <div className="flex items-center gap-1 rounded-lg bg-slate-800 p-1">
            <button
              type="button"
              onClick={() => setAspect("9:16")}
              className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition ${
                aspect === "9:16" ? "bg-indigo-600 text-white" : "text-slate-300 hover:text-white"
              }`}
            >
              <Smartphone className="h-3.5 w-3.5" aria-hidden="true" />
              9:16
            </button>
            <button
              type="button"
              onClick={() => setAspect("16:9")}
              className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition ${
                aspect === "16:9" ? "bg-indigo-600 text-white" : "text-slate-300 hover:text-white"
              }`}
            >
              <Monitor className="h-3.5 w-3.5" aria-hidden="true" />
              16:9
            </button>
            <button
              type="button"
              onClick={() => setAspect("1:1")}
              className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition ${
                aspect === "1:1" ? "bg-indigo-600 text-white" : "text-slate-300 hover:text-white"
              }`}
            >
              <Square className="h-3.5 w-3.5" aria-hidden="true" />
              1:1
            </button>
          </div>
        </div>
      </div>

      <div className="flex flex-col items-center justify-center rounded-xl bg-slate-950/70 p-4">
        <div
          className={`relative overflow-hidden rounded-xl border border-slate-700 bg-gradient-to-br from-slate-800 via-slate-900 to-indigo-950 shadow-2xl ${frameClass}`}
        >
          {/* Ambient visual tone label */}
          <div className="absolute inset-0 flex items-center justify-center opacity-25">
            <span className="px-4 text-center text-xs tracking-widest uppercase text-slate-300">
              {visualTone || "Fondo visual"}
            </span>
          </div>

          {effectiveMode === "hook" && subtitles.visual_hook_enabled ? (
            /* Full-Screen Visual Hook Overlay (ZERO bottom subtitles during hook) */
            <div
              className="absolute inset-0 z-10 flex flex-col items-center justify-center px-5 text-center transition-all"
              style={{
                backgroundColor: subtitles.visual_hook_bg_color || "#0B0F1A",
                opacity: Math.max(0.25, Math.min(0.98, subtitles.visual_hook_bg_opacity ?? 0.72)),
              }}
            >
              <div
                className={`flex w-full flex-col items-center justify-center ${
                  subtitles.visual_hook_style === "editorial_card"
                    ? "rounded-2xl border-2 p-4"
                    : ""
                }`}
                style={{
                  borderColor:
                    subtitles.visual_hook_style === "editorial_card"
                      ? subtitles.visual_hook_accent_color
                      : "transparent",
                }}
              >
                {subtitles.visual_hook_style === "kinetic_fullscreen" && (
                  <div
                    className="mb-3 h-1.5 w-12 rounded-full"
                    style={{ backgroundColor: subtitles.visual_hook_accent_color }}
                  />
                )}
                <div
                  className="flex flex-wrap justify-center gap-x-1.5 gap-y-1 font-extrabold leading-tight"
                  style={{
                    fontFamily: `"${fontId}", sans-serif`,
                    fontSize: `${Math.round(scaledPx * 1.15)}px`,
                  }}
                >
                  {SAMPLE_HOOK_WORDS.map((w, idx) => {
                    const active = idx === activeIdx % SAMPLE_HOOK_WORDS.length;
                    const text = subtitles.visual_hook_style === "kinetic_fullscreen" ? w.toUpperCase() : w;
                    return (
                      <span
                        key={w + idx}
                        className="inline-block transition-transform duration-150"
                        style={{
                          color: active ? subtitles.visual_hook_accent_color : subtitles.visual_hook_text_color,
                          textDecoration:
                            active && subtitles.visual_hook_style === "kinetic_fullscreen" ? "underline" : "none",
                          transform: active ? "scale(1.06)" : "scale(1)",
                        }}
                      >
                        {text}
                      </span>
                    );
                  })}
                </div>
                {subtitles.visual_hook_style === "bold_minimal" && (
                  <div className="mt-4 h-1.5 w-24 overflow-hidden rounded-full bg-white/20">
                    <div
                      className="h-full rounded-full transition-all duration-300"
                      style={{
                        width: `${(((activeIdx % SAMPLE_HOOK_WORDS.length) + 1) / SAMPLE_HOOK_WORDS.length) * 100}%`,
                        backgroundColor: subtitles.visual_hook_accent_color,
                      }}
                    />
                  </div>
                )}
              </div>
              <span className="mt-4 rounded-full bg-black/40 px-2.5 py-0.5 text-[10px] font-medium text-slate-200">
                Gancho pantalla completa • Sin subtítulos inferiores
              </span>
            </div>
          ) : (
            <>
              {/* 9:16 TikTok / Reels Safe Zone guide */}
              {aspect === "9:16" && showSafeZones && (
                <div aria-hidden="true" className="pointer-events-none absolute inset-0">
                  <div className="absolute inset-x-0 bottom-0 h-[18%] border-t border-dashed border-rose-400/60 bg-rose-500/10" />
                  <div className="absolute inset-y-[25%] right-0 w-[15%] border-l border-dashed border-rose-400/60 bg-rose-500/10" />
                  <span className="absolute bottom-1.5 left-2 text-[10px] font-semibold text-rose-300">
                    Zona UI TikTok / Reels (protegida)
                  </span>
                </div>
              )}

              {subtitles.speaker_colors_enabled && (
                <div className="absolute top-3 left-3 flex items-center gap-1.5 rounded-full bg-black/65 px-2.5 py-1 text-[10px] font-semibold">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: activeSpeakerHighlight }}
                  />
                  <span>{activeTurn.speakerLabel}</span>
                </div>
              )}

              {/* Karaoke Subtitles in Safe Margin */}
              <div
                className={`absolute inset-x-4 flex justify-center ${
                  isShort ? "bottom-[22%] pr-6" : "bottom-[10%]"
                }`}
              >
                <div
                  className="rounded-lg px-2.5 py-1 text-center font-extrabold leading-snug transition-all"
                  style={{
                    fontFamily: `"${fontId}", sans-serif`,
                    fontSize: `${scaledPx}px`,
                    backgroundColor: boxBg,
                    textShadow: outlineShadow,
                  }}
                >
                  {visibleWords.map((w, idx) => {
                    const text = subtitles.uppercase ? w.toUpperCase() : w.toLowerCase();
                    const active = idx === activeIdx % visibleWords.length;
                    return (
                      <span
                        key={w + idx}
                        className="mx-1 inline-block transition-transform duration-150"
                        style={{
                          color: active ? activeSpeakerHighlight : subtitles.primary_color,
                          transform: active ? "scale(1.08)" : "scale(1)",
                        }}
                      >
                        {text}
                      </span>
                    );
                  })}
                </div>
              </div>

              {/* CTA footer preview */}
              {subtitles.cta_text && (
                <div className="absolute inset-x-5 bottom-[5%] flex justify-center">
                  <span className="rounded-full bg-black/75 px-2.5 py-0.5 text-[10px] text-slate-200 ring-1 ring-white/20">
                    {subtitles.cta_text}
                  </span>
                </div>
              )}
            </>
          )}
        </div>

        <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs text-slate-300">
          <input
            type="checkbox"
            checked={showSafeZones}
            onChange={(e) => setShowSafeZones(e.target.checked)}
            className="h-4 w-4 rounded"
          />
          Mostrar guías de zona segura TikTok / Reels
        </label>
      </div>
    </div>
  );
}
