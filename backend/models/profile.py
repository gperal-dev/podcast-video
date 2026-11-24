"""Pydantic v2 schemas for Podcaster Profiles."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


AspectRatio = Literal["9:16", "16:9", "1:1"]
ImagePriority = Literal["alta", "media", "baja"]
ImageDisplayMode = Literal["fullscreen_zoom", "pip_overlay", "watermark", "outro_cta"]
VisualHookStyle = Literal["kinetic_fullscreen", "editorial_card", "bold_minimal"]
MediaProviderId = Literal["custom_images", "pexels", "pixabay", "wikimedia_commons", "internet_archive"]
TTSEngineId = Literal["edge_tts", "openrouter_audio", "offline_local"]
IntroCurtainBgMode = Literal["blurred_clip_video", "solid_gradient", "custom_image"]
IntroCurtainDisplayMode = Literal["multi_phrase", "single_screen"]
IntroCurtainMode = Literal["auto_if_needed", "always"]
OutroAudioMode = Literal["no_overlap_lcut", "duck_podcast_bg"]

DEFAULT_INTRO_HOOK_TEMPLATES: list[str] = [
    "auto",
    "¿Sabías que...?",
    "¿Sabías este dato de...?",
    "¿A que no sabías...?",
    "¿Cómo funciona...?",
    "Lo que nadie te contó de...",
]

DEFAULT_MEDIA_PROVIDERS: list[MediaProviderId] = [
    "custom_images",
    "pexels",
    "pixabay",
    "wikimedia_commons",
    "internet_archive",
]


class DurationWindow(BaseModel):
    """Minimum and maximum post-cut duration in seconds."""

    min_sec: float = Field(gt=0)
    max_sec: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "DurationWindow":
        if self.max_sec < self.min_sec:
            raise ValueError("max_sec must be greater than or equal to min_sec")
        return self


class QuotaConfig(BaseModel):
    """Default clip quotas and flexible post-cut duration windows."""

    shorts_cliffhanger_count: int = Field(default=3, ge=0, le=30)
    shorts_conclusive_count: int = Field(default=3, ge=0, le=30)
    medium_videos_count: int = Field(default=1, ge=0, le=10)
    long_videos_count: int = Field(default=1, ge=0, le=10)

    shorts_window: DurationWindow = Field(default_factory=lambda: DurationWindow(min_sec=35.0, max_sec=90.0))
    medium_window: DurationWindow = Field(default_factory=lambda: DurationWindow(min_sec=270.0, max_sec=420.0))
    long_window: DurationWindow = Field(default_factory=lambda: DurationWindow(min_sec=600.0, max_sec=840.0))

    allow_short_as_teaser: bool = Field(
        default=True,
        description="Permite que un Short coincida temporalmente con un vídeo medio o largo como avance.",
    )


class AudioEditConfig(BaseModel):
    """Silence trimming, surgical word censorship, loudness, and background music."""

    remove_silences: bool = True
    silence_threshold_db: float = Field(default=-38.0, ge=-70.0, le=-10.0)
    min_silence_sec: float = Field(default=0.7, ge=0.2, le=5.0)
    keep_silence_sec: float = Field(default=0.18, ge=0.0, le=1.5)
    censored_words: list[str] = Field(default_factory=list)
    crossfade_ms: int = Field(default=20, ge=5, le=60)
    target_lufs: float = Field(default=-14.0, ge=-24.0, le=-8.0)
    level_multi_speaker_voices: bool = True
    bg_music_filename: Optional[str] = None
    bg_music_volume_db: float = Field(default=-26.0, ge=-50.0, le=-6.0)


class CustomImage(BaseModel):
    """User-uploaded image asset with editorial usage instructions."""

    id: str
    filename: str
    title: str
    usage_instructions: str = ""
    priority: ImagePriority = "alta"
    display_mode: ImageDisplayMode = "fullscreen_zoom"


class VisualConfig(BaseModel):
    """Visual direction, pacing, stock exclusions, and aspect ratios."""

    visual_tone: str = "oscuro cinematográfico"
    forbidden_stock_keywords: list[str] = Field(default_factory=lambda: ["watermarked", "cartoon", "3d text"])
    short_cut_min_sec: float = Field(default=3.5, ge=1.0, le=10.0)
    short_cut_max_sec: float = Field(default=6.0, ge=1.5, le=15.0)
    long_cut_min_sec: float = Field(default=7.0, ge=2.0, le=20.0)
    long_cut_max_sec: float = Field(default=12.0, ge=3.0, le=30.0)

    enable_public_domain_archives: bool = True
    allowed_media_providers: list[MediaProviderId] = Field(
        default_factory=lambda: list(DEFAULT_MEDIA_PROVIDERS)
    )
    enable_vlm_stock_check: bool = True
    enable_smooth_transitions: bool = True
    transition_sec: float = Field(default=0.25, ge=0.0, le=0.6)

    shorts_aspect: AspectRatio = "9:16"
    medium_aspect: AspectRatio = "16:9"
    long_aspect: AspectRatio = "16:9"
    active_aspects: list[AspectRatio] = Field(default_factory=lambda: ["9:16", "16:9"])


class SubtitleStyleConfig(BaseModel):
    """ASS subtitle styling, karaoke highlight colors, visual hook, and speaker palette."""

    font_name: str = "Montserrat ExtraBold"
    font_file: str = "Montserrat-ExtraBold.ttf"
    font_size_short: int = Field(default=68, ge=24, le=140)
    font_size_long: int = Field(default=52, ge=20, le=120)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#0B0B0E"
    outline_width: float = Field(default=4.0, ge=0.0, le=12.0)
    shadow_depth: float = Field(default=2.0, ge=0.0, le=10.0)
    box_enabled: bool = False
    box_color: str = "#000000"
    box_opacity: float = Field(default=0.55, ge=0.0, le=1.0)
    uppercase: bool = True
    words_per_line_short: int = Field(default=3, ge=1, le=8)
    words_per_line_long: int = Field(default=7, ge=2, le=14)
    hook_prefix_text: str = ""
    cta_text: str = "Escucha el episodio completo en iVoox y Spotify"

    # Full-screen visual hook (replaces subtitles during opening hook segment when enabled)
    visual_hook_enabled: bool = True
    visual_hook_style: VisualHookStyle = "kinetic_fullscreen"
    visual_hook_bg_color: str = "#0B0F1A"
    visual_hook_text_color: str = "#FFFFFF"
    visual_hook_accent_color: str = "#FFD400"
    visual_hook_bg_opacity: float = Field(default=0.72, ge=0.0, le=1.0)

    # Multi-speaker subtitle color highlighting
    speaker_colors_enabled: bool = True
    speaker_palette: list[str] = Field(
        default_factory=lambda: [
            "#FFD400",
            "#38BDF8",
            "#4ADE80",
            "#FB7185",
            "#C084FC",
            "#FB923C",
            "#2DD4BF",
            "#F472B6",
        ]
    )


class IntroCurtainConfig(BaseModel):
    """Configurable opening curtain (visual hook screen + AI voice narration)."""

    enabled: bool = True
    mode: IntroCurtainMode = "auto_if_needed"
    apply_to_shorts: bool = True
    apply_to_medium: bool = True
    apply_to_long: bool = True

    hook_template: str = "auto"
    custom_hook_prompt: str = ""
    display_mode: IntroCurtainDisplayMode = "multi_phrase"

    bg_mode: IntroCurtainBgMode = "blurred_clip_video"
    bg_primary_color: str = "#0F172A"
    bg_secondary_color: str = "#1E1B4B"
    bg_overlay_opacity: float = Field(default=0.68, ge=0.0, le=0.95)
    accent_color: str = "#FFD400"
    text_color: str = "#FFFFFF"
    bg_image_id: Optional[str] = None
    badge_text: str = ""

    tts_enabled: bool = True
    tts_engine: TTSEngineId = "edge_tts"
    tts_voice: str = "es-ES-AlvaroNeural"
    tts_rate: str = "+6%"
    openrouter_audio_model: str = "openai/gpt-audio-mini"

    transition_sfx: bool = True
    min_sec: float = Field(default=2.2, ge=1.2, le=6.0)
    max_sec: float = Field(default=6.2, ge=2.0, le=8.0)


class OutroCurtainConfig(BaseModel):
    """Configurable closing curtain (end screen + profile-cached AI CTA or custom audio)."""

    enabled: bool = True
    apply_to_shorts: bool = True
    apply_to_medium: bool = True
    apply_to_long: bool = True

    duration_sec: float = Field(default=4.5, ge=2.0, le=12.0)
    audio_mode: OutroAudioMode = "no_overlap_lcut"

    bg_mode: IntroCurtainBgMode = "blurred_clip_video"
    bg_primary_color: str = "#090D16"
    bg_secondary_color: str = "#1E293B"
    bg_overlay_opacity: float = Field(default=0.78, ge=0.0, le=0.95)
    accent_color: str = "#38BDF8"
    text_color: str = "#FFFFFF"
    bg_image_id: Optional[str] = None

    headline_text: str = "SIGUE EL PODCAST COMPLETO"
    platform_cta_text: str = ""
    channel_name_text: str = ""
    use_profile_outro_cta_logo: bool = True

    tts_enabled: bool = True
    tts_text: str = ""
    custom_audio_filename: Optional[str] = None
    custom_audio_volume_db: float = Field(default=-3.0, ge=-24.0, le=3.0)
    podcast_duck_volume_db: float = Field(default=-16.0, ge=-30.0, le=-6.0)

    cached_tts_filename: Optional[str] = None
    cached_tts_hash: Optional[str] = None


class PodcasterProfile(BaseModel):
    """Complete persistent profile controlling editorial, audio, visual, and subtitle behavior."""

    id: str
    name: str
    podcast_name: str
    target_platform: str = "iVoox / Spotify / YouTube"
    language: str = "es"
    niche_description: str = ""
    show_tone: str = "conversacional, directo y entretenido"
    editorial_instructions: str = (
        "Prioriza confesiones personales, opiniones polémicas bien argumentadas, "
        "datos sorprendentes, giros narrativos inesperados y consejos accionables."
    )

    quotas: QuotaConfig = Field(default_factory=QuotaConfig)
    audio: AudioEditConfig = Field(default_factory=AudioEditConfig)
    custom_images: list[CustomImage] = Field(default_factory=list)
    visual: VisualConfig = Field(default_factory=VisualConfig)
    subtitles: SubtitleStyleConfig = Field(default_factory=SubtitleStyleConfig)
    intro_curtain: IntroCurtainConfig = Field(default_factory=IntroCurtainConfig)
    outro_curtain: OutroCurtainConfig = Field(default_factory=OutroCurtainConfig)

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
