export type AspectRatio = "9:16" | "16:9" | "1:1";
export type ImagePriority = "alta" | "media" | "baja";
export type ImageDisplayMode = "fullscreen_zoom" | "pip_overlay" | "watermark" | "outro_cta";
export type ClipType = "short_cliffhanger" | "short_conclusive" | "medium" | "long";
export type VisualHookStyle = "kinetic_fullscreen" | "editorial_card" | "bold_minimal";
export type TTSEngineId = "edge_tts" | "openrouter_audio" | "offline_local";
export type IntroCurtainBgMode = "blurred_clip_video" | "solid_gradient" | "custom_image";
export type IntroCurtainDisplayMode = "multi_phrase" | "single_screen";
export type IntroCurtainMode = "auto_if_needed" | "always";
export type OutroAudioMode = "no_overlap_lcut" | "duck_podcast_bg";

export const INTRO_HOOK_TEMPLATES: { id: string; label: string }[] = [
  { id: "auto", label: "Automático (la IA elige el mejor gancho)" },
  { id: "¿Sabías que...?", label: "¿Sabías que...?" },
  { id: "¿Sabías este dato de...?", label: "¿Sabías este dato de...?" },
  { id: "¿A que no sabías...?", label: "¿A que no sabías...?" },
  { id: "¿Cómo funciona...?", label: "¿Cómo funciona...?" },
  { id: "Lo que nadie te contó de...", label: "Lo que nadie te contó de..." },
];
export type MediaProviderId =
  | "custom_images"
  | "pexels"
  | "pixabay"
  | "wikimedia_commons"
  | "internet_archive";

export const ALL_MEDIA_PROVIDERS: {
  id: MediaProviderId;
  label: string;
  licenseBadge: string;
  desc: string;
}[] = [
  {
    id: "custom_images",
    label: "Imágenes propias y Pre-producción IA",
    licenseBadge: "100% Propio / Libre",
    desc: "Tus fotos del perfil, del episodio e imágenes IA creadas antes de iniciar",
  },
  {
    id: "pexels",
    label: "Pexels Stock (Vídeo y Foto HD)",
    licenseBadge: "Pexels Free License",
    desc: "Licencia libre y gratuita para uso comercial sin atribución obligatoria",
  },
  {
    id: "pixabay",
    label: "Pixabay Stock (Vídeo y Foto HD)",
    licenseBadge: "Pixabay Content License",
    desc: "Licencia libre y gratuita para uso comercial sin atribución obligatoria",
  },
  {
    id: "wikimedia_commons",
    label: "Wikimedia Commons (Archivo Histórico)",
    licenseBadge: "Solo Dominio Público / CC0",
    desc: "Verificación estricta por archivo: rechaza CC-BY, SA, NC y copyright",
  },
  {
    id: "internet_archive",
    label: "Internet Archive (Documental Clásico)",
    licenseBadge: "Solo Dominio Público / CC0",
    desc: "Solo obras históricas verificadas en Dominio Público o CC0 1.0",
  },
];

export interface VisualCreditAudit {
  shot_id?: string;
  asset_id?: string;
  provider?: string;
  source?: string;
  title?: string;
  author?: string;
  license?: string;
  license_name?: string;
  license_url?: string;
  source_url?: string;
  source_page_url?: string;
  verified_public_domain?: boolean;
  requires_license_warning?: boolean;
  license_summary?: string;
  verified_at?: string;
}

export interface StandalonePackagingAudit {
  comprehension_score?: number;
  hook_strength?: number;
  context_clarity?: number;
  resolution_score?: number;
  standalone_summary?: string;
  audit_action?: string;
  audit_issue_fixed?: string;
  who_identified?: string;
  what_and_context?: string;
  opening_4_6s_clear?: boolean;
  needs_ai_intro_curtain?: boolean;
  model_tier_used?: string;
}

export interface AIImagePreviewResponse {
  preview_filename: string;
  preview_url: string;
  model_used: string;
  width: number;
  height: number;
  aspect_ratio: AspectRatio;
}

export interface DurationWindow {
  min_sec: number;
  max_sec: number;
}

