"""Unit tests for Pass 1 syntactic segmentation, orphan fragment merging, and Pass 2 hallucination defense."""

from __future__ import annotations

from backend.core.transcriber import (
    _merge_orphan_fragments,
    _split_segment_into_sentences,
    detect_pass2_hallucination,
)
from backend.models.job import SentenceSegment, WordTimestamp


def test_pass1_never_splits_mid_noun_phrase_and_merges_orphans() -> None:
    # Simulate a fast 24-word Spanish phrase without terminal period until the end
    toks = (
        "Resulta que recibe el aviso de que tiene que echarle un vistazo a esto "
        "y se acercó ella a los aeródromos donde estaban operando."
    ).split()
    words = [
        WordTimestamp(word=t, start=round(i * 0.32, 3), end=round(i * 0.32 + 0.28, 3))
        for i, t in enumerate(toks)
    ]
    pieces = _split_segment_into_sentences(0.0, words[-1].end, " ".join(toks), words=words)
    # 24 words must stay intact as 1 complete sentence instead of chopping at word 22 ("a los")
    assert len(pieces) == 1
    assert pieces[0][2].endswith("operando.")

    # Test merging of Whisper segment-boundary orphans ("a los | aeródromos...", "por lo | visto...")
    raw_sents = [
        SentenceSegment(id="S0001", start=1.0, end=5.0, text="se acercó ella a los"),
        SentenceSegment(id="S0002", start=5.05, end=10.0, text="aeródromos donde operaban los cazas."),
        SentenceSegment(id="S0003", start=10.5, end=11.0, text="por lo"),
        SentenceSegment(id="S0004", start=11.05, end=15.0, text="visto no les gustó nada la idea."),
    ]
    merged = _merge_orphan_fragments(raw_sents)
    assert len(merged) == 2
    assert merged[0].id == "S0001"
    assert "a los aeródromos" in merged[0].text
    assert merged[1].id == "S0002"
    assert merged[1].text.startswith("por lo visto")

    # Lowercase run-on split across a short Whisper pause attaches cleanly backward
    runon_sents = [
        SentenceSegment(id="S0001", start=1.0, end=4.0, text="no consiguió atravesar la barrera o sea así"),
        SentenceSegment(id="S0002", start=4.15, end=5.4, text="claro lo tenía ella."),
        SentenceSegment(id="S0003", start=5.5, end=9.5, text="Ya a finales de los años 30 la contrataron."),
    ]
    merged_runon = _merge_orphan_fragments(runon_sents)
    assert len(merged_runon) == 2
    assert merged_runon[0].text.endswith("así claro lo tenía ella.")
    assert merged_runon[1].text.startswith("Ya a finales")


def test_detect_pass2_hallucination_patterns() -> None:
    # 1. Repetitive prompt-echo loop from Prueba 3 short_cliffhanger (23 words in 64.5s)
    loop_tokens = (
        "de Francia, Beatrice Shilling, comentaristas la Caída Beatrice Shilling, "
        "comentaristas de la Caída de Francia Beatrice Shilling, comentaristas de la Caída de Francia"
    ).split()
    loop_words = [
        WordTimestamp(
            word=tok,
            start=round(i * 2.5, 2),
            end=round(i * 2.5 + 0.4, 2),
            probability=0.15 if i % 2 == 0 else 0.45,
        )
        for i, tok in enumerate(loop_tokens)
    ]
    bad, reason = detect_pass2_hallucination(
        loop_words,
        span_duration=64.5,
        reference_text=" ".join(["palabra"] * 165),
    )
    assert bad is True
    assert reason != ""

    # 2. Low-probability tail burst on a 9s span
    tail_tokens = "Caída de Francia, Royal Aircraft Establishment, comentaristas aquí ahora".split()
    tail_words = [
        WordTimestamp(word=t, start=i * 0.9, end=i * 0.9 + 0.5, probability=0.04)
        for i, t in enumerate(tail_tokens)
    ]
    bad_tail, _ = detect_pass2_hallucination(
        tail_words,
        span_duration=9.0,
        reference_text=" ".join(["real"] * 24),
    )
    assert bad_tail is True

    # 3. Healthy dense Spanish transcription passes cleanly
    clean_text = (
        "Los pilotos del Spitfire tenían un problema grave cuando picaban frente a los cazas alemanes. "
        "Beatrice Shilling analizó el carburador y diseñó una pequeña arandela metálica que resolvía el fallo "
        "en veinte minutos sin desmontar el motor de aviación británico durante toda la batalla."
    )
    clean_toks = clean_text.split()
    clean_words = [
        WordTimestamp(word=t, start=round(i * 0.35, 2), end=round(i * 0.35 + 0.3, 2), probability=0.92)
        for i, t in enumerate(clean_toks)
    ]
    bad_clean, _ = detect_pass2_hallucination(
        clean_words,
        span_duration=16.0,
        reference_text=clean_text,
    )
    assert bad_clean is False
