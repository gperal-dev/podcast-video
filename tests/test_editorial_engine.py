"""Unit tests for adaptive editorial_engine and visual_director."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.editorial_engine import (
    adjust_bounds_to_window,
    build_time_blocks,
    format_output_filename,
    run_editorial_selection,
)
from backend.core.visual_director import generate_storyboard
from backend.models.job import SentenceSegment, WordTimestamp
from backend.models.profile import CustomImage, DurationWindow, PodcasterProfile


class _MockOpenRouter:
    """Deterministic offline mock for testing editorial selection & storyboard logic."""

    def __init__(self, sentences: list[SentenceSegment]) -> None:
        self.sentences = sentences
        self.calls: list[str] = []
        self.blind_prompts: list[str] = []

    def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(role)
        if "Biblia Visual" in user_prompt or "glossary" in user_prompt:
            return {
                "episode_topic": "Negocios y errores de escalado",
                "era_or_setting": "Actualidad empresarial",
                "niche_category": "negocios",
                "proper_nouns": ["Stripe", "Apple"],
                "glossary": {"Estraip": "Stripe"},
                "visual_bible": {
                    "visual_style": "dark moody corporate documentary",
                    "prefer_historical_archive": False,
                    "forbidden_anachronisms": ["medieval"],
                    "concrete_filmable_motifs": ["startup founder working late office"],
                    "fallback_query": "modern glass office building night",
                },
            }
        if "REPARACIÓN EDITORIAL" in user_prompt:
            return {
                "action": "refine",
                "issue": "Ajustado tramo para contexto completo",
                "refined_spans": [
                    {"start_sentence_id": self.sentences[2].id, "end_sentence_id": self.sentences[12].id, "role": "core"}
                ],
            }
        if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
            self.blind_prompts.append(user_prompt)
            return {
                "who": "Carlos en Stripe",
                "what_and_context": "Un error de escalado casi hace quebrar la startup en 2024.",
                "opening_4_6s_clear": True,
                "opening_issue": "",
                "central_question_or_conflict": "¿Cómo evitar que un error técnico arruine la empresa?",
                "resolution_answered": True,
                "answer_or_mystery_stated": "Queda claro el error y el conflicto.",
                "clean_start": True,
                "clean_end": True,
                "missing_context_items": [],
                "comprehension_score": 9,
                "hook_strength": 9,
                "context_clarity": 9,
                "clean_boundaries": 9,
                "resolution_score": 9,
                "standalone_summary": "Carlos cuenta cómo un fallo técnico puso en riesgo Stripe.",
            }
        if "CORTINILLA DE APERTURA" in system_prompt:
            return {
                "spoken_hook": "En 1940, la ingeniera Beatrice Shilling investigó por qué los Spitfire se paraban en pleno combate.",
                "phrases": [
                    "En 1940, Beatrice Shilling",
                    "investigó por qué los Spitfire",
                    "se paraban en combate.",
                ],
                "badge": "CONTEXTO 1940",
            }
        if "GUION FINAL EDITADO CON TIEMPOS REALES" in user_prompt:
            return {
                "titles": ["Título Post-Corte", "Opción 2", "Opción 3"],
                "summary": "Resumen fiel al guion final editado.",
                "hook_text": "Esto lo cambió todo",
                "description": "Descripción post-corte",
                "hashtags": ["#negocios", "#podcast"],
                "thumbnail_text": "CLAVE FINAL",
                "cliffhanger_question": "",
                "chapters": [
                    {"title": "Inicio real", "time_sec": 0.0},
                    {"title": "Clímax final", "time_sec": 25.0},
                    {"title": "Fuera de rango", "time_sec": 9999.0},
                ],
            }
        if "MODALIDAD: SHORTS CON CLIFFHANGER" in user_prompt:
            return {
                "clips": [
                    {
                        "narrative_archetype": "cliffhanger_70_30",
                        "spans": [{"start_sentence_id": self.sentences[2].id, "end_sentence_id": self.sentences[12].id, "role": "core"}],
                        "score": 93,
                        "summary": "Historia con lazo abierto sobre un error millonario.",
                        "hook_reason": "Arranca con confesión directa",
                        "hook_text": "El error que casi nos arruina",
                        "cliffhanger_question": "¿Cómo lograron salvar la empresa?",
                        "titles": ["Casi perdemos todo por esto", "Error millonario", "La llamada clave"],
                        "description": "Clip de intriga",
                        "hashtags": ["#podcast", "#negocios"],
                        "thumbnail_text": "CASI QUEBRAMOS",
                    }
                ]
            }
        if "MODALIDAD: SHORTS CONCLUSIVOS" in user_prompt:
            return {
                "clips": [
                    {
                        "narrative_archetype": "pregunta_respuesta",
                        "spans": [
                            {"start_sentence_id": self.sentences[18].id, "end_sentence_id": self.sentences[19].id, "role": "question"},
                            {"start_sentence_id": self.sentences[22].id, "end_sentence_id": self.sentences[30].id, "role": "core"},
                        ],
                        "score": 88,
                        "summary": "Pregunta y regla práctica cerrada para negociar.",
                        "hook_reason": "Pregunta directa",
                        "hook_text": "La regla del silencio en ventas",
                        "cliffhanger_question": "",
                        "titles": ["La regla del silencio", "Negocia mejor", "Truco clave"],
                        "description": "Clip conclusivo",
                        "hashtags": ["#ventas", "#podcast"],
                        "thumbnail_text": "REGLA CLAVE",
                    }
                ]
            }
        if "block_index" in user_prompt and "continua_tema_anterior" in user_prompt:
            return {
                "blocks": [
                    {
                        "block_index": 0,
                        "tema": "Emprendimiento",
                        "continua_tema_anterior": False,
                        "interes_general": 9,
                        "potencial_cliffhanger": 9,
                        "potencial_conclusivo": 7,
                        "resumen_breve": "Anécdota inicial",
                    }
                ]
            }
        if "start_block" in user_prompt:
            return {"arcs": [{"start_block": 0, "end_block": 0, "theme": "Emprendimiento"}]}
        if "TRANSCRIPCIÓN DEL TRAMO" in user_prompt:
            return {
                "clips": [
                    {
                        "start_sentence_id": self.sentences[0].id,
                        "end_sentence_id": self.sentences[-1].id,
                        "score": 84,
                        "summary": "Arco completo",
                        "titles": ["Episodio completo destacado"],
                        "chapters": [{"title": "Inicio", "sentence_id": self.sentences[0].id}],
                    }
                ]
            }
        if "storyboard" in system_prompt.lower():
            return {
                "shots": [
                    {
                        "start": 0.0,
                        "end": 4.0,
                        "source_type": "custom_image",
                        "custom_image_id": "img_host",
                        "reason": "Habla del presentador",
                    },
                    {
                        "start": 4.0,
                        "end": 12.0,
                        "source_type": "stock_video",
                        "stock_query": "dark office entrepreneur desk",
                        "reason": "Ambiente de trabajo",
                    },
                ]
            }
        return {}


def _make_sentences(count: int = 80, step_sec: float = 5.0) -> list[SentenceSegment]:
    return [
        SentenceSegment(
            id=f"S{i + 1:04d}",
            start=round(i * step_sec, 2),
            end=round((i + 1) * step_sec - 0.1, 2),
            text=f"Frase número {i + 1} con contenido interesante sobre negocios y tecnología.",
        )
        for i in range(count)
    ]


def test_short_podcast_graceful_adjustment_and_selection() -> None:
    # 400s (~6.6 min) podcast while user asks for 12-min long video -> should warn and adapt cleanly
    sents = _make_sentences(count=80, step_sec=5.0)
    sents[3].text = "Frase número 4 con contenido interesante sobre Estraip, negocios y tecnología."
    profile = PodcasterProfile(id="test", name="Test", podcast_name="Test Show")
    mock_llm = _MockOpenRouter(sents)

    clips, warnings, _, glossary, visual_bible = run_editorial_selection(
        sentences=sents,
        energy_blocks=[{"block_index": 0, "start": 0.0, "end": 400.0, "energy": "alta"}],
        silence_intervals=[(12.0, 14.0), (50.0, 52.0)],
        profile=profile,
        llm=mock_llm,  # type: ignore[arg-type]
        req_cliffhanger=1,
        req_conclusive=1,
        req_medium=0,
        req_long=1,
    )

    assert glossary.get("Estraip") == "Stripe"
    assert visual_bible.get("niche_category") == "negocios"
    assert any("ajustado automáticamente" in w for w in warnings)
    types = {c.clip_type for c in clips}
    assert {"short_cliffhanger", "short_conclusive", "long"}.issubset(types)

    cliff = next(c for c in clips if c.clip_type == "short_cliffhanger")
    assert cliff.metadata.cliffhanger_question != ""
    assert cliff.output_filename is not None and cliff.output_filename.startswith("96_short_cliffhanger_")
    assert profile.quotas.shorts_window.min_sec <= cliff.estimated_post_cut_sec <= profile.quotas.shorts_window.max_sec + 8.0

    concl = next(c for c in clips if c.clip_type == "short_conclusive")
    assert concl.narrative_archetype == "pregunta_respuesta"
    assert len(concl.narrative_spans) == 2
    assert concl.narrative_spans[0].role == "question"


def test_adjust_bounds_to_window_and_filename() -> None:
    sents = _make_sentences(count=40, step_sec=4.0)
    profile = PodcasterProfile(id="t", name="T", podcast_name="T")
    s, e, est = adjust_bounds_to_window(
        start_idx=2,
        end_idx=4,  # only 12s initially -> must grow into 35-65s
        sentences=sents,
        window=DurationWindow(min_sec=35.0, max_sec=65.0),
        silence_intervals=[],
        audio_config=profile.audio,
    )
    assert 35.0 <= est <= 65.0
    assert e > 4 and s == 2
    assert format_output_filename(93, "short_cliffhanger", "El secreto de Apple") == "93_short_cliffhanger_el_secreto_de_apple.mp4"


def test_storyboard_coverage_with_custom_and_outro_images() -> None:
    sents = _make_sentences(count=15, step_sec=4.0)
    profile = PodcasterProfile(
        id="t",
        name="T",
        podcast_name="T",
        custom_images=[
            CustomImage(
                id="img_host",
                filename="host.jpg",
                title="Carlos",
                usage_instructions="Usar cuando hable en primera persona",
                priority="alta",
                display_mode="fullscreen_zoom",
            ),
            CustomImage(
                id="img_outro",
                filename="logo.png",
                title="Logo Podcast",
                usage_instructions="Usar al cierre",
                priority="alta",
                display_mode="outro_cta",
            ),
        ],
    )
    mock_llm = _MockOpenRouter(sents)
    clips, _, _, _, _ = run_editorial_selection(
        sentences=sents,
        energy_blocks=[],
        silence_intervals=[],
        profile=profile,
        llm=mock_llm,  # type: ignore[arg-type]
        req_cliffhanger=1,
        req_conclusive=0,
        req_medium=0,
        req_long=0,
    )
    clip = clips[0]
    clip.mapped_words = [
        WordTimestamp(word="Hola", start=0.2, end=0.6),
        WordTimestamp(word="mundo", start=4.5, end=5.0),
        WordTimestamp(word="final", start=38.0, end=39.5),
    ]
    from backend.models.job import EDLPlan

    clip.edl = EDLPlan(
        clip_id=clip.id,
        orig_clip_start=clip.orig_start,
        orig_clip_end=clip.orig_end,
        orig_duration=40.0,
        edited_duration=40.0,
    )

    sb = generate_storyboard(clip, profile, [], mock_llm)  # type: ignore[arg-type]
    assert sb.shots[0].start == 0.0
    assert sb.shots[-1].end == 40.0
    assert sb.shots[-1].display_mode == "outro_cta"
    assert sb.shots[-1].custom_image_id == "img_outro"
    assert any(sh.custom_image_id == "img_host" for sh in sb.shots)

    blocks = build_time_blocks(sents, [], block_sec=30.0)
    assert len(blocks) >= 2


def test_normalize_sid_and_fractional_score() -> None:
    from backend.core.editorial_engine import _normalize_score, _normalize_sid

    sents = _make_sentences(count=20, step_sec=5.0)
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    assert _normalize_sid("[S0005]", id_to_idx) == "S0005"
    assert _normalize_sid("S5", id_to_idx) == "S0005"
    assert _normalize_sid(5, id_to_idx) == "S0005"
    assert _normalize_score(0.92) == 92
    assert _normalize_score(8.8) == 88
    assert _normalize_score(95) == 95


def test_post_cut_metadata_and_blind_audit_refine() -> None:
    from backend.core.editorial_engine import (
        _audit_and_refine_candidate,
        generate_final_clip_metadata,
        generate_intro_curtain_script,
    )
    from backend.models.job import EDLPlan

    sents = _make_sentences(count=30, step_sec=5.0)
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="T")

    class _RefineLLM(_MockOpenRouter):
        def __init__(self, sents_arg: list[SentenceSegment], fail_verify: bool = False) -> None:
            super().__init__(sents_arg)
            self.fail_verify = fail_verify
            self.audit_passes: list[str] = []
            self.repair_roles: list[str] = []
            self.intro_calls: list[tuple[str, str]] = []

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(role)
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.repair_roles.append(role)
                return {
                    "action": "refine",
                    "issue": "Falta frase intro",
                    "refined_spans": [
                        {"start_sentence_id": "S0004", "end_sentence_id": "S0012", "role": "core"}
                    ],
                }
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                self.blind_prompts.append(user_prompt)
                if len(self.audit_passes) == 0:
                    self.audit_passes.append("initial")
                    if self.fail_verify:
                        return {
                            "who": "DESCONOCIDO",
                            "what_and_context": "DESCONOCIDO",
                            "opening_4_6s_clear": False,
                            "opening_issue": "Sin sujeto ni resolución",
                            "central_question_or_conflict": "",
                            "resolution_answered": False,
                            "clean_start": True,
                            "clean_end": True,
                            "comprehension_score": 4,
                            "hook_strength": 4,
                            "context_clarity": 4,
                            "clean_boundaries": 8,
                            "resolution_score": 4,
                        }
                    return {
                        "who": "Carlos en Stripe",
                        "what_and_context": "Un problema técnico en 2024.",
                        "opening_4_6s_clear": False,
                        "opening_issue": "Falta contexto claro en los primeros 4-6s",
                        "central_question_or_conflict": "¿Cómo se arregló?",
                        "resolution_answered": True,
                        "answer_or_mystery_stated": "Se explica al final",
                        "clean_start": True,
                        "clean_end": True,
                        "missing_context_items": ["Año exacto"],
                        "comprehension_score": 7,
                        "hook_strength": 6,
                        "context_clarity": 7,
                        "clean_boundaries": 8,
                        "resolution_score": 8,
                        "standalone_summary": "Falta frase intro",
                    }
                self.audit_passes.append("verify")
                if self.fail_verify:
                    return {
                        "who": "DESCONOCIDO",
                        "what_and_context": "DESCONOCIDO",
                        "opening_4_6s_clear": False,
                        "opening_issue": "Sigue sin sujeto",
                        "central_question_or_conflict": "",
                        "resolution_answered": False,
                        "clean_start": True,
                        "clean_end": True,
                        "comprehension_score": 9,
                        "hook_strength": 9,
                        "context_clarity": 9,
                        "clean_boundaries": 9,
                        "resolution_score": 9,
                    }
                return {
                    "who": "Carlos en Stripe",
                    "what_and_context": "Un fallo de servidores en 2024.",
                    "opening_4_6s_clear": True,
                    "opening_issue": "",
                    "central_question_or_conflict": "¿Cómo salvaron los pagos?",
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 9,
                    "hook_strength": 9,
                    "context_clarity": 9,
                    "clean_boundaries": 9,
                    "resolution_score": 9,
                    "standalone_summary": "Aprobado en segunda verificación",
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                self.intro_calls.append((role, user_prompt))
                return {
                    "spoken_hook": "En 1940, Beatrice Shilling descubrió por qué se paraban los Spitfire en pleno picado.",
                    "phrases": ["En 1940, Beatrice Shilling", "descubrió por qué los Spitfire", "se paraban en picado."],
                    "badge": "HISTORIA RAF",
                }
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    mock = _RefineLLM(sents, fail_verify=False)
    clips, _, _, gloss, _ = run_editorial_selection(
        sentences=sents,
        energy_blocks=[],
        silence_intervals=[],
        profile=profile,
        llm=mock,  # type: ignore[arg-type]
        req_cliffhanger=1,
        req_conclusive=0,
        req_medium=0,
        req_long=0,
    )
    assert clips[0].start_sentence_id == "S0004"
    assert mock.audit_passes == ["initial", "verify"]
    assert mock.repair_roles == ["editorial"]
    assert "editorial_strong" not in mock.calls
    assert clips[0].standalone_packaging.get("audit_action") == "refine->approve"
    # Blind audit prompt must NEVER leak episode topic, surrounding context, or titles
    assert all(
        "CONTEXTO CERCANO" not in bp and "Negocios y errores" not in bp and "Casi perdemos todo" not in bp
        for bp in mock.blind_prompts
    )

    # Hard fail when who == DESCONOCIDO or resolution_answered == False even if scores look high
    mock_reject = _RefineLLM(sents, fail_verify=True)
    res_rej = _audit_and_refine_candidate(
        cand=clips[0],
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=profile.quotas.shorts_window,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock_reject,  # type: ignore[arg-type]
    )
    assert res_rej is None
    assert "editorial_strong" in mock_reject.repair_roles

    # Test intro curtain generation uses proper_nouns / episode_topic / era_or_setting / missing_context_items
    clip = clips[0].model_copy(update={"clip_type": "medium"})
    clip.edl = EDLPlan(clip_id=clip.id, orig_clip_start=0, orig_clip_end=40, orig_duration=40, edited_duration=40.0)
    clip.mapped_words = [WordTimestamp(word="Hola", start=0.2, end=0.8), WordTimestamp(word="final.", start=35.0, end=38.0)]
    clip.standalone_packaging = {"missing_context_items": ["Nombre de Beatrice Shilling"]}
    vb = {
        "episode_topic": "Beatrice Shilling y los Spitfire",
        "era_or_setting": "Segunda Guerra Mundial 1940",
        "proper_nouns": ["Beatrice Shilling", "Spitfire"],
    }
    ic_res = generate_intro_curtain_script(clip, profile, vb, mock)  # type: ignore[arg-type]
    assert mock.intro_calls[0][0] == "fast"
    assert "Beatrice Shilling" in mock.intro_calls[0][1]
    assert "Segunda Guerra Mundial 1940" in mock.intro_calls[0][1]
    assert "Nombre de Beatrice Shilling" in mock.intro_calls[0][1]
    assert "Beatrice Shilling" in ic_res["spoken_hook"]

    # Test post-cut metadata sync (summary updated in-place + intro_curtain_text accepted + chapter clamping)
    meta = generate_final_clip_metadata(clip, profile, gloss, mock, intro_curtain_text=ic_res["spoken_hook"])  # type: ignore[arg-type]
    assert meta.titles[0] == "Título Post-Corte"
    assert clip.summary == "Resumen fiel al guion final editado."
    assert len(meta.chapters) == 2
    assert all(0.0 <= ch.edited_time_sec <= 32.0 for ch in meta.chapters)


def test_tiered_escalation_to_editorial_strong_only_on_severe_failure() -> None:
    from backend.core.editorial_engine import _audit_and_refine_candidate
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = _make_sentences(count=35, step_sec=5.0)
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="T")

    class _EscalateLLM(_MockOpenRouter):
        def __init__(self, sents_arg: list[SentenceSegment]) -> None:
            super().__init__(sents_arg)
            self.blind_count = 0
            self.repair_roles: list[str] = []

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(role)
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.repair_roles.append(role)
                return {
                    "action": "refine",
                    "issue": "Añadido context_intro con Beatrice Shilling",
                    "refined_spans": [
                        {"start_sentence_id": "S0002", "end_sentence_id": "S0003", "role": "context_intro"},
                        {"start_sentence_id": "S0015", "end_sentence_id": "S0023", "role": "core"},
                    ],
                }
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                self.blind_count += 1
                if self.blind_count == 1:
                    return {
                        "who": "DESCONOCIDO",
                        "what_and_context": "DESCONOCIDO",
                        "opening_4_6s_clear": False,
                        "resolution_answered": False,
                        "clean_start": True,
                        "clean_end": True,
                        "missing_context_items": ["Quién es la ingeniera"],
                        "comprehension_score": 4,
                        "hook_strength": 5,
                        "context_clarity": 3,
                        "clean_boundaries": 8,
                        "resolution_score": 4,
                    }
                return {
                    "who": "Beatrice Shilling",
                    "what_and_context": "En 1940 investiga el fallo del Spitfire en picado.",
                    "opening_4_6s_clear": True,
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "missing_context_items": [],
                    "comprehension_score": 9,
                    "hook_strength": 9,
                    "context_clarity": 9,
                    "clean_boundaries": 9,
                    "resolution_score": 9,
                    "standalone_summary": "Historia completa reparada con contexto inicial.",
                }
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    cand = ClipCandidate(
        id="c1",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=88,
        slug="test",
        start_sentence_id="S0015",
        end_sentence_id="S0023",
        orig_start=sents[14].start,
        orig_end=sents[22].end,
        estimated_post_cut_sec=45.0,
        narrative_spans=[
            NarrativeSpan(
                start_sentence_id="S0015",
                end_sentence_id="S0023",
                orig_start=sents[14].start,
                orig_end=sents[22].end,
                role="core",
            )
        ],
        metadata=ClipMetadata(titles=["Test"]),
    )
    esc_llm = _EscalateLLM(sents)
    repaired = _audit_and_refine_candidate(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=profile.quotas.shorts_window,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=esc_llm,  # type: ignore[arg-type]
    )
    assert repaired is not None
    assert esc_llm.repair_roles == ["editorial_strong"]
    assert repaired.standalone_packaging.get("audit_action") == "strong_refine->approve"
    assert repaired.standalone_packaging.get("model_tier_used") == "editorial_strong"
    assert repaired.standalone_packaging.get("needs_ai_intro_curtain") is False


def test_chronological_no_overlap_blacklist_and_visual_hook_no_dangling() -> None:
    from backend.core.editorial_engine import _detect_blacklisted_indices, _opening_needs_context_bridge, _parse_raw_spans
    from backend.core.video_renderer import compute_visual_hook_segment

    sents = _make_sentences(count=40, step_sec=5.0)
    id_to_idx = {s.id: i for i, s in enumerate(sents)}

    # 1. Disjoint flash-forward cold_open_hook stays at index 0 ahead of chronological spans
    disjoint = {
        "spans": [
            {"start_sentence_id": "S0030", "end_sentence_id": "S0031", "role": "cold_open_hook"},
            {"start_sentence_id": "S0005", "end_sentence_id": "S0006", "role": "context_intro"},
            {"start_sentence_id": "S0012", "end_sentence_id": "S0020", "role": "core"},
        ]
    }
    parsed_disjoint = _parse_raw_spans(disjoint, id_to_idx)
    assert parsed_disjoint == [(29, 30, "cold_open_hook"), (4, 5, "context_intro"), (11, 19, "core")]

    # 2. Edge-overlapping cold_open_hook trims the later chronological span with zero repetition
    raw_edge = {
        "spans": [
            {"start_sentence_id": "S0020", "end_sentence_id": "S0021", "role": "cold_open_hook"},
            {"start_sentence_id": "S0021", "end_sentence_id": "S0029", "role": "core"},
            {"start_sentence_id": "S0005", "end_sentence_id": "S0006", "role": "context_intro"},
        ]
    }
    parsed_edge = _parse_raw_spans(raw_edge, id_to_idx)
    assert parsed_edge == [(19, 20, "cold_open_hook"), (4, 5, "context_intro"), (21, 28, "core")]

    # 3. Interior cold_open_hook demotes cleanly without ripping a hole in the explanation
    raw_interior = {
        "spans": [
            {"start_sentence_id": "S0015", "end_sentence_id": "S0016", "role": "cold_open_hook"},
            {"start_sentence_id": "S0010", "end_sentence_id": "S0025", "role": "core"},
        ]
    }
    parsed_interior = _parse_raw_spans(raw_interior, id_to_idx)
    assert parsed_interior == [(9, 24, "core")]

    # Orphan pronoun opening detector
    vb = {"proper_nouns": ["Beatrice Shilling", "Spitfire"]}
    assert _opening_needs_context_bridge("Bueno, pues esta mujer ya con 29 años recibe el aviso de echarle un vistazo a esto.", vb) is True
    assert _opening_needs_context_bridge("En 1940, Beatrice Shilling descubrió por qué el Spitfire perdía potencia en picado.", vb) is False

    # Blacklisted promo indices inside a span are stripped automatically
    parsed_clean = _parse_raw_spans(
        {"spans": [{"start_sentence_id": "S0001", "end_sentence_id": "S0010", "role": "core"}]},
        id_to_idx,
        blacklisted_indices={0, 1, 5},
    )
    assert parsed_clean == [(2, 4, "core"), (6, 9, "core")]

    # Deterministic promo detection catches paid promo & fan club chatter
    promo_sents = [
        SentenceSegment(id="S0001", start=0.0, end=4.0, text="Ediciones especiales remasterizadas, 5.95 y es tuya."),
        SentenceSegment(id="S0002", start=4.2, end=8.0, text="La tienes en el histórico para fans."),
        SentenceSegment(id="S0003", start=18.0, end=24.0, text="El Supermarine Spitfire tuvo un problema grave en 1940."),
        SentenceSegment(id="S0004", start=24.2, end=30.0, text="Su carburador se paraba en picado."),
        SentenceSegment(id="S0005", start=30.2, end=36.0, text="Beatrice Shilling encontró una arandela clave."),
        SentenceSegment(id="S0006", start=36.2, end=42.0, text="Así salvó cientos de pilotos británicos."),
        SentenceSegment(id="S0007", start=42.2, end=48.0, text="Los alemanes no entendían qué había pasado."),
        SentenceSegment(id="S0008", start=48.2, end=54.0, text="Ese detalle cambió la Batalla de Inglaterra."),
        SentenceSegment(id="S0009", start=54.2, end=60.0, text="Únete al club de fans por tres euros al mes y me despido ya."),
    ]
    blocked = _detect_blacklisted_indices(promo_sents, [], [], {s.id: i for i, s in enumerate(promo_sents)})
    assert 0 in blocked and 1 in blocked and 8 in blocked
    assert 2 not in blocked and 3 not in blocked

    # Visual hook must not cut prematurely on 'Mientras tanto, en el Reino Unido,'
    words = [
        WordTimestamp(word="Mientras", start=0.0, end=0.4),
        WordTimestamp(word="tanto,", start=0.4, end=0.8),
        WordTimestamp(word="en", start=0.85, end=1.0),
        WordTimestamp(word="el", start=1.0, end=1.15),
        WordTimestamp(word="Reino", start=1.15, end=1.5),
        WordTimestamp(word="Unido,", start=1.5, end=2.1),
        WordTimestamp(word="descubrieron", start=2.2, end=2.8),
        WordTimestamp(word="el", start=2.8, end=3.0),
        WordTimestamp(word="punto", start=3.0, end=3.4),
        WordTimestamp(word="flaco.", start=3.4, end=4.0),
    ]
    hook_end, spoken, _ = compute_visual_hook_segment(words)
    assert "punto flaco." in spoken
    assert hook_end >= 3.9

    # Visual hook must NEVER end on dangling prepositions/conjunctions like "de que si"
    dangling_words = [
        WordTimestamp(word="Los", start=0.0, end=0.35),
        WordTimestamp(word="alemanes", start=0.36, end=0.9),
        WordTimestamp(word="se", start=0.92, end=1.1),
        WordTimestamp(word="dieron", start=1.12, end=1.6),
        WordTimestamp(word="cuenta", start=1.62, end=2.1),
        WordTimestamp(word="de", start=2.2, end=2.6),
        WordTimestamp(word="que", start=2.7, end=3.2),
        WordTimestamp(word="si", start=3.3, end=4.9),
        WordTimestamp(word="picaban", start=5.0, end=6.9),
        WordTimestamp(word="brusco.", start=7.0, end=7.6),
    ]
    _, d_spoken, _ = compute_visual_hook_segment(dangling_words, max_hook_sec=4.8)
    assert d_spoken.split()[-1].lower().strip(".,") not in {"de", "que", "si"}


def test_openrouter_reasoning_budget_no_length_retry_and_cancel(tmp_path: Path) -> None:
    import json
    import time
    from types import SimpleNamespace
    import pytest

    from backend.config import get_settings
    from backend.core.diagnostics import (
        EDITORIAL_MODEL_FALLBACKS,
        EDITORIAL_STRONG_MODEL_FALLBACKS,
        FAST_MODEL_FALLBACKS,
    )
    from backend.core.openrouter_client import OpenRouterClient
    from backend.core.pipeline import JobCancelledError

    for lst in (FAST_MODEL_FALLBACKS, EDITORIAL_MODEL_FALLBACKS, EDITORIAL_STRONG_MODEL_FALLBACKS):
        assert "google/gemini-2.0-flash-001" not in lst
        assert "anthropic/claude-sonnet-4" not in lst

    (tmp_path / "_openrouter_pricing.json").write_text(
        json.dumps({"fetched_at": time.time(), "models": {"openai/gpt-4.1": [2.0, 8.0]}}),
        encoding="utf-8",
    )
    cfg = get_settings().model_copy(
        update={
            "openrouter_api_key": "sk-test-dummy",
            "processing_dir": tmp_path,
            "openrouter_model_editorial_strong": "google/gemini-2.5-pro",
        }
    )

    client = OpenRouterClient(cfg)
    assert client.client.max_retries == 0
    assert client._reasoning_params_for_model("google/gemini-2.5-pro", 1600) == (
        2133,
        {"reasoning": {"max_tokens": 533}},
    )
    assert client._reasoning_params_for_model("google/gemini-2.5-flash", 1600) == (
        1600,
        {"reasoning": {"max_tokens": 0}},
    )
    assert client._reasoning_params_for_model("openai/gpt-4.1", 1600) == (1600, None)

    called_models: list[str] = []

    def fake_create(**kwargs: Any) -> Any:
        m = kwargs["model"]
        called_models.append(m)
        if m == "google/gemini-2.5-pro":
            assert kwargs.get("extra_body") == {"reasoning": {"max_tokens": 682}}
            assert kwargs["max_tokens"] == 2048 + 682
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="\n\n"))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2730),
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5),
        )

    client.client.chat.completions.create = fake_create  # type: ignore[method-assign]
    res = client.chat_json("editorial_strong", "sys", "usr", max_tokens=2048)
    assert res == {"ok": True}
    # finish_reason=length must fall back immediately instead of retrying the same starving model
    assert called_models == ["google/gemini-2.5-pro", "google/gemini-2.5-flash"]

    # Cooperative cancellation aborts immediately before/during retries without fallback
    cancelled = [False]

    def check_cancel() -> None:
        if cancelled[0]:
            raise JobCancelledError("cancelado")

    cancel_client = OpenRouterClient(cfg, cancel_check=check_cancel)
    spy_calls: list[str] = []

    def failing_then_cancel(**kwargs: Any) -> Any:
        spy_calls.append(kwargs["model"])
        cancelled[0] = True
        raise RuntimeError("HTTP 503 transient")

    cancel_client.client.chat.completions.create = failing_then_cancel  # type: ignore[method-assign]
    with pytest.raises(JobCancelledError):
        cancel_client.chat_json("editorial", "sys", "usr")
    assert spy_calls == ["deepseek/deepseek-v3.2"]

    with pytest.raises(JobCancelledError):
        cancel_client.chat_json("editorial", "sys", "usr")
    assert spy_calls == ["deepseek/deepseek-v3.2"]


def test_multispan_duration_clamping_and_visual_bible_opening() -> None:
    from backend.core.editorial_engine import (
        _adjust_multispan_to_window,
        _deduplicate_and_audit,
        _run_cold_viewer_test,
    )
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = _make_sentences(count=32, step_sec=5.0)
    sents[4] = SentenceSegment(
        id="S0005",
        start=20.0,
        end=24.9,
        text="En 1940, Beatrice Shilling descubrió por qué ella tenía que rediseñar el carburador del Spitfire.",
    )
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="T")
    win = DurationWindow(min_sec=25.0, max_sec=80.0)

    # 120s 4-span Short candidate must clamp inside [min_sec*0.85, max_sec + 4.0]
    raw_spans = [
        (25, 26, "cold_open_hook"),
        (3, 5, "context_intro"),
        (7, 9, "context_intro"),
        (12, 23, "core"),
    ]
    adj, est = _adjust_multispan_to_window(raw_spans, sents, win, [], profile.audio)
    assert len(adj) >= 2
    assert win.min_sec * 0.85 <= est <= win.max_sec + 4.0

    # _run_cold_viewer_test forwards visual_bible so "ella" alongside "Beatrice Shilling" passes opening check
    vb = {"proper_nouns": ["Beatrice Shilling", "Spitfire"]}
    mock_ok = _MockOpenRouter(sents)

    def _ok_blind(role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "who": "Beatrice Shilling",
            "what_and_context": "En 1940 rediseñó el carburador del Spitfire.",
            "opening_4_6s_clear": True,
            "opening_issue": "",
            "central_question_or_conflict": "¿Cómo resolvió el fallo?",
            "resolution_answered": True,
            "clean_start": True,
            "clean_end": True,
            "missing_context_items": [],
            "comprehension_score": 9,
            "hook_strength": 9,
            "context_clarity": 9,
            "clean_boundaries": 9,
            "resolution_score": 9,
            "standalone_summary": "Beatrice Shilling resolvió el fallo del Spitfire.",
        }

    mock_ok.chat_json = _ok_blind  # type: ignore[method-assign]
    cand_open = ClipCandidate(
        id="c_open",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=90,
        slug="beatrice",
        start_sentence_id="S0005",
        end_sentence_id="S0013",
        orig_start=sents[4].start,
        orig_end=sents[12].end,
        estimated_post_cut_sec=45.0,
        narrative_spans=[
            NarrativeSpan(
                start_sentence_id="S0005",
                end_sentence_id="S0013",
                orig_start=sents[4].start,
                orig_end=sents[12].end,
                role="core",
            )
        ],
        metadata=ClipMetadata(titles=["Beatrice"]),
    )
    ev = _run_cold_viewer_test(cand_open, sents, id_to_idx, mock_ok, visual_bible=vb)  # type: ignore[arg-type]
    assert ev["opening_4_6s_clear"] is True
    assert ev["needs_ai_intro_curtain"] is False

    # _deduplicate_and_audit caps strong escalation budget and emits progress_cb
    strong_repairs = 0

    class _SevereRejectLLM(_MockOpenRouter):
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal strong_repairs
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "resolution_answered": False,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 4,
                    "hook_strength": 4,
                    "context_clarity": 4,
                    "clean_boundaries": 8,
                    "resolution_score": 4,
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                if role == "editorial_strong":
                    strong_repairs += 1
                return {"action": "reject", "issue": "Sin arreglo", "refined_spans": []}
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    cands = [
        ClipCandidate(
            id=f"c_{i}",
            clip_type="short_cliffhanger",
            aspect_ratio="9:16",
            score=90 - i,
            slug=f"c_{i}",
            start_sentence_id=sents[i * 9].id,
            end_sentence_id=sents[i * 9 + 7].id,
            orig_start=sents[i * 9].start,
            orig_end=sents[i * 9 + 7].end,
            estimated_post_cut_sec=40.0,
            narrative_spans=[
                NarrativeSpan(
                    start_sentence_id=sents[i * 9].id,
                    end_sentence_id=sents[i * 9 + 7].id,
                    orig_start=sents[i * 9].start,
                    orig_end=sents[i * 9 + 7].end,
                    role="core",
                )
            ],
            metadata=ClipMetadata(titles=[f"Clip {i}"]),
        )
        for i in range(3)
    ]
    progress_events: list[tuple[float, str]] = []
    _deduplicate_and_audit(
        cands,
        requested=1,
        existing_short_spans=[],
        longform_spans=[],
        allow_short_as_teaser=True,
        clip_type="short_cliffhanger",
        sentences=sents,
        id_to_idx=id_to_idx,
        window=win,
        silence_intervals=[],
        energy_blocks=[],
        profile=profile,
        llm=_SevereRejectLLM(sents),  # type: ignore[arg-type]
        allow_fallback=False,
        progress_cb=lambda pct, msg: progress_events.append((pct, msg)),
        progress_range=(66.0, 78.0),
        stage_label="Auditando",
        max_strong_repairs=1,
    )
    assert len(progress_events) == 3
    assert all(66.0 <= pct <= 78.0 for pct, _ in progress_events)
    assert [p for p, _ in progress_events] == sorted(p for p, _ in progress_events)
    assert strong_repairs == 1


def test_job_manager_recover_orphaned_jobs_and_cancel_resume(tmp_path: Path) -> None:
    import threading
    import time

    from backend.config import get_settings
    from backend.core.pipeline import JobCancelledError, JobManager
    from backend.core.profile_store import ProfileStore

    local_settings = get_settings().model_copy(
        update={
            "profiles_dir": tmp_path / "profiles",
            "processing_dir": tmp_path / "processing",
            "output_dir": tmp_path / "output",
            "cache_stock_dir": tmp_path / "cache" / "stock",
            "input_dir": tmp_path / "input",
        }
    )
    local_settings.ensure_directories()
    store = ProfileStore(local_settings)
    store.save_profile(PodcasterProfile(id="p_test", name="Test", podcast_name="Test Pod"))

    audio_file = tmp_path / "input" / "ep.mp3"
    audio_file.write_bytes(b"ID3")

    jm1 = JobManager(local_settings)
    job = jm1.create_job(profile_id="p_test", source_audio_path=audio_file, episode_name="Ep Test")
    job.status = "running"
    job.current_phase = "editorial"
    job.phases[0].status = "completed"
    job.phases[1].status = "running"
    jm1._save_job(job)

    # Fresh manager simulates server restart -> orphaned running job transitions to failed keeping Phase 1
    jm2 = JobManager(local_settings)
    recovered = jm2.recover_orphaned_jobs()
    assert recovered == 1
    reloaded = jm2.load_job(job.job_id)
    assert reloaded.status == "failed"
    assert reloaded.phases[0].status == "completed"
    assert reloaded.phases[1].status == "failed"
    assert "Reanudar" in (reloaded.error_message or "")

    # Simulate cancelled old thread winding down while resume starts a fresh run
    old_cancel = threading.Event()
    old_cancel.set()
    old_done = threading.Event()
    new_done = threading.Event()

    def fake_run(job_id: str, cancel_ev: threading.Event, log_cb: Any = None, run_id: str | None = None) -> None:
        if run_id == "old_run":
            time.sleep(0.08)
            try:
                jm2._check_cancel(cancel_ev)
            except JobCancelledError:
                with jm2._lock:
                    is_cur = run_id is None or jm2._active_run_ids.get(job_id) == run_id
                if is_cur:
                    st = jm2.load_job(job_id)
                    st.status = "cancelled"
                    jm2._save_job(st)
            finally:
                old_done.set()
            return
        st = jm2.load_job(job_id)
        st.status = "completed"
        jm2._save_job(st)
        new_done.set()

    t_old = threading.Thread(target=fake_run, args=(job.job_id, old_cancel, None, "old_run"), daemon=True)
    with jm2._lock:
        jm2._cancel_flags[job.job_id] = old_cancel
        jm2._active_run_ids[job.job_id] = "old_run"
        jm2._active_threads[job.job_id] = t_old
    t_old.start()

    jm2._run_job_thread = fake_run  # type: ignore[method-assign]
    jm2.start_job_async(job.job_id)
    assert old_done.wait(timeout=3.0)
    assert new_done.wait(timeout=3.0)
    time.sleep(0.05)
    assert jm2.load_job(job.job_id).status == "completed"


def test_cliffhanger_prompt_no_redundant_strong_and_best_effort_fallback() -> None:
    from backend.core.editorial_engine import (
        _audit_and_refine_candidate,
        _deduplicate_and_audit,
        _run_cold_viewer_test,
    )
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = _make_sentences(count=24, step_sec=5.0)
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="T")
    win = DurationWindow(min_sec=25.0, max_sec=80.0)

    # 1. _run_cold_viewer_test prompt for short_cliffhanger must clarify NO exige respuesta final
    captured_prompts: list[str] = []
    cliff_cand = ClipCandidate(
        id="cliff_1",
        clip_type="short_cliffhanger",
        aspect_ratio="9:16",
        score=92,
        slug="cliff_1",
        start_sentence_id="S0001",
        end_sentence_id="S0008",
        orig_start=sents[0].start,
        orig_end=sents[7].end,
        estimated_post_cut_sec=40.0,
        narrative_spans=[
            NarrativeSpan(
                start_sentence_id="S0001",
                end_sentence_id="S0008",
                orig_start=sents[0].start,
                orig_end=sents[7].end,
                role="core",
            )
        ],
        metadata=ClipMetadata(titles=["Intriga"]),
    )

    class _PromptCaptureLLM(_MockOpenRouter):
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                captured_prompts.append(user_prompt)
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    _run_cold_viewer_test(cliff_cand, sents, id_to_idx, _PromptCaptureLLM(sents))  # type: ignore[arg-type]
    assert captured_prompts and "NO exige respuesta final" in captured_prompts[0]

    # 2. _audit_and_refine_candidate must NOT escalate to editorial_strong if eval_a already passed_with_intro_only
    class _IntroOnlyNoEscalateLLM(_MockOpenRouter):
        def __init__(self, sents_arg: list[SentenceSegment]) -> None:
            super().__init__(sents_arg)
            self.repair_roles: list[str] = []

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                return {
                    "who": "Beatrice Shilling",
                    "what_and_context": "Investiga por qué falla el Spitfire en 1940.",
                    "opening_4_6s_clear": False,
                    "opening_issue": "Falta contexto inicial",
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 7,
                    "hook_strength": 6,
                    "context_clarity": 6,
                    "clean_boundaries": 8,
                    "resolution_score": 8,
                    "standalone_summary": "Válido con cortinilla intro.",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.repair_roles.append(role)
                return {"action": "reject", "issue": "Mantener original", "refined_spans": []}
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    intro_llm = _IntroOnlyNoEscalateLLM(sents)
    kept = _audit_and_refine_candidate(
        cand=cliff_cand.model_copy(deep=True),
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=win,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=intro_llm,  # type: ignore[arg-type]
    )
    assert kept is not None
    assert kept.standalone_packaging.get("audit_action") == "approve_needs_intro"
    assert intro_llm.repair_roles == ["editorial"]
    assert "editorial_strong" not in intro_llm.repair_roles

    # 3. _deduplicate_and_audit fallback picks best LLM-curated candidate instead of mechanical "Momento destacado 1"
    class _AllRejectLLM(_MockOpenRouter):
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "resolution_answered": False,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 4,
                    "hook_strength": 4,
                    "context_clarity": 4,
                    "clean_boundaries": 8,
                    "resolution_score": 4,
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {"action": "reject", "issue": "Sin arreglo", "refined_spans": []}
            return super().chat_json(role, system_prompt, user_prompt, **kwargs)

    curated = cliff_cand.model_copy(deep=True)
    picked = _deduplicate_and_audit(
        [curated],
        requested=1,
        existing_short_spans=[],
        longform_spans=[],
        allow_short_as_teaser=True,
        clip_type="short_cliffhanger",
        sentences=sents,
        id_to_idx=id_to_idx,
        window=win,
        silence_intervals=[],
        energy_blocks=[],
        profile=profile,
        llm=_AllRejectLLM(sents),  # type: ignore[arg-type]
        allow_fallback=True,
    )
    assert len(picked) == 1
    assert picked[0].metadata.titles[0] == "Intriga"
    assert picked[0].standalone_packaging.get("audit_action") == "best_effort_needs_intro"
    assert picked[0].standalone_packaging.get("needs_ai_intro_curtain") is True


def test_semantic_topic_deduplication_blocks_cloned_subangles() -> None:
    from backend.core.editorial_engine import (
        _build_global_entity_stopwords,
        _compute_topic_similarity,
        _deduplicate_and_audit,
    )
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = [
        SentenceSegment(
            id=f"S{i+1:04d}",
            start=i * 5.0,
            end=(i + 1) * 5.0 - 0.1,
            text=(
                f"Beatrice Shilling inventó la arandela perforada del carburador Merlin para evitar fallos en picado {i}."
                if i < 16
                else f"Durante su juventud ganó carreras de motos en el circuito de Brooklands a cien millas por hora {i}."
            ),
        )
        for i in range(26)
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="Historia")
    vb = {"proper_nouns": ["Beatrice Shilling", "Spitfire"], "era_or_setting": "Segunda Guerra Mundial 1940"}
    gstop = _build_global_entity_stopwords(vb, profile)

    c1 = ClipCandidate(
        id="c1",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=92,
        slug="arandela-carburador-1",
        angle_id="A1",
        angle_theme="El fallo del carburador Merlin y la arandela",
        summary="Explicación de la arandela perforada en el carburador del motor Merlin.",
        start_sentence_id="S0001",
        end_sentence_id="S0007",
        orig_start=0.0,
        orig_end=34.9,
        estimated_post_cut_sec=34.9,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0007", orig_start=0.0, orig_end=34.9, role="core")],
        metadata=ClipMetadata(titles=["La arandela del carburador Merlin"]),
    )
    # Same sub-angle at different timestamps (S0009..S0015), even different angle_id!
    c2_clone = ClipCandidate(
        id="c2",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=90,
        slug="arandela-carburador-2",
        angle_id="A9",
        angle_theme="La arandela perforada del carburador Merlin",
        summary="Cómo funcionaba la arandela perforada en el carburador del motor Merlin.",
        start_sentence_id="S0009",
        end_sentence_id="S0015",
        orig_start=40.0,
        orig_end=74.9,
        estimated_post_cut_sec=34.9,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0009", end_sentence_id="S0015", orig_start=40.0, orig_end=74.9, role="core")],
        metadata=ClipMetadata(titles=["Cómo la arandela arregló el carburador Merlin"]),
    )
    # Distinct sub-angle (motorcycle racing at Brooklands)
    c3_distinct = ClipCandidate(
        id="c3",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=88,
        slug="carreras-brooklands",
        angle_id="A2",
        angle_theme="Las carreras de motos en el circuito de Brooklands",
        summary="Su juventud batiendo récords de velocidad en motocicleta en Brooklands.",
        start_sentence_id="S0017",
        end_sentence_id="S0024",
        orig_start=80.0,
        orig_end=119.9,
        estimated_post_cut_sec=39.9,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0017", end_sentence_id="S0024", orig_start=80.0, orig_end=119.9, role="core")],
        metadata=ClipMetadata(titles=["La piloto rebelde de Brooklands"]),
    )

    assert _compute_topic_similarity(c1, c2_clone, sents, id_to_idx, gstop) >= 0.55
    assert _compute_topic_similarity(c1, c3_distinct, sents, id_to_idx, gstop) < 0.35

    mock = _MockOpenRouter(sents)
    picked = _deduplicate_and_audit(
        candidates=[c1, c2_clone, c3_distinct],
        requested=2,
        existing_short_spans=[],
        longform_spans=[],
        allow_short_as_teaser=True,
        clip_type="short_conclusive",
        sentences=sents,
        id_to_idx=id_to_idx,
        window=DurationWindow(min_sec=25.0, max_sec=75.0),
        silence_intervals=[],
        energy_blocks=[],
        profile=profile,
        llm=mock,  # type: ignore[arg-type]
        allow_fallback=False,
        global_topic_stopwords=gstop,
    )
    assert len(picked) == 2
    picked_themes = [c.angle_id for c in picked]
    assert picked_themes == ["A1", "A2"]


def test_blind_judge_prioritizes_real_voice_and_validates_intro_curtain() -> None:
    from backend.core.editorial_engine import _audit_and_refine_candidate
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = [
        SentenceSegment(id="S0001", start=0.0, end=7.9, text="En 1940 en Inglaterra la ingeniera Beatrice Shilling detectó el fallo de carburación en los Spitfire."),
        SentenceSegment(id="S0002", start=8.0, end=15.9, text="Ella decidió viajar de noche en su moto para resolver el problema técnico antes de cada combate."),
        SentenceSegment(id="S0003", start=16.0, end=23.9, text="La presión negativa inundaba el motor en pleno descenso."),
        SentenceSegment(id="S0004", start=24.0, end=31.9, text="Con una pieza de latón calibrada resolvió el corte de combustible al instante."),
        SentenceSegment(id="S0005", start=32.0, end=39.9, text="Los pilotos salvaron cientos de vidas gracias al diseño."),
        SentenceSegment(id="S0006", start=40.0, end=47.9, text="Aquel pequeño disco pasó a la historia de la aviación."),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="Historia")
    win = DurationWindow(min_sec=22.0, max_sec=60.0)
    vb = {"episode_topic": "Beatrice Shilling", "era_or_setting": "1940", "proper_nouns": ["Beatrice Shilling", "Spitfire"]}

    # Starts on pronoun "Ella" (S0002..S0006) -> real voice repair expands back to S0001 and passes WITHOUT needing AI intro!
    class _VoiceFirstMock:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                if "S0001" in user_prompt or "Beatrice Shilling detectó" in user_prompt:
                    self.calls.append("judge_pass_real_voice")
                    return {
                        "who": "Beatrice Shilling",
                        "what_and_context": "Fallo en los Spitfire en 1940",
                        "opening_4_6s_clear": True,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "has_spoiler": False,
                        "resolution_answered": True,
                        "clean_start": True,
                        "clean_end": True,
                        "comprehension_score": 9,
                        "hook_strength": 9,
                        "context_clarity": 9,
                        "clean_boundaries": 9,
                        "resolution_score": 9,
                        "standalone_summary": "Beatrice Shilling resuelve el fallo del Spitfire con voz propia del podcast.",
                    }
                self.calls.append("judge_fail_pronoun")
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "Pronombre huérfano 'Ella'",
                    "opening_4_6s_clear": False,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": ["Ella"],
                    "has_spoiler": False,
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 4,
                    "hook_strength": 5,
                    "context_clarity": 4,
                    "clean_boundaries": 8,
                    "resolution_score": 8,
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.calls.append("repair_include_S0001")
                return {
                    "action": "adjust",
                    "issue": "Faltaba S0001 para presentar a Beatrice Shilling",
                    "refined_spans": [{"start_sentence_id": "S0001", "end_sentence_id": "S0006", "role": "core"}],
                }
            return {}

    cand = ClipCandidate(
        id="c_pron",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=85,
        slug="test-voice",
        start_sentence_id="S0002",
        end_sentence_id="S0006",
        orig_start=8.0,
        orig_end=47.9,
        estimated_post_cut_sec=39.9,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0002", end_sentence_id="S0006", orig_start=8.0, orig_end=47.9, role="core")],
        metadata=ClipMetadata(titles=["El límite del Spitfire"]),
    )
    vf = _VoiceFirstMock()
    res_voice = _audit_and_refine_candidate(
        cand=cand.model_copy(deep=True),
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=win,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=vf,  # type: ignore[arg-type]
        visual_bible=vb,
        profile=profile,
    )
    assert res_voice is not None
    assert res_voice.intro_curtain_enabled is False
    assert res_voice.standalone_packaging.get("needs_ai_intro_curtain") is False
    assert "repair_include_S0001" in vf.calls

    # Now simulate body with broken internal splices -> MUST be rejected even if intro could exist!
    class _BrokenSpliceMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                return {
                    "who": "Beatrice Shilling",
                    "what_and_context": "Spitfire",
                    "opening_4_6s_clear": False,
                    "splices_coherent": False,
                    "broken_splices": ["Salto brusco en EMPALME #1"],
                    "orphan_or_meta_refs": [],
                    "has_spoiler": False,
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 4,
                    "hook_strength": 5,
                    "context_clarity": 4,
                    "clean_boundaries": 4,
                    "resolution_score": 7,
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {"action": "reject", "issue": "Cortes internos incoherentes", "refined_spans": []}
            return {}

    cand_broken_2span = cand.model_copy(
        update={
            "narrative_spans": [
                NarrativeSpan(start_sentence_id="S0002", end_sentence_id="S0003", orig_start=sents[1].start, orig_end=sents[2].end, role="core"),
                NarrativeSpan(start_sentence_id="S0005", end_sentence_id="S0006", orig_start=sents[4].start, orig_end=sents[5].end, role="payoff"),
            ]
        }
    )
    rejected = _audit_and_refine_candidate(
        cand=cand_broken_2span,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=win,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=_BrokenSpliceMock(),  # type: ignore[arg-type]
        visual_bible=vb,
        profile=profile,
    )
    assert rejected is None


def test_vision_uses_detail_low_and_skips_deepseek(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from backend.config import get_settings
    from backend.core.diagnostics import VISION_MODEL_FALLBACKS
    from backend.core.openrouter_client import OpenRouterClient

    cfg = get_settings().model_copy(
        update={
            "openrouter_api_key": "sk-test-dummy",
            "openrouter_model_editorial": "deepseek/deepseek-v3.2",
            "processing_dir": tmp_path,
        }
    )
    client = OpenRouterClient(cfg)
    assert client._candidate_models("editorial", vision=False)[0] == "deepseek/deepseek-v3.2"
    vis_models = client._candidate_models("fast", vision=True)
    assert all(not m.startswith("deepseek/") for m in vis_models)
    assert vis_models[:3] == VISION_MODEL_FALLBACKS[:3]

    captured_kwargs: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"selected_index": 1}'))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=10),
        )

    client.client.chat.completions.create = fake_create  # type: ignore[method-assign]
    out = client.chat_vision_json("fast", "sys", "pick frame", [b"fakejpegbytes"])
    assert out == {"selected_index": 1}
    assert not str(captured_kwargs["model"]).startswith("deepseek/")
    user_content = captured_kwargs["messages"][1]["content"]
    img_part = [p for p in user_content if p.get("type") == "image_url"][0]
    assert img_part["image_url"]["detail"] == "low"


def test_strict_glossary_filters_verbose_entries_and_applies_to_sentences() -> None:
    from backend.core.editorial_engine import (
        apply_glossary_to_sentences,
        extract_episode_glossary_and_bible,
    )

    sents = [
        SentenceSegment(id="S0001", start=0.0, end=4.0, text="Beatriz Silling trabajó en el Spitfire Mk I con Roy Fedden."),
        SentenceSegment(id="S0002", start=4.0, end=8.0, text="En Farnborou probo el motor Merlín."),
    ]
    profile = PodcasterProfile(id="t", name="T", podcast_name="Historia")

    class _GlossaryMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "glossary": {
                    "Beatriz Silling": "Beatrice Shilling",
                    "Farnborou": "Farnborough",
                    # Verbose sentence/explanation -> MUST be rejected by strict validator!
                    "Beatrice Shilling creó un dispositivo limitador famoso": "Explicación demasiado larga que no es un nombre propio válido en glosario",
                    "Roy Fedden": "Roy Fedden",
                },
                "visual_bible": {
                    "episode_topic": "Beatrice Shilling",
                    "era_or_setting": "1940",
                    "visual_style": "WWII documentary",
                    "proper_nouns": ["Beatrice Shilling", "Supermarine Spitfire", "Esta frase larguísima inventada que supera cinco palabras"],
                },
            }

    gloss, vb = extract_episode_glossary_and_bible(sents, [], profile, _GlossaryMock())  # type: ignore[arg-type]
    assert gloss == {"Beatriz Silling": "Beatrice Shilling", "Farnborou": "Farnborough"}
    assert "Beatrice Shilling" in vb["proper_nouns"]
    assert all(len(str(x).split()) <= 5 for x in vb["proper_nouns"])

    updated = apply_glossary_to_sentences(sents, gloss)
    assert "Beatrice Shilling trabajó" in updated[0].text
    assert "Farnborough" in updated[1].text


def test_local_thumbnail_scorer_prefers_sharp_balanced_frame(tmp_path: Path) -> None:
    import numpy as np
    from PIL import Image
    from backend.core.video_renderer import _score_thumbnail_local

    dark_blurry = tmp_path / "dark_blurry.jpg"
    sharp_balanced = tmp_path / "sharp_balanced.jpg"

    # Dark flat blurry frame
    arr_dark = np.full((360, 640, 3), 18, dtype=np.uint8)
    Image.fromarray(arr_dark).save(dark_blurry, quality=85)

    # Sharp high-contrast well-exposed checkerboard pattern
    yy, xx = np.mgrid[0:360, 0:640]
    pattern = np.where(((xx // 12 + yy // 12) % 2) == 0, 200, 60).astype(np.uint8)
    rgb = np.stack([pattern, pattern, pattern], axis=-1)
    Image.fromarray(rgb).save(sharp_balanced, quality=90)

    s_dark = _score_thumbnail_local(dark_blurry)
    s_sharp = _score_thumbnail_local(sharp_balanced)
    assert s_sharp > s_dark * 3.0
    assert s_sharp > 0.65


def test_blind_judge_step_c_validates_ai_intro_when_who_unknown_in_raw_opening() -> None:
    from backend.core.editorial_engine import _audit_and_refine_candidate
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = [
        SentenceSegment(id="S0001", start=0.0, end=8.0, text="Esta chica decidió montar su taller de motores en la campiña inglesa con su propio sueldo."),
        SentenceSegment(id="S0002", start=8.1, end=16.0, text="Compitió en Brooklands a más de cien millas por hora derrotando a pilotos veteranos"),
        SentenceSegment(id="S0003", start=16.1, end=24.5, text="Por eso recibió la estrella de oro del circuito antes de que estallara la guerra."),
        SentenceSegment(id="S0004", start=24.6, end=33.0, text="Ese palmarés la llevó directamente al centro aeronáutico secreto en Farnborough."),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="t", name="T", podcast_name="Historia")
    win = DurationWindow(min_sec=20.0, max_sec=60.0)
    vb = {
        "episode_topic": "Beatrice Shilling",
        "era_or_setting": "Reino Unido años 30",
        "proper_nouns": ["Beatrice Shilling", "Brooklands", "Farnborough"],
    }

    class _StepCAIMock:
        def __init__(self, intro_passes: bool) -> None:
            self.intro_passes = intro_passes
            self.roles_seen: list[str] = []
            self.intro_calls = 0

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                if "[INTRO IA]:" in user_prompt:
                    if self.intro_passes:
                        return {
                            "who": "Beatrice Shilling",
                            "what_and_context": "Piloto e ingeniera británica de los años 30 en Brooklands.",
                            "opening_4_6s_clear": True,
                            "splices_coherent": True,
                            "broken_splices": [],
                            "orphan_or_meta_refs": [],
                            "has_spoiler": False,
                            "resolution_answered": True,
                            "clean_start": True,
                            "clean_end": True,
                            "comprehension_score": 9,
                            "hook_strength": 9,
                            "context_clarity": 9,
                            "clean_boundaries": 9,
                            "resolution_score": 9,
                            "standalone_summary": "La historia de Beatrice Shilling compitiendo en Brooklands hasta entrar en Farnborough.",
                        }
                    return {
                        "who": "DESCONOCIDO",
                        "what_and_context": "DESCONOCIDO",
                        "opening_4_6s_clear": False,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "has_spoiler": False,
                        "resolution_answered": True,
                        "clean_start": True,
                        "clean_end": True,
                        "comprehension_score": 5,
                        "hook_strength": 5,
                        "context_clarity": 4,
                        "clean_boundaries": 8,
                        "resolution_score": 8,
                    }
                # Raw podcast audio starts with "Esta chica..." -> who=DESCONOCIDO, body sound!
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "opening_issue": "Arranca con 'Esta chica' sin nombrar a Beatrice Shilling.",
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": ["Esta chica"],
                    "has_spoiler": False,
                    "resolution_answered": True,
                    "clean_start": True,
                    "clean_end": True,
                    "comprehension_score": 7,
                    "hook_strength": 7,
                    "context_clarity": 5,
                    "clean_boundaries": 9,
                    "resolution_score": 9,
                    "standalone_summary": "Una ingeniera gana carreras en Brooklands y entra en Farnborough.",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.roles_seen.append(role)
                return {"action": "reject", "issue": "El tramo no nombra explícitamente a Shilling antes de S0001", "refined_spans": []}
            if "CORTINILLA DE APERTURA" in system_prompt:
                self.intro_calls += 1
                return {
                    "spoken_hook": "En los años treinta, Beatrice Shilling corrió en Brooklands antes de entrar en aeronáutica.",
                    "phrases": ["En los años treinta", "Beatrice Shilling corrió", "en Brooklands."],
                    "badge": "ORÍGENES",
                }
            return {}

    cand = ClipCandidate(
        id="c_stepc",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=89,
        slug="brooklands-story",
        start_sentence_id="S0001",
        end_sentence_id="S0004",
        orig_start=0.0,
        orig_end=33.0,
        estimated_post_cut_sec=33.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0004", orig_start=0.0, orig_end=33.0, role="core")],
        metadata=ClipMetadata(titles=["La piloto de Brooklands"]),
    )

    mock_ok = _StepCAIMock(intro_passes=True)
    res_ok = _audit_and_refine_candidate(
        cand=cand.model_copy(deep=True),
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=win,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock_ok,  # type: ignore[arg-type]
        visual_bible=vb,
        profile=profile,
    )
    assert res_ok is not None
    assert res_ok.intro_curtain_enabled is True
    assert "Beatrice Shilling" in res_ok.intro_curtain_text
    assert mock_ok.roles_seen == ["editorial"]
    assert mock_ok.intro_calls == 1

    mock_bad = _StepCAIMock(intro_passes=False)
    res_bad = _audit_and_refine_candidate(
        cand=cand.model_copy(deep=True),
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=win,
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock_bad,  # type: ignore[arg-type]
        visual_bible=vb,
        profile=profile,
    )
    assert res_bad is None
    assert mock_bad.intro_calls == 2


def test_format_sentences_block_with_durations() -> None:
    sents = [
        SentenceSegment(id="S0001", text="Primera frase de prueba.", start=1.0, end=5.5, words=[]),
        SentenceSegment(id="S0002", text="Segunda frase.", start=5.5, end=8.0, words=[]),
    ]
    from backend.core.editorial_engine import _format_sentences_block

    plain = _format_sentences_block(sents)
    with_dur = _format_sentences_block(sents, include_durations=True)
    assert "[S0001] Primera frase de prueba." in plain
    assert "| 4.5s]" not in plain
    assert "[S0001 | 4.5s] Primera frase de prueba." in with_dur
    assert "[S0002 | 2.5s] Segunda frase." in with_dur


def test_parse_raw_spans_preserves_payoff_over_four_spans() -> None:
    from backend.core.editorial_engine import _parse_raw_spans

    sents = [
        SentenceSegment(id=f"S{i:04d}", text=f"Frase {i}.", start=float(i * 2), end=float(i * 2 + 1.8), words=[])
        for i in range(1, 15)
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    raw = {
        "spans": [
            {"start_sentence_id": "S0001", "end_sentence_id": "S0010", "role": "core"},
            {"start_sentence_id": "S0012", "end_sentence_id": "S0014", "role": "payoff"},
        ],
        # Split the core span into 5 sub-spans (even indices dropped -> 5 single-sentence runs) + 1 payoff = 6 spans total
        "drop_sentence_ids": ["S0002", "S0004", "S0006", "S0008"],
    }
    parsed = _parse_raw_spans(raw, id_to_idx, allow_payoff_role=True)
    assert len(parsed) == 6
    assert parsed[-1][2] == "payoff"
    assert sents[parsed[-1][0]].id == "S0012"
    assert sents[parsed[-1][1]].id == "S0014"


def test_cold_viewer_combined_intro_allows_pronoun_opening() -> None:
    from backend.core.editorial_engine import _run_cold_viewer_test
    from backend.models.job import ClipCandidate

    sents = [
        SentenceSegment(
            id="S0001",
            text="esta mujer patentó un sistema de salto de frecuencias sin recibir ni un céntimo",
            start=10.0,
            end=15.0,
            words=[],
        ),
        SentenceSegment(
            id="S0002",
            text="décadas después ese invento dio origen al wifi moderno que usas cada día",
            start=15.1,
            end=21.0,
            words=[],
        ),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    cand = ClipCandidate(
        id="clip_wifi_test",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=85,
        slug="hedy-lamarr-wifi",
        titles=["La actriz que inventó el WiFi"],
        summary="Hedy Lamarr creó la base del WiFi.",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=10.0,
        orig_end=21.0,
        estimated_post_cut_sec=14.0,
        intro_curtain_text="En 1941, la actriz Hedy Lamarr diseñó en secreto un invento militar clave.",
    )

    class _PronounFalsePositiveMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "who": "Hedy Lamarr",
                "what_and_context": "Inventó el salto de frecuencias en 1941 base del WiFi",
                "opening_4_6s_clear": True,
                "opening_issue": "",
                "central_question_or_conflict": "Cómo pasó de actriz a pionera del WiFi",
                "resolution_answered": True,
                "has_spoiler": False,
                "answer_or_mystery_stated": "Su patente originó el WiFi moderno.",
                "clean_start": True,
                "clean_end": True,
                "splices_coherent": True,
                "broken_splices": [],
                "orphan_or_meta_refs": ["'esta mujer' al inicio del audio"],
                "missing_context_items": [],
                "edit_operations": [],
                "comprehension_score": 8,
                "hook_strength": 8,
                "context_clarity": 8,
                "clean_boundaries": 8,
                "resolution_score": 8,
                "standalone_summary": "Hedy Lamarr inventó la base del WiFi.",
            }

    audit = _run_cold_viewer_test(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        llm=_PronounFalsePositiveMock(),  # type: ignore[arg-type]
        include_intro_curtain=True,
    )
    assert audit["orphan_or_meta_refs"] == []
    assert audit["context_clarity"] >= 75
    assert audit["passed_full"] is True


def test_resolve_clip_intro_curtain_enabled_honors_editorial_flag() -> None:
    from backend.core.pipeline import _resolve_clip_intro_curtain_enabled
    from backend.models.job import ClipCandidate
    from backend.models.profile import IntroCurtainConfig

    cfg = IntroCurtainConfig(enabled=True, mode="auto_if_needed", apply_to_shorts=True)
    clip_true = ClipCandidate(
        id="clip_true",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=80,
        slug="clip-true",
        titles=["T"],
        summary="S",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=0.0,
        orig_end=15.0,
        intro_curtain_enabled=True,
        intro_curtain_text="Contexto IA.",
    )
    clip_false = clip_true.model_copy(update={"id": "clip_false", "intro_curtain_enabled": False, "intro_curtain_text": ""})

    en_t, reason_t = _resolve_clip_intro_curtain_enabled(None, cfg, clip_true)
    en_f, reason_f = _resolve_clip_intro_curtain_enabled(None, cfg, clip_false)
    assert en_t is True and "editorial" in reason_t
    assert en_f is False and "voz propia" in reason_f


def test_escape_ffmpeg_filter_path_handles_windows_colon_and_single_quote() -> None:
    from backend.core.video_renderer import _escape_ffmpeg_filter_path

    p = Path("C:/Users/John's Clips/sub_file.ass")
    escaped = _escape_ffmpeg_filter_path(p)
    assert "\\:" in escaped
    assert "\\'" in escaped
    assert "C\\:" in escaped


def test_nle_repair_and_intro_salvages_mid_episode_whisper_clip() -> None:
    """Mid-episode Whisper clip (no periods, pronoun opening) is edited + packaged with AI intro instead of discarded."""
    sentences = [
        SentenceSegment(id="S0001", text="bueno pues como íbamos diciendo antes", start=0.0, end=4.0, words=[]),
        SentenceSegment(id="S0002", text="cuando llegamos al laboratorio vimos la caja fuerte abierta", start=4.1, end=10.0, words=[]),
        SentenceSegment(id="S0003", text="esta mujer se llevó los planos secretos en plena noche sin dejar rastro", start=10.2, end=19.0, words=[]),
        SentenceSegment(id="S0004", text="es que tenía hambre me acuerdo de unas croquetas", start=19.2, end=23.5, words=[]),
        SentenceSegment(id="S0005", text="y gracias a ese robo en París la patente cambió de manos para siempre", start=23.7, end=33.0, words=[]),
        SentenceSegment(id="S0006", text="al año siguiente el invento revolucionó la industria aeronáutica entera", start=33.2, end=42.0, words=[]),
        SentenceSegment(id="S0007", text="y por cierto dadle a like y suscribíos al canal", start=42.2, end=47.0, words=[]),
    ]
    profile = PodcasterProfile(
        id="nle_salvage",
        name="NLE Salvage Test",
        podcast_name="NLE Salvage Podcast",
        language="es",
        intro_curtain={"enabled": True, "mode": "auto_if_needed"},
    )
    profile.quotas.shorts_window = DurationWindow(min_sec=15.0, max_sec=35.0)

    class _NleSalvageMock:
        def __init__(self) -> None:
            self.blind_calls = 0

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {
                    "action": "refine",
                    "issue": "Mantiene el recorte sin la digresión S0004 y solicita cortinilla IA para situar a Marie Durand.",
                    "refined_spans": [{"start_sentence_id": "S0003", "end_sentence_id": "S0006", "role": "core"}],
                    "drop_sentence_ids": ["S0004"],
                    "needs_ai_intro_curtain": True,
                    "suggested_intro_curtain": "París, 1912: nadie sospechaba que Marie Durand cambiaría la aviación.",
                }
            if "Biblia Visual" in user_prompt or "glossary" in user_prompt:
                return {
                    "episode_topic": "Robo industrial de 1912 en París",
                    "narrative_spine": "Marie Durand robó los planos aeronáuticos en París en 1912.",
                    "era_or_setting": "París 1912",
                    "niche_category": "historia",
                    "proper_nouns": ["Marie Durand", "París"],
                    "glossary": {},
                    "visual_bible": {
                        "visual_style": "sepia historical documentary",
                        "prefer_historical_archive": True,
                        "forbidden_anachronisms": ["smartphone"],
                        "concrete_filmable_motifs": ["vintage blueprint workshop"],
                        "fallback_query": "vintage paris street 1912",
                    },
                }
            if "block_index" in user_prompt and "continua_tema_anterior" in user_prompt:
                return {
                    "blocks": [
                        {
                            "block_index": 0,
                            "tema": "Robo de planos de Marie Durand",
                            "continua_tema_anterior": False,
                            "interes_general": 9,
                            "potencial_cliffhanger": 7,
                            "potencial_conclusivo": 9,
                            "resumen_breve": "Marie Durand robó los planos y revolucionó la aviación.",
                        }
                    ]
                }
            if "MODALIDAD: SHORTS CONCLUSIVOS" in user_prompt:
                return {
                    "clips": [
                        {
                            "narrative_archetype": "caso_cerrado",
                            "titles": ["El robo de planos que cambió la aviación"],
                            "summary": "Marie Durand se llevó los planos secretos en París.",
                            "spans": [{"start_sentence_id": "S0003", "end_sentence_id": "S0006", "role": "core"}],
                            "drop_sentence_ids": ["S0004"],
                            "score": 88,
                            "hook_reason": "Robo nocturno misterioso",
                            "hook_text": "El robo de planos de París",
                            "cliffhanger_question": "",
                            "description": "Historia cerrada de 1912",
                            "hashtags": ["#historia", "#aviacion"],
                            "thumbnail_text": "ROBO EN PARÍS",
                        }
                    ]
                }
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                self.blind_calls += 1
                has_intro = "[INTRO IA]:" in user_prompt
                return {
                    "who": "Marie Durand" if has_intro else "DESCONOCIDO",
                    "what_and_context": (
                        "Marie Durand robó unos planos aeronáuticos en París en 1912"
                        if has_intro
                        else "Alguien robó unos planos"
                    ),
                    "opening_4_6s_clear": has_intro,
                    "opening_issue": "" if has_intro else "Abre con 'esta mujer' sin nombrarla",
                    "central_question_or_conflict": "Qué pasó tras el robo de los planos",
                    "resolution_answered": True,
                    "has_spoiler": False,
                    "answer_or_mystery_stated": "Revolucionó la industria aeronáutica.",
                    "clean_start": True,
                    "clean_end": True,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": [] if has_intro else ["'esta mujer' sin antecedente"],
                    "missing_context_items": [] if has_intro else ["Quién es 'esta mujer'"],
                    "edit_operations": [] if has_intro else ["add_ai_intro_curtain"],
                    "comprehension_score": 8 if has_intro else 6,
                    "hook_strength": 8,
                    "context_clarity": 8 if has_intro else 5,
                    "clean_boundaries": 8,
                    "resolution_score": 8,
                    "standalone_summary": "Marie Durand robó los planos y revolucionó la aviación.",
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                return {
                    "spoken_hook": "París, 1912: nadie sospechaba que Marie Durand cambiaría la aviación.",
                    "phrases": [
                        "París, 1912: nadie sospechaba",
                        "que Marie Durand cambiaría",
                        "la historia de la aviación.",
                    ],
                    "badge": "PARÍS 1912",
                }
            if "GUION FINAL EDITADO CON TIEMPOS REALES" in user_prompt:
                return {
                    "titles": ["El robo de París de 1912", "La mujer tras los planos", "Aviación secreta"],
                    "summary": "Marie Durand robó los planos aeronáuticos que cambiaron la industria.",
                    "hook_text": "El robo secreto de París",
                    "description": "Historia completa del robo de planos en 1912.",
                    "hashtags": ["#historia", "#aviacion"],
                    "thumbnail_text": "PARÍS 1912",
                    "cliffhanger_question": "",
                    "chapters": [],
                }
            return {}

    selected, _errors, _emap, _glossary, _vb = run_editorial_selection(
        sentences,
        [],
        [],
        profile,
        _NleSalvageMock(),  # type: ignore[arg-type]
        req_cliffhanger=0,
        req_conclusive=1,
        req_medium=0,
        req_long=0,
    )
    assert len(selected) == 1
    clip = selected[0]
    assert clip.intro_curtain_enabled is True
    assert "Marie Durand" in clip.intro_curtain_text
    # Digression S0004 ("croquetas") stayed excised by drop_sentence_ids
    cut_text = " ".join(
        s.text
        for sp in clip.narrative_spans
        for s in sentences
        if sp.orig_start <= s.start and s.end <= sp.orig_end
    )
    assert "croquetas" not in cut_text
    assert "aeronáutica" in cut_text


def test_video_outline_coverage_triggers_nle_repair_and_hook_cleanup() -> None:
    """Missing beat from a clip's own video_outline triggers NLE repair even if Blind Judge liked the initial cut."""
    from backend.core.editorial_engine import _audit_and_refine_candidate, _clean_trailing_hook_words
    from backend.models.job import ClipCandidate, NarrativeSpan

    assert _clean_trailing_hook_words("Una arandela salvó al Spitfire de la") == "Una arandela salvó al Spitfire"
    assert _clean_trailing_hook_words("El secreto de") == "El secreto"
    assert _clean_trailing_hook_words("Secreto de") == "Secreto de"  # keeps >=2 words

    sents = [
        SentenceSegment(id="S0001", text="Beatrice Shilling nació en 1909 en Hampshire y le encantaban las motos.", start=0.0, end=8.0, words=[]),
        SentenceSegment(id="S0002", text="Estudió ingeniería en Manchester cuando pocas mujeres lo hacían.", start=8.2, end=16.0, words=[]),
        SentenceSegment(id="S0003", text="En 1940 diseñó una arandela clave para el carburador del Spitfire que evitaba que el motor se parase.", start=16.2, end=28.0, words=[]),
        SentenceSegment(id="S0004", text="En 1948 recibió el reconocimiento del rey Jorge por su legado.", start=28.2, end=38.0, words=[]),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    # Initial draft jumped from childhood/studies (S0001..S0002) straight to epilogue (S0004), skipping S0003 (core invention)
    cand = ClipCandidate(
        id="med_outline_test",
        clip_type="medium",
        aspect_ratio="16:9",
        score=90,
        slug="beatrice-outline",
        titles=["La vida de Beatrice Shilling"],
        summary="Historia de Beatrice Shilling.",
        start_sentence_id="S0001",
        end_sentence_id="S0004",
        orig_start=0.0,
        orig_end=38.0,
        estimated_post_cut_sec=26.0,
        narrative_spans=[
            NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0002", orig_start=0.0, orig_end=16.0, role="core"),
            NarrativeSpan(start_sentence_id="S0004", end_sentence_id="S0004", orig_start=28.2, orig_end=38.0, role="payoff"),
        ],
        standalone_packaging={
            "video_outline": [
                "Infancia y formación de Beatrice Shilling",
                "Su invento clave del orificio/arandela en el carburador del Spitfire",
                "Reconocimiento final",
            ]
        },
    )

    class _OutlineRepairMock:
        def __init__(self) -> None:
            self.repaired = False

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "MINI-ÍNDICE PROPIO DE ESTE VÍDEO" in user_prompt:
                has_invent = "arandela clave" in user_prompt
                return {
                    "covered": has_invent,
                    "missing_beats": [] if has_invent else ["Su invento clave del orificio/arandela en el carburador del Spitfire"],
                    "repair_instruction": "Incluye S0003 donde se explica la arandela del carburador.",
                }
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                return {
                    "who": "Beatrice Shilling",
                    "what_and_context": "Ingeniera británica que salvó al Spitfire en 1940 y recibió honores.",
                    "opening_4_6s_clear": True,
                    "opening_issue": "",
                    "central_question_or_conflict": "Cómo destacó en ingeniería y qué logró",
                    "resolution_answered": True,
                    "has_spoiler": False,
                    "answer_or_mystery_stated": "Diseñó la solución del Spitfire y fue reconocida en 1948.",
                    "clean_start": True,
                    "clean_end": True,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": [],
                    "missing_context_items": [],
                    "comprehension_score": 9,
                    "hook_strength": 8,
                    "context_clarity": 9,
                    "clean_boundaries": 9,
                    "resolution_score": 9,
                    "standalone_summary": "Beatrice Shilling salvó los motores del Spitfire.",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.repaired = True
                return {
                    "action": "refine",
                    "issue": "Incluido S0003 con el invento del carburador pedido en el mini-índice.",
                    "drop_sentence_ids": [],
                    "needs_ai_intro_curtain": False,
                    "suggested_intro_curtain": "",
                    "refined_spans": [{"start_sentence_id": "S0001", "end_sentence_id": "S0004", "role": "core"}],
                }
            return {}

    mock = _OutlineRepairMock()
    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": False})
    res = _audit_and_refine_candidate(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock,  # type: ignore[arg-type]
        profile=profile,
    )
    assert res is not None
    assert mock.repaired is True
    assert res.start_sentence_id == "S0001"
    assert res.end_sentence_id == "S0004"
    assert len(res.narrative_spans) == 1


def test_trim_intro_cleanly_and_conversational_whisper_tolerance() -> None:
    """Conversational lowercase starts and trailing commas stay allowed while orphan connectors snap cleanly."""
    from backend.core.editorial_engine import (
        _build_candidate_from_spans,
        _run_cold_viewer_test,
        _trim_intro_cleanly,
    )
    from backend.models.job import ClipCandidate, NarrativeSpan
    from backend.models.profile import AudioEditConfig

    trimmed = _trim_intro_cleanly(
        "En 1942, tras la derrota en Gazala, el ejército británico se retiraba en desorden. "
        "Su última esperanza era Mersa Matruh, pero sus defensas eran vulnerables.",
        max_words=22,
    )
    assert trimmed.endswith("desorden.")
    no_dangle = _trim_intro_cleanly(
        "En junio de mil novecientos cuarenta y dos el general británico esperaba órdenes desesperadas en el puerto pero sus",
        max_words=19,
    )
    assert not no_dangle.endswith(("sus.", "pero."))

    sents = [
        SentenceSegment(id="S0001", text="El ejército estaba cercado,", start=0.0, end=5.0, words=[]),
        SentenceSegment(id="S0002", text="y sin munición ni agua.", start=5.1, end=10.0, words=[]),
        SentenceSegment(id="S0003", text="fijan bayonetas para romper el cerco nocturno,", start=12.0, end=20.0, words=[]),
        SentenceSegment(id="S0004", text="logrando escapar hacia Alejandría al amanecer.", start=20.2, end=28.0, words=[]),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": False})

    # Span edge snap pulls S0002 back to S0001 because "y" is in _ORPHAN_START_WORDS with gap 0.1s
    snapped = _build_candidate_from_spans(
        raw={"score": 85, "titles": ["Cerco en el desierto"], "summary": "Cerco"},
        spans_idx=[(1, 2, "core")],
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=10.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=AudioEditConfig(),
        energy_blocks=[],
        idx_counter=1,
    )
    assert snapped is not None
    assert snapped.start_sentence_id == "S0001"

    class _ConvJudgeMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "who": "Brigada neozelandesa",
                "what_and_context": "Carga nocturna a la bayoneta.",
                "opening_4_6s_clear": True,
                "opening_issue": "",
                "central_question_or_conflict": "Cómo rompieron el cerco",
                "resolution_answered": True,
                "has_spoiler": False,
                "answer_or_mystery_stated": "Escaparon al amanecer.",
                "clean_start": True,
                "clean_end": True,
                "splices_coherent": True,
                "broken_splices": [],
                "orphan_or_meta_refs": [],
                "missing_context_items": [],
                "comprehension_score": 8,
                "hook_strength": 8,
                "context_clarity": 8,
                "clean_boundaries": 8,
                "resolution_score": 8,
                "standalone_summary": "Rompieron el cerco.",
            }

    # Lowercase non-orphan opening S0003 ("fijan bayonetas...,") stays clean_start=True and clean_end=True
    conv_cand = ClipCandidate(
        id="c_conv",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=85,
        slug="c-conv",
        titles=["Carga nocturna"],
        summary="Carga",
        start_sentence_id="S0003",
        end_sentence_id="S0003",
        orig_start=12.0,
        orig_end=20.0,
        estimated_post_cut_sec=8.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0003", end_sentence_id="S0003", orig_start=12.0, orig_end=20.0, role="core")],
    )
    ev_conv = _run_cold_viewer_test(conv_cand, sents, id_to_idx, _ConvJudgeMock())  # type: ignore[arg-type]
    assert ev_conv["clean_start"] is True
    assert ev_conv["clean_end"] is True

    # Real orphan connector S0002 ("y sin munición...") with tight gap still flags clean_start=False
    orphan_cand = ClipCandidate(
        id="c_orp",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=85,
        slug="c-orp",
        titles=["Orphan"],
        summary="Orphan",
        start_sentence_id="S0002",
        end_sentence_id="S0003",
        orig_start=5.1,
        orig_end=20.0,
        estimated_post_cut_sec=14.9,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0002", end_sentence_id="S0003", orig_start=5.1, orig_end=20.0, role="core")],
    )
    ev_orp = _run_cold_viewer_test(orphan_cand, sents, id_to_idx, _ConvJudgeMock())  # type: ignore[arg-type]
    assert ev_orp["clean_start"] is False


def test_severe_budget_exhausted_still_repairs_and_step_c_preserves_splices() -> None:
    """Exhausted strong budget still runs cheap editorial repair, and Step C honors pre-intro splice coherence."""
    from backend.core.editorial_engine import _audit_and_refine_candidate
    from backend.models.job import ClipCandidate, NarrativeSpan

    sents = [
        SentenceSegment(id="S0001", text="El general Holmes queda aislado en Mersa Matruh en 1942.", start=0.0, end=10.0, words=[]),
        SentenceSegment(id="S0002", text="Evalúa romper el cerco alemán esa misma noche.", start=10.3, end=20.0, words=[]),
        SentenceSegment(id="S0003", text="Mientras Rommel esperaba la rendición británica al amanecer.", start=20.3, end=30.0, words=[]),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    cand = ClipCandidate(
        id="c_stepc",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=88,
        slug="holmes-cerco",
        titles=["El cerco a Holmes"],
        summary="El cerco en Mersa Matruh.",
        start_sentence_id="S0002",
        end_sentence_id="S0003",
        orig_start=10.3,
        orig_end=30.0,
        estimated_post_cut_sec=19.7,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0002", end_sentence_id="S0003", orig_start=10.3, orig_end=30.0, role="core")],
    )

    class _RepairAndStepCMock:
        def __init__(self) -> None:
            self.repair_roles: list[str] = []

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                if "[INTRO IA]:" in user_prompt:
                    return {
                        "who": "General Holmes",
                        "what_and_context": "Aislado en Mersa Matruh en junio de 1942 intenta romper el cerco.",
                        "opening_4_6s_clear": True,
                        "opening_issue": "",
                        "central_question_or_conflict": "Si logrará romper el cerco",
                        "resolution_answered": True,
                        "has_spoiler": False,
                        "answer_or_mystery_stated": "Intentó escapar por la noche.",
                        "clean_start": True,
                        "clean_end": True,
                        "splices_coherent": False,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "missing_context_items": [],
                        "comprehension_score": 5,
                        "hook_strength": 8,
                        "context_clarity": 8,
                        "clean_boundaries": 8,
                        "resolution_score": 8,
                        "standalone_summary": "Holmes escapó en Mersa Matruh.",
                    }
                if "El general Holmes" in user_prompt:
                    return {
                        "who": "General Holmes",
                        "what_and_context": "Aislado en Mersa Matruh en 1942.",
                        "opening_4_6s_clear": False,
                        "opening_issue": "Necesita contexto adicional de época.",
                        "central_question_or_conflict": "Cómo escapar del cerco",
                        "resolution_answered": True,
                        "has_spoiler": False,
                        "answer_or_mystery_stated": "Rompió el cerco.",
                        "clean_start": True,
                        "clean_end": True,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "missing_context_items": [],
                        "comprehension_score": 6,
                        "hook_strength": 7,
                        "context_clarity": 5,
                        "clean_boundaries": 8,
                        "resolution_score": 8,
                        "standalone_summary": "Holmes en Mersa Matruh.",
                    }
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "opening_issue": "Falta sujeto",
                    "central_question_or_conflict": "Falta",
                    "resolution_answered": False,
                    "has_spoiler": False,
                    "answer_or_mystery_stated": "",
                    "clean_start": False,
                    "clean_end": True,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": [],
                    "missing_context_items": ["Sujeto"],
                    "comprehension_score": 4,
                    "hook_strength": 5,
                    "context_clarity": 4,
                    "clean_boundaries": 5,
                    "resolution_score": 4,
                    "standalone_summary": "",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                self.repair_roles.append(role)
                return {
                    "action": "refine",
                    "issue": "Añadido S0001 para identificar a Holmes",
                    "drop_sentence_ids": [],
                    "needs_ai_intro_curtain": True,
                    "suggested_intro_curtain": "En junio de 1942 el general Holmes quedó cercado en Mersa Matruh.",
                    "refined_spans": [
                        {"start_sentence_id": "S0001", "end_sentence_id": "S0001", "role": "context_intro"},
                        {"start_sentence_id": "S0003", "end_sentence_id": "S0003", "role": "core"},
                    ],
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                return {
                    "spoken_hook": "En junio de 1942 el general Holmes quedó cercado en Mersa Matruh frente a Rommel.",
                    "phrases": ["Junio de 1942", "El general Holmes", "cercado en Mersa Matruh"],
                    "badge": "CONTEXTO CLAVE",
                }
            return {}

    mock = _RepairAndStepCMock()
    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": True})
    res = _audit_and_refine_candidate(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock,  # type: ignore[arg-type]
        allow_strong_escalation=False,
        profile=profile,
    )
    assert "editorial" in mock.repair_roles
    assert res is not None
    assert res.intro_curtain_enabled is True
    assert res.standalone_packaging.get("audit_action") == "refine->approve_needs_intro"


def test_step_c_evaluates_best_repaired_cand_when_raw_whisper_fails_without_intro() -> None:
    """Step B repair feeds best_repaired_cand into Step C even if un-framed conversational Whisper scored low."""
    from backend.core.editorial_engine import _audit_and_refine_candidate
    from backend.models.job import ClipCandidate, NarrativeSpan

    sents = [
        SentenceSegment(id="S0001", text="a ver los británicos no se entendieron en Marsa Matruh", start=0.0, end=12.0, words=[]),
        SentenceSegment(id="S0002", text="Rommel aprovechó la brecha entre las divisiones aliadas", start=12.2, end=25.0, words=[]),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    cand = ClipCandidate(
        id="c_rep_stepc",
        clip_type="medium",
        aspect_ratio="16:9",
        score=86,
        slug="marsa-repair",
        titles=["El fallo de Marsa Matruh"],
        summary="Fallo táctico en Marsa Matruh.",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=0.0,
        orig_end=25.0,
        estimated_post_cut_sec=25.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0002", orig_start=0.0, orig_end=25.0, role="core")],
    )

    class _StepCMock:
        def __init__(self, fail_intro_who: bool = False) -> None:
            self.fail_intro_who = fail_intro_who
            self.intro_checked = False

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                if "[INTRO IA]:" in user_prompt:
                    self.intro_checked = True
                    return {
                        "who": "DESCONOCIDO" if self.fail_intro_who else "Ejército británico y Rommel",
                        "what_and_context": "Alguien habla de algo sin nombrar lugar ni protagonistas." if self.fail_intro_who else "En 1942 en Marsa Matruh los fallos de mando abrieron paso a Rommel.",
                        "opening_4_6s_clear": not self.fail_intro_who,
                        "opening_issue": "",
                        "central_question_or_conflict": "Por qué cayó Marsa Matruh",
                        "resolution_answered": False,
                        "has_spoiler": False,
                        "answer_or_mystery_stated": "Falta de coordinación.",
                        "clean_start": True,
                        "clean_end": False,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "missing_context_items": [],
                        "comprehension_score": 7,
                        "hook_strength": 8,
                        "context_clarity": 8,
                        "clean_boundaries": 7,
                        "resolution_score": 6,
                        "standalone_summary": "Caída de Marsa Matruh por desorden británico.",
                    }
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "opening_issue": "Falta sujeto sin intro",
                    "central_question_or_conflict": "Falta",
                    "resolution_answered": False,
                    "has_spoiler": False,
                    "answer_or_mystery_stated": "",
                    "clean_start": False,
                    "clean_end": False,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": [],
                    "missing_context_items": ["Sujeto"],
                    "comprehension_score": 4,
                    "hook_strength": 5,
                    "context_clarity": 4,
                    "clean_boundaries": 4,
                    "resolution_score": 4,
                    "standalone_summary": "",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {
                    "action": "refine",
                    "issue": "Tramo limpio listo para enmarcar con cortinilla IA.",
                    "drop_sentence_ids": [],
                    "needs_ai_intro_curtain": True,
                    "suggested_intro_curtain": "En junio de 1942 el ejército británico se jugaba Egipto frente a Rommel en Marsa Matruh.",
                    "refined_spans": [{"start_sentence_id": "S0001", "end_sentence_id": "S0002", "role": "core"}],
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                return {
                    "spoken_hook": "En junio de 1942 el ejército británico se jugaba Egipto frente a Rommel en Marsa Matruh.",
                    "phrases": ["Junio de 1942", "Egipto en juego", "Marsa Matruh"],
                    "badge": "MARSA MATRUH 1942",
                }
            return {}

    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": True})
    mock_ok = _StepCMock(fail_intro_who=False)
    approved = _audit_and_refine_candidate(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock_ok,  # type: ignore[arg-type]
        profile=profile,
    )
    assert mock_ok.intro_checked is True
    assert approved is not None
    assert approved.intro_curtain_enabled is True
    assert approved.standalone_packaging.get("audit_action") == "editorial_strong_refine->needs_intro"

    mock_bad = _StepCMock(fail_intro_who=True)
    rejected = _audit_and_refine_candidate(
        cand=cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock_bad,  # type: ignore[arg-type]
        profile=profile,
    )
    assert mock_bad.intro_checked is True
    assert rejected is None


def test_cliffhanger_raw_res_and_collective_who_with_intro() -> None:
    """Cliffhangers with resolution_answered=False and multi-actor stories with named entities pass Step C."""
    from backend.core.editorial_engine import _audit_and_refine_candidate, _run_cold_viewer_test
    from backend.models.job import ClipCandidate, NarrativeSpan

    sents = [
        SentenceSegment(
            id="S0001",
            text="En Mersa Matruh las unidades británicas e indias quedan rodeadas tras la derrota de Tobruk.",
            start=0.0,
            end=15.0,
            words=[],
        ),
        SentenceSegment(
            id="S0002",
            text="Rommel avanza sobre El Alamein mientras las órdenes no llegan al frente.",
            start=15.2,
            end=30.0,
            words=[],
        ),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}

    cliff_cand = ClipCandidate(
        id="c_cliff_raw",
        clip_type="short_cliffhanger",
        aspect_ratio="9:16",
        score=88,
        slug="cliff-raw-res",
        titles=["6000 rodeados en Mersa Matruh"],
        summary="Cerco en Mersa Matruh.",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=0.0,
        orig_end=30.0,
        estimated_post_cut_sec=30.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0002", orig_start=0.0, orig_end=30.0, role="core")],
    )

    class _CliffAndCollectiveMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                if "[INTRO IA]:" in user_prompt:
                    return {
                        "who": "10º Cuerpo británico y Rommel",
                        "what_and_context": "En 1942 el 10º Cuerpo británico queda cercado en Mersa Matruh por Rommel.",
                        "opening_4_6s_clear": True,
                        "opening_issue": "",
                        "central_question_or_conflict": "Si lograrán escapar del cerco en Mersa Matruh",
                        "resolution_answered": False,
                        "has_spoiler": False,
                        "answer_or_mystery_stated": "Quedan atrapados sin órdenes claras.",
                        "clean_start": True,
                        "clean_end": True,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "orphan_or_meta_refs": [],
                        "missing_context_items": [],
                        "comprehension_score": 7,
                        "hook_strength": 8,
                        "context_clarity": 9,
                        "clean_boundaries": 8,
                        "resolution_score": 7,
                        "standalone_summary": "Cerco en Mersa Matruh sin desenlace.",
                    }
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "opening_issue": "Falta contexto inicial",
                    "central_question_or_conflict": "Cerco en el desierto",
                    "resolution_answered": False,
                    "has_spoiler": False,
                    "answer_or_mystery_stated": "",
                    "clean_start": False,
                    "clean_end": False,
                    "splices_coherent": True,
                    "broken_splices": [],
                    "orphan_or_meta_refs": [],
                    "missing_context_items": ["Contexto"],
                    "comprehension_score": 4,
                    "hook_strength": 5,
                    "context_clarity": 4,
                    "clean_boundaries": 4,
                    "resolution_score": 4,
                    "standalone_summary": "",
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {
                    "action": "refine",
                    "issue": "Tramo limpio para cortinilla IA.",
                    "drop_sentence_ids": [],
                    "needs_ai_intro_curtain": True,
                    "suggested_intro_curtain": "En junio de 1942, Rommel cercó al 10º Cuerpo británico en Mersa Matruh.",
                    "refined_spans": [{"start_sentence_id": "S0001", "end_sentence_id": "S0002", "role": "core"}],
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                return {
                    "spoken_hook": "En junio de 1942, Rommel cercó al 10º Cuerpo británico en Mersa Matruh sin salida.",
                    "phrases": ["Junio de 1942", "Rommel cercó", "al 10º Cuerpo británico"],
                    "badge": "MARSA MATRUH",
                }
            return {}

    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": True})
    approved = _audit_and_refine_candidate(
        cand=cliff_cand,
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=_CliffAndCollectiveMock(),  # type: ignore[arg-type]
        profile=profile,
    )
    assert approved is not None
    assert approved.intro_curtain_enabled is True

    class _CollectiveWhoFallbackMock:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "who": "DESCONOCIDO",
                "what_and_context": "En 1942, tras la derrota en Tobruk, los aliados se reagrupan en El Alamein frente a Rommel.",
                "opening_4_6s_clear": True,
                "opening_issue": "",
                "central_question_or_conflict": "Cómo frenar a Rommel",
                "resolution_answered": False,
                "has_spoiler": False,
                "answer_or_mystery_stated": "Se reagruparon en El Alamein.",
                "clean_start": True,
                "clean_end": True,
                "splices_coherent": True,
                "broken_splices": [],
                "orphan_or_meta_refs": [],
                "missing_context_items": [],
                "comprehension_score": 7,
                "hook_strength": 8,
                "context_clarity": 8,
                "clean_boundaries": 8,
                "resolution_score": 7,
                "standalone_summary": "Retirada hacia El Alamein.",
            }

    cliff_cand.intro_curtain_text = "En 1942 los aliados intentan frenar a Rommel en El Alamein."
    ev_col = _run_cold_viewer_test(
        cliff_cand,
        sents,
        id_to_idx,
        _CollectiveWhoFallbackMock(),  # type: ignore[arg-type]
        visual_bible={"proper_nouns": ["Rommel", "El Alamein", "Tobruk"]},
        include_intro_curtain=True,
    )
    assert ev_col["who_unknown"] is False
    assert ev_col["context_clarity"] >= 80
    assert ev_col["raw_resolution_score"] == 70


def test_grounded_glossary_blocks_contamination_and_refines_pass2() -> None:
    from backend.core.editorial_engine import (
        extract_episode_glossary_and_bible,
        refine_clip_glossary_from_pass2,
    )
    from backend.models.job import WordTimestamp

    sents = [
        SentenceSegment(id="S0001", start=0.0, end=5.0, text="En junio de 1942 Romer avanzó por el desierto."),
        SentenceSegment(id="S0002", start=5.0, end=10.0, text="El cuerpo británico se replegó hacia Matruc."),
    ]
    profile = PodcasterProfile(id="p", name="P", podcast_name="Historia", language="es")

    class _ContaminatedGlossaryLLM:
        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "lingüista forense" in system_prompt or "glossary" in user_prompt:
                return {
                    "episode_topic": "Batalla de Mersa Matruh",
                    "era_or_setting": "Junio de 1942, Norte de África",
                    "niche_category": "historia",
                    "proper_nouns": ["Erwin Rommel", "Mersa Matruh"],
                    "glossary": {
                        "Rorro y Merlin": "Rolls-Royce Merlin",
                        "Beatriz Silling": "Beatrice Shilling",
                        "RIA": "RAE",
                        "Romer": "Rommel",
                    },
                    "visual_bible": {
                        "visual_style": "historical desert warfare",
                        "prefer_historical_archive": True,
                        "forbidden_anachronisms": [],
                        "concrete_filmable_motifs": ["desert tank convoy"],
                        "fallback_query": "desert armored column",
                    },
                }
            if "PASADA 2" in user_prompt:
                return {
                    "fixes": {
                        "Matruc": "Mersa Matruh",
                        "Inventado Fantasma": "Ghost Division",
                    }
                }
            return {}

    glossary, vb = extract_episode_glossary_and_bible(sents, [], profile, _ContaminatedGlossaryLLM())  # type: ignore[arg-type]
    assert glossary == {"Romer": "Rommel"}
    assert "Rorro y Merlin" not in glossary
    assert "Beatriz Silling" not in glossary
    assert "RIA" not in glossary

    p2_words = [
        WordTimestamp(word="El", start=4.6, end=4.8),
        WordTimestamp(word="ejército", start=4.8, end=5.1),
        WordTimestamp(word="avanzó", start=5.1, end=5.3),
        WordTimestamp(word="hacia", start=5.3, end=5.5),
        WordTimestamp(word="Matruc", start=5.5, end=6.0),
        WordTimestamp(word="en", start=6.1, end=6.3),
        WordTimestamp(word="1942.", start=6.4, end=6.9),
    ]
    p2_fixes = refine_clip_glossary_from_pass2(p2_words, vb, glossary, _ContaminatedGlossaryLLM())  # type: ignore[arg-type]
    assert p2_fixes == {"Matruc": "Mersa Matruh"}


def test_do_no_harm_repair_intro_parity_and_dedup_signatures() -> None:
    from backend.core.editorial_engine import (
        _audit_and_refine_candidate,
        _deduplicate_and_audit,
        generate_intro_curtain_script,
    )
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan

    sents = [
        SentenceSegment(id="S0001", start=0.0, end=8.0, text="Avanzaron en silencio por la noche."),
        SentenceSegment(id="S0002", start=8.0, end=16.0, text="Al amanecer tomaron el nido de ametralladoras."),
        SentenceSegment(id="S0003", start=16.0, end=24.0, text="El batallón maorí resistió el contraataque."),
    ]
    id_to_idx = {s.id: i for i, s in enumerate(sents)}
    profile = PodcasterProfile(id="p", name="P", podcast_name="P", language="es", intro_curtain={"enabled": True})

    class _HarmfulRepairMock:
        def __init__(self) -> None:
            self.eval_calls = 0

        def chat_json(self, role: str, system_prompt: str, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
            if "PRUEBA DE ESPECTADOR FRÍO" in user_prompt:
                self.eval_calls += 1
                if "[INTRO IA]" in user_prompt:
                    return {
                        "who": "El batallón maorí en Mersa Matruh",
                        "what_and_context": "En 1942 rompieron el cerco alemán en Mersa Matruh.",
                        "opening_4_6s_clear": True,
                        "resolution_answered": True,
                        "clean_start": True,
                        "clean_end": True,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "has_spoiler": False,
                        "comprehension_score": 8,
                        "hook_strength": 8,
                        "context_clarity": 9,
                        "clean_boundaries": 8,
                        "resolution_score": 8,
                        "standalone_summary": "Carga nocturna maorí.",
                    }
                if self.eval_calls == 1:
                    # Healthy body, lacking WHO at 0-4s
                    return {
                        "who": "DESCONOCIDO",
                        "what_and_context": "DESCONOCIDO",
                        "opening_4_6s_clear": False,
                        "resolution_answered": False,
                        "clean_start": True,
                        "clean_end": True,
                        "splices_coherent": True,
                        "broken_splices": [],
                        "has_spoiler": False,
                        "comprehension_score": 6,
                        "hook_strength": 7,
                        "context_clarity": 5,
                        "clean_boundaries": 8,
                        "resolution_score": 9,
                        "standalone_summary": "Asalto nocturno.",
                    }
                # Post-repair evaluation fails splices (simulating NLE harm)
                return {
                    "who": "DESCONOCIDO",
                    "what_and_context": "DESCONOCIDO",
                    "opening_4_6s_clear": False,
                    "resolution_answered": False,
                    "clean_start": False,
                    "clean_end": True,
                    "splices_coherent": False,
                    "broken_splices": ["Corte brusco"],
                    "has_spoiler": False,
                    "comprehension_score": 4,
                    "hook_strength": 4,
                    "context_clarity": 4,
                    "clean_boundaries": 4,
                    "resolution_score": 4,
                }
            if "REPARACIÓN EDITORIAL" in user_prompt:
                return {
                    "action": "refine",
                    "issue": "Intento de empalme",
                    "refined_spans": [
                        {"start_sentence_id": "S0001", "end_sentence_id": "S0001", "role": "context"},
                        {"start_sentence_id": "S0003", "end_sentence_id": "S0003", "role": "payoff"},
                    ],
                }
            if "CORTINILLA DE APERTURA" in system_prompt:
                return {
                    "spoken_hook": "En junio de 1942 el batallón maorí cargó de noche en Mersa Matruh.",
                    "phrases": ["Junio de 1942", "carga nocturna"],  # intentionally omits words!
                    "badge": "MERSA MATRUH",
                }
            return {}

    cand = ClipCandidate(
        id="c_maori",
        candidate_id="cand_maori",
        original_title="Carga maorí",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=90,
        slug="carga-maori",
        start_sentence_id="S0001",
        end_sentence_id="S0003",
        orig_start=0.0,
        orig_end=24.0,
        estimated_post_cut_sec=24.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0003", orig_start=0.0, orig_end=24.0, role="core")],
        metadata=ClipMetadata(titles=["Carga maorí"]),
    )

    mock = _HarmfulRepairMock()
    res = _audit_and_refine_candidate(
        cand=cand.model_copy(deep=True),
        sentences=sents,
        id_to_idx=id_to_idx,
        target_window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        audio_config=profile.audio,
        energy_blocks=[],
        llm=mock,  # type: ignore[arg-type]
        profile=profile,
    )
    assert res is not None
    # Original unbroken 1-span body preserved by Do-No-Harm guard
    assert len(res.narrative_spans) == 1
    assert res.intro_curtain_enabled is True
    assert len(res.intro_curtain_text.split()) <= 17
    assert " ".join(res.intro_curtain_phrases).lower().replace(",", "").replace(".", "") == res.intro_curtain_text.lower().replace(",", "").replace(".", "")

    # Ensure generate_intro_curtain_script always enforces exact word parity
    script = generate_intro_curtain_script(cand, profile, {}, mock, sentences=sents, id_to_idx=id_to_idx)  # type: ignore[arg-type]
    assert len(script["spoken_hook"].split()) <= 17
    assert " ".join(script["phrases"]).split() == script["spoken_hook"].split()

    # Verify audited_signatures avoids re-auditing previously failed candidate
    audited_sigs: set[str] = {"short_conclusive:S0001-S0003"}
    calls_before = mock.eval_calls
    chosen = _deduplicate_and_audit(
        [cand.model_copy(deep=True)],
        requested=1,
        existing_short_spans=[],
        longform_spans=[],
        allow_short_as_teaser=True,
        clip_type="short_conclusive",
        sentences=sents,
        id_to_idx=id_to_idx,
        window=DurationWindow(min_sec=15.0, max_sec=60.0),
        silence_intervals=[],
        energy_blocks=[],
        profile=profile,
        llm=mock,  # type: ignore[arg-type]
        allow_fallback=False,
        audited_signatures=audited_sigs,
    )
    assert chosen == []
    assert mock.eval_calls == calls_before