export interface QuotaConfig {
  shorts_cliffhanger_count: number;
  shorts_conclusive_count: number;
  medium_videos_count: number;
  long_videos_count: number;
  shorts_window: DurationWindow;
  medium_window: DurationWindow;
  long_window: DurationWindow;
  allow_short_as_teaser: boolean;
}

export interface AudioEditConfig {
  remove_silences: boolean;
  silence_threshold_db: number;
  min_silence_sec: number;
  keep_silence_sec: number;
  censored_words: string[];
  crossfade_ms: number;
  target_lufs: number;
  level_multi_speaker_voices: boolean;
  bg_music_filename: string | null;
  bg_music_volume_db: number;
}

export interface CustomImage {
  id: string;
  filename: string;
  title: string;
  usage_instructions: string;
  priority: ImagePriority;
  display_mode: ImageDisplayMode;
}

export interface VisualConfig {
  visual_tone: string;
  forbidden_stock_keywords: string[];
  short_cut_min_sec: number;
  short_cut_max_sec: number;
  long_cut_min_sec: number;
  long_cut_max_sec: number;
  enable_public_domain_archives: boolean;
  allowed_media_providers?: MediaProviderId[];
  enable_vlm_stock_check: boolean;
  enable_smooth_transitions: boolean;
  transition_sec: number;
  shorts_aspect: AspectRatio;
  medium_aspect: AspectRatio;
  long_aspect: AspectRatio;
  active_aspects: AspectRatio[];
}

export interface SubtitleStyleConfig {
  font_name: string;
  font_file: string;
  font_size_short: number;
  font_size_long: number;
  primary_color: string;
  highlight_color: string;
  outline_color: string;
  outline_width: number;
  shadow_depth: number;
  box_enabled: boolean;
  box_color: string;
  box_opacity: number;
  uppercase: boolean;
  words_per_line_short: number;
  words_per_line_long: number;
  hook_prefix_text: string;
  cta_text: string;
  visual_hook_enabled: boolean;
  visual_hook_style: VisualHookStyle;
  visual_hook_bg_color: string;
  visual_hook_text_color: string;
  visual_hook_accent_color: string;
  visual_hook_bg_opacity: number;
  speaker_colors_enabled: boolean;
  speaker_palette: string[];
}

export interface IntroCurtainConfig {
  enabled: boolean;
  mode?: IntroCurtainMode;
  apply_to_shorts: boolean;
  apply_to_medium: boolean;
  apply_to_long: boolean;
  hook_template: string;
  custom_hook_prompt: string;
  display_mode: IntroCurtainDisplayMode;
  bg_mode: IntroCurtainBgMode;
  bg_primary_color: string;
  bg_secondary_color: string;
  bg_overlay_opacity: number;
  accent_color: string;
  text_color: string;
  bg_image_id?: string | null;
  badge_text: string;
  tts_enabled: boolean;
  tts_engine: TTSEngineId;
  tts_voice: string;
  tts_rate: string;
  openrouter_audio_model: string;
  transition_sfx: boolean;
  min_sec: number;
  max_sec: number;
}

export interface OutroCurtainConfig {
  enabled: boolean;
  apply_to_shorts: boolean;
  apply_to_medium: boolean;
  apply_to_long: boolean;
  duration_sec: number;
  audio_mode: OutroAudioMode;
  bg_mode: IntroCurtainBgMode;
  bg_primary_color: string;
  bg_secondary_color: string;
  bg_overlay_opacity: number;
  accent_color: string;
  text_color: string;
  bg_image_id?: string | null;
  headline_text: string;
  platform_cta_text: string;
  channel_name_text: string;
  use_profile_outro_cta_logo: boolean;
  tts_enabled: boolean;
  tts_text: string;
  custom_audio_filename?: string | null;
  custom_audio_volume_db: number;
  podcast_duck_volume_db: number;
  cached_tts_filename?: string | null;
  cached_tts_hash?: string | null;
}

export interface TTSVoiceEntry {
  id: string;
  label: string;
  gender: string;
}

export interface TTSEngineEntry {
  id: TTSEngineId;
  label: string;
  cost_note: string;
  requires_api_key: boolean;
  configured: boolean;
  voices: TTSVoiceEntry[];
}

export interface TTSVoiceCatalog {
  engines: TTSEngineEntry[];
  sample_text_default: string;
}

export interface TTSPreviewResponse {
  filename: string;
  url: string;
  duration_sec: number;
  cached: boolean;
  engine_used: string;
  voice_used: string;
}

export interface OutroTTSPreviewResponse {
  filename: string;
  url: string;
  duration_sec: number;
  hash: string;
  cached: boolean;
  text_used: string;
  profile: PodcasterProfile;
}

export interface PodcasterProfile {
  id: string;
  name: string;
  podcast_name: string;
  target_platform: string;
  language: string;
  niche_description: string;
  show_tone: string;
  editorial_instructions: string;
  quotas: QuotaConfig;
  audio: AudioEditConfig;
  custom_images: CustomImage[];
  visual: VisualConfig;
  subtitles: SubtitleStyleConfig;
  intro_curtain?: IntroCurtainConfig;
  outro_curtain?: OutroCurtainConfig;
  created_at?: string;
  updated_at?: string;
}

export interface FontItem {
  filename: string;
  display_name: string;
  scope: string;
  path: string;
}

export interface SystemStatus {
  python_version: string;
  openrouter_configured: boolean;
  openrouter_model_fast: string;
  openrouter_model_editorial: string;
  pexels_configured: boolean;
  pixabay_configured: boolean;
  ffmpeg_ok: boolean;
  libass_ok: boolean;
  selected_encoder: string;
  whisper_device: string;
  whisper_compute_type: string;
  whisper_draft_model: string;
  whisper_precision_model: string;
}

export interface PhaseProgress {
  phase: string;
  label: string;
  status: "pending" | "running" | "completed" | "skipped" | "failed";
  progress_pct: number;
  detail: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_sec?: number | null;
  eta_sec?: number | null;
  substep?: string;
  items_done?: number;
  items_total?: number;
}

export interface ChapterMark {
  title: string;
  sentence_id?: string | null;
  orig_time_sec: number;
  edited_time_sec: number;
}

export interface ClipMetadata {
  titles: string[];
  description: string;
  hashtags: string[];
  thumbnail_text: string;
  cliffhanger_question: string;
  hook_text: string;
  chapters: ChapterMark[];
}

export interface StoryboardShot {
  id: string;
  start: number;
  end: number;
  source_type: "custom_image" | "stock_video" | "stock_photo" | "archive_media" | "title_card" | "quote_card";
  custom_image_id?: string | null;
  custom_image_filename?: string | null;
  display_mode: ImageDisplayMode;
  stock_query: string;
  stock_asset_id?: string | null;
  local_media_path?: string | null;
  card_title: string;
  card_subtitle: string;
  reason: string;
}

export interface EditableSubtitleLine {
  index: number;
  start: number;
  end: number;
  text: string;
  speaker?: string | null;
}

export interface EDLPlan {
  clip_id: string;
  orig_duration: number;
  edited_duration: number;
  silences_removed_count: number;
  silences_removed_sec: number;
  music_gaps_removed_count?: number;
  music_gaps_removed_sec?: number;
  censored_hits_count: number;
  censored_terms_matched: string[];
}

export interface ClipCandidate {
  id: string;
  candidate_id?: string | null;
  original_title?: string | null;
  user_rescued?: boolean;
  clip_type: ClipType;
  aspect_ratio: AspectRatio;
  score: number;
  slug: string;
  angle_id?: string;
  angle_theme?: string;
  summary: string;
  hook_reason: string;
  enabled: boolean;
  orig_start: number;
  orig_end: number;
  estimated_post_cut_sec: number;
  narrative_archetype?: string;
  speakers_detected?: string[];
  speaker_color_map?: Record<string, string>;
  visual_hook_enabled?: boolean | null;
  visual_hook_end_sec?: number;
  visual_hook_spoken_text?: string;
  intro_curtain_enabled?: boolean | null;
  intro_curtain_text?: string;
  intro_curtain_phrases?: string[];
  intro_curtain_badge?: string;
  intro_curtain_duration_sec?: number;
  outro_curtain_enabled?: boolean | null;
  outro_curtain_duration_sec?: number;
  visual_credits_audit?: VisualCreditAudit[];
  license_warnings?: string[];
  license_manifest_path?: string | null;
  standalone_packaging?: StandalonePackagingAudit;
  narrative_spans?: {
    start_sentence_id: string;
    end_sentence_id: string;
    orig_start: number;
    orig_end: number;
    role: string;
  }[];
  edl?: EDLPlan | null;
  preview_audio_path?: string | null;
  metadata: ClipMetadata;
  storyboard?: {
    clip_id: string;
    aspect_ratio: AspectRatio;
    duration: number;
    shots: StoryboardShot[];
  } | null;
  subtitle_lines: EditableSubtitleLine[];
  output_filename?: string | null;
  render_completed: boolean;
}

export type ExecutionMode = "auto" | "semi_auto" | "review_before_render";

export interface EditorialBlockItem {
  block_index: number;
  tema?: string;
  resumen_breve?: string;
  interes_general?: number;
  start?: number;
  end?: number;
  is_ad_or_promo?: boolean;
  [key: string]: unknown;
}

export interface RejectedCandidateRecord {
  candidate_id: string;
  title: string;
  clip_type: ClipType;
  score: number;
  orig_start: number;
  orig_end: number;
  reason: string;
  comprehension_score?: number | null;
  context_clarity?: number | null;
  clean_boundaries?: number | null;
  resolution_score?: number | null;
  story_interest_score?: number | null;
  who?: string | null;
  candidate?: ClipCandidate;
}

export interface ModelUsageRecord {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  total_latency_sec?: number;
}

export interface CostMetrics {
  total_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cost_eur: number;
  by_model?: Record<string, ModelUsageRecord>;
}

export interface JobState {
  job_id: string;
  profile_id: string;
  episode_name: string;
  audio_duration_sec: number;
  detected_language: string;
  auto_mode: boolean;
  execution_mode?: ExecutionMode;
  pause_before_render?: boolean;
  topics_confirmed?: boolean;
  requested_shorts_cliffhanger: number;
  requested_shorts_conclusive: number;
  requested_medium: number;
  requested_long: number;
  episode_images: CustomImage[];
  allowed_media_providers?: MediaProviderId[] | null;
  visual_hook_enabled?: boolean | null;
  visual_hook_style?: VisualHookStyle | null;
  intro_curtain_enabled?: boolean | null;
  outro_curtain_enabled?: boolean | null;
  episode_glossary?: Record<string, string>;
  editorial_blocks?: EditorialBlockItem[];
  candidate_pool?: ClipCandidate[];
  rejected_candidates?: RejectedCandidateRecord[];
  live_metrics?: Record<string, number | string>;
  license_warnings?: string[];
  status:
    | "queued"
    | "running"
    | "awaiting_topic_selection"
    | "awaiting_review"
    | "completed"
    | "failed"
    | "cancelled";
  current_phase: string;
  active_step?: string;
  heartbeat_at?: string;
  overall_progress_pct: number;
  phases: PhaseProgress[];
  warnings: string[];
  error_message?: string | null;
  clips: ClipCandidate[];
  output_dir?: string | null;
  output_dir_override?: string | null;
  save_analytics_report?: boolean;
  cost: CostMetrics;
  logs: { ts: string; level: string; message: string }[];
  created_at: string;
  updated_at: string;
}

export interface WatcherStatus {
  running: boolean;
  profile_id: string | null;
  input_dir: string;
  processed_count: number;
}

export interface GalleryItem {
  id: string;
  job_id: string | null;
  folder: string;
  episode_name: string;
  clip_type: ClipType;
  aspect_ratio: AspectRatio;
  score: number;
  duration_sec: number;
  size_bytes: number;
  video_filename: string;
  video_url: string;
  thumbnail_url: string | null;
  license_manifest_url?: string | null;
  summary: string;
  metadata: ClipMetadata;
  narrative_archetype?: string;
  speakers_detected?: string[];
  speaker_color_map?: Record<string, string>;
  visual_hook_enabled?: boolean | null;
  visual_credits_audit?: VisualCreditAudit[];
  license_warnings?: string[];
  standalone_packaging?: StandalonePackagingAudit;
  created_at: string;
}
