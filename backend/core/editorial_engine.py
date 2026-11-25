"""Adaptive 2-level editorial engine v2: glossary, visual bible, multi-span narrative stitching, blind coherence audit, and post-cut metadata."""

from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Literal, Optional

from backend.core.audio_editor import normalize_token
from backend.core.openrouter_client import OpenRouterClient
from backend.core.transcriber import (
    DANGLING_END_WORDS,
    _norm_last_word,
    _normalize_glossary_token,
    _starts_with_lowercase,
)
from backend.models.job import (
    ChapterMark,
    ClipCandidate,
    ClipMetadata,
    ClipType,
    NarrativeSpan,
    SentenceSegment,
    WordTimestamp,
)
from backend.models.profile import AudioEditConfig, DurationWindow, PodcasterProfile


def make_slug(text: str, max_len: int = 42) -> str:
    """Convert a Spanish/English title into a safe ASCII filename slug."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_only.lower()).strip("_")
    return (slug[:max_len].strip("_")) or "clip"


def format_output_filename(score: int, clip_type: ClipType, title: str) -> str:
    """Format output filename with score prefix, clip type, and title slug."""
    return f"{int(score):02d}_{clip_type}_{make_slug(title)}.mp4"


def _compute_overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if inter <= 0.0:
        return 0.0
    min_len = max(0.1, min(a_end - a_start, b_end - b_start))
    return inter / min_len


_HOOK_TRAILING_DANGLING_WORDS: frozenset[str] = frozenset(
    {
        "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
        "en", "con", "por", "para", "y", "o", "a", "al", "que", "su", "sus",
        "pero", "mas", "sin", "e", "u",
    }
)

_ORPHAN_START_WORDS: frozenset[str] = frozenset(
    {
        "de", "del", "que", "y", "e", "o", "u", "pero", "porque", "como", "al",
        "para", "con", "sin", "por", "sino", "ni", "aunque", "donde", "cuando",
        "mientras", "entonces", "asi", "pues",
    }
)


def _clean_trailing_hook_words(text: str) -> str:
    """Ensure short screen hooks never end mid-phrase on a hanging Spanish article/preposition."""
    words = (text or "").strip().split()
    while len(words) > 2:
        norm = _strip_accents_lower(re.sub(r"[^\wáéíóúüñ]", "", words[-1]))
        if norm in _HOOK_TRAILING_DANGLING_WORDS:
            words.pop()
        else:
            break
    return " ".join(words).rstrip(",;:")


def _trim_intro_cleanly(text: str, max_words: int = 26) -> str:
    """Trim an AI intro script cleanly on sentence bounds or without dangling trailing words."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    words = cleaned.split()
    if len(words) > max_words:
        chosen: Optional[str] = None
        for idx in range(max_words - 1, 8, -1):
            if words[idx].rstrip("\"'”’").endswith((".", "!", "?")):
                chosen = " ".join(words[: idx + 1])
                break
        if chosen is None:
            chosen = " ".join(words[:max_words]).rstrip(",;:")
        cleaned = chosen
    cleaned = cleaned.rstrip(",;:")
    if not cleaned.endswith((".", "!", "?")):
        cleaned = _clean_trailing_hook_words(cleaned) or cleaned
    if cleaned.startswith("¿") and not cleaned.endswith("?"):
        cleaned += "?"
    elif not cleaned.endswith((".", "!", "?")):
        cleaned += "."
    return cleaned


def _format_sentences_block(
    sentences: list[SentenceSegment],
    show_speakers: bool = False,
    music_breaks: Optional[dict[str, float]] = None,
    include_durations: bool = False,
) -> str:
    lines = []
    for s in sentences:
        if music_breaks and s.id in music_breaks:
            lines.append(f"--- [PAUSA MUSICAL {music_breaks[s.id]:.1f}s / CAMBIO DE BLOQUE] ---")
        dur_tag = f" | {max(0.1, float(s.end) - float(s.start)):.1f}s" if include_durations else ""
        if show_speakers and s.speaker:
            lines.append(f"[{s.id}{dur_tag}] [{s.speaker}] {s.text}")
        else:
            lines.append(f"[{s.id}{dur_tag}] {s.text}")
    return "\n".join(lines)


def _compute_music_breaks(
    sentences: list[SentenceSegment],
    non_speech_intervals: Optional[list[tuple[float, float]]] = None,
) -> dict[str, float]:
    """Map sentence IDs preceded by long non-speech/musical gaps (>= 2.8s) to gap duration."""
    breaks: dict[str, float] = {}
    if len(sentences) < 2:
        return breaks
    for i in range(1, len(sentences)):
        gap = float(sentences[i].start) - float(sentences[i - 1].end)
        if gap >= 2.8:
            breaks[sentences[i].id] = round(gap, 1)
            continue
        if non_speech_intervals:
            prev_e = float(sentences[i - 1].end)
            cur_s = float(sentences[i].start)
            for ns_s, ns_e in non_speech_intervals:
                if (ns_e - ns_s) >= 2.8 and min(cur_s, ns_e) - max(prev_e, ns_s) >= 1.5:
                    breaks[sentences[i].id] = round(ns_e - ns_s, 1)
                    break
    return breaks


def build_time_blocks(
    sentences: list[SentenceSegment],
    energy_blocks: list[dict],
    block_sec: float = 300.0,
) -> list[dict[str, Any]]:
    """Group indexed sentences into fixed time blocks (default 5 min) tagged with RMS energy."""
    if not sentences:
        return []

    total_end = sentences[-1].end
    num_blocks = max(1, int((total_end + block_sec - 0.01) // block_sec))
    blocks: list[dict[str, Any]] = []

    for idx in range(num_blocks):
        b_start = idx * block_sec
        b_end = (idx + 1) * block_sec
        b_sents = [
            s
            for s in sentences
            if b_start <= (s.start + s.end) * 0.5 < b_end
        ]
        if not b_sents:
            continue

        energy = "media"
        mid = (b_start + min(b_end, total_end)) * 0.5
        for eb in energy_blocks:
            if eb["start"] <= mid <= eb["end"]:
                energy = eb.get("energy", "media")
                break

        blocks.append(
            {
                "block_index": len(blocks),
                "orig_start": round(b_sents[0].start, 2),
                "orig_end": round(b_sents[-1].end, 2),
                "energy": energy,
                "sentences": b_sents,
                "first_id": b_sents[0].id,
                "last_id": b_sents[-1].id,
            }
        )
    return blocks


def estimate_post_cut_duration(
    start_idx: int,
    end_idx: int,
    sentences: list[SentenceSegment],
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
) -> float:
    """Estimate clean clip duration for one index range after removing silences and censored words."""
    s_idx = max(0, start_idx)
    e_idx = min(len(sentences) - 1, end_idx)
    if e_idx < s_idx:
        return 0.0

    orig_s = sentences[s_idx].start
    orig_e = sentences[e_idx].end
    raw_dur = max(0.0, orig_e - orig_s)

    cut_est = 0.0
    if audio_config.remove_silences and silence_intervals:
        min_sil = max(audio_config.min_silence_sec, audio_config.keep_silence_sec + 0.06)
        for ss, se in silence_intervals:
            ov_s = max(orig_s, ss)
            ov_e = min(orig_e, se)
            if ov_e - ov_s >= min_sil:
                cut_est += (ov_e - ov_s) - audio_config.keep_silence_sec

    if audio_config.censored_words:
        norm_terms = [normalize_token(t) for t in audio_config.censored_words if normalize_token(t)]
        if norm_terms:
            joined = normalize_token(" ".join(s.text for s in sentences[s_idx : e_idx + 1]))
            for term in norm_terms:
                hits = len(re.findall(rf"\b{re.escape(term)}\b", joined))
                cut_est += hits * 0.45

    return round(max(1.0, raw_dur - cut_est), 2)


def estimate_spans_post_cut_duration(
    span_indices: list[tuple[int, int]],
    sentences: list[SentenceSegment],
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
) -> float:
    """Estimate combined post-cut duration across one or more non-overlapping index spans."""
    if not span_indices:
        return 0.0
    total = 0.0
    for s_idx, e_idx in span_indices:
        total += estimate_post_cut_duration(s_idx, e_idx, sentences, silence_intervals, audio_config)
    # Subtract tiny splice crossfades
    if len(span_indices) > 1:
        total = max(1.0, total - 0.045 * (len(span_indices) - 1))
    return round(total, 2)


_DEAD_OPENING_RE = re.compile(
    r"^(?:"
    r"mientras tanto|y bueno|pero bueno|pues bueno|bueno,?\s+pues|o sea|como\s+ibamos\s+diciendo|por lo visto|"
    r"asi que|ya por cerrar|no,?\s+no\b|hola qu[eé] tal|"
    r"[oh]*la vieja guardia|en fin|como dec[ií]a|a ver\b|entonces bueno"
    r")\b",
    flags=re.IGNORECASE,
)

_PROMO_AD_PATTERNS = [
    re.compile(r"\b5[\s,.]*95\b"),
    re.compile(r"595\s+y\s+es\s*tuya"),
    re.compile(r"\btres euros al mes\b"),
    re.compile(r"\bclub de fans\b"),
    re.compile(r"\bpropaganda electoral\b"),
    re.compile(r"\benlace en la descripci"),
    re.compile(r"\bdale a (?:me |no )?gusta\b"),
    re.compile(r"\bme despido ya\b"),
    re.compile(r"\bhasta el pr[oó]ximo programa\b"),
    re.compile(r"\bedici[oó]n(?:es)? especial(?:es)? remasterizada"),
    re.compile(r"\bhist[oó]rico para fans\b"),
    re.compile(r"\bsuscr[ií]bete al canal\b"),
    re.compile(r"\bpeque[nñ]a aportaci[oó]n\b"),
    re.compile(r"\bbienvenidos a (?:otro|un nuevo) (?:programa|episodio|podcast)\b"),
    re.compile(r"\bpatreon\b"),
]

_META_PODCAST_REF_RE = re.compile(
    r"(?:\bcomo (?:hemos|os|ya) (?:dicho|comentado|visto|explicado)\b|"
    r"\bpero esto es otra historia\b|"
    r"\bsi me sobra (?:otra )?media hora\b|\bcomo dec[ií]a antes\b|"
    r"\banteriormente en el programa\b|\bme he pasado (?:un |de )?minut)",
    flags=re.IGNORECASE,
)

_TOPIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "para", "como", "pero", "sobre", "desde", "hasta", "entre", "donde", "cuando",
        "porque", "aunque", "mientras", "durante", "antes", "despues", "hacia", "contra",
        "esta", "este", "estos", "estas", "esos", "esas", "aquel", "aquella", "todo",
        "toda", "todos", "todas", "muy", "mas", "menos", "solo", "sola", "bien",
        "historia", "episodio", "podcast", "programa", "momento", "parte", "tema",
        "forma", "manera", "tiempo", "vida", "anos", "gran", "grande", "mayor",
        "primera", "primer", "segundo", "segunda", "guerra", "mundial", "habia",
        "tenia", "fueron", "siendo", "hacer", "hizo", "decia", "dijo", "puede",
        "pueden", "tras", "algo", "nada", "quien", "quienes", "cual", "cuales",
    }
)


def _strip_accents_lower(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _is_clean_sentence_end(text: str) -> bool:
    """True when sentence ends in terminal punctuation (. ? !) and not a dangling function word."""
    clean = (text or "").strip()
    if not clean.endswith((".", "?", "!")):
        return False
    return _norm_last_word(clean) not in DANGLING_END_WORDS


def _is_dead_opening(text: str, prev_text: str = "") -> bool:
    """True when a sentence begins with a dead transitional filler or is a lowercase continuation fragment."""
    clean = (text or "").strip()
    if not clean:
        return True
    stripped_prefix = clean.lstrip("¿¡\"'(- ").strip()
    norm = _strip_accents_lower(stripped_prefix)
    if _DEAD_OPENING_RE.match(norm):
        return True
    prev_clean = (prev_text or "").strip()
    if prev_clean and (
        prev_clean.endswith((",", ";", ":"))
        or _norm_last_word(prev_clean) in DANGLING_END_WORDS
    ):
        for ch in stripped_prefix:
            if ch.isalpha():
                if ch.islower():
                    return True
                break
    return False


_ORPHAN_OPENING_REF_RE = re.compile(
    r"(?:^bueno,?\s+pues\b|^asi\s+de\s+claro\b|^pues\s+est[ae]\b|^y\s+(?:ella|el)\b|"
    r"\b(?:esta\s+mujer|esta\s+chica|esta\s+senora|este\s+hombre|este\s+senor|este\s+chico|"
    r"echarle\s+un\s+vistazo\s+a\s+esto|a\s+esto|esto\s+paso|eso\s+era|aquello\s+que)\b|"
    r"^(?:ella|el)\s+(?:tenia|hizo|fue|dijo|vio|recibio|estaba|empezo|consiguio|era|sabia|decidio)\b|"
    r"\bse\s+\w+\s+ella\b)",
    flags=re.IGNORECASE,
)

_GENERIC_NOUN_STOPWORDS: frozenset[str] = frozenset(
    {"historia", "ciencia", "tecnologia", "motores", "mecanica", "aeronautica", "ingenieria", "combate aereo", "historia de la aviacion"}
)


def _opening_needs_context_bridge(
    words_or_text: Any,
    visual_bible: Optional[dict[str, Any]] = None,
) -> bool:
    """True when the first ~4-6s (~22 words) start mid-fragment or use orphan pronouns without naming the subject."""
    if isinstance(words_or_text, str):
        tokens = words_or_text.split()[:22]
    elif isinstance(words_or_text, list):
        tokens = []
        for item in words_or_text:
            tok = getattr(item, "word", None)
            if tok is None:
                tok = getattr(item, "text", None)
            if tok is None:
                tok = str(item)
            for part in str(tok).split():
                if part:
                    tokens.append(part)
                    if len(tokens) >= 22:
                        break
            if len(tokens) >= 22:
                break
    else:
        return True

    snippet = " ".join(tokens).strip()
    if not snippet:
        return True
    if _starts_with_lowercase(snippet) or _is_dead_opening(snippet):
        return True

    lowered = " ".join(snippet.lower().split())
    norm = _strip_accents_lower(snippet)

    raw_nouns = (visual_bible or {}).get("proper_nouns") or (visual_bible or {}).get("key_entities") or []
    specific_nouns = [
        _strip_accents_lower(str(n))
        for n in raw_nouns[:14]
        if len(_strip_accents_lower(str(n))) >= 3 and _strip_accents_lower(str(n)) not in _GENERIC_NOUN_STOPWORDS
    ]
    has_named_entity = any(pn in norm for pn in specific_nouns)
    if not has_named_entity:
        # Also detect mid-sentence capitalized proper nouns even when visual_bible is omitted (true blind audit)
        mid_words = snippet.split()[1:]
        for mw in mid_words:
            clean_mw = mw.strip("¿¡\"'().,;:!?")
            if len(clean_mw) >= 3 and clean_mw[0].isupper() and not clean_mw.isupper():
                has_named_entity = True
                break

    if re.search(r"\bél\b", lowered) and not has_named_entity:
        return True
    if _ORPHAN_OPENING_REF_RE.search(norm) and not has_named_entity:
        return True
    if re.search(r"\bella\b", norm) and not has_named_entity:
        return True
    return False


def _detect_blacklisted_indices(
    sentences: list[SentenceSegment],
    block_map: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    id_to_idx: dict[str, int],
) -> set[int]:
    """Identify ads, paid episode promos, campaign disclaimers, and long greeting/outro ranges."""
    n = len(sentences)
    if n == 0:
        return set()

    max_allowed = int(n * 0.40)
    llm_blocked: set[int] = set()

    for m in block_map:
        if not isinstance(m, dict):
            continue
        b_idx_raw = m.get("block_index")
        if bool(m.get("is_ad_or_promo")) and b_idx_raw is not None:
            try:
                bi = int(b_idx_raw)
                if 0 <= bi < len(blocks):
                    for bs in blocks[bi].get("sentences", []):
                        idx = id_to_idx.get(bs.id)
                        if idx is not None:
                            llm_blocked.add(idx)
            except (TypeError, ValueError):
                pass
        for rng in (m.get("excluded_ranges") or []):
            if not isinstance(rng, dict):
                continue
            sid = _normalize_sid(rng.get("start_sentence_id"), id_to_idx)
            eid = _normalize_sid(rng.get("end_sentence_id"), id_to_idx)
            if sid in id_to_idx and eid in id_to_idx:
                si, ei = id_to_idx[sid], id_to_idx[eid]
                if ei < si:
                    si, ei = ei, si
                # Sanity cap on a single range
                if (ei - si + 1) <= max(10, int(n * 0.30)):
                    llm_blocked.update(range(si, ei + 1))

    regex_hits: list[int] = []
    for idx, s in enumerate(sentences):
        norm = _strip_accents_lower(s.text)
        if any(pat.search(norm) for pat in _PROMO_AD_PATTERNS):
            regex_hits.append(idx)

    regex_blocked: set[int] = set(regex_hits)
    # Bridge small gaps (<= 10 sentences) between promo hits in the opening quarter or closing 15%
    opening_limit = int(n * 0.28)
    closing_start = int(n * 0.85)
    opening_hits = [i for i in regex_hits if i <= opening_limit]
    if len(opening_hits) >= 2:
        first_op, last_op = opening_hits[0], opening_hits[-1]
        if first_op <= 6:
            first_op = 0
        # Advance last_op up to the next clear musical/segment pause (>= 5.0s) if close by
        end_op = last_op
        for k in range(last_op, min(opening_limit, last_op + 18)):
            if k + 1 < n and (sentences[k + 1].start - sentences[k].end) >= 5.0:
                end_op = k
                break
        if (end_op - first_op + 1) <= max(12, opening_limit):
            regex_blocked.update(range(first_op, end_op + 1))

    closing_hits = [i for i in regex_hits if i >= closing_start]
    if closing_hits:
        start_cl = max(closing_start, closing_hits[0] - 8)
        for k in range(closing_hits[0], max(closing_start - 1, closing_hits[0] - 12), -1):
            if k > 0 and (sentences[k].start - sentences[k - 1].end) >= 3.0:
                start_cl = k
                break
        regex_blocked.update(range(start_cl, n))

    combined = llm_blocked | regex_blocked
    if len(combined) > max_allowed:
        return regex_blocked if len(regex_blocked) <= max_allowed else set(regex_hits)
    return combined


def _snap_span_boundaries(
    si: int,
    ei: int,
    sentences: list[SentenceSegment],
    blacklisted_indices: Optional[set[int]] = None,
    min_bound: int = 0,
    max_bound: Optional[int] = None,
) -> tuple[int, int]:
    """Align span [si, ei] onto complete grammatical sentences outside blacklisted promo/ad ranges."""
    n = len(sentences)
    if n == 0:
        return si, ei
    blocked = blacklisted_indices or set()
    lo = max(0, min_bound)
    hi = min(n - 1, max_bound if max_bound is not None else n - 1)
    si = max(lo, min(si, hi))
    ei = max(si, min(ei, hi))

    # Push start past any blacklisted indices
    while si < ei and si in blocked:
        si += 1
    while ei > si and ei in blocked:
        ei -= 1

    # Snap start to nearest clean sentence start within +-2 (Whisper-tolerant, respecting excised gaps)
    best_si = si
    best_s_score = -999
    for cand_s in range(max(lo, si - 2), min(ei, si + 2) + 1):
        if cand_s in blocked:
            continue
        if any(k in blocked for k in range(min(si, cand_s), max(si, cand_s) + 1)):
            continue
        prev_txt = sentences[cand_s - 1].text if cand_s > 0 else ""
        hard_broken_prev = (
            cand_s > 0
            and ((cand_s - 1) not in blocked)
            and (
                (_norm_last_word(prev_txt) in DANGLING_END_WORDS)
                or (
                    prev_txt.rstrip().endswith((",", ";", ":"))
                    and (sentences[cand_s].start - sentences[cand_s - 1].end) < 0.35
                )
            )
        )
        exact_punct_prev = (cand_s > 0) and _is_clean_sentence_end(prev_txt)
        dead_open = _is_dead_opening(sentences[cand_s].text, prev_txt)
        sc = (12 if not hard_broken_prev else 0) + (2 if exact_punct_prev else 0) + (10 if not dead_open else 0) - abs(cand_s - si) * 3
        if sc > best_s_score:
            best_s_score = sc
            best_si = cand_s

    # Snap end to nearest complete sentence end within [-2, +3] from ei (preferring minimal shift)
    best_ei = ei
    best_e_score = -999
    for cand_e in range(max(best_si, ei - 2), min(hi, ei + 3) + 1):
        if cand_e in blocked:
            continue
        # Ensure we don't jump across a blacklisted sentence
        if any(k in blocked for k in range(min(ei, cand_e), max(ei, cand_e) + 1)):
            continue
        txt = sentences[cand_e].text.strip()
        clean_end = _is_clean_sentence_end(txt)
        non_dangling = _norm_last_word(txt) not in DANGLING_END_WORDS
        sc = (20 if clean_end else (5 if non_dangling else -15)) - abs(cand_e - ei) * 3
        if sc > best_e_score:
            best_e_score = sc
            best_ei = cand_e

    return best_si, max(best_si, best_ei)


def adjust_bounds_to_window(
    start_idx: int,
    end_idx: int,
    sentences: list[SentenceSegment],
    window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    min_bound_idx: int = 0,
    max_bound_idx: Optional[int] = None,
    blacklisted_indices: Optional[set[int]] = None,
) -> tuple[int, int, float]:
    """Adjust [start_idx, end_idx] on complete sentence boundaries without entering blacklisted ranges."""
    n = len(sentences)
    blocked = blacklisted_indices or set()
    lo_limit = max(0, min_bound_idx)
    hi_limit = min(n - 1, max_bound_idx if max_bound_idx is not None else n - 1)

    s = max(lo_limit, min(start_idx, hi_limit))
    e = max(s, min(end_idx, hi_limit))

    while s < hi_limit and s in blocked:
        s += 1
    e = max(s, e)
    while e > s and e in blocked:
        e -= 1

    est = estimate_post_cut_duration(s, e, sentences, silence_intervals, audio_config)

    while est < window.min_sec and (e < hi_limit or s > lo_limit):
        grew = False
        if e < hi_limit and (e + 1) not in blocked:
            cand = estimate_post_cut_duration(s, e + 1, sentences, silence_intervals, audio_config)
            if cand <= window.max_sec + 8.0 or est < window.min_sec:
                e += 1
                est = cand
                grew = True
        if est < window.min_sec and s > lo_limit and (s - 1) not in blocked and not grew:
            cand = estimate_post_cut_duration(s - 1, e, sentences, silence_intervals, audio_config)
            s -= 1
            est = cand
            grew = True
        if not grew:
            break

    while est > window.max_sec and e > s:
        cand_end = estimate_post_cut_duration(s, e - 1, sentences, silence_intervals, audio_config)
        if cand_end >= window.min_sec or e - s <= 2:
            e -= 1
            est = cand_end
        else:
            cand_start = estimate_post_cut_duration(s + 1, e, sentences, silence_intervals, audio_config)
            if cand_start >= window.min_sec and (s + 1) not in blocked:
                s += 1
                est = cand_start
            else:
                e -= 1
                est = cand_end
                break

    # Final syntactic snap so we never stop mid-phrase ("por lo", "a los", "el")
    if sentences and not _is_clean_sentence_end(sentences[e].text):
        snapped = False
        # First try extending slightly (+1..+2 sentences within +5.5s grace over window.max_sec)
        for step in (1, 2):
            if e + step <= hi_limit and not any((e + k) in blocked for k in range(1, step + 1)):
                if _is_clean_sentence_end(sentences[e + step].text):
                    cand_dur = estimate_post_cut_duration(s, e + step, sentences, silence_intervals, audio_config)
                    if cand_dur <= window.max_sec + 5.5:
                        e = e + step
                        est = cand_dur
                        snapped = True
                        break
        # Otherwise retreat up to 2 sentences if still well within minimum duration
        if not snapped:
            for step in (1, 2):
                if e - step >= s and _is_clean_sentence_end(sentences[e - step].text):
                    cand_dur = estimate_post_cut_duration(s, e - step, sentences, silence_intervals, audio_config)
                    if cand_dur >= window.min_sec * 0.85:
                        e = e - step
                        est = cand_dur
                        break

    return s, e, est


def _adjust_multispan_to_window(
    spans_idx: list[tuple[int, int, str]],
    sentences: list[SentenceSegment],
    window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    blacklisted_indices: Optional[set[int]] = None,
) -> tuple[list[tuple[int, int, str]], float]:
    """Adjust optional leading cold_open_hook + chronological multi-span selection without overlap."""
    blocked = blacklisted_indices or set()
    if not spans_idx:
        return [], 0.0

    cold_open_span: Optional[tuple[int, int, str]] = None
    chrono_raw = list(spans_idx)
    if chrono_raw and chrono_raw[0][2] == "cold_open_hook":
        co_s, co_e, _ = chrono_raw.pop(0)
        co_s, co_e = _snap_span_boundaries(
            co_s,
            co_e,
            sentences,
            blacklisted_indices=blocked,
            min_bound=0,
        )
        while co_e > co_s and estimate_post_cut_duration(co_s, co_e, sentences, silence_intervals, audio_config) > 8.5:
            co_e -= 1
        cold_open_span = (co_s, co_e, "cold_open_hook")

    cold_open_indices = set(range(cold_open_span[0], cold_open_span[1] + 1)) if cold_open_span else set()
    excised_gap_indices: set[int] = set()
    for idx_sp in range(len(chrono_raw) - 1):
        prev_e = chrono_raw[idx_sp][1]
        next_s = chrono_raw[idx_sp + 1][0]
        if next_s > prev_e + 1:
            excised_gap_indices.update(range(prev_e + 1, next_s))
    blocked_for_chrono = blocked | cold_open_indices | excised_gap_indices

    cleaned_chrono: list[tuple[int, int, str]] = []
    min_cur = 0
    for si, ei, role in chrono_raw:
        ns_i, ne_i = _snap_span_boundaries(
            si,
            ei,
            sentences,
            blacklisted_indices=blocked_for_chrono,
            min_bound=min_cur,
        )
        if cleaned_chrono and ns_i <= cleaned_chrono[-1][1]:
            ns_i = cleaned_chrono[-1][1] + 1
        if ns_i <= ne_i and ns_i < len(sentences):
            run_s: Optional[int] = None
            for k in range(ns_i, ne_i + 1):
                if k in cold_open_indices:
                    if run_s is not None:
                        cleaned_chrono.append((run_s, k - 1, role))
                        run_s = None
                else:
                    if run_s is None:
                        run_s = k
            if run_s is not None:
                cleaned_chrono.append((run_s, ne_i, role))
            if cleaned_chrono:
                min_cur = cleaned_chrono[-1][1] + 1

    if not cleaned_chrono:
        if cold_open_span is not None:
            ns, ne, est = adjust_bounds_to_window(
                cold_open_span[0],
                cold_open_span[1],
                sentences,
                window,
                silence_intervals,
                audio_config,
                blacklisted_indices=blocked,
            )
            return [(ns, ne, "core")], est
        cleaned_chrono = [(si, ei, role) for si, ei, role in spans_idx if role != "cold_open_hook"] or spans_idx

    def _finish_single_chrono(single: tuple[int, int, str]) -> tuple[list[tuple[int, int, str]], float]:
        s0, e0, role0 = single
        if cold_open_span is None:
            ns, ne, est = adjust_bounds_to_window(
                s0,
                e0,
                sentences,
                window,
                silence_intervals,
                audio_config,
                blacklisted_indices=blocked,
            )
            return [(ns, ne, role0)], est
        co_dur = estimate_post_cut_duration(
            cold_open_span[0], cold_open_span[1], sentences, silence_intervals, audio_config
        )
        rem_min = max(6.0, window.min_sec - co_dur)
        rem_max = max(rem_min + 5.0, window.max_sec - co_dur)
        ns, ne, _ = adjust_bounds_to_window(
            s0,
            e0,
            sentences,
            DurationWindow(min_sec=rem_min, max_sec=rem_max),
            silence_intervals,
            audio_config,
            min_bound_idx=0,
            blacklisted_indices=blocked_for_chrono,
        )
        out = [cold_open_span, (ns, ne, role0)]
        est = estimate_spans_post_cut_duration([(s, e) for s, e, _ in out], sentences, silence_intervals, audio_config)
        return out, est

    if len(cleaned_chrono) == 1:
        return _finish_single_chrono(cleaned_chrono[0])

    # Multi-span chronological path (with optional cold_open_span)
    def _combined_dur(chrono_list: list[tuple[int, int, str]]) -> float:
        all_pairs = ([(cold_open_span[0], cold_open_span[1])] if cold_open_span else []) + [
            (s, e) for s, e, _ in chrono_list
        ]
        return estimate_spans_post_cut_duration(all_pairs, sentences, silence_intervals, audio_config)

    # If over window.max_sec and the last span is a dedicated 'payoff', trim an earlier 'core' span first
    cur_total = _combined_dur(cleaned_chrono)
    if cur_total > window.max_sec and cleaned_chrono[-1][2] == "payoff":
        core_idxs = [i for i, (_, _, r) in enumerate(cleaned_chrono[:-1]) if r == "core"]
        if core_idxs:
            core_idx = max(core_idxs, key=lambda idx: cleaned_chrono[idx][1] - cleaned_chrono[idx][0])
            cs, ce, crole = cleaned_chrono[core_idx]
            while cur_total > window.max_sec and (ce - cs) >= 2:
                next_ce = None
                for back_e in range(ce - 1, cs, -1):
                    if _is_clean_sentence_end(sentences[back_e].text):
                        next_ce = back_e
                        break
                if next_ce is None:
                    next_ce = ce - 1
                cand_list = list(cleaned_chrono)
                cand_list[core_idx] = (cs, next_ce, crole)
                if _combined_dur(cand_list) < window.min_sec * 0.9:
                    break
                cleaned_chrono = cand_list
                ce = next_ce
                cur_total = _combined_dur(cleaned_chrono)

    # Drop excess context_intro spans from the back (keeping the first) if still well over window
    has_payoff_tail = bool(cleaned_chrono and cleaned_chrono[-1][2] == "payoff")
    soft_max_cap = window.max_sec + (6.0 if has_payoff_tail else 4.0)
    cur_total = _combined_dur(cleaned_chrono)
    if cur_total > soft_max_cap:
        ctx_idxs = [i for i, (_, _, r) in enumerate(cleaned_chrono) if r == "context_intro"]
        for drop_idx in reversed(ctx_idxs[1:]):
            if len(cleaned_chrono) <= 1 or cur_total <= soft_max_cap:
                break
            cand_list = [sp for i, sp in enumerate(cleaned_chrono) if i != drop_idx]
            cand_dur = _combined_dur(cand_list)
            if cand_dur >= window.min_sec * 0.9:
                cleaned_chrono = cand_list
                cur_total = cand_dur

    if len(cleaned_chrono) == 1:
        return _finish_single_chrono(cleaned_chrono[0])

    # Iteratively trim sentences off the end of the longest shrinkable span if still over window
    unshrinkable: set[int] = set()
    while _combined_dur(cleaned_chrono) > soft_max_cap:
        pool_idxs = [
            i
            for i, (cs, ce, role) in enumerate(cleaned_chrono)
            if i not in unshrinkable and ce > cs and role != "payoff"
        ]
        if not pool_idxs:
            pool_idxs = [
                i
                for i, (cs, ce, role) in enumerate(cleaned_chrono)
                if i not in unshrinkable and ce > cs and role != "context_intro"
            ]
        if not pool_idxs:
            pool_idxs = [
                i
                for i, (cs, ce, _) in enumerate(cleaned_chrono)
                if i not in unshrinkable and ce > cs
            ]
        if not pool_idxs:
            break
        target_idx = max(
            pool_idxs,
            key=lambda idx: estimate_post_cut_duration(
                cleaned_chrono[idx][0],
                cleaned_chrono[idx][1],
                sentences,
                silence_intervals,
                audio_config,
            ),
        )
        cs, ce, crole = cleaned_chrono[target_idx]
        next_ce: Optional[int] = None
        for back_e in range(ce - 1, cs - 1, -1):
            if _is_clean_sentence_end(sentences[back_e].text):
                next_ce = back_e
                break
        if next_ce is None:
            next_ce = ce - 1

        cand_list = list(cleaned_chrono)
        cand_list[target_idx] = (cs, next_ce, crole)
        cand_dur = _combined_dur(cand_list)
        if cand_dur < window.min_sec * 0.85 and next_ce < ce - 1:
            next_ce = ce - 1
            cand_list[target_idx] = (cs, next_ce, crole)
            cand_dur = _combined_dur(cand_list)
        if cand_dur < window.min_sec * 0.85:
            unshrinkable.add(target_idx)
            continue
        cleaned_chrono = cand_list

    fixed = cleaned_chrono[:-1]
    fixed_pairs = ([(cold_open_span[0], cold_open_span[1])] if cold_open_span else []) + [(s, e) for s, e, _ in fixed]
    fixed_dur = estimate_spans_post_cut_duration(fixed_pairs, sentences, silence_intervals, audio_config)
    last_s, last_e, last_role = cleaned_chrono[-1]
    min_bound = fixed[-1][1] + 1

    if last_role == "payoff" and _combined_dur(cleaned_chrono) <= window.max_sec + 6.0 and _combined_dur(cleaned_chrono) >= window.min_sec:
        out = ([cold_open_span] if cold_open_span else []) + cleaned_chrono
        return out, _combined_dur(cleaned_chrono)

    rem_min = max(6.0, window.min_sec - fixed_dur)
    rem_max = max(rem_min + 5.0, window.max_sec - fixed_dur)
    ns, ne, _ = adjust_bounds_to_window(
        last_s,
        last_e,
        sentences,
        DurationWindow(min_sec=rem_min, max_sec=rem_max),
        silence_intervals,
        audio_config,
        min_bound_idx=min_bound,
        blacklisted_indices=blocked_for_chrono,
    )
    final_chrono = [*fixed, (ns, ne, last_role)]
    out = ([cold_open_span] if cold_open_span else []) + final_chrono
    est = estimate_spans_post_cut_duration([(s, e) for s, e, _ in out], sentences, silence_intervals, audio_config)
    return out, est


def _resolve_adaptive_windows(
    total_duration: float,
    profile: PodcasterProfile,
    req_medium: int,
    req_long: int,
) -> tuple[DurationWindow, DurationWindow, int, int, list[str]]:
    """Gracefully adjust medium/long windows if the source podcast is shorter than requested targets."""
    warnings: list[str] = []
    med_win = profile.quotas.medium_window.model_copy()
    long_win = profile.quotas.long_window.model_copy()
    eff_med = req_medium
    eff_long = req_long

    usable_est = total_duration * 0.90

    if eff_long > 0 and usable_est < long_win.min_sec:
        if total_duration < 150.0:
            eff_long = 0
            warnings.append(
                f"El audio dura {total_duration / 60:.1f} min, demasiado corto para generar un vídeo largo; "
                "se ha omitido el vídeo largo."
            )
        else:
            new_max = round(max(120.0, usable_est * 0.92), 1)
            new_min = round(max(90.0, new_max * 0.72), 1)
            long_win = DurationWindow(min_sec=new_min, max_sec=new_max)
            warnings.append(
                f"El podcast dura {total_duration / 60:.1f} min, menor que la ventana larga "
                f"({profile.quotas.long_window.min_sec / 60:.1f}–{profile.quotas.long_window.max_sec / 60:.1f} min). "
                f"Objetivo largo ajustado automáticamente a {new_min / 60:.1f}–{new_max / 60:.1f} min."
            )

    if eff_med > 0 and usable_est < med_win.min_sec:
        if total_duration < 90.0:
            eff_med = 0
            warnings.append(
                f"El audio dura {total_duration / 60:.1f} min, demasiado corto para vídeo medio; se ha omitido."
            )
        else:
            new_max = round(max(75.0, usable_est * 0.85), 1)
            new_min = round(max(60.0, new_max * 0.68), 1)
            med_win = DurationWindow(min_sec=new_min, max_sec=new_max)
            warnings.append(
                f"El podcast dura {total_duration / 60:.1f} min, menor que la ventana media "
                f"({profile.quotas.medium_window.min_sec / 60:.1f}–{profile.quotas.medium_window.max_sec / 60:.1f} min). "
                f"Objetivo medio ajustado automáticamente a {new_min / 60:.1f}–{new_max / 60:.1f} min."
            )

    return med_win, long_win, eff_med, eff_long, warnings


def _profile_editorial_context(profile: PodcasterProfile) -> str:
    return (
        f"Podcast: {profile.podcast_name}\n"
        f"Plataforma principal: {profile.target_platform}\n"
        f"Nicho: {profile.niche_description or 'General'}\n"
        f"Tono del programa: {profile.show_tone}\n"
        f"Criterios virales del podcaster: {profile.editorial_instructions}"
    )


def _normalize_sid(raw_val: Any, id_to_idx: dict[str, int]) -> str:
    """Normalize sentence IDs like '[S0015]', 'S15', or integer 15 to match dictionary keys."""
    cleaned = re.sub(r"[\[\]\s]", "", str(raw_val or "")).upper()
    if cleaned in id_to_idx:
        return cleaned
    m = re.fullmatch(r"S?0*(\d+)", cleaned)
    if m:
        padded = f"S{int(m.group(1)):04d}"
        if padded in id_to_idx:
            return padded
    return cleaned


def _normalize_score(raw_val: Any, default: int = 78) -> int:
    """Normalize LLM scores whether provided in 0-1, 1-10, or 0-100 scale."""
    try:
        val = float(raw_val)
    except (TypeError, ValueError):
        return default
    if 0.0 < val <= 1.0:
        val *= 100.0
    elif 1.0 < val <= 10.0:
        val *= 10.0
    return max(40, min(98, int(round(val))))


def _phrase_exists_in_tokens(phrase_parts: list[str], stream_tokens: list[str]) -> bool:
    """Return True if normalized phrase_parts appear consecutively inside stream_tokens."""
    k = len(phrase_parts)
    if k == 0 or k > len(stream_tokens):
        return False
    first = phrase_parts[0]
    limit = len(stream_tokens) - k + 1
    for i in range(limit):
        if stream_tokens[i] == first and stream_tokens[i : i + k] == phrase_parts:
            return True
    return False


def extract_episode_glossary_and_bible(
    sentences: list[SentenceSegment],
    block_map: list[dict[str, Any]],
    profile: PodcasterProfile,
    llm: OpenRouterClient,
    blacklisted_indices: Optional[set[int]] = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Extract grounded episode proper nouns glossary and Visual Bible, ignoring ad/promo segments."""
    blocked = blacklisted_indices or set()
    clean_sents = [s for i, s in enumerate(sentences) if i not in blocked]
    if not clean_sents:
        clean_sents = list(sentences)

    if len(clean_sents) <= 180:
        sample_sents = clean_sents
    else:
        # Evenly sample ~180 sentences across opening, quartiles, middle, and ending so mid-episode distortions are caught
        target_n = 180
        step = (len(clean_sents) - 1) / float(target_n - 1)
        idxs = sorted({min(len(clean_sents) - 1, int(round(k * step))) for k in range(target_n)})
        sample_sents = [clean_sents[i] for i in idxs]

    # Build full normalized token stream across all episode sentences for strict grounding verification
    all_episode_tokens: list[str] = []
    for sent in clean_sents:
        all_episode_tokens.extend(_normalize_glossary_token(sent.text).split())

    clean_blocks = [m for m in block_map if not m.get("is_ad_or_promo")] or block_map
    map_summary = "\n".join(
        f"- Bloque {m.get('block_index')}: {m.get('tema', '')} ({m.get('resumen_breve', '')})"
        for m in clean_blocks[:32]
    )

    sys_prompt = (
        "Eres lingüista forense, historiador/especialista de dominio y director creativo de documentales. "
        "Tu misión doble es:\n"
        "1. Detectar nombres propios reales, siglas, lugares, modelos y términos técnicos mal transcritos fonéticamente por Whisper "
        "en ESTE episodio concreto (cuando el ASR escribe fonéticamente un nombre real o término extranjero/técnico) "
        "y construir un glosario limpio de corrección.\n"
        "2. Definir la Biblia Visual y la columna vertebral narrativa ('narrative_spine') de ESTE episodio.\n\n"
        "REGLAS CRÍTICAS ANTI-ALUCINACIÓN:\n"
        "- Cada clave de 'glossary' DEBE aparecer LITERALMENTE en la transcripción proporcionada abajo.\n"
        "- PROHIBIDO inventar términos, copiar ejemplos externos o añadir personajes/lugares que no pertenezcan a este episodio.\n"
        "- SOLO incluye en 'glossary' distorsiones fonéticas claras de 1 a 5 palabras cuyo término real aparezca o sea inequívocamente aludido en el episodio.\n"
        "- PROHIBIDO incluir palabras comunes del español o frases explicativas largas en 'glossary'.\n"
        "- IGNORA por completo anuncios, autopromociones de otros episodios o menciones a plataformas.\n"
        "Responde exclusivamente en JSON válido."
    )
    usr_prompt = (
        f"{_profile_editorial_context(profile)}\n"
        f"Tono visual deseado por el perfil: {profile.visual.visual_tone}\n\n"
        f"RESUMEN DE BLOQUES:\n{map_summary or 'Episodio corto'}\n\n"
        f"MUESTRA AMPLIA DE TRANSCRIPCIÓN (SIN ANUNCIOS):\n{_format_sentences_block(sample_sents)}\n\n"
        "Devuelve JSON exactamente con:\n"
        "{\n"
        '  "episode_topic": "Tema central del episodio en 1 frase clara con protagonista/tema y ámbito",\n'
        '  "era_or_setting": "Época exacta, ámbito o escenario real de este episodio",\n'
        '  "niche_category": "historia | tecnologia | negocios | ciencia | cultura | actualidad | entrevista | otro",\n'
        '  "narrative_spine": "Resumen de 2-3 frases explicando el hilo conductor completo del episodio (contexto inicial -> conflicto/desarrollo -> desenlace)",\n'
        '  "proper_nouns": ["Lista de hasta 20 nombres propios, lugares, máquinas o términos técnicos reales del tema central de este episodio (sin anuncios)"],\n'
        '  "glossary": {"texto_erroneo_literal_del_transcript": "forma_correcta_real"} (SOLO 1-5 palabras por clave, LITERALES del transcript, y 1-6 por valor),\n'
        '  "visual_bible": {\n'
        '    "visual_style": "Descripción visual en inglés para guiar las búsquedas acorde al episodio",\n'
        '    "prefer_historical_archive": true/false (true solo si el tema es histórico/documental clásico de dominio público),\n'
        '    "forbidden_anachronisms": ["elementos visuales prohibidos en inglés incoherentes con la época/tema"],\n'
        '    "concrete_filmable_motifs": ["6-8 objetos o escenas reales y filmables en inglés de 2-5 palabras cada una, coherentes con el episodio"],\n'
        '    "fallback_query": "Query limpia, concreta y filmable en inglés de 2 a 4 palabras acorde al tema"\n'
        "  }\n"
        "}"
    )

    try:
        resp = llm.chat_json("editorial", sys_prompt, usr_prompt, temperature=0.1, max_tokens=2600, stage="glossary_and_bible")
        raw_gloss = resp.get("glossary") or {}
        glossary: dict[str, str] = {}
        for k, v in raw_gloss.items():
            k_clean = " ".join(str(k).strip().split())
            v_clean = " ".join(str(v).strip().split())
            k_parts = _normalize_glossary_token(k_clean).split()
            if (
                k_clean
                and v_clean
                and k_parts
                and k_clean.lower() != v_clean.lower()
                and _normalize_glossary_token(k_clean) != _normalize_glossary_token(v_clean)
                and len(k_clean) <= 48
                and len(v_clean) <= 56
                and len(k_parts) <= 5
                and len(v_clean.split()) <= 6
                and _phrase_exists_in_tokens(k_parts, all_episode_tokens)
            ):
                glossary[k_clean] = v_clean
        bible = resp.get("visual_bible") or {}
        if not isinstance(bible, dict):
            bible = {}
        bible.setdefault("episode_topic", str(resp.get("episode_topic") or ""))
        bible.setdefault("era_or_setting", str(resp.get("era_or_setting") or ""))
        bible.setdefault("niche_category", str(resp.get("niche_category") or "otro"))
        if resp.get("narrative_spine") and not bible.get("narrative_spine"):
            bible["narrative_spine"] = str(resp.get("narrative_spine") or "").strip()
        raw_pn = bible.get("proper_nouns")
        if raw_pn is None:
            raw_pn = resp.get("proper_nouns") or []
        if isinstance(raw_pn, list):
            bible["proper_nouns"] = [
                " ".join(str(x).strip().split())
                for x in raw_pn
                if str(x).strip()
                and len(" ".join(str(x).strip().split())) <= 48
                and len(" ".join(str(x).strip().split()).split()) <= 5
            ][:16]
        else:
            bible["proper_nouns"] = []
        bible.setdefault("visual_style", profile.visual.visual_tone)
        bible.setdefault("prefer_historical_archive", False)
        bible.setdefault("forbidden_anachronisms", [])
        bible.setdefault("concrete_filmable_motifs", [])
        fb_q = " ".join(str(bible.get("fallback_query") or "").strip().split())
        if not fb_q:
            fb_q = "cinematic dramatic moody atmosphere"
        elif len(fb_q.split()) > 4:
            fb_q = " ".join(fb_q.split()[:4])
        bible["fallback_query"] = fb_q
        return glossary, bible
    except Exception:
        return {}, {
            "episode_topic": profile.podcast_name,
            "era_or_setting": "",
            "niche_category": "otro",
            "narrative_spine": "",
            "proper_nouns": [],
            "visual_style": profile.visual.visual_tone,
            "prefer_historical_archive": False,
            "forbidden_anachronisms": [],
            "concrete_filmable_motifs": [],
            "fallback_query": "cinematic atmospheric scene",
        }


def refine_clip_glossary_from_pass2(
    words: list[WordTimestamp],
    visual_bible: Optional[dict[str, Any]],
    existing_glossary: Optional[dict[str, str]],
    llm: Optional[OpenRouterClient],
) -> dict[str, str]:
    """Detect residual Pass-2 ASR phonetic distortions on proper nouns/terms inside a single clip (grounded)."""
    if not words or llm is None:
        return {}
    vb = visual_bible or {}
    proper_nouns = [str(x).strip() for x in (vb.get("proper_nouns") or []) if str(x).strip()][:16]
    clip_tokens: list[str] = []
    for w in words:
        clip_tokens.extend(_normalize_glossary_token(w.word).split())
    if len(clip_tokens) < 6:
        return {}

    raw_text = " ".join(w.word.strip() for w in words if w.word.strip())
    if len(raw_text) > 4500:
        raw_text = raw_text[:4500]

    sys_prompt = (
        "Eres corrector ortográfico forense para subtítulos de vídeo en español.\n"
        "Detecta ÚNICAMENTE erratas fonéticas evidentes de Whisper en nombres propios, lugares, siglas o expresiones mal oídas "
        "dentro del texto literal de abajo.\n"
        "REGLAS ESTRICTAS:\n"
        "- Cada clave del JSON 'fixes' DEBE aparecer literalmente en el texto proporcionado.\n"
        "- NO cambies palabras comunes bien escritas, NO inventes sinónimos ni cambies el sentido.\n"
        "- Si no hay erratas claras, devuelve {\"fixes\": {}}.\n"
        "Responde SOLO JSON válido: {\"fixes\": {\"errata_literal\": \"forma_correcta\"}}."
    )
    usr_prompt = (
        f"Tema del episodio: {vb.get('episode_topic', '')}\n"
        f"Época/Ámbito: {vb.get('era_or_setting', '')}\n"
        f"Nombres propios conocidos: {', '.join(proper_nouns) if proper_nouns else 'No especificados'}\n"
        f"Glosario previo: {json.dumps(existing_glossary or {}, ensure_ascii=False)}\n\n"
        f"TEXTO LITERAL DEL CLIP (PASADA 2):\n{raw_text}"
    )
    try:
        resp = llm.chat_json("fast", sys_prompt, usr_prompt, temperature=0.05, max_tokens=700, stage="pass2_glossary")
    except Exception:
        return {}

    raw_fixes = resp.get("fixes") or resp.get("glossary") or {}
    if not isinstance(raw_fixes, dict):
        return {}
    grounded: dict[str, str] = {}
    for k, v in raw_fixes.items():
        k_clean = " ".join(str(k).strip().split())
        v_clean = " ".join(str(v).strip().split())
        k_parts = _normalize_glossary_token(k_clean).split()
        if (
            k_clean
            and v_clean
            and k_parts
            and _normalize_glossary_token(k_clean) != _normalize_glossary_token(v_clean)
            and 1 <= len(k_parts) <= 5
            and 1 <= len(v_clean.split()) <= 6
            and len(k_clean) <= 48
            and len(v_clean) <= 56
            and _phrase_exists_in_tokens(k_parts, clip_tokens)
        ):
            grounded[k_clean] = v_clean
    return grounded


def apply_glossary_to_sentences(
    sentences: list[SentenceSegment],
    glossary: dict[str, str],
) -> list[SentenceSegment]:
    """Apply strict glossary corrections to Pass 1 sentences before selection & blind audit."""
    if not sentences or not glossary:
        return sentences

    rules: list[tuple[list[str], str]] = []
    for wrong, right in glossary.items():
        norm_parts = _normalize_glossary_token(wrong).split()
        clean_right = " ".join((right or "").strip().split())
        if (
            norm_parts
            and len(norm_parts) <= 5
            and clean_right
            and len(clean_right.split()) <= 6
            and _normalize_glossary_token(clean_right) != " ".join(norm_parts)
        ):
            rules.append((norm_parts, clean_right))

    if not rules:
        return sentences

    rules.sort(key=lambda r: len(r[0]), reverse=True)
    updated: list[SentenceSegment] = []

    for sent in sentences:
        tokens = sent.text.split()
        if not tokens:
            updated.append(sent)
            continue
        norm_toks = [_normalize_glossary_token(t) for t in tokens]
        out_toks: list[str] = []
        i = 0
        n = len(tokens)
        changed = False
        while i < n:
            matched = False
            for wrong_parts, right_phrase in rules:
                k = len(wrong_parts)
                if i + k <= n and norm_toks[i : i + k] == wrong_parts:
                    lead_m = re.match(r'^([¿¡"\'(]+)', tokens[i])
                    lead = lead_m.group(1) if lead_m else ""
                    trail_m = re.search(r'([.,;:!?)"\']+)$', tokens[i + k - 1])
                    trail = trail_m.group(1) if trail_m else ""
                    rep_words = right_phrase.split()
                    if out_toks and len(rep_words) >= 2:
                        prev_norm = _normalize_glossary_token(out_toks[-1])
                        if prev_norm and _normalize_glossary_token(rep_words[0]) == prev_norm:
                            rep_words = rep_words[1:]
                    if rep_words:
                        if lead and not rep_words[0].startswith(lead):
                            rep_words[0] = lead + rep_words[0]
                        if trail and not rep_words[-1].endswith(tuple(".,;:!?")):
                            rep_words[-1] = rep_words[-1] + trail
                        out_toks.extend(rep_words)
                    i += k
                    matched = True
                    changed = True
                    break
            if not matched:
                out_toks.append(tokens[i])
                i += 1
        if changed:
            updated.append(sent.model_copy(update={"text": " ".join(out_toks)}))
        else:
            updated.append(sent)
    return updated


def _map_single_block_batch(
    llm: OpenRouterClient,
    profile: PodcasterProfile,
    batch_blocks: list[dict[str, Any]],
    show_speakers: bool = False,
) -> list[dict[str, Any]]:
    blocks_text = []
    for b in batch_blocks:
        blocks_text.append(
            f"=== BLOQUE {b['block_index']} ({b['orig_start']:.0f}s - {b['orig_end']:.0f}s | "
            f"energía_acústica: {b['energy']} | {b['first_id']}..{b['last_id']}) ===\n"
            + _format_sentences_block(b["sentences"], show_speakers=show_speakers, include_durations=True)
        )

    sys_prompt = (
        "Eres un Analista Editorial y Showrunner experto en estructura narrativa de podcasts y documentales. "
        "Para cada bloque de transcripción:\n"
        "1. Identifica el tema concreto, si continúa la historia del bloque anterior y si introduce por primera vez "
        "a un protagonista, lugar o problema clave (`introduces_context`).\n"
        "2. Marca con precisión quirúrgica los rangos exactos `[S####..S####]` que son anuncios, cuñas publicitarias, "
        "venta de episodios de pago, saludos largos al público o despedidas finales.\n"
        "3. Evalúa el potencial real para vídeo corto con intriga (`potencial_cliffhanger`) y para vídeo corto conclusivo (`potencial_conclusivo`).\n"
        "Nunca calcules tiempos; usa únicamente el `block_index` y los IDs `[S####]` indicados. Responde estrictamente en JSON con la clave 'blocks'."
    )
    usr_prompt = (
        f"{_profile_editorial_context(profile)}\n\n"
        "Para cada bloque devuelve un objeto JSON con:\n"
        "- block_index (int)\n"
        "- tema (string corto y específico, ej. 'Fallo del carburador del Spitfire' en vez de 'Historia')\n"
        "- continua_tema_anterior (boolean)\n"
        "- introduces_context (boolean: true si este bloque presenta por primera vez al protagonista, época o premisa del episodio)\n"
        "- is_ad_or_promo (boolean: true solo si TODO o casi todo el bloque es publicidad, promo de pago o despedida)\n"
        '- excluded_ranges (lista de objetos {"start_sentence_id": "S...", "end_sentence_id": "S...", "reason": "ad|promo|greeting|outro"} '
        "marcando exactamente qué frases dentro del bloque son anuncios, cuñas, venta de episodios de pago, saludos de bienvenida o despedidas finales; lista vacía [] si no hay)\n"
        "- interes_general (int 1-10)\n"
        "- potencial_cliffhanger (int 1-10: alto si hay conflicto/tensión/misterio fuerte cuyo desenlace llega más tarde)\n"
        "- potencial_conclusivo (int 1-10: alto si contiene una mini-historia completa, dato contraintuitivo con explicación o pregunta-respuesta con remate real)\n"
        "- resumen_breve (1-2 frases precisas indicando nombres, hechos y si la historia cierra dentro del bloque o sigue después)\n\n"
        + "\n\n".join(blocks_text)
    )
    resp = llm.chat_json("fast", sys_prompt, usr_prompt, temperature=0.15, max_tokens=3200, stage="map_blocks")
    items = resp.get("blocks") or resp.get("items") or []
    return [x for x in items if isinstance(x, dict)]


def _parse_raw_spans(
    raw: dict[str, Any],
    id_to_idx: dict[str, int],
    blacklisted_indices: Optional[set[int]] = None,
    allow_payoff_role: bool = True,
) -> list[tuple[int, int, str]]:
    """Extract optional leading cold_open_hook + up to 4 chronological spans, stripping ads & dropped digressions."""
    raw_spans = raw.get("spans")
    cold_open: Optional[tuple[int, int]] = None
    chrono_raw: list[tuple[int, int, str]] = []
    valid_roles = {"context_intro", "question", "core", "payoff", "cold_open_hook"}
    if isinstance(raw_spans, list) and raw_spans:
        for sp in raw_spans[:5]:
            if not isinstance(sp, dict):
                continue
            sid = _normalize_sid(sp.get("start_sentence_id"), id_to_idx)
            eid = _normalize_sid(sp.get("end_sentence_id"), id_to_idx)
            if sid not in id_to_idx or eid not in id_to_idx:
                continue
            si, ei = id_to_idx[sid], id_to_idx[eid]
            if ei < si:
                si, ei = ei, si
            role = str(sp.get("role") or "core").strip().lower()
            if role not in valid_roles:
                role = "core"
            if role == "payoff" and not allow_payoff_role:
                role = "core"
            if role == "cold_open_hook":
                if cold_open is None:
                    co_end = min(ei, si + 1) if (ei - si + 1) > 3 else ei
                    cold_open = (si, co_end)
            else:
                chrono_raw.append((si, ei, role))

    if cold_open is None and not chrono_raw:
        s_id = _normalize_sid(raw.get("start_sentence_id"), id_to_idx)
        e_id = _normalize_sid(raw.get("end_sentence_id"), id_to_idx)
        if s_id in id_to_idx and e_id in id_to_idx:
            si, ei = id_to_idx[s_id], id_to_idx[e_id]
            if ei < si:
                si, ei = ei, si
            chrono_raw.append((si, ei, "core"))

    if cold_open is None and not chrono_raw:
        return []

    drop_indices: set[int] = set()
    raw_drops = raw.get("drop_sentence_ids") or []
    if isinstance(raw_drops, list):
        for d_raw in raw_drops[:10]:
            d_sid = _normalize_sid(d_raw, id_to_idx)
            if d_sid in id_to_idx:
                drop_indices.add(id_to_idx[d_sid])

    blocked = (blacklisted_indices or set()) | drop_indices

    # Strip blacklisted indices from cold open
    clean_cold: Optional[tuple[int, int]] = None
    if cold_open is not None:
        co_s, co_e = cold_open
        run_s: Optional[int] = None
        for k in range(co_s, co_e + 1):
            if k in blocked:
                if run_s is not None:
                    clean_cold = (run_s, k - 1)
                    break
            elif run_s is None:
                run_s = k
        if clean_cold is None and run_s is not None:
            clean_cold = (run_s, co_e)

    # Strictly chronological order for chronological spans + strip blacklisted indices
    chrono_raw.sort(key=lambda x: (x[0], x[1]))
    filtered: list[tuple[int, int, str]] = []
    for si, ei, role in chrono_raw:
        clean_run_s: Optional[int] = None
        for k in range(si, ei + 1):
            if k in blocked:
                if clean_run_s is not None:
                    filtered.append((clean_run_s, k - 1, role))
                    clean_run_s = None
            else:
                if clean_run_s is None:
                    clean_run_s = k
        if clean_run_s is not None:
            filtered.append((clean_run_s, ei, role))

    merged: list[tuple[int, int, str]] = []
    if filtered:
        merged.append(filtered[0])
        for si, ei, role in filtered[1:]:
            ps, pe, prole = merged[-1]
            if si <= pe + 1:
                keep_role = "core" if ("core" in (prole, role)) else prole
                merged[-1] = (ps, max(pe, ei), keep_role)
            else:
                merged.append((si, ei, role))
        if len(merged) > 8:
            if merged[-1][2] == "payoff":
                merged = [*merged[:7], merged[-1]]
            else:
                merged = merged[:8]

    if clean_cold is None:
        return merged
    if not merged:
        return [(clean_cold[0], clean_cold[1], "cold_open_hook")]

    co_s, co_e = clean_cold
    # If cold_open lies strictly inside the interior of a chronological span, demote it into chronological order (no interior hole)
    if any(cs < co_s and co_e < ce for cs, ce, _ in merged):
        return merged

    # Trim any edge overlap off chronological spans so zero sentence plays twice
    trimmed_chrono: list[tuple[int, int, str]] = []
    for cs, ce, role in merged:
        if co_e < cs or co_s > ce:
            trimmed_chrono.append((cs, ce, role))
        elif co_s <= cs and ce <= co_e:
            continue
        elif co_s <= cs <= co_e < ce:
            trimmed_chrono.append((co_e + 1, ce, role))
        elif cs < co_s <= ce <= co_e:
            trimmed_chrono.append((cs, co_s - 1, role))

    if not trimmed_chrono:
        return [(co_s, co_e, "core")]

    if co_e < trimmed_chrono[0][0]:
        if co_e + 1 == trimmed_chrono[0][0]:
            first_s, first_e, first_role = trimmed_chrono[0]
            trimmed_chrono[0] = (co_s, first_e, "core" if first_role == "core" else first_role)
            return trimmed_chrono
        return [(co_s, co_e, "cold_open_hook"), *trimmed_chrono[:7]]

    return [(co_s, co_e, "cold_open_hook"), *trimmed_chrono[:8]]


def _build_candidate_from_spans(
    raw: dict[str, Any],
    spans_idx: list[tuple[int, int, str]],
    clip_type: ClipType,
    aspect_ratio: str,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    target_window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    energy_blocks: list[dict],
    idx_counter: int,
    blacklisted_indices: Optional[set[int]] = None,
) -> Optional[ClipCandidate]:
    if not spans_idx:
        return None

    blk = blacklisted_indices or set()
    snapped_spans_idx: list[tuple[int, int, str]] = []
    last_sp_k = len(spans_idx) - 1
    for sp_k, (si, ei, role) in enumerate(spans_idx):
        prev_ei = snapped_spans_idx[-1][1] if snapped_spans_idx else -1
        next_si = spans_idx[sp_k + 1][0] if sp_k < last_sp_k else len(sentences)
        if sp_k == 0 and si > 0 and (si - 1) not in blk and (si - 1) > prev_ei:
            cur_txt = sentences[si].text
            first_norm = _strip_accents_lower(re.sub(r"[^\wáéíóúüñ]", "", (cur_txt.split() or [""])[0]))
            if (
                _starts_with_lowercase(cur_txt)
                and first_norm in _ORPHAN_START_WORDS
                and (float(sentences[si].start) - float(sentences[si - 1].end)) < 0.35
            ):
                si -= 1
        if sp_k == last_sp_k and ei + 1 < len(sentences) and (ei + 1) not in blk and (ei + 1) < next_si:
            if _norm_last_word(sentences[ei].text) in DANGLING_END_WORDS:
                ei += 1
        snapped_spans_idx.append((si, ei, role))

    adj_spans, est_dur = _adjust_multispan_to_window(
        snapped_spans_idx,
        sentences,
        target_window,
        silence_intervals,
        audio_config,
        blacklisted_indices=blacklisted_indices,
    )
    narrative_spans = [
        NarrativeSpan(
            start_sentence_id=sentences[si].id,
            end_sentence_id=sentences[ei].id,
            orig_start=sentences[si].start,
            orig_end=sentences[ei].end,
            role=role,  # type: ignore[arg-type]
        )
        for si, ei, role in adj_spans
    ]

    first_si = adj_spans[0][0]
    last_ei = adj_spans[-1][1]

    base_score = _normalize_score(raw.get("score"), default=78)
    core_sp = next((sp for sp in narrative_spans if sp.role == "core"), narrative_spans[0])
    mid_t = (core_sp.orig_start + core_sp.orig_end) * 0.5
    energy_bonus = 0
    for eb in energy_blocks:
        if eb["start"] <= mid_t <= eb["end"]:
            if eb.get("energy") == "alta":
                energy_bonus = 3
            elif eb.get("energy") == "baja":
                energy_bonus = -2
            break
    final_score = max(1, min(99, base_score + energy_bonus))

    titles = [str(t).strip() for t in (raw.get("titles") or []) if str(t).strip()][:3]
    if not titles:
        fallback_title = str(raw.get("title") or raw.get("tema") or "Momento destacado").strip()
        titles = [fallback_title]

    chapters_raw = raw.get("chapters") or []
    chapters: list[ChapterMark] = []
    for ch in chapters_raw:
        if not isinstance(ch, dict):
            continue
        ch_sid = _normalize_sid(ch.get("sentence_id"), id_to_idx)
        ch_t = sentences[id_to_idx[ch_sid]].start if ch_sid in id_to_idx else sentences[first_si].start
        # Compute approximate cumulative edited time across stitched spans
        ed_t = 0.0
        matched_span = False
        for nsp in narrative_spans:
            if nsp.orig_start <= ch_t <= nsp.orig_end:
                ed_t += ch_t - nsp.orig_start
                matched_span = True
                break
            ed_t += max(0.0, nsp.orig_end - nsp.orig_start)
        if not matched_span:
            ed_t = max(0.0, ch_t - sentences[first_si].start)
        chapters.append(
            ChapterMark(
                title=str(ch.get("title", "Sección")).strip(),
                sentence_id=ch_sid if ch_sid in id_to_idx else None,
                orig_time_sec=round(ch_t, 2),
                edited_time_sec=round(ed_t, 2),
            )
        )

    hashtags = [str(h).strip() for h in (raw.get("hashtags") or []) if str(h).strip()]
    if not hashtags:
        hashtags = ["#podcast", "#clips"]

    hook_raw = str(raw.get("hook_text") or titles[0]).strip()
    hook_words = hook_raw.split()
    if len(hook_words) > 9:
        hook_raw = " ".join(hook_words[:8])
    hook_raw = _clean_trailing_hook_words(hook_raw)

    meta = ClipMetadata(
        titles=titles,
        description=str(raw.get("description") or raw.get("summary") or "").strip(),
        hashtags=hashtags,
        thumbnail_text=str(raw.get("thumbnail_text") or titles[0][:36]).strip(),
        cliffhanger_question=str(raw.get("cliffhanger_question") or "").strip(),
        hook_text=hook_raw,
        chapters=chapters,
    )

    slug = make_slug(titles[0])
    archetype = str(raw.get("narrative_archetype") or raw.get("archetype") or "mini_historia").strip()
    spks = list(
        dict.fromkeys(
            sentences[k].speaker
            for si, ei, _ in adj_spans
            for k in range(si, ei + 1)
            if sentences[k].speaker
        )
    )
    packaging = dict(raw.get("standalone_packaging")) if isinstance(raw.get("standalone_packaging"), dict) else {}
    raw_outline = raw.get("video_outline")
    if isinstance(raw_outline, list):
        cleaned_outline = [str(x).strip() for x in raw_outline if str(x).strip()][:6]
        if cleaned_outline:
            packaging["video_outline"] = cleaned_outline

    cand_uid = str(raw.get("candidate_id") or f"cand_{uuid.uuid4().hex[:8]}").strip()
    orig_title = str(raw.get("original_title") or titles[0]).strip()

    return ClipCandidate(
        id=f"{clip_type}_{idx_counter:02d}_{slug[:16]}",
        candidate_id=cand_uid,
        original_title=orig_title,
        clip_type=clip_type,
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        score=final_score,
        slug=slug,
        summary=str(raw.get("summary") or meta.description or titles[0]).strip(),
        hook_reason=str(raw.get("hook_reason") or "").strip(),
        start_sentence_id=narrative_spans[0].start_sentence_id,
        end_sentence_id=narrative_spans[-1].end_sentence_id,
        orig_start=min(sp.orig_start for sp in narrative_spans),
        orig_end=max(sp.orig_end for sp in narrative_spans),
        estimated_post_cut_sec=est_dur,
        narrative_archetype=archetype,
        angle_id=str(raw.get("angle_id") or "").strip(),
        angle_theme=str(raw.get("angle_theme") or "").strip(),
        narrative_spans=narrative_spans,
        speakers_detected=spks,
        standalone_packaging=packaging,
        metadata=meta,
    )


def _parse_candidates_from_llm(
    raw_list: list[dict[str, Any]],
    clip_type: ClipType,
    aspect_ratio: str,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    target_window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    energy_blocks: list[dict],
    blacklisted_indices: Optional[set[int]] = None,
) -> list[ClipCandidate]:
    results: list[ClipCandidate] = []
    allow_payoff = clip_type != "short_cliffhanger"
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        spans_idx = _parse_raw_spans(
            raw,
            id_to_idx,
            blacklisted_indices=blacklisted_indices,
            allow_payoff_role=allow_payoff,
        )
        cand = _build_candidate_from_spans(
            raw=raw,
            spans_idx=spans_idx,
            clip_type=clip_type,
            aspect_ratio=aspect_ratio,
            sentences=sentences,
            id_to_idx=id_to_idx,
            target_window=target_window,
            silence_intervals=silence_intervals,
            audio_config=audio_config,
            energy_blocks=energy_blocks,
            idx_counter=len(results) + 1,
            blacklisted_indices=blacklisted_indices,
        )
        if cand is not None:
            results.append(cand)
    return results


def _format_candidate_script(
    cand: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    show_speakers: bool = False,
    for_blind_audit: bool = False,
    include_intro_curtain: bool = False,
) -> tuple[str, list[int]]:
    spans = cand.narrative_spans or [
        NarrativeSpan(
            start_sentence_id=cand.start_sentence_id,
            end_sentence_id=cand.end_sentence_id,
            orig_start=cand.orig_start,
            orig_end=cand.orig_end,
            role="core",
        )
    ]
    assembled_parts: list[str] = []
    span_indices: list[int] = []
    for sp in spans:
        si = id_to_idx.get(sp.start_sentence_id)
        ei = id_to_idx.get(sp.end_sentence_id)
        if si is None or ei is None:
            continue
        span_indices.extend([si, ei])
        if for_blind_audit:
            if show_speakers:
                spoken = " ".join(
                    f"[{s.speaker}] {s.text}" if s.speaker else s.text
                    for s in sentences[si : ei + 1]
                )
            else:
                spoken = " ".join(s.text for s in sentences[si : ei + 1])
            assembled_parts.append(spoken.strip())
        else:
            assembled_parts.append(
                f"[TRAMO role={sp.role} ({sp.start_sentence_id}..{sp.end_sentence_id})]\n"
                + _format_sentences_block(
                    sentences[si : ei + 1],
                    show_speakers=show_speakers,
                    include_durations=True,
                )
            )
    if for_blind_audit:
        if len(assembled_parts) <= 1:
            body = assembled_parts[0] if assembled_parts else ""
        else:
            buf: list[str] = [assembled_parts[0]]
            for s_i in range(1, len(assembled_parts)):
                buf.append(f"\n[EMPALME #{s_i}]\n{assembled_parts[s_i]}")
            body = "".join(buf)
        if include_intro_curtain and cand.intro_curtain_text.strip():
            body = f"[INTRO IA]: {cand.intro_curtain_text.strip()}\n[AUDIO PODCAST]:\n{body}"
        return body, span_indices

    return "\n[CORTE DE EDICIÓN]\n".join(assembled_parts), span_indices


def _is_unknown_or_vague(val: Any) -> bool:
    """True if cold-viewer entity/context answer is missing, unknown, or an unnamed generic reference."""
    norm = _strip_accents_lower(str(val or ""))
    if len(norm) < 3:
        return True
    if any(tok in norm for tok in ("desconocid", "no mencionad", "sin nombre", "no se indica", "no se nombra")):
        return True
    return bool(
        re.match(
            r"^(?:no se (?:sabe|dice|especifica)|ningun[oa]|indeterminad[oa]|"
            r"una? (?:mujer|chica|hombre|senora|senor|persona|avion|motor)|"
            r"est[ea] (?:mujer|chica|senora|hombre|senor)|esto|eso|algo|sin contexto)$",
            norm,
        )
    )


def _build_global_entity_stopwords(
    visual_bible: Optional[dict[str, Any]],
    profile: Optional[PodcasterProfile] = None,
    sentences: Optional[list[SentenceSegment]] = None,
) -> set[str]:
    """Build episode-wide subject stopwords so sub-angle comparison focuses on distinct themes."""
    out: set[str] = set()
    if profile and profile.podcast_name:
        for tok in re.findall(r"[a-z0-9]{4,}", _strip_accents_lower(profile.podcast_name)):
            out.add(tok)
    vb = visual_bible or {}
    for noun in (vb.get("proper_nouns") or [])[:2]:
        for tok in re.findall(r"[a-z0-9]{4,}", _strip_accents_lower(str(noun))):
            out.add(tok)
    era = str(vb.get("era_or_setting") or "")
    for tok in re.findall(r"[a-z0-9]{4,}", _strip_accents_lower(era)):
        out.add(tok)
    if sentences and len(sentences) >= 40:
        from collections import Counter

        doc_freq: Counter[str] = Counter()
        for s in sentences:
            toks = set(re.findall(r"[a-z]{4,}", _strip_accents_lower(s.text)))
            doc_freq.update(toks)
        cutoff = len(sentences) * 0.45
        for tok, cnt in doc_freq.items():
            if cnt >= cutoff:
                out.add(tok)
    return out


def _extract_topic_keywords(text: str, global_stopwords: Optional[set[str]] = None) -> set[str]:
    norm = _strip_accents_lower(text)
    gstop = global_stopwords or set()
    toks = set()
    for tok in re.findall(r"[a-z0-9]{4,}", norm):
        if tok in _TOPIC_STOPWORDS or tok in gstop:
            continue
        # Basic Spanish plural stem normalization
        stem = tok[:-2] if len(tok) > 5 and tok.endswith("es") else (tok[:-1] if len(tok) > 4 and tok.endswith("s") else tok)
        toks.add(stem)
    return toks


def _compute_topic_similarity(
    cand_a: ClipCandidate,
    cand_b: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    global_stopwords: Optional[set[str]] = None,
) -> float:
    """Compute thematic similarity in [0, 1] between two clips based on angle, title, and spoken script keywords."""
    if cand_a.angle_id and cand_b.angle_id and cand_a.angle_id == cand_b.angle_id:
        return 1.0

    # 1. Angle + title + hook similarity (never diluted by post-audit Blind Judge boilerplate)
    ta_raw = f"{cand_a.angle_theme or ''} {' '.join(cand_a.metadata.titles[:2])} {cand_a.metadata.hook_text or ''}".strip()
    tb_raw = f"{cand_b.angle_theme or ''} {' '.join(cand_b.metadata.titles[:2])} {cand_b.metadata.hook_text or ''}".strip()
    ta = _extract_topic_keywords(ta_raw or cand_a.summary or "", global_stopwords)
    tb = _extract_topic_keywords(tb_raw or cand_b.summary or "", global_stopwords)
    title_sim = 0.0
    if ta and tb:
        t_jacc = len(ta & tb) / max(1, len(ta | tb))
        t_ov = len(ta & tb) / max(1, min(len(ta), len(tb)))
        title_sim = max(t_jacc, t_ov * 0.80)

    # 2. Spoken script similarity across actual selected sentences (up to 100 words)
    scr_a, _ = _format_candidate_script(cand_a, sentences, id_to_idx, for_blind_audit=True)
    scr_b, _ = _format_candidate_script(cand_b, sentences, id_to_idx, for_blind_audit=True)
    sa = _extract_topic_keywords(" ".join(scr_a.split()[:100]), global_stopwords)
    sb = _extract_topic_keywords(" ".join(scr_b.split()[:100]), global_stopwords)
    script_sim = (len(sa & sb) / max(1, len(sa | sb))) if (sa and sb) else 0.0

    # Combine: identical angle/title OR high script overlap flags a duplicate sub-angle
    return round(max(title_sim, script_sim * 0.90), 3)


def _run_cold_viewer_test(
    cand: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    llm: OpenRouterClient,
    show_speakers: bool = False,
    model_role: Literal["editorial", "editorial_strong"] = "editorial",
    visual_bible: Optional[dict[str, Any]] = None,
    include_intro_curtain: bool = False,
) -> dict[str, Any]:
    """Evaluate ONLY the literal spoken script as heard by a cold viewer with zero episode context."""
    blind_script, _ = _format_candidate_script(
        cand,
        sentences,
        id_to_idx,
        show_speakers=show_speakers,
        for_blind_audit=True,
        include_intro_curtain=include_intro_curtain,
    )
    has_intro_curtain = bool(include_intro_curtain and cand.intro_curtain_text.strip())
    opening_source = (
        blind_script.replace("[INTRO IA]:", " ")
        .replace("[AUDIO PODCAST]:", " ")
        .replace("[EMPALME #1]", " ")
        .replace("[EMPALME #2]", " ")
        .replace("[EMPALME #3]", " ")
        .replace("[EMPALME #4]", " ")
    )
    all_open_toks = opening_source.split()
    opening_22 = " ".join(all_open_toks[:22]).strip()
    preview_toks: list[str] = []
    for tok in all_open_toks[:36]:
        preview_toks.append(tok)
        if len(preview_toks) >= 14 and tok.rstrip("\"'”’").endswith((".", "?", "!")):
            break
    if preview_toks and not preview_toks[-1].rstrip("\"'”’").endswith((".", "?", "!")):
        opening_preview = (" ".join(all_open_toks[:28]) + "...").strip()
    else:
        opening_preview = " ".join(preview_toks).strip()

    is_cliff = cand.clip_type == "short_cliffhanger"
    closing_rule = (
        "En short_cliffhanger: `resolution_answered` es true SOLO si el misterio/pregunta central queda 100% claro "
        "y corta en una idea/oración completa justo en el pico de intriga (sin revelar la solución ni cortar a mitad de frase)."
        if is_cliff
        else "En vídeos conclusivos (short_conclusive/medium/long): `resolution_answered` es true si la respuesta, solución o conclusión principal aparece explícitamente en este guion literal."
    )
    missing_data_rule = (
        "Si un dato (quién es, qué ocurre, cuándo/dónde o cuál es la incógnita) NO está dicho en el guion, debes escribir 'DESCONOCIDO'. "
        "En un cliffhanger NO debe revelarse la solución final (`has_spoiler`=true si ya la revela)."
        if is_cliff
        else "Si un dato (quién es, qué pasa, cuándo/dónde, o la conclusión) NO está dicho en el guion, debes escribir 'DESCONOCIDO'."
    )
    intro_rule = (
        "\nREGLA DE CORTINILLA COMBINADA ([INTRO IA] + [AUDIO PODCAST]):\n"
        "- El espectador escucha PRIMERO el texto de `[INTRO IA]` y acto seguido `[AUDIO PODCAST]`.\n"
        "- Si `[INTRO IA]` ya nombra explícitamente al protagonista (`who`), época/lugar y problema (`what_and_context`), "
        "entonces pronombres o referencias iniciales en `[AUDIO PODCAST]` como 'ella', 'él', 'esta mujer', 'este hombre', 'este invento' o 'esto' "
        "QUEDAN COMPLETAMENTE RESUELTOS por la cortinilla: NO los marques en `orphan_or_meta_refs`, NO pongas `who=DESCONOCIDO` ni penalices `context_clarity`.\n"
        "- SOLO marca `orphan_or_meta_refs` si hay referencias reales al podcast completo ('en este programa', 'como decíamos antes', 'Patreon', 'esta santa casa') o un sujeto totalmente distinto sin presentar.\n"
        if has_intro_curtain
        else ""
    )
    resolution_schema_line = (
        '  "resolution_answered": true/false (true si la incógnita/tensión queda clara y corta en idea completa ANTES de revelar la solución; NO exige respuesta final),\n'
        if is_cliff
        else '  "resolution_answered": true/false (true si el guion incluye la respuesta, explicación o conclusión real),\n'
    )
    spoiler_schema_line = (
        '  "has_spoiler": true/false (true si la intro o guion destripa la solución final en un cliffhanger),\n'
        if is_cliff
        else '  "has_spoiler": false,\n'
    )

    sys_prompt = (
        "Eres un espectador frío y Director de Montaje Ciego que NO conoce este podcast ni ha visto el episodio completo.\n"
        "Evalúas EXCLUSIVAMENTE el guion literal que vas a leer, como un vídeo independiente vertical u horizontal.\n"
        "Tu objetivo NO es descartar buenas historias por gusto, sino diagnosticar con precisión quirúrgica:\n"
        "1) Qué entiende exactamente un espectador nuevo que entra sin haber visto el podcast.\n"
        "2) Si cada [EMPALME #k] interno fluye con sentido causal.\n"
        "3) Qué operación concreta de edición profesional (`trim_start`, `trim_end`, `extend_end_to_payoff`, `prune_digression`, "
        "`add_context_intro_span`, `reorder_cold_open_hook`, `add_ai_intro_curtain`, `fix_splice`) convertiría este borrador en un vídeo redondo.\n\n"
        "TOLERANCIA A TRANSCRIPCIÓN WHISPER:\n"
        "- El texto proviene de reconocimiento automático de voz (ASR): puede carecer de puntos finales, tener comas mal puestas o erratas fonéticas menores.\n"
        "- NO penalices ausencia de signos de puntuación si la idea hablada se entiende cerrada y completa.\n"
        "- SOLO reporta `broken_splices` si existe un marcador `[EMPALME #k]` con discontinuidad causal real o frase cortada.\n"
        f"{missing_data_rule}{intro_rule}\n"
        "Responde estrictamente en JSON válido."
    )
    usr_prompt = (
        "PRUEBA DE ESPECTADOR FRÍO (AUDITORÍA OBLIGATORIA A CIEGAS)\n"
        f"Tipo de vídeo: {cand.clip_type} | Duración estimada post-corte: {cand.estimated_post_cut_sec:.1f}s\n\n"
        f"APERTURA (primeros segundos — el guion continúa completo abajo):\n\"{opening_preview}\"\n\n"
        f"GUION LITERAL COMPLETO QUE SE OYE:\n{blind_script}\n\n"
        f"REGLA DE CIERRE: {closing_rule}\n\n"
        "ESCALA DE PUNTUACIÓN (1-10):\n"
        "- 9-10: Se entiende solo de principio a fin, gancho potente, cierres limpios.\n"
        "- 7-8: Se entiende la historia/idea, quizá le falta situar mejor los primeros 4-6s o pulir un borde.\n"
        "- 4-6: Faltan datos clave (protagonista o remate) que deben resolverse editando tramos o añadiendo cortinilla IA.\n"
        "- 1-3: Ininteligible, anuncio puro o corte totalmente roto.\n\n"
        "Responde JSON exactamente con:\n"
        "{\n"
        '  "who": "Protagonista, personaje, ejército, país, institución o grupo concreto del texto literal (p. ej. \'Rommel y el ejército británico\', \'los aliados\'; solo escribe DESCONOCIDO si realmente no se nombra ningún actor ni bando)",\n'
        '  "what_and_context": "Qué ocurre, cuándo/dónde y por qué importa según el texto literal (DESCONOCIDO si falta)",\n'
        '  "opening_4_6s_clear": true/false,\n'
        '  "opening_issue": "Motivo concreto si los primeros 4-6s empiezan con pronombre huérfano, muletilla o sin sujeto claro",\n'
        '  "central_question_or_conflict": "Pregunta central o conflicto que plantea el vídeo",\n'
        + resolution_schema_line
        + spoiler_schema_line
        + '  "answer_or_mystery_stated": "Conclusión dada o incógnita exacta en la que termina",\n'
        '  "clean_start": true/false,\n'
        '  "clean_end": true/false,\n'
        '  "splices_coherent": true/false (true si CADA [EMPALME #k] fluye con sentido lógico sin saltarse el paso causal ni cortar frase),\n'
        '  "broken_splices": ["descripción de empalme roto o salto incoherente si lo hay"],\n'
        '  "orphan_or_meta_refs": ["frases huérfanas sin resolver o meta-referencias al podcast como \'en esta santa casa\', \'esto es otra historia\'"],\n'
        '  "missing_context_items": ["dato concreto faltante 1"],\n'
        '  "edit_operations": ["lista de operaciones recomendadas: trim_start | trim_end | extend_end_to_payoff | prune_digression | add_context_intro_span | reorder_cold_open_hook | add_ai_intro_curtain | fix_splice"],\n'
        '  "director_instructions": "Instrucción concreta de montaje NLE para dejar el vídeo autónomo y perfecto (qué recortar, extender, podar o enmarcar con intro IA)",\n'
        '  "comprehension_score": 1-10 (qué tan bien se entiende la historia central del fragmento, aunque le falte presentar contexto previo),\n'
        '  "story_interest_score": 1-10 (atractivo narrativo, emoción, giro o valor de entretenimiento del relato en sí),\n'
        '  "hook_strength": 1-10,\n'
        '  "context_clarity": 1-10 (solo de los primeros 4-6 segundos + presentación inicial),\n'
        '  "clean_boundaries": 1-10,\n'
        '  "resolution_score": 1-10 (si el tramo concluye bien su idea o deja la intriga en el punto exacto),\n'
        '  "standalone_summary": "1 frase describiendo SOLO lo que se entiende de este texto literal"\n'
        "}"
    )

    try:
        resp = llm.chat_json(
            model_role,
            sys_prompt,
            usr_prompt,
            temperature=0.1,
            max_tokens=1200,
            stage="cold_viewer_with_intro" if include_intro_curtain else "cold_viewer",
        )
    except Exception as exc:
        return {
            "who": "DESCONOCIDO",
            "what_and_context": "DESCONOCIDO",
            "opening_4_6s_clear": False,
            "opening_issue": f"Error auditoría: {exc}",
            "central_question_or_conflict": "",
            "resolution_answered": False,
            "has_spoiler": False,
            "answer_or_mystery_stated": "",
            "clean_start": False,
            "clean_end": False,
            "splices_coherent": False,
            "broken_splices": [],
            "orphan_or_meta_refs": [],
            "missing_context_items": ["No se pudo validar el contexto en la auditoría a ciegas."],
            "edit_operations": ["add_ai_intro_curtain"],
            "director_instructions": "",
            "comprehension_score": 40,
            "story_interest_score": 50,
            "hook_strength": 40,
            "context_clarity": 40,
            "clean_boundaries": 40,
            "resolution_score": 40,
            "raw_resolution_score": 40,
            "standalone_summary": cand.summary,
            "who_unknown": True,
            "what_unknown": True,
            "passed_full": False,
            "passed_with_intro_only": False,
            "needs_ai_intro_curtain": True,
            "composite_score": 40,
        }

    who = str(resp.get("who") or "").strip()
    what_ctx = str(resp.get("what_and_context") or "").strip()
    who_unknown = _is_unknown_or_vague(who)
    what_unknown = _is_unknown_or_vague(what_ctx)

    comp_sc = _normalize_score(resp.get("comprehension_score"), default=60)
    interest_sc = _normalize_score(resp.get("story_interest_score"), default=max(60, comp_sc))
    hook_sc = _normalize_score(resp.get("hook_strength"), default=60)
    ctx_sc = _normalize_score(resp.get("context_clarity"), default=60)
    bnd_sc = _normalize_score(resp.get("clean_boundaries"), default=60)
    res_sc = _normalize_score(resp.get("resolution_score"), default=60)
    raw_res_sc = res_sc

    clean_start = bool(resp.get("clean_start", True))
    clean_end = bool(resp.get("clean_end", True))
    splices_coherent = bool(resp.get("splices_coherent", True))
    broken_splices = [str(x).strip() for x in (resp.get("broken_splices") or []) if str(x).strip()]
    orphan_or_meta = [str(x).strip() for x in (resp.get("orphan_or_meta_refs") or []) if str(x).strip()]
    has_spoiler = bool(resp.get("has_spoiler", False)) if is_cliff else False

    spans_check = cand.narrative_spans or [
        NarrativeSpan(
            start_sentence_id=cand.start_sentence_id,
            end_sentence_id=cand.end_sentence_id,
            orig_start=cand.orig_start,
            orig_end=cand.orig_end,
            role="core",
        )
    ]
    if len(spans_check) <= 1:
        # Uncut 1-span continuous clips have no [EMPALME] editing jumps
        splices_coherent = True
        broken_splices = []

    hard_bad_start = False
    for sp_i, sp in enumerate(spans_check):
        sp_si = id_to_idx.get(sp.start_sentence_id)
        sp_ei = id_to_idx.get(sp.end_sentence_id)
        if sp_si is not None:
            st_txt = sentences[sp_si].text
            prev_txt = sentences[sp_si - 1].text if sp_si > 0 else ""
            first_word_norm = _strip_accents_lower(re.sub(r"[^\wáéíóúüñ]", "", (st_txt.split() or [""])[0]))
            tight_prev_gap = (
                sp_si > 0
                and (sentences[sp_si].start - sentences[sp_si - 1].end) < 0.35
                and (_norm_last_word(prev_txt) in DANGLING_END_WORDS or prev_txt.rstrip().endswith((",", ";", ":")))
            )
            bad_start = _is_dead_opening(st_txt, "") or (
                _starts_with_lowercase(st_txt)
                and tight_prev_gap
                and first_word_norm in _ORPHAN_START_WORDS
            )
            if bad_start:
                if sp_i == 0:
                    if has_intro_curtain and not _is_dead_opening(st_txt, ""):
                        pass
                    else:
                        clean_start = False
                        hard_bad_start = True
                else:
                    splices_coherent = False
                    broken_splices.append(f"Empalme #{sp_i} arranca con fragmento incompleto/muletilla: '{st_txt[:45]}'")
        if sp_ei is not None:
            en_txt = sentences[sp_ei].text.strip()
            # Whisper draft transcripts frequently omit periods on complete spoken thoughts;
            # only force bad_end when ending on a dangling function word.
            bad_end = _norm_last_word(en_txt) in DANGLING_END_WORDS
            if bad_end:
                if sp_i == len(spans_check) - 1:
                    clean_end = False
                else:
                    splices_coherent = False
                    broken_splices.append(f"Empalme #{sp_i + 1} corta frase inconclusa: '{en_txt[-40:]}'")

    meta_hit = _META_PODCAST_REF_RE.search(blind_script)
    if meta_hit:
        orphan_or_meta.append(f"Meta-referencia al podcast: '{meta_hit.group(0)}'")

    # When [INTRO IA] is present and clearly establishes WHO + WHAT without podcast meta-refs,
    # ignore false-positive pronoun alerts inside the podcast audio body.
    if has_intro_curtain and not who_unknown and not what_unknown and not meta_hit:
        true_meta_refs = [
            ref for ref in orphan_or_meta
            if any(
                kw in _strip_accents_lower(ref)
                for kw in ("programa", "episodio", "podcast", "santa casa", "otra historia", "patreon", "suscrib", "meta-referencia")
            )
        ]
        orphan_or_meta = true_meta_refs

    resolution_answered = bool(resp.get("resolution_answered", False))
    opening_4_6s_clear = bool(resp.get("opening_4_6s_clear", False))
    if not has_intro_curtain:
        if _opening_needs_context_bridge(opening_22, visual_bible):
            opening_4_6s_clear = False
    elif who_unknown and (not what_unknown) and len(what_ctx) >= 40:
        vb_entities = [
            str(e).strip()
            for e in ((visual_bible or {}).get("proper_nouns") or (visual_bible or {}).get("key_entities") or [])
            if str(e).strip()
        ]
        what_low = _strip_accents_lower(what_ctx)
        named_hits = [e for e in vb_entities if _strip_accents_lower(e) in what_low]
        if not named_hits:
            cap_hits = re.findall(r"(?<=[a-z0-9,;:\)]\s)([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{2,})", what_ctx)
            if len(cap_hits) >= 2:
                named_hits = list(dict.fromkeys(cap_hits[:3]))
        if named_hits:
            who = ", ".join(named_hits[:3])
            who_unknown = False

    if who_unknown or what_unknown:
        ctx_sc = min(ctx_sc, 45)
    if not clean_start or not clean_end:
        bnd_sc = min(bnd_sc, 55)
    if not splices_coherent or broken_splices:
        bnd_sc = min(bnd_sc, 45)
        comp_sc = min(comp_sc, 55)
    if orphan_or_meta:
        ctx_sc = min(ctx_sc, 50)
    if has_spoiler:
        res_sc = min(res_sc, 40)
    if not resolution_answered:
        res_sc = min(res_sc, 45)

    passed_full = bool(
        min(comp_sc, ctx_sc, bnd_sc, res_sc) >= 75
        and hook_sc >= 70
        and not who_unknown
        and not what_unknown
        and clean_start
        and clean_end
        and splices_coherent
        and not broken_splices
        and not orphan_or_meta
        and not has_spoiler
        and resolution_answered
    )
    passed_with_intro_only = bool(
        bnd_sc >= 65
        and res_sc >= 70
        and not who_unknown
        and not what_unknown
        and clean_start
        and clean_end
        and splices_coherent
        and not broken_splices
        and not orphan_or_meta
        and not has_spoiler
        and resolution_answered
        and comp_sc >= 60
    )
    needs_ai_intro = bool(
        (not opening_4_6s_clear)
        or (ctx_sc < 80)
        or (hook_sc < 75)
        or (not has_intro_curtain and _opening_needs_context_bridge(opening_22, visual_bible))
    )
    missing_items = [str(x).strip() for x in (resp.get("missing_context_items") or []) if str(x).strip()]
    edit_ops = [str(x).strip() for x in (resp.get("edit_operations") or []) if str(x).strip()]
    composite = int(round((comp_sc + hook_sc + ctx_sc + bnd_sc + res_sc) / 5.0))

    return {
        "who": who,
        "what_and_context": what_ctx,
        "opening_4_6s_clear": opening_4_6s_clear,
        "opening_issue": str(resp.get("opening_issue") or "").strip(),
        "central_question_or_conflict": str(resp.get("central_question_or_conflict") or "").strip(),
        "resolution_answered": resolution_answered,
        "has_spoiler": has_spoiler,
        "answer_or_mystery_stated": str(resp.get("answer_or_mystery_stated") or "").strip(),
        "clean_start": clean_start,
        "clean_end": clean_end,
        "splices_coherent": splices_coherent,
        "broken_splices": broken_splices,
        "orphan_or_meta_refs": orphan_or_meta,
        "missing_context_items": missing_items,
        "edit_operations": edit_ops,
        "director_instructions": str(resp.get("director_instructions") or "").strip(),
        "comprehension_score": comp_sc,
        "story_interest_score": interest_sc,
        "hook_strength": hook_sc,
        "context_clarity": ctx_sc,
        "clean_boundaries": bnd_sc,
        "resolution_score": res_sc,
        "raw_resolution_score": raw_res_sc,
        "standalone_summary": str(resp.get("standalone_summary") or cand.summary).strip(),
        "who_unknown": who_unknown,
        "what_unknown": what_unknown,
        "passed_full": passed_full,
        "passed_with_intro_only": passed_with_intro_only,
        "needs_ai_intro_curtain": needs_ai_intro,
        "composite_score": composite,
    }


def _gather_repair_context(
    cand: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    blocks: Optional[list[dict[str, Any]]],
    block_map: Optional[list[dict[str, Any]]],
    visual_bible: Optional[dict[str, Any]],
    blacklisted_indices: Optional[set[int]],
    show_speakers: bool = False,
) -> str:
    """Gather premise/character intro blocks + local surrounding window (<=200 sentences) for NLE surgical repair."""
    blocked = blacklisted_indices or set()
    n = len(sentences)
    span_idxs: list[int] = []
    if cand.narrative_spans:
        for sp in cand.narrative_spans:
            if sp.start_sentence_id in id_to_idx:
                span_idxs.append(id_to_idx[sp.start_sentence_id])
            if sp.end_sentence_id in id_to_idx:
                span_idxs.append(id_to_idx[sp.end_sentence_id])
    if not span_idxs:
        if cand.start_sentence_id in id_to_idx:
            span_idxs.append(id_to_idx[cand.start_sentence_id])
        if cand.end_sentence_id in id_to_idx:
            span_idxs.append(id_to_idx[cand.end_sentence_id])
    lo_span = min(span_idxs) if span_idxs else 0
    hi_span = max(span_idxs) if span_idxs else min(n - 1, 25)

    included: set[int] = set()
    for k in range(max(0, lo_span - 25), min(n, hi_span + 35)):
        if k not in blocked:
            included.add(k)

    # Include earliest non-ad story premise/character introduction sentences
    early_added = 0
    for k in range(0, n):
        if k not in blocked:
            included.add(k)
            early_added += 1
            if early_added >= 38:
                break

    map_lines: list[str] = []
    if blocks and block_map:
        non_ad = [m for m in block_map if not m.get("is_ad_or_promo")]
        for m in non_ad[:15]:
            bi = int(m.get("block_index", 0))
            if 0 <= bi < len(blocks):
                b = blocks[bi]
                map_lines.append(
                    f"- Bloque {bi} ({b['first_id']}..{b['last_id']}): {m.get('tema', '')} — {m.get('resumen_breve', '')}"
                )
        # Add up to 2 high-interest non-ad blocks (e.g., character intro or solution) within 200-sentence budget
        ranked_extra = sorted(
            non_ad,
            key=lambda m: (
                float(m.get("potencial_conclusivo", 5)) + float(m.get("interes_general", 5))
            ),
            reverse=True,
        )
        added_blocks = 0
        for m in ranked_extra:
            if added_blocks >= 2 or len(included) >= 200:
                break
            bi = int(m.get("block_index", 0))
            if not (0 <= bi < len(blocks)):
                continue
            b_sents = blocks[bi].get("sentences") or []
            b_clean_idxs = [id_to_idx[s.id] for s in b_sents if s.id in id_to_idx and id_to_idx[s.id] not in blocked]
            if not b_clean_idxs:
                continue
            if all(idx in included for idx in b_clean_idxs[:5]):
                continue
            for idx in b_clean_idxs:
                if len(included) >= 200:
                    break
                included.add(idx)
            added_blocks += 1

    ordered = sorted(included)[:205]
    chunk_lines: list[str] = []
    prev_k: Optional[int] = None
    for k in ordered:
        if prev_k is not None and k > prev_k + 1:
            chunk_lines.append("... [salto temporal en el episodio] ...")
        chunk_lines.append(
            _format_sentences_block(
                [sentences[k]],
                show_speakers=show_speakers,
                include_durations=True,
            )
        )
        prev_k = k

    vb = visual_bible or {}
    header_parts: list[str] = []
    if vb.get("episode_topic"):
        header_parts.append(f"Tema del episodio: {vb['episode_topic']}")
    if vb.get("era_or_setting"):
        header_parts.append(f"Época/Entorno: {vb['era_or_setting']}")
    if vb.get("narrative_spine"):
        header_parts.append(f"Hilo narrativo del episodio: {vb['narrative_spine']}")
    nouns = vb.get("proper_nouns") or []
    if nouns:
        header_parts.append(f"Nombres propios reales: {', '.join(str(x) for x in nouns[:14])}")
    if map_lines:
        header_parts.append("ÍNDICE DE BLOQUES DEL EPISODIO:\n" + "\n".join(map_lines))

    header = ("\n".join(header_parts) + "\n\n") if header_parts else ""
    return f"{header}FRAGMENTOS DISPONIBLES DEL EPISODIO (CON DURACIÓN POR FRASE, SIN ANUNCIOS):\n" + "\n".join(chunk_lines)


def _request_editorial_repair(
    cand: ClipCandidate,
    cold_eval: dict[str, Any],
    repair_ctx: str,
    target_window: DurationWindow,
    llm: OpenRouterClient,
    model_role: Literal["editorial", "editorial_strong"],
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    show_speakers: bool = False,
) -> dict[str, Any]:
    """Pro NLE video editor: trims, extends, prunes digressions, moves/reorders spans, and frames standalone context."""
    cur_script, _ = _format_candidate_script(cand, sentences, id_to_idx, show_speakers=show_speakers, for_blind_audit=False)
    is_cliff = cand.clip_type == "short_cliffhanger"
    is_short = cand.clip_type.startswith("short")
    closing_req = (
        "En short_cliffhanger: mantén el misterio abierto, PERO que quede 100% claro quién, qué ocurre, cuándo y qué incógnita queda. PROHIBIDO usar role='payoff' o hacer spoiler de la solución."
        if is_cliff
        else "En vídeos conclusivos: DEBE incluir el tramo real del podcast (`payoff` o final del `core`) donde se responde la pregunta, se explica el porqué o cierra la historia."
    )
    span_roles_hint = (
        "cold_open_hook|context_intro|question|core"
        if is_cliff
        else "cold_open_hook|context_intro|question|core|payoff"
    )

    broken_str = ", ".join(cold_eval.get("broken_splices") or []) or "Ninguno"
    orphan_str = ", ".join(cold_eval.get("orphan_or_meta_refs") or []) or "Ninguna"
    ops_str = ", ".join(cold_eval.get("edit_operations") or []) or "Ajustar montaje"
    dir_instr = str(cold_eval.get("director_instructions") or "").strip() or "Mejorar contexto inicial, continuidad y cierre"
    format_label = "vídeo vertical corto (9:16)" if is_short else f"vídeo {cand.clip_type} ({cand.aspect_ratio})"
    outline_items = [str(x).strip() for x in ((cand.standalone_packaging or {}).get("video_outline") or []) if str(x).strip()]
    outline_block = (
        "\nMINI-ÍNDICE OBLIGATORIO DEL SUBTEMA DE ESTE VÍDEO:\n"
        + "\n".join(f"  {idx + 1}. {item}" for idx, item in enumerate(outline_items))
        + "\n- Verifica que el montaje cubra todos estos puntos del subtema (poda relleno secundario con `drop_sentence_ids` si hace falta espacio, pero NO omitas el hecho/hito central).\n"
        if outline_items
        else ""
    )

    sys_prompt = (
        "Eres un Editor de Vídeo NLE Profesional y Showrunner de documentales y reels virales (estilo Premiere/DaVinci). "
        "Tu trabajo NO es descartar buenas historias: es MONTAR, RECORTAR, EXTENDER, PODAR y REORDENAR los tramos reales `[S####]` "
        "para convertir este borrador en un vídeo independiente brillante, con ritmo y 100% comprensible para alguien que NO ha visto el podcast.\n"
        "Dispones de la duración exacta `[S#### | X.Xs]` de cada frase para cuadrar el montaje sin cortar finales.\n"
        "NUNCA inventes IDs ni repitas frases. Responde estrictamente en JSON válido."
    )
    usr_prompt = (
        f"REPARACIÓN EDITORIAL NLE — {format_label.upper()}\n"
        f"Tipo de vídeo: {cand.clip_type} | Ángulo asignado: {cand.angle_theme or cand.summary[:90]}\n"
        f"Ventana objetivo post-corte: {target_window.min_sec:.0f}s - {target_window.max_sec:.0f}s\n\n"
        "DIAGNÓSTICO DEL JUEZ CIEGO:\n"
        f"- Protagonista detectado: {cold_eval.get('who')}\n"
        f"- Qué/Cuándo detectado: {cold_eval.get('what_and_context')}\n"
        f"- Fallo en los primeros 4-6s: {cold_eval.get('opening_issue') or 'Falta contexto inicial claro'}\n"
        f"- Empalmes rotos: {broken_str}\n"
        f"- Pronombres huérfanos / meta-podcast: {orphan_str}\n"
        f"- Datos faltantes: {', '.join(cold_eval.get('missing_context_items') or []) or 'Revisar sujeto/resolución'}\n"
        f"- Spoiler detectado (cliffhanger): {cold_eval.get('has_spoiler', False)}\n"
        f"- Operaciones sugeridas: {ops_str}\n"
        f"- Instrucción del Director: {dir_instr}\n"
        f"- Inicio limpio={cold_eval.get('clean_start')} | Final limpio={cold_eval.get('clean_end')} | Empalmes coherentes={cold_eval.get('splices_coherent', True)} | Resolución={cold_eval.get('resolution_answered')}\n"
        f"{outline_block}\n"
        f"TRAMOS ACTUALES DEL CANDIDATO:\n{cur_script}\n\n"
        f"{repair_ctx}\n\n"
        "CAJA DE HERRAMIENTAS DE EDICIÓN PROFESIONAL (APLICA LAS QUE NECESITES):\n"
        "1. RECORTAR (Trim): Elimina frases muletilla al inicio ('Bueno, pues...', 'A ver...') o colas flojas al final desplazando el ID de inicio/fin a la frase fuerte.\n"
        "2. EXTENDER (Extend): Si falta el sujeto al principio o falta el remate/solución al final, amplía 1–8 frases hacia atrás o adelante usando los fragmentos cercanos.\n"
        "3. PODAR DIGRESIONES (Prune): Si dentro de un buen bloque continuo hay 1–3 frases de relleno, bromas internas o meta-referencias al podcast ('pero esto es otra historia', 'en esta santa casa', 'si me sobra media hora'), mételas en `drop_sentence_ids` en lugar de romper la historia.\n"
        "4. MOVER Y REORDENAR (Move & Stitch): Si ayuda al gancho, puedes poner un `cold_open_hook` de 1–2 frases impactantes delante (sin duplicarlo luego ni hacer spoiler), o traer 1–2 frases de presentación temprana como `context_intro` antes del `core` y rematar con `payoff`. Prefiere 1–2 macro-tramos fluidos antes que un picadillo de micro-cortes.\n"
        f"5. PRESUPUESTO DE DURACIÓN: Suma los segundos `[X.Xs]` de los tramos elegidos para quedar entre {target_window.min_sec:.0f}s y {target_window.max_sec:.0f}s SIN amputar la conclusión.\n"
        f"6. {closing_req}\n"
        "7. CONTEXTO SEPARADO DEL PODCAST (CORTINILLA IA): Si una gran anécdota de mitad del episodio empieza con pronombres ('ella', 'esta mujer', 'esto') y forzar un empalme lejano sonaría artificial, mantén el audio del podcast natural, marca `needs_ai_intro_curtain: true` y redacta `suggested_intro_curtain` (12–22 palabras en español situando QUIÉN, CUÁNDO/DÓNDE y QUÉ problema ocurre, enlazando justo antes de la primera palabra del podcast).\n"
        "8. POLÍTICA ANTI-DESCARTE: Tu misión es SALVAR Y EDITAR el vídeo con calidad profesional. SOLO devuelve `action: \"reject\"` si el fragmento es publicidad/promo pura o un galimatías irrecuperable.\n\n"
        "Devuelve JSON exacto:\n"
        "{\n"
        '  "action": "refine" | "reject",\n'
        '  "issue": "resumen concreto de las ediciones realizadas (recorte, extensión, poda, reordenamiento o intro)",\n'
        '  "drop_sentence_ids": ["S..."],\n'
        '  "needs_ai_intro_curtain": true/false,\n'
        '  "suggested_intro_curtain": "Texto de cortinilla IA de 12-22 palabras si needs_ai_intro_curtain=true (vacío si no hace falta)",\n'
        '  "refined_spans": [\n'
        f'    {{"start_sentence_id": "S...", "end_sentence_id": "S...", "role": "{span_roles_hint}"}}\n'
        "  ]\n"
        "}"
    )

    try:
        return llm.chat_json(model_role, sys_prompt, usr_prompt, temperature=0.15, max_tokens=2200, stage=f"nle_repair_{model_role}")
    except Exception as exc:
        return {"action": "reject", "issue": f"Error en reparación ({model_role}): {exc}", "refined_spans": []}


def _check_outline_coverage(
    cand: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    llm: OpenRouterClient,
    show_speakers: bool = False,
) -> dict[str, Any]:
    """Verify that the clip covers its own subtopic mini-outline (separate from Blind Judge cold-viewer check)."""
    outline = [str(x).strip() for x in ((cand.standalone_packaging or {}).get("video_outline") or []) if str(x).strip()]
    if len(outline) < 2:
        return {"covered": True, "missing_beats": [], "repair_instruction": ""}

    script_txt, _ = _format_candidate_script(cand, sentences, id_to_idx, show_speakers=show_speakers, for_blind_audit=True)
    if not script_txt:
        return {"covered": True, "missing_beats": [], "repair_instruction": ""}

    outline_str = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(outline))
    sys_prompt = (
        "Eres Editor Jefe de Contenido. Verificas si el guion seleccionado de un vídeo cubre los puntos clave "
        "de SU PROPIO mini-índice temático (sin exigir temas de otras partes del podcast). "
        "Si un vídeo promete contar un subtema/biografía/hito y se salta justo el hecho central o la solución de ese subtema, "
        "marca covered=false indicando exactamente qué punto falta. Responde solo en JSON válido."
    )
    usr_prompt = (
        f"Subtema del vídeo: {cand.angle_theme or cand.summary}\n"
        f"MINI-ÍNDICE PROPIO DE ESTE VÍDEO:\n{outline_str}\n\n"
        f"GUION ACTUAL DEL CORTE:\n{script_txt}\n\n"
        'Devuelve JSON exacto: {"covered": true/false, "missing_beats": ["punto del mini-índice que falta en el guion"], '
        '"repair_instruction": "qué tramo o hecho concreto falta añadir y qué relleno podría podarse para encajar en duración"}'
    )
    try:
        resp = llm.chat_json("fast", sys_prompt, usr_prompt, temperature=0.1, max_tokens=700, stage="outline_check")
        covered = bool(resp.get("covered", True))
        missing = [str(x).strip() for x in (resp.get("missing_beats") or []) if str(x).strip()]
        if not missing:
            covered = True
        return {
            "covered": covered,
            "missing_beats": missing,
            "repair_instruction": str(resp.get("repair_instruction") or "").strip(),
        }
    except Exception:
        return {"covered": True, "missing_beats": [], "repair_instruction": ""}


def _audit_and_refine_candidate(
    cand: ClipCandidate,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    target_window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    energy_blocks: list[dict],
    llm: OpenRouterClient,
    show_speakers: bool = False,
    blacklisted_indices: Optional[set[int]] = None,
    blocks: Optional[list[dict[str, Any]]] = None,
    block_map: Optional[list[dict[str, Any]]] = None,
    visual_bible: Optional[dict[str, Any]] = None,
    allow_strong_escalation: bool = True,
    used_strong_ref: Optional[list[int]] = None,
    profile: Optional[PodcasterProfile] = None,
    already_used_intros: Optional[list[str]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    telemetry_cb: Optional[Callable[[dict[str, Any]], None]] = None,
    rejected_records: Optional[list[dict[str, Any]]] = None,
) -> Optional[ClipCandidate]:
    """Blind Director / Cold-Viewer audit prioritizing real podcast voice first, then validated AI intro if needed."""
    blocked = blacklisted_indices or set()
    assembled_script, _ = _format_candidate_script(cand, sentences, id_to_idx, show_speakers=show_speakers)
    if not assembled_script:
        return None
    if not cand.candidate_id:
        cand.candidate_id = f"cand_{uuid.uuid4().hex[:8]}"
    if not cand.original_title:
        cand.original_title = (cand.metadata.titles[0] if cand.metadata.titles else cand.slug) or cand.id

    def _emit(msg: str) -> None:
        if log_cb is not None:
            try:
                log_cb(msg)
            except Exception:
                pass

    def _emit_tel(ev: dict[str, Any]) -> None:
        if telemetry_cb is not None:
            try:
                telemetry_cb({"ts": time.time(), "candidate_id": cand.candidate_id, **ev})
            except Exception:
                pass

    def _record_rejection(reason_txt: str, eval_snap: Optional[dict[str, Any]] = None) -> None:
        if rejected_records is None:
            return
        snap = eval_snap or {}
        rejected_records.append(
            {
                "candidate_id": cand.candidate_id,
                "title": clip_label,
                "clip_type": cand.clip_type,
                "score": cand.score,
                "orig_start": cand.orig_start,
                "orig_end": cand.orig_end,
                "reason": reason_txt,
                "comprehension_score": snap.get("comprehension_score"),
                "context_clarity": snap.get("context_clarity"),
                "clean_boundaries": snap.get("clean_boundaries"),
                "resolution_score": snap.get("resolution_score"),
                "story_interest_score": snap.get("story_interest_score"),
                "who": snap.get("who"),
                "candidate": cand.model_dump(mode="json"),
            }
        )

    def _build_pkg(
        ev: dict[str, Any],
        action_label: str,
        tier_used: str,
        issue_fixed: str = "",
        force_intro: Optional[bool] = None,
    ) -> dict[str, Any]:
        return {
            "comprehension_score": ev["comprehension_score"],
            "hook_strength": ev["hook_strength"],
            "context_clarity": ev["context_clarity"],
            "clean_boundaries": ev["clean_boundaries"],
            "resolution_score": ev["resolution_score"],
            "standalone_summary": ev["standalone_summary"],
            "audit_action": action_label,
            "audit_issue_fixed": issue_fixed,
            "who_identified": ev.get("who", ""),
            "what_and_context": ev.get("what_and_context", ""),
            "opening_4_6s_clear": bool(ev.get("opening_4_6s_clear", False)),
            "needs_ai_intro_curtain": bool(ev.get("needs_ai_intro_curtain", False)) if force_intro is None else force_intro,
            "missing_context_items": ev.get("missing_context_items", []),
            "opening_issue": ev.get("opening_issue", ""),
            "splices_coherent": bool(ev.get("splices_coherent", True)),
            "broken_splices": ev.get("broken_splices", []),
            "orphan_or_meta_refs": ev.get("orphan_or_meta_refs", []),
            "has_spoiler": bool(ev.get("has_spoiler", False)),
            "model_tier_used": tier_used,
            "video_outline": list((cand.standalone_packaging or {}).get("video_outline") or []),
        }

    clip_label = cand.metadata.titles[0] if cand.metadata.titles else cand.slug
    _emit(f"Juez Ciego evaluando '{clip_label}' ({cand.clip_type}, ~{cand.estimated_post_cut_sec:.0f}s)...")

    # Step A: True blind cold-viewer evaluation on real podcast audio (NO episode context leaked)
    eval_a = _run_cold_viewer_test(
        cand,
        sentences,
        id_to_idx,
        llm,
        show_speakers=show_speakers,
        model_role="editorial",
        visual_bible=visual_bible,
    )
    outline_chk = _check_outline_coverage(cand, sentences, id_to_idx, llm, show_speakers=show_speakers)
    missing_outline = (not outline_chk["covered"]) and bool(outline_chk["missing_beats"])
    if missing_outline:
        eval_a["missing_context_items"] = [
            *(eval_a.get("missing_context_items") or []),
            *[f"Falta punto clave del mini-índice: {b}" for b in outline_chk["missing_beats"]],
        ]
        prev_dir = str(eval_a.get("director_instructions") or "").strip()
        eval_a["director_instructions"] = (
            (outline_chk.get("repair_instruction") or "Incluir los puntos clave faltantes del mini-índice sin pasarse de duración.")
            + (f" | {prev_dir}" if prev_dir else "")
        )
    _emit_tel(
        {
            "type": "judge_audit",
            "clip_title": clip_label,
            "clip_type": cand.clip_type,
            "stage": "initial",
            "who": eval_a.get("who", ""),
            "what_and_context": eval_a.get("what_and_context", ""),
            "comprehension_score": eval_a["comprehension_score"],
            "hook_strength": eval_a["hook_strength"],
            "context_clarity": eval_a["context_clarity"],
            "clean_boundaries": eval_a["clean_boundaries"],
            "resolution_score": eval_a["resolution_score"],
            "clean_start": eval_a["clean_start"],
            "clean_end": eval_a["clean_end"],
            "splices_coherent": eval_a.get("splices_coherent", True),
            "resolution_answered": eval_a["resolution_answered"],
            "has_spoiler": eval_a.get("has_spoiler", False),
            "edit_operations": eval_a.get("edit_operations", []),
            "missing_context_items": eval_a.get("missing_context_items", []),
            "orphan_or_meta_refs": eval_a.get("orphan_or_meta_refs", []),
            "opening_issue": eval_a.get("opening_issue", ""),
            "passed_full": eval_a["passed_full"],
            "passed_with_intro_only": eval_a["passed_with_intro_only"],
            "outline_covered": outline_chk["covered"],
        }
    )
    if (not missing_outline) and eval_a["passed_full"] and not eval_a["needs_ai_intro_curtain"]:
        cand.standalone_packaging = _build_pkg(eval_a, "approve", "editorial", force_intro=False)
        cand.intro_curtain_enabled = False
        cand.summary = eval_a["standalone_summary"] or cand.summary
        _emit_tel(
            {
                "type": "candidate_outcome",
                "clip_title": clip_label,
                "clip_type": cand.clip_type,
                "accepted": True,
                "audit_action": "approve",
                "reason": "100% voz del podcast",
                "intro_curtain_text": "",
            }
        )
        _emit(
            f"Juez Ciego APRUEBA '{clip_label}' con 100% voz del podcast "
            f"(comp={eval_a['comprehension_score']}, ctx={eval_a['context_clarity']}, bnd={eval_a['clean_boundaries']})."
        )
        return cand

    best_intro_cand: Optional[ClipCandidate] = None
    best_intro_score = -1
    if (not missing_outline) and (eval_a["passed_full"] or eval_a["passed_with_intro_only"]):
        act = "approve_needs_intro"
        cand.standalone_packaging = _build_pkg(
            eval_a,
            act,
            "editorial",
            issue_fixed=eval_a.get("opening_issue", ""),
            force_intro=True,
        )
        best_intro_cand = cand
        best_intro_score = eval_a["composite_score"]

    _emit(
        f"Juez Ciego solicita reparación de guion para '{clip_label}' "
        f"(who={eval_a.get('who')}, comp={eval_a['comprehension_score']}, ctx={eval_a['context_clarity']}, "
        f"bnd={eval_a['clean_boundaries']}, res={eval_a['resolution_score']}, "
        f"start_ok={eval_a['clean_start']}, end_ok={eval_a['clean_end']}, splices_ok={eval_a.get('splices_coherent', True)}, "
        f"res_ok={eval_a['resolution_answered']}, intro_ok={eval_a['passed_with_intro_only']}, "
        f"spoiler={eval_a.get('has_spoiler', False)}). Priorizando voz real del podcast..."
    )

    repair_ctx = _gather_repair_context(
        cand,
        sentences,
        id_to_idx,
        blocks=blocks,
        block_map=block_map,
        visual_bible=visual_bible,
        blacklisted_indices=blocked,
        show_speakers=show_speakers,
    )

    is_severe = (not eval_a["passed_with_intro_only"]) and (
        eval_a["who_unknown"]
        or eval_a["what_unknown"]
        or (not eval_a["resolution_answered"])
        or (not eval_a.get("splices_coherent", True))
        or bool(eval_a.get("has_spoiler", False))
        or min(
            eval_a["comprehension_score"],
            eval_a["context_clarity"],
            eval_a["clean_boundaries"],
            eval_a["resolution_score"],
        )
        < 60
    )

    def _run_repair_and_verify(
        tier: Literal["editorial", "editorial_strong"],
    ) -> tuple[Optional[ClipCandidate], Optional[dict[str, Any]], str, str]:
        rep = _request_editorial_repair(
            cand,
            eval_a,
            repair_ctx,
            target_window,
            llm,
            model_role=tier,
            sentences=sentences,
            id_to_idx=id_to_idx,
            show_speakers=show_speakers,
        )
        rep_action = str(rep.get("action") or "refine").strip().lower()
        rep_issue = str(rep.get("issue") or "").strip()
        rep_intro_hint = str(rep.get("suggested_intro_curtain") or "").strip()
        before_spans = [
            {"start": sp.start_sentence_id, "end": sp.end_sentence_id, "role": sp.role}
            for sp in (cand.narrative_spans or [])
        ]
        _emit_tel(
            {
                "type": "nle_repair",
                "clip_title": clip_label,
                "tier": tier,
                "action": rep_action,
                "issue": rep_issue,
                "before_spans": before_spans,
                "after_spans": rep.get("refined_spans") or [],
                "drop_sentence_ids": rep.get("drop_sentence_ids") or [],
                "needs_ai_intro_curtain": bool(rep.get("needs_ai_intro_curtain", False)),
                "suggested_intro_curtain": rep_intro_hint,
            }
        )
        if rep_action == "reject":
            return None, None, rep_issue, rep_intro_hint
        new_spans_idx = _parse_raw_spans(
            {
                "spans": rep.get("refined_spans") or [],
                "drop_sentence_ids": rep.get("drop_sentence_ids") or [],
            },
            id_to_idx,
            blacklisted_indices=blocked,
            allow_payoff_role=(cand.clip_type != "short_cliffhanger"),
        )
        if not new_spans_idx:
            return None, None, rep_issue, rep_intro_hint
        rebuilt = _build_candidate_from_spans(
            raw={
                "score": cand.score,
                "titles": cand.metadata.titles,
                "description": cand.metadata.description,
                "hashtags": cand.metadata.hashtags,
                "thumbnail_text": cand.metadata.thumbnail_text,
                "cliffhanger_question": cand.metadata.cliffhanger_question,
                "hook_text": cand.metadata.hook_text,
                "summary": cand.summary,
                "hook_reason": cand.hook_reason,
                "narrative_archetype": cand.narrative_archetype,
                "angle_id": cand.angle_id,
                "angle_theme": cand.angle_theme,
                "video_outline": list((cand.standalone_packaging or {}).get("video_outline") or []),
                "chapters": [ch.model_dump() for ch in cand.metadata.chapters],
            },
            spans_idx=new_spans_idx,
            clip_type=cand.clip_type,
            aspect_ratio=cand.aspect_ratio,
            sentences=sentences,
            id_to_idx=id_to_idx,
            target_window=target_window,
            silence_intervals=silence_intervals,
            audio_config=audio_config,
            energy_blocks=energy_blocks,
            idx_counter=1,
            blacklisted_indices=blocked,
        )
        if rebuilt is None:
            return None, None, rep_issue, rep_intro_hint
        ver_eval = _run_cold_viewer_test(
            rebuilt,
            sentences,
            id_to_idx,
            llm,
            show_speakers=show_speakers,
            model_role="editorial",
            visual_bible=visual_bible,
        )
        _emit_tel(
            {
                "type": "judge_audit",
                "clip_title": clip_label,
                "clip_type": rebuilt.clip_type,
                "stage": f"post_repair_{tier}",
                "who": ver_eval.get("who", ""),
                "what_and_context": ver_eval.get("what_and_context", ""),
                "comprehension_score": ver_eval["comprehension_score"],
                "hook_strength": ver_eval["hook_strength"],
                "context_clarity": ver_eval["context_clarity"],
                "clean_boundaries": ver_eval["clean_boundaries"],
                "resolution_score": ver_eval["resolution_score"],
                "clean_start": ver_eval["clean_start"],
                "clean_end": ver_eval["clean_end"],
                "splices_coherent": ver_eval.get("splices_coherent", True),
                "resolution_answered": ver_eval["resolution_answered"],
                "has_spoiler": ver_eval.get("has_spoiler", False),
                "edit_operations": ver_eval.get("edit_operations", []),
                "missing_context_items": ver_eval.get("missing_context_items", []),
                "orphan_or_meta_refs": ver_eval.get("orphan_or_meta_refs", []),
                "opening_issue": ver_eval.get("opening_issue", ""),
                "passed_full": ver_eval["passed_full"],
                "passed_with_intro_only": ver_eval["passed_with_intro_only"],
            }
        )
        _emit(
            f"Juez Ciego post-reparación ({tier}) '{clip_label}': who={ver_eval.get('who')}, "
            f"comp={ver_eval['comprehension_score']}, ctx={ver_eval['context_clarity']}, "
            f"bnd={ver_eval['clean_boundaries']}, res={ver_eval['resolution_score']}, "
            f"start_ok={ver_eval['clean_start']}, end_ok={ver_eval['clean_end']}, "
            f"splices_ok={ver_eval.get('splices_coherent', True)}, res_ok={ver_eval['resolution_answered']}, "
            f"intro_ok={ver_eval['passed_with_intro_only']}."
        )
        return rebuilt, ver_eval, rep_issue, rep_intro_hint

    def _is_splice_safe_for_intro(ev_cand: dict[str, Any]) -> bool:
        """True when clip body has clean structure/resolution and can be framed with an AI intro curtain."""
        raw_res_val = int(ev_cand.get("raw_resolution_score", ev_cand.get("resolution_score", 0)))
        res_ok = bool(ev_cand.get("resolution_answered", False)) or (
            raw_res_val >= 65 and not ev_cand.get("has_spoiler", False)
        )
        return bool(
            ev_cand.get("splices_coherent", True)
            and not ev_cand.get("broken_splices")
            and not ev_cand.get("has_spoiler", False)
            and ev_cand.get("clean_end", True)
            and int(ev_cand.get("comprehension_score", 0)) >= 45
            and res_ok
        )

    achieved_passed_full = bool(eval_a["passed_full"])
    best_salvage_cand: Optional[ClipCandidate] = None
    best_salvage_score = -1
    best_repaired_cand: Optional[ClipCandidate] = None
    best_repaired_score = -1
    best_intro_hint = ""

    def _track_repaired(
        rebuilt_obj: ClipCandidate,
        ver_obj: dict[str, Any],
        tier_name: Literal["editorial", "editorial_strong"],
        issue_txt: str,
        hint_txt: str,
    ) -> None:
        nonlocal best_repaired_cand, best_repaired_score, best_intro_hint
        if not ver_obj.get("has_spoiler", False) and (
            best_repaired_cand is None or ver_obj["composite_score"] >= best_repaired_score
        ):
            rebuilt_obj.standalone_packaging = _build_pkg(
                ver_obj, f"{tier_name}_refine->needs_intro", tier_name, issue_fixed=issue_txt, force_intro=True
            )
            rebuilt_obj.summary = ver_obj["standalone_summary"] or rebuilt_obj.summary
            best_repaired_cand = rebuilt_obj
            best_repaired_score = ver_obj["composite_score"]
            if hint_txt:
                best_intro_hint = hint_txt

    if best_intro_cand is None and _is_splice_safe_for_intro(eval_a):
        cand.standalone_packaging = _build_pkg(
            eval_a,
            "salvage_needs_intro",
            "editorial",
            issue_fixed=eval_a.get("opening_issue", ""),
            force_intro=True,
        )
        best_salvage_cand = cand
        best_salvage_score = eval_a["composite_score"]

    # Step B1: Fast-Track keeps healthy body safe from expensive strong-tier escalation while still
    # allowing 1 cheap boundary-repair attempt for 100% real podcast voice (guarded by Do-No-Harm).
    intro_allowed = bool(profile is not None and profile.intro_curtain.enabled)
    fast_track_healthy = bool(
        intro_allowed
        and (not missing_outline)
        and (not eval_a.get("has_spoiler", False))
        and eval_a.get("splices_coherent", True)
        and (not eval_a.get("broken_splices"))
        and eval_a.get("clean_end", True)
        and (eval_a.get("resolution_answered", False) or int(eval_a.get("raw_resolution_score", 0)) >= 65)
        and eval_a.get("comprehension_score", 0) >= 55
        and (best_intro_cand is not None or best_salvage_cand is not None)
    )
    if fast_track_healthy:
        _emit(
            f"Fast-Track editorial para '{clip_label}': cuerpo sólido (comp={eval_a['comprehension_score']}, "
            f"res={eval_a['resolution_score']}), protegiendo cuerpo original y omitiendo escalado fuerte."
        )

    def _repair_preserves_or_improves(ver_obj: dict[str, Any]) -> bool:
        """Do-No-Harm guard: reject repairs that break splice coherence or degrade the original body."""
        if not ver_obj.get("splices_coherent", True) or ver_obj.get("broken_splices"):
            return False
        if ver_obj.get("has_spoiler", False):
            return False
        orig_res = int(eval_a.get("raw_resolution_score", eval_a.get("resolution_score", 0)))
        new_res = int(ver_obj.get("raw_resolution_score", ver_obj.get("resolution_score", 0)))
        if orig_res >= 70 and new_res < orig_res - 15 and ver_obj["composite_score"] <= eval_a["composite_score"]:
            return False
        return ver_obj["composite_score"] >= (eval_a["composite_score"] - 5)

    if fast_track_healthy or (not is_severe) or (not allow_strong_escalation):
        rebuilt_ed, ver_ed, issue_ed, hint_ed = _run_repair_and_verify("editorial")
        if rebuilt_ed is not None and ver_ed is not None:
            rebuilt_ed.candidate_id = cand.candidate_id
            rebuilt_ed.original_title = cand.original_title
            if ver_ed["passed_full"] and not ver_ed["needs_ai_intro_curtain"]:
                rebuilt_ed.standalone_packaging = _build_pkg(
                    ver_ed, "refine->approve", "editorial", issue_fixed=issue_ed, force_intro=False
                )
                rebuilt_ed.intro_curtain_enabled = False
                rebuilt_ed.summary = ver_ed["standalone_summary"] or rebuilt_ed.summary
                _emit_tel(
                    {
                        "type": "candidate_outcome",
                        "clip_title": clip_label,
                        "clip_type": rebuilt_ed.clip_type,
                        "accepted": True,
                        "audit_action": "refine->approve",
                        "reason": issue_ed or "Aprobado tras reparación con voz del podcast",
                        "intro_curtain_text": "",
                    }
                )
                _emit(f"Juez Ciego APRUEBA '{clip_label}' tras reparación con voz del podcast: {issue_ed}")
                return rebuilt_ed
            if ver_ed["passed_full"]:
                achieved_passed_full = True
                if best_intro_cand is None or ver_ed["composite_score"] >= best_intro_score:
                    rebuilt_ed.standalone_packaging = _build_pkg(
                        ver_ed,
                        "refine->approve",
                        "editorial",
                        issue_fixed=issue_ed,
                        force_intro=bool(ver_ed["needs_ai_intro_curtain"]),
                    )
                    rebuilt_ed.summary = ver_ed["standalone_summary"] or rebuilt_ed.summary
                    best_intro_cand = rebuilt_ed
                    best_intro_score = ver_ed["composite_score"]
                    if hint_ed:
                        best_intro_hint = hint_ed
            elif ver_ed["passed_with_intro_only"] and _repair_preserves_or_improves(ver_ed) and ver_ed["composite_score"] >= best_intro_score:
                rebuilt_ed.standalone_packaging = _build_pkg(
                    ver_ed, "refine->approve_needs_intro", "editorial", issue_fixed=issue_ed, force_intro=True
                )
                rebuilt_ed.summary = ver_ed["standalone_summary"] or rebuilt_ed.summary
                best_intro_cand = rebuilt_ed
                best_intro_score = ver_ed["composite_score"]
                if hint_ed:
                    best_intro_hint = hint_ed
            elif _is_splice_safe_for_intro(ver_ed) and _repair_preserves_or_improves(ver_ed) and ver_ed["composite_score"] >= best_salvage_score:
                rebuilt_ed.standalone_packaging = _build_pkg(
                    ver_ed, "refine->salvage_needs_intro", "editorial", issue_fixed=issue_ed, force_intro=True
                )
                rebuilt_ed.summary = ver_ed["standalone_summary"] or rebuilt_ed.summary
                best_salvage_cand = rebuilt_ed
                best_salvage_score = ver_ed["composite_score"]
                if hint_ed:
                    best_intro_hint = hint_ed
            elif _repair_preserves_or_improves(ver_ed):
                _track_repaired(rebuilt_ed, ver_ed, "editorial", issue_ed, hint_ed)

    # Step B2: Escalate to editorial_strong ONLY when severe or cheap repair failed without a valid intro candidate
    if (not fast_track_healthy) and allow_strong_escalation and (is_severe or (not achieved_passed_full and best_intro_cand is None)):
        if used_strong_ref is not None:
            used_strong_ref[0] += 1
        _emit(f"Escalando reparación quirúrgica de '{clip_label}' a modelo experto...")
        rebuilt_st, ver_st, issue_st, hint_st = _run_repair_and_verify("editorial_strong")
        if rebuilt_st is not None and ver_st is not None:
            rebuilt_st.candidate_id = cand.candidate_id
            rebuilt_st.original_title = cand.original_title
            if ver_st["passed_full"] and not ver_st["needs_ai_intro_curtain"]:
                rebuilt_st.standalone_packaging = _build_pkg(
                    ver_st,
                    "strong_refine->approve",
                    "editorial_strong",
                    issue_fixed=issue_st,
                    force_intro=False,
                )
                rebuilt_st.intro_curtain_enabled = False
                rebuilt_st.summary = ver_st["standalone_summary"] or rebuilt_st.summary
                _emit_tel(
                    {
                        "type": "candidate_outcome",
                        "clip_title": clip_label,
                        "clip_type": rebuilt_st.clip_type,
                        "accepted": True,
                        "audit_action": "strong_refine->approve",
                        "reason": issue_st or "Aprobado por experto con voz real",
                        "intro_curtain_text": "",
                    }
                )
                _emit(f"Juez Ciego APRUEBA '{clip_label}' (experto, voz real): {issue_st}")
                return rebuilt_st
            if ver_st["passed_full"]:
                rebuilt_st.standalone_packaging = _build_pkg(
                    ver_st,
                    "strong_refine->approve",
                    "editorial_strong",
                    issue_fixed=issue_st,
                    force_intro=bool(ver_st["needs_ai_intro_curtain"]),
                )
                rebuilt_st.summary = ver_st["standalone_summary"] or rebuilt_st.summary
                best_intro_cand = rebuilt_st
                best_intro_score = ver_st["composite_score"]
                achieved_passed_full = True
                if hint_st:
                    best_intro_hint = hint_st
            elif ver_st["passed_with_intro_only"] and _repair_preserves_or_improves(ver_st) and ver_st["composite_score"] >= best_intro_score:
                rebuilt_st.standalone_packaging = _build_pkg(
                    ver_st,
                    "strong_refine->approve_needs_intro",
                    "editorial_strong",
                    issue_fixed=issue_st,
                    force_intro=True,
                )
                rebuilt_st.summary = ver_st["standalone_summary"] or rebuilt_st.summary
                best_intro_cand = rebuilt_st
                best_intro_score = ver_st["composite_score"]
                if hint_st:
                    best_intro_hint = hint_st
            elif _is_splice_safe_for_intro(ver_st) and _repair_preserves_or_improves(ver_st) and ver_st["composite_score"] >= best_salvage_score:
                rebuilt_st.standalone_packaging = _build_pkg(
                    ver_st,
                    "strong_refine->salvage_needs_intro",
                    "editorial_strong",
                    issue_fixed=issue_st,
                    force_intro=True,
                )
                rebuilt_st.summary = ver_st["standalone_summary"] or rebuilt_st.summary
                best_salvage_cand = rebuilt_st
                best_salvage_score = ver_st["composite_score"]
                if hint_st:
                    best_intro_hint = hint_st
            elif _repair_preserves_or_improves(ver_st):
                _track_repaired(rebuilt_st, ver_st, "editorial_strong", issue_st, hint_st)

    # Step C: Package body with AI intro curtain (pre-validated by Blind Judge!) so standalone context is crystal clear
    target_cand = best_intro_cand or ((best_salvage_cand or best_repaired_cand) if intro_allowed else None)
    if (
        target_cand is None
        and intro_allowed
        and (not missing_outline)
        and eval_a.get("splices_coherent", True)
        and (not eval_a.get("broken_splices"))
        and (not eval_a.get("has_spoiler", False))
        and int(eval_a.get("comprehension_score", 0)) >= 45
    ):
        # Last-resort Step C check on the unbroken original body when NLE repairs degraded coherence
        cand.standalone_packaging = _build_pkg(
            eval_a,
            "orig_needs_intro",
            "editorial",
            issue_fixed=eval_a.get("opening_issue", ""),
            force_intro=True,
        )
        target_cand = cand
    if target_cand is not None:
        target_cand.candidate_id = cand.candidate_id
        target_cand.original_title = cand.original_title
        target_cand.standalone_packaging["needs_ai_intro_curtain"] = True
        if intro_allowed and profile is not None:
            ic_script = generate_intro_curtain_script(
                target_cand,
                profile,
                visual_bible or {},
                llm,
                sentences=sentences,
                id_to_idx=id_to_idx,
                already_used_intros=already_used_intros,
                suggested_intro_hint=best_intro_hint,
            )
            target_cand.intro_curtain_enabled = True
            target_cand.intro_curtain_text = ic_script["spoken_hook"]
            target_cand.intro_curtain_phrases = ic_script["phrases"]
            target_cand.intro_curtain_badge = ic_script["badge"]

            last_ei = id_to_idx.get(
                target_cand.narrative_spans[-1].end_sentence_id
                if target_cand.narrative_spans
                else target_cand.end_sentence_id
            )
            dangling_last = (
                last_ei is not None
                and _norm_last_word(sentences[last_ei].text) in DANGLING_END_WORDS
            )
            pre_splices_ok = bool((target_cand.standalone_packaging or {}).get("splices_coherent", True))
            pre_res_score = int((target_cand.standalone_packaging or {}).get("resolution_score", 0) or 0)
            pre_res_ok = bool(eval_a.get("resolution_answered", False)) or pre_res_score >= 70

            def _eval_intro_ok(ev_ic: dict[str, Any]) -> bool:
                splices_native = bool(ev_ic.get("splices_coherent", True)) and not bool(ev_ic.get("broken_splices"))
                splices_pass = splices_native or pre_splices_ok
                min_comp = 60 if splices_native else 50
                raw_ic_res = int(ev_ic.get("raw_resolution_score", ev_ic.get("resolution_score", 0)))
                res_pass = (
                    bool(ev_ic.get("resolution_answered", False))
                    or (
                        target_cand.clip_type == "short_cliffhanger"
                        and (
                            raw_ic_res >= 55
                            or (
                                ev_ic["comprehension_score"] >= 65
                                and ev_ic["context_clarity"] >= 75
                                and not bool(ev_ic.get("has_spoiler", False))
                                and bool(
                                    str(ev_ic.get("central_question_or_conflict") or "").strip()
                                    or str(ev_ic.get("answer_or_mystery_stated") or "").strip()
                                )
                            )
                        )
                    )
                    or (
                        target_cand.clip_type == "short_conclusive"
                        and not bool(ev_ic.get("has_spoiler", False))
                        and (
                            raw_ic_res >= 65
                            or (
                                pre_res_ok
                                and ev_ic["comprehension_score"] >= 65
                                and ev_ic["context_clarity"] >= 80
                                and splices_pass
                            )
                        )
                    )
                    or (
                        target_cand.clip_type in ("medium", "long")
                        and ev_ic["comprehension_score"] >= 65
                        and ev_ic["context_clarity"] >= 70
                    )
                )
                return bool(
                    (not ev_ic["who_unknown"])
                    and (not ev_ic["what_unknown"])
                    and (not dangling_last)
                    and splices_pass
                    and res_pass
                    and (not ev_ic.get("has_spoiler", False))
                    and ev_ic["comprehension_score"] >= min_comp
                    and ev_ic["context_clarity"] >= 65
                )

            # Pre-validate [INTRO IA] + [AUDIO PODCAST] with Blind Judge
            intro_eval = _run_cold_viewer_test(
                target_cand,
                sentences,
                id_to_idx,
                llm,
                show_speakers=show_speakers,
                model_role="editorial",
                visual_bible=visual_bible,
                include_intro_curtain=True,
            )
            intro_ok = _eval_intro_ok(intro_eval)
            if not intro_ok:
                fail_reasons: list[str] = []
                if intro_eval.get("has_spoiler") and target_cand.clip_type == "short_cliffhanger":
                    fail_reasons.append("Hizo SPOILER de la solución final en cliffhanger")
                if intro_eval["who_unknown"]:
                    fail_reasons.append("NO nombró explícitamente al protagonista/sujeto real (who=DESCONOCIDO)")
                if intro_eval["what_unknown"]:
                    fail_reasons.append("NO situó el problema, época o escenario (what=DESCONOCIDO)")
                if intro_eval.get("missing_context_items"):
                    fail_reasons.append("Faltó: " + ", ".join(str(x) for x in intro_eval["missing_context_items"][:4]))
                if intro_eval.get("opening_issue"):
                    fail_reasons.append(str(intro_eval["opening_issue"]))
                retry_note = (
                    "IMPORTANTE — La cortinilla anterior NO pasó el Juez Ciego por: "
                    + ("; ".join(fail_reasons) or f"comprehension={intro_eval['comprehension_score']}, context={intro_eval['context_clarity']}")
                    + ". Reescríbela dando el protagonista exacto, año/lugar y conflicto en 12-22 palabras"
                    + (" SIN revelar el desenlace." if target_cand.clip_type == "short_cliffhanger" else ".")
                )
                ic_retry = generate_intro_curtain_script(
                    target_cand,
                    profile,
                    visual_bible or {},
                    llm,
                    sentences=sentences,
                    id_to_idx=id_to_idx,
                    already_used_intros=already_used_intros,
                    anti_spoiler_note=retry_note,
                    suggested_intro_hint=best_intro_hint,
                )
                target_cand.intro_curtain_text = ic_retry["spoken_hook"]
                target_cand.intro_curtain_phrases = ic_retry["phrases"]
                target_cand.intro_curtain_badge = ic_retry["badge"]
                intro_eval = _run_cold_viewer_test(
                    target_cand,
                    sentences,
                    id_to_idx,
                    llm,
                    show_speakers=show_speakers,
                    model_role="editorial",
                    visual_bible=visual_bible,
                    include_intro_curtain=True,
                )
                intro_ok = _eval_intro_ok(intro_eval)

            if not intro_ok:
                rej_reason = (
                    f"who={intro_eval.get('who')}, comp={intro_eval['comprehension_score']}, "
                    f"ctx={intro_eval['context_clarity']}, splices_ok={intro_eval.get('splices_coherent', True)}, "
                    f"res_ok={intro_eval.get('resolution_answered')}"
                )
                _emit_tel(
                    {
                        "type": "candidate_outcome",
                        "clip_title": clip_label,
                        "clip_type": target_cand.clip_type,
                        "accepted": False,
                        "audit_action": "reject_with_intro",
                        "reason": rej_reason,
                        "intro_curtain_text": target_cand.intro_curtain_text,
                    }
                )
                _record_rejection(f"Rechazado incluso con cortinilla IA ({rej_reason})", intro_eval)
                _emit(
                    f"Juez Ciego RECHAZA '{clip_label}' incluso con cortinilla IA "
                    f"(who={intro_eval.get('who')}, what={intro_eval.get('what_and_context')}, "
                    f"comp={intro_eval['comprehension_score']}, ctx={intro_eval['context_clarity']}, "
                    f"splices_ok={intro_eval.get('splices_coherent', True)}, res_ok={intro_eval.get('resolution_answered')}, "
                    f"spoiler={intro_eval.get('has_spoiler', False)})."
                )
                return None

            if already_used_intros is not None and target_cand.intro_curtain_text:
                already_used_intros.append(target_cand.intro_curtain_text)
            target_cand.standalone_packaging["standalone_summary"] = (
                intro_eval.get("standalone_summary") or target_cand.standalone_packaging.get("standalone_summary", "")
            )
            target_cand.standalone_packaging["who_identified"] = (
                intro_eval.get("who") or target_cand.standalone_packaging.get("who_identified", "")
            )
            target_cand.standalone_packaging["what_and_context"] = (
                intro_eval.get("what_and_context") or target_cand.standalone_packaging.get("what_and_context", "")
            )
            target_cand.standalone_packaging["comprehension_score"] = max(
                int(target_cand.standalone_packaging.get("comprehension_score", 70)),
                int(intro_eval.get("comprehension_score", 70)),
            )
            target_cand.standalone_packaging["context_clarity"] = max(
                int(target_cand.standalone_packaging.get("context_clarity", 70)),
                int(intro_eval.get("context_clarity", 70)),
            )
            target_cand.summary = target_cand.standalone_packaging["standalone_summary"] or target_cand.summary
            _emit_tel(
                {
                    "type": "candidate_outcome",
                    "clip_title": clip_label,
                    "clip_type": target_cand.clip_type,
                    "accepted": True,
                    "audit_action": str((target_cand.standalone_packaging or {}).get("audit_action") or "approve_needs_intro"),
                    "reason": f"comp={intro_eval['comprehension_score']}, ctx={intro_eval['context_clarity']}",
                    "intro_curtain_text": target_cand.intro_curtain_text,
                }
            )
            _emit(
                f"Juez Ciego APRUEBA '{clip_label}' con cortinilla IA pre-validada "
                f"(comp={intro_eval['comprehension_score']}, ctx={intro_eval['context_clarity']}): "
                f"\"{target_cand.intro_curtain_text}\""
            )
        return target_cand

    dis_reason = eval_a.get("opening_issue") or "Sin contexto autónomo válido"
    _emit_tel(
        {
            "type": "candidate_outcome",
            "clip_title": clip_label,
            "clip_type": cand.clip_type,
            "accepted": False,
            "audit_action": "discard",
            "reason": dis_reason,
            "intro_curtain_text": "",
        }
    )
    _record_rejection(f"Descartado por Juez Ciego: {dis_reason}", eval_a)
    _emit(f"Juez Ciego DESCARTA '{clip_label}': no pudo garantizar contexto autónomo válido; se buscará otro ángulo.")
    return None


def _plan_distinct_episode_angles(
    llm: OpenRouterClient,
    profile: PodcasterProfile,
    blocks: list[dict[str, Any]],
    block_map: list[dict[str, Any]],
    sentences: list[SentenceSegment],
    visual_bible: Optional[dict[str, Any]],
    total_shorts: int,
    modality_counts: Optional[dict[str, int]] = None,
    blacklisted_indices: Optional[set[int]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> list[dict[str, Any]]:
    """Plan mutually exclusive thematic sub-angles/chapters before Short selection."""
    if total_shorts <= 0 or not sentences:
        return []

    blocked = blacklisted_indices or set()
    ask_angles = min(12, max(total_shorts * 3, 6))

    overview_lines: list[str] = []
    if block_map:
        for m in block_map:
            if m.get("is_ad_or_promo"):
                continue
            bi = int(m.get("block_index", 0))
            b_ref = blocks[bi] if 0 <= bi < len(blocks) else None
            sample = ""
            if b_ref and b_ref.get("sentences"):
                clean_b = [s for s in b_ref["sentences"][:3]]
                sample = " ".join(s.text for s in clean_b)[:140]
            resumen = " ".join(str(m.get("resumen_breve") or "").split())[:220]
            overview_lines.append(
                f"- BLOQUE B{bi} ({m.get('tema', '')}) | interés={m.get('interes_general', 5)}/10 "
                f"hook={m.get('potencial_cliffhanger', 5)}/10 concl={m.get('potencial_conclusivo', 5)}/10 "
                f"| Resumen: {resumen} | Muestra: {sample}"
            )
    else:
        for bi, b in enumerate(blocks):
            clean_b = [s for s in b.get("sentences", [])]
            if not clean_b:
                continue
            sample_txt = " ".join(s.text for s in clean_b[:5])[:220]
            overview_lines.append(
                f"- BLOQUE B{bi} ({clean_b[0].start:.0f}s..{clean_b[-1].end:.0f}s, energía={b.get('energy', 'media')}): {sample_txt}"
            )

    if not overview_lines:
        return []

    vb = visual_bible or {}
    mod_desc = ""
    if modality_counts:
        mod_desc = ", ".join(f"{k}={v}" for k, v in modality_counts.items() if v > 0)

    sys_prompt = (
        "Eres Showrunner Editorial y Estratega de Contenido Vertical experto en planificación temática de podcasts. "
        "Incluso cuando un episodio trata sobre un único protagonista o gran tema, SIEMPRE contiene sub-ángulos, "
        "capítulos, anécdotas humanas/épicas, giros de inteligencia o espionaje, dilemas y clímax COMPLETAMENTE DISTINTOS entre sí.\n"
        "Tu misión es detectar las historias MÁS MAGNÉTICAS, DRAMÁTICAS Y MEMORABLES del episodio completo "
        "(priorizando anécdotas de acción, giros inesperados, espionaje, errores fatales o datos sorprendentes por encima de descripciones geográficas o listas técnicas), "
        "identificando qué bloques contienen cada historia.\n"
        "Responde SOLO JSON válido con clave 'angles'."
    )
    usr_prompt = (
        f"Podcast: {profile.podcast_name} | Nicho: {profile.niche_description}\n"
        f"Tema global del episodio: {vb.get('episode_topic', '')}\n"
        f"Época/Escenario: {vb.get('era_or_setting', '')}\n"
        f"Hilo narrativo: {vb.get('narrative_spine', '')}\n"
        f"Planifica {ask_angles} sub-ángulos TOTALMENTE INDEPENDIENTES ordenados de mayor a menor gancho narrativo para cubrir cuotas ({mod_desc or f'shorts={total_shorts}'}) y tener alternativas sólidas.\n\n"
        "REGLAS ESTRICTAS DE DIVERSIFICACIÓN Y SELECCIÓN:\n"
        "1. PRIORIZA LAS GRANDES HISTORIAS DEL EPISODIO: Busca episodios memorables, hazañas, choques inesperados, espías/engaños o dilemas humanos a lo largo de TODO el mapa (principio, nudo y clímax), NO te quedes solo con la introducción logística.\n"
        "2. CADA sub-ángulo debe responder a una pregunta, conflicto o anécdota DISTINTA.\n"
        "3. PROHIBIDO proponer dos ángulos sobre el mismo evento si solo cambia el título.\n"
        "4. Indica qué bloques `B#` contienen la historia (`target_blocks`) y qué contexto necesita un espectador nuevo que no ha visto el podcast (`cold_viewer_context`).\n\n"
        "MAPA DEL EPISODIO:\n"
        + "\n".join(overview_lines[:48])
        + "\n\nDevuelve JSON exacto:\n"
        '{"angles": [{"angle_id": "A1", "angle_theme": "título concreto del sub-ángulo (3-8 palabras)", '
        '"central_question": "pregunta específica que responde o plantea este ángulo", '
        '"cold_viewer_context": "quién, cuándo y qué necesita saber un espectador nuevo en 1 frase", '
        '"target_blocks": [0, 1], "recommended_modality": "short_conclusive | short_cliffhanger", '
        '"why_distinct": "en qué se diferencia del resto"}]}'
    )

    try:
        resp = llm.chat_json("editorial", sys_prompt, usr_prompt, temperature=0.25, max_tokens=2600, stage="plan_angles")
    except Exception:
        return []

    raw_angles = resp.get("angles") or []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, a in enumerate(raw_angles, start=1):
        if not isinstance(a, dict):
            continue
        theme = str(a.get("angle_theme") or "").strip()
        if not theme:
            continue
        aid = str(a.get("angle_id") or f"A{idx}").strip() or f"A{idx}"
        if aid in seen_ids:
            aid = f"A{idx}"
        seen_ids.add(aid)
        out.append(
            {
                "angle_id": aid,
                "angle_theme": theme,
                "central_question": str(a.get("central_question") or "").strip(),
                "target_blocks": [int(x) for x in (a.get("target_blocks") or []) if isinstance(x, (int, float))],
                "recommended_modality": str(a.get("recommended_modality") or "short_conclusive").strip(),
                "why_distinct": str(a.get("why_distinct") or "").strip(),
            }
        )

    if out and log_cb is not None:
        summary_str = " | ".join(f"{a['angle_id']}: {a['angle_theme']}" for a in out[:6])
        try:
            log_cb(f"Planificador temático detectó {len(out)} sub-ángulos distintos: {summary_str}")
        except Exception:
            pass
    return out


def _select_shorts_by_modality(
    llm: OpenRouterClient,
    profile: PodcasterProfile,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    transcript_excerpt: str,
    modality: Literal["short_cliffhanger", "short_conclusive"],
    count: int,
    window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    energy_blocks: list[dict],
    show_speakers: bool = False,
    blacklisted_indices: Optional[set[int]] = None,
    visual_bible: Optional[dict[str, Any]] = None,
    block_map: Optional[list[dict[str, Any]]] = None,
    model_role: Literal["editorial", "editorial_strong"] = "editorial",
    exclude_intervals: Optional[list[tuple[float, float]]] = None,
    exclude_topics: Optional[list[str]] = None,
    assigned_angles: Optional[list[dict[str, Any]]] = None,
) -> list[ClipCandidate]:
    if count <= 0:
        return []

    ask_n = max(count + 2, 4) if model_role == "editorial" else min(4, count + 2)
    buf_min = round(max(30.0, window.min_sec * 1.05), 1)
    buf_max = round(max(buf_min + 12.0, window.max_sec * 0.90), 1)

    if modality == "short_cliffhanger":
        mode_rules = (
            "MODALIDAD: SHORTS CON CLIFFHANGER (LAZO ABIERTO E INTRIGA GENUINA CON CONTEXTO 100% CLARO).\n"
            "- Presenta EXPLÍCITAMENTE quién es el protagonista, qué problema ocurre, cuándo/dónde y por qué importa.\n"
            "- Corta en una idea/oración completa justo en el pico de intriga ANTES de revelar la solución final, dejando clarísimo qué falta por resolver.\n"
            "- PROHIBIDO usar `role=\"payoff\"` en short_cliffhanger: NO reveles la solución ni el desenlace (ni en el texto ni en títulos).\n"
            "- Si usas un `cold_open_hook` delante, NUNCA debe destripar la resolución ni repetirse luego.\n"
            "- Incluye obligatoriamente 'cliffhanger_question' (pregunta magnética máx. 9 palabras)."
        )
        span_example = (
            '  "spans": [\n'
            '    {"start_sentence_id": "S0066", "end_sentence_id": "S0088", "role": "core"}\n'
            "  ],\n"
            '  "drop_sentence_ids": [],\n'
        )
    else:
        mode_rules = (
            "MODALIDAD: SHORTS CONCLUSIVOS (VÍDEO VERTICAL AUTOCONCLUSIVO CON GANCHO, DESARROLLO Y REMATE REAL).\n"
            "- Elige historias, descubrimientos o anécdotas COMPLETAS donde haya principio, contexto, nudo y CONCLUSIÓN/RESPUESTA REAL.\n"
            "- EDICIÓN PROFESIONAL: Prefiere 1 macro-tramo continuo sólido o 2-3 tramos bien hilados (`cold_open_hook` opcional + `context_intro` + `core` + `payoff`), "
            "usando `drop_sentence_ids` para podar digresiones o meta-referencias ('pero esto es otra historia', 'en esta santa casa') sin romper el relato.\n"
            "- Arquetipos:\n"
            "  * mini_historia (anécdota completa con principio, nudo y desenlace real)\n"
            "  * pregunta_respuesta (pregunta o enigma + respuesta explícita del podcast)\n"
            "  * dato_contraintuitivo (afirmación sorprendente + explicación completa)\n"
            "  * debate_contraste (contraste de ideas con cierre)"
        )
        span_example = (
            '  "spans": [\n'
            '    {"start_sentence_id": "S0066", "end_sentence_id": "S0068", "role": "context_intro"},\n'
            '    {"start_sentence_id": "S0148", "end_sentence_id": "S0175", "role": "core"},\n'
            '    {"start_sentence_id": "S0176", "end_sentence_id": "S0180", "role": "payoff"}\n'
            "  ],\n"
            '  "drop_sentence_ids": [],\n'
        )

    speaker_note = (
        "\n- Hay varios hablantes etiquetados como [SPEAKER_01], [SPEAKER_02], etc. Aprovecha intercambios Pregunta->Respuesta o debates."
        if show_speakers
        else ""
    )

    vb = visual_bible or {}
    ep_context_lines: list[str] = []
    if vb.get("episode_topic"):
        ep_context_lines.append(f"Tema del episodio: {vb['episode_topic']}")
    if vb.get("era_or_setting"):
        ep_context_lines.append(f"Época/Entorno: {vb['era_or_setting']}")
    if vb.get("narrative_spine"):
        ep_context_lines.append(f"Hilo narrativo: {vb['narrative_spine']}")
    if vb.get("proper_nouns"):
        ep_context_lines.append(f"Nombres propios reales: {', '.join(str(x) for x in vb['proper_nouns'][:12])}")
    if block_map:
        non_ad = [m for m in block_map if not m.get("is_ad_or_promo")]
        if non_ad:
            ep_context_lines.append(
                "Mapa resumido del episodio: "
                + " | ".join(f"B{m.get('block_index')}: {m.get('tema', '')}" for m in non_ad[:12])
            )
    ep_context_block = ("\n" + "\n".join(ep_context_lines) + "\n") if ep_context_lines else ""

    angles_block = ""
    if assigned_angles:
        lines_a = []
        for a in assigned_angles[:8]:
            blks = ", ".join(f"B{b}" for b in (a.get("target_blocks") or []))
            cv_ctx = f" | Contexto frío: {a['cold_viewer_context']}" if a.get("cold_viewer_context") else ""
            lines_a.append(
                f"  * [{a.get('angle_id', '')}] {a.get('angle_theme', '')} (Bloques: {blks or 'cualquiera'}) -> {a.get('central_question', '')}{cv_ctx}"
            )
        angles_block = (
            "\nSUB-ÁNGULOS / CAPÍTULOS COMPLETAMENTE DISTINTOS PLANIFICADOS (CADA CANDIDATO DEBE TRATAR UN ÁNGULO DISTINTO):\n"
            + "\n".join(lines_a)
            + "\n- Asigna a cada candidato su 'angle_id' y 'angle_theme' correspondiente. PROHIBIDO que dos candidatos repitan el mismo sub-ángulo.\n"
        )

    exclude_note = ""
    if exclude_intervals:
        excl_str = ", ".join(f"[{s:.0f}s..{e:.0f}s]" for s, e in exclude_intervals[:12])
        exclude_note += f"\n- PROHIBIDO repetir o solapar con estos tramos ya seleccionados: {excl_str}."
    if exclude_topics:
        excl_t = " | ".join(t[:90] for t in exclude_topics[:10] if t)
        if excl_t:
            exclude_note += f"\n- PROHIBIDO repetir ideas, preguntas o sub-temas ya cubiertos en clips previos: {excl_t}."

    sys_prompt = (
        "Eres Showrunner y Editor de Vídeo Vertical Profesional (YouTube Shorts, Reels, TikTok estilo Veritasium, Diary of a CEO, documentales virales). "
        "Editas tramos reales `[S#### | X.Xs]` del podcast como un montador experto: recortas inicios flojos, podas digresiones con `drop_sentence_ids`, "
        "empalmas `context_intro` + `core` + `payoff` (o un `cold_open_hook` opcional sin repetición) y presupuestas la duración exacta sumando los `[X.Xs]`.\n"
        "Cada vídeo DEBE entenderse de forma 100% independiente sin haber visto el podcast completo.\n"
        "PROHIBIDO repetir frases, clonar sub-temas entre clips o cortar oraciones a la mitad. Responde exclusivamente en JSON con clave 'clips'."
    )
    usr_prompt = (
        f"{_profile_editorial_context(profile)}\n"
        f"{ep_context_block}"
        f"{angles_block}\n"
        f"{mode_rules}\n\n"
        "CONTRATO DE MONTAJE NLE Y VÍDEO AUTOCONCLUSIVO (OBLIGATORIO):\n"
        "1. DIVERSIDAD TEMÁTICA ESTRICTA: Cada candidato en 'clips' DEBE tratar un capítulo/sub-tema/anécdota COMPLETAMENTE DISTINTO y no solaparse temporalmente con los demás.\n"
        "2. MONTAJE ORGÁNICO (RECORTAR, PODAR Y UNIR): Usa preferentemente 1 tramo continuo completo o máximo 2-3 tramos bien hilados en ORDEN CRONOLÓGICO. "
        "Poda cualquier inciso molesto con `drop_sentence_ids`. Respeta las marcas `--- [PAUSA MUSICAL / CAMBIO DE BLOQUE] ---` (no pegues dos frases desconectadas de bloques musicales distintos sin puente).\n"
        "3. ARRANQUE FUERTE Y CONTEXTO (PRIMEROS 4-6s): El primer tramo debe empezar con fuerza (PROHIBIDO abrir con muletillas como 'Como la cosa estaba...', 'Bueno, pues...', 'A ver...' ni con verbos en infinitivo dependientes como 'revelar...'). "
        "Si la historia viene de mitad del episodio y empieza con 'ella' o 'esta mujer', antepón `context_intro` de 1-2 frases limpias o asume que irá con cortinilla IA de contexto.\n"
        "4. MINI-ÍNDICE DEL SUBTEMA (`video_outline`): Antes de elegir los IDs, redacta un `video_outline` de 3–4 puntos concretos PARA ESTE VÍDEO (en contexto al subtema elegido, NO temas de otras partes del podcast). Cada tramo elegido debe cubrir ese mini-índice sin saltarse el hecho/nudo/solución central del subtema (usa `drop_sentence_ids` para podar relleno si hace falta espacio).\n"
        "5. PRESUPUESTO EXACTO DE TIEMPO: Cada línea tiene su duración `[S#### | X.Xs]`. Suma los segundos para que el total bruto quede entre "
        f"~{buf_min:.0f}s y {buf_max:.0f}s (y post-corte en {window.min_sec:.0f}s–{window.max_sec:.0f}s) SIN quedarte corto ni pasarte y cortar el final.\n"
        "6. CERO ANUNCIOS NI PROMOS: Nunca incluyas menciones a episodios de pago, Patreon, precios, suscripciones, capítulos anteriores ni despedidas.\n"
        "7. CIERRE LIMPIO (HARD STOP): Termina exactamente en la oración completa que remata la idea/respuesta (en conclusivo) o fija la pregunta abierta (en cliffhanger)."
        f"{speaker_note}{exclude_note}\n"
        f"- Selecciona {ask_n} candidatos TEMÁTICAMENTE DISTINTOS.\n\n"
        "Formato JSON de cada objeto en 'clips':\n"
        "{\n"
        '  "angle_id": "A1",\n'
        '  "angle_theme": "sub-tema único distinto al resto de clips",\n'
        '  "video_outline": ["1) Contexto o detonante del subtema", "2) Hecho/problema central del subtema", "3) Resolución o clímax exacto"],\n'
        '  "narrative_archetype": "mini_historia | pregunta_respuesta | dato_contraintuitivo | debate_contraste | cliffhanger_70_30",\n'
        '  "narrative_contract": {\n'
        '    "who": "Protagonista/entidad nombrada literalmente en el audio o en contexto",\n'
        '    "what_and_when": "Problema/contexto + época o lugar",\n'
        '    "central_question": "Pregunta o tensión central del vídeo",\n'
        '    "payoff_or_cliffhanger": "Respuesta real del podcast o incógnita abierta exacta"\n'
        "  },\n"
        f"{span_example}"
        '  "score": 92,\n'
        '  "summary": "resumen fiel explicando Gancho -> Contexto -> Cierre autónomo en audio",\n'
        '  "hook_reason": "por qué los primeros 4-6 segundos enganchan y se entienden sin contexto previo",\n'
        '  "hook_text": "titular corto máx 7 palabras",\n'
        '  "cliffhanger_question": "pregunta final si aplica sino vacío",\n'
        '  "titles": ["título 1", "título 2", "título 3"],\n'
        '  "description": "descripción",\n'
        '  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4"],\n'
        '  "thumbnail_text": "2-4 palabras"\n'
        "}\n\n"
        f"TRANSCRIPCIÓN:\n{transcript_excerpt}"
    )

    resp = llm.chat_json(model_role, sys_prompt, usr_prompt, temperature=0.22, max_tokens=5500, stage=f"select_{modality}")
    raw_clips = resp.get("clips") or resp.get("items") or []
    return _parse_candidates_from_llm(
        raw_clips,
        modality,
        profile.visual.shorts_aspect,
        sentences,
        id_to_idx,
        window,
        silence_intervals,
        profile.audio,
        energy_blocks,
        blacklisted_indices=blacklisted_indices,
    )


def _select_longform_arcs(
    llm: OpenRouterClient,
    profile: PodcasterProfile,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    blocks: list[dict[str, Any]],
    block_map_summaries: list[dict[str, Any]],
    clip_type: Literal["medium", "long"],
    count: int,
    window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    energy_blocks: list[dict],
    show_speakers: bool = False,
    blacklisted_indices: Optional[set[int]] = None,
    visual_bible: Optional[dict[str, Any]] = None,
) -> list[ClipCandidate]:
    if count <= 0 or not blocks:
        return []

    blocked = blacklisted_indices or set()
    aspect = profile.visual.medium_aspect if clip_type == "medium" else profile.visual.long_aspect
    label_dur = "5-6 minutos" if clip_type == "medium" else "10-14 minutos"
    buf_min = round(window.min_sec * 1.05, 1)
    buf_max = round(window.max_sec * 1.12, 1)

    if len(blocks) > 3 and block_map_summaries:
        compact_lines = [
            f"Bloque {m.get('block_index')}: tema='{m.get('tema','')}' | "
            f"ad_promo={bool(m.get('is_ad_or_promo', False))} | "
            f"continua={m.get('continua_tema_anterior', False)} | "
            f"interes={m.get('interes_general', 5)}/10 | {m.get('resumen_breve','')}"
            for m in block_map_summaries
        ]
        arc_sys = (
            "Eres director editorial de YouTube. A partir del mapa de bloques del podcast, elige los mejores "
            "arcos temáticos continuos (EXCLUYENDO cualquier bloque de anuncios o promos). Responde en JSON con clave 'arcs' (lista)."
        )
        arc_usr = (
            f"{_profile_editorial_context(profile)}\n\n"
            f"Necesitamos {count + 1} arcos temáticos continuos y conclusivos para vídeos tipo '{clip_type}' ({label_dur}, "
            f"objetivo bruto {buf_min:.0f}s–{buf_max:.0f}s). Prioriza bloques que formen una historia o capítulo completo (inicio -> desarrollo -> desenlace).\n"
            "Cada objeto en 'arcs' DEBE incluir exactamente estos campos enteros: "
            '{"start_block": 1, "end_block": 2, "theme": "Título del arco"}.\n\n'
            + "\n".join(compact_lines)
        )
        arc_resp = llm.chat_json("editorial", arc_sys, arc_usr, temperature=0.2, max_tokens=1500, stage=f"select_arcs_{clip_type}")
        arcs = arc_resp.get("arcs") or []
    else:
        arcs = [{"start_block": 0, "end_block": len(blocks) - 1, "theme": "Arco principal"}]

    if not arcs or not any(isinstance(a, dict) and "start_block" in a for a in arcs):
        if block_map_summaries:
            non_ad = [m for m in block_map_summaries if not m.get("is_ad_or_promo")] or block_map_summaries
            ranked_m = sorted(non_ad, key=lambda m: float(m.get("interes_general", 5)), reverse=True)
            best_b = max(0, min(len(blocks) - 1, int(ranked_m[0].get("block_index", 0))))
            span = 1 if clip_type == "medium" else 2
            sb_def = max(0, best_b)
            eb_def = min(len(blocks) - 1, sb_def + span)
            arcs = [{"start_block": sb_def, "end_block": eb_def, "theme": str(ranked_m[0].get("tema", "Arco destacado"))}]
        else:
            arcs = [{"start_block": 0, "end_block": min(len(blocks) - 1, 2), "theme": "Arco principal"}]

    candidates: list[ClipCandidate] = []
    for arc in arcs[: count + 1]:
        sb = max(0, min(len(blocks) - 1, int(arc.get("start_block", 0))))
        eb = min(len(blocks) - 1, max(sb, int(arc.get("end_block", sb))))
        if eb == sb and len(blocks) > sb + 1:
            eb = sb + 1
        first_sid = blocks[sb]["first_id"]
        last_sid = blocks[eb]["last_id"]
        ctx_start = max(0, id_to_idx[first_sid] - 24)
        ctx_end = min(len(sentences) - 1, id_to_idx[last_sid] + 30)
        arc_sents = [sentences[k] for k in range(ctx_start, ctx_end + 1) if k not in blocked]
        if not arc_sents:
            arc_sents = sentences[ctx_start : ctx_end + 1]
        arc_text = _format_sentences_block(arc_sents, show_speakers=show_speakers, include_durations=True)

        anchor_block = ""
        early_clean = [sentences[k] for k in range(0, ctx_start) if k not in blocked][:24]
        if ctx_start > 15 and early_clean:
            anchor_block = (
                "--- PRESENTACIÓN INICIAL DEL TEMA EN EL EPISODIO (solo si el tramo necesita un context_intro cronológico previo) ---\n"
                + _format_sentences_block(early_clean, show_speakers=show_speakers, include_durations=True)
                + "\n\n"
            )

        vb_arc = visual_bible or {}
        vb_context = ""
        if vb_arc.get("episode_topic") or vb_arc.get("narrative_spine"):
            vb_context = (
                f"Contexto del episodio: {vb_arc.get('episode_topic', '')}\n"
                f"Hilo global del episodio (útil solo si encaja con este arco): {vb_arc.get('narrative_spine', '')}\n"
            )

        sys_prompt = (
            "Eres Showrunner y Montador Jefe de YouTube (documentales y vídeo-podcasts de alta retención). "
            "Para este arco concreto defines primero su MINI-ÍNDICE (`video_outline`) con los 3–5 hitos esenciales "
            "de ESE subtema específico y seleccionas tramos `[S#### | X.Xs]` que los cubran TODOS sin saltarse lo más importante "
            "(NUNCA saltes del pasado/infancia directo al epílogo olvidando el logro, conflicto o solución principal). Responde exclusivamente en JSON con clave 'clips'."
        )
        usr_prompt = (
            f"{_profile_editorial_context(profile)}\n\n"
            f"{vb_context}"
            f"Tema del arco: {arc.get('theme', 'Tema completo')}\n"
            f"Selecciona 1 pieza magistral ({label_dur}, bruto aprox. {buf_min:.0f}s–{buf_max:.0f}s, post-corte {window.min_sec:.0f}s–{window.max_sec:.0f}s).\n\n"
            "CONTRATO DE VÍDEO AUTOCONCLUSIVO Y MONTAJE NLE:\n"
            "1. MINI-ÍNDICE DEL VÍDEO (`video_outline`, 3–5 puntos): Define exactamente qué 3–5 hitos debe cubrir ESTE vídeo según su tema "
            "(p. ej. si trata de un personaje/subtema, incluye origen -> hecho o conflicto central de ese subtema -> cómo lo resolvió -> desenlace/legado). "
            "Selecciona los tramos para cumplir el 100% de ese mini-índice dentro del presupuesto de tiempo.\n"
            "2. ESTRUCTURA CLARA DESDE EL INICIO: Usa hasta 4 tramos cronológicos (`context_intro`, `core`, `payoff`) + 1 `cold_open_hook` opcional y disjunto al principio (sin repetirlo después). "
            "Los primeros 4-6 segundos deben situar sujeto, escenario y conflicto con oraciones completas (PROHIBIDO empezar con 'Mientras tanto...', 'Y bueno...', 'Como la cosa estaba...').\n"
            "3. PODA INTELIGENTE CON `drop_sentence_ids`: Si incluir el hito central del mini-índice exige espacio, poda anécdotas secundarias o digresiones con `drop_sentence_ids` para que quepa todo sin cortar la parte más importante.\n"
            "4. CIERRE CONCLUSIVO REAL DEL PODCAST: Suma las duraciones `[X.Xs]` para llegar al clímax y abarca/empalma `payoff` con el remate completo.\n"
            "5. Usa score entero entre 0 y 100. El hook_text debe tener máximo 7 palabras y terminar en una frase completa.\n\n"
            "Devuelve JSON {'clips': [{'narrative_archetype': 'arco_documental', "
            "'video_outline': ['1) ...', '2) ...', '3) ...', '4) ...'], 'drop_sentence_ids': [], 'spans': ["
            "{'start_sentence_id': 'S...', 'end_sentence_id': 'S...', 'role': 'cold_open_hook|context_intro|core|payoff'}], "
            "score, summary, hook_reason, hook_text, titles (3), description, hashtags (5), thumbnail_text, "
            "chapters: [{title, sentence_id}]}]}.\n\n"
            f"{anchor_block}"
            f"TRANSCRIPCIÓN DEL TRAMO:\n{arc_text}"
        )
        resp = llm.chat_json("editorial", sys_prompt, usr_prompt, temperature=0.2, max_tokens=3500, stage=f"select_longform_{clip_type}")
        raw_clips = (
            resp.get("clips")
            or resp.get("items")
            or ([resp["clip"]] if isinstance(resp.get("clip"), dict) else [])
            or ([resp] if "start_sentence_id" in resp or "spans" in resp else [])
        )
        parsed = _parse_candidates_from_llm(
            raw_clips,
            clip_type,
            aspect,
            sentences,
            id_to_idx,
            window,
            silence_intervals,
            profile.audio,
            energy_blocks,
            blacklisted_indices=blocked,
        )
        candidates.extend(parsed)

    return candidates


def _deduplicate_and_audit(
    candidates: list[ClipCandidate],
    requested: int,
    existing_short_spans: list[tuple[float, float]],
    longform_spans: list[tuple[float, float]],
    allow_short_as_teaser: bool,
    clip_type: ClipType,
    sentences: list[SentenceSegment],
    id_to_idx: dict[str, int],
    window: DurationWindow,
    silence_intervals: list[tuple[float, float]],
    energy_blocks: list[dict],
    profile: PodcasterProfile,
    llm: OpenRouterClient,
    show_speakers: bool = False,
    blacklisted_indices: Optional[set[int]] = None,
    blocks: Optional[list[dict[str, Any]]] = None,
    block_map: Optional[list[dict[str, Any]]] = None,
    visual_bible: Optional[dict[str, Any]] = None,
    allow_fallback: bool = True,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    progress_range: Optional[tuple[float, float]] = None,
    stage_label: str = "",
    max_strong_repairs: Optional[int] = None,
    existing_clips: Optional[list[ClipCandidate]] = None,
    already_used_intros: Optional[list[str]] = None,
    global_topic_stopwords: Optional[set[str]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    telemetry_cb: Optional[Callable[[dict[str, Any]], None]] = None,
    audited_signatures: Optional[set[str]] = None,
    rejected_records: Optional[list[dict[str, Any]]] = None,
) -> list[ClipCandidate]:
    """Select Top N non-overlapping, thematically distinct clips with Blind Director audit before rendering."""
    if requested <= 0 or not sentences:
        return []

    blocked = blacklisted_indices or set()
    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    chosen: list[ClipCandidate] = []
    chosen_topic_clips: list[ClipCandidate] = list(existing_clips or [])
    seen_sigs = audited_signatures if audited_signatures is not None else set()

    def _cand_sig(c: ClipCandidate) -> str:
        if c.narrative_spans:
            sp_key = ",".join(f"{sp.start_sentence_id}-{sp.end_sentence_id}" for sp in c.narrative_spans)
        else:
            sp_key = f"{c.start_sentence_id}-{c.end_sentence_id}"
        return f"{c.clip_type}:{sp_key}"

    def _emit(msg: str) -> None:
        if log_cb is not None:
            try:
                log_cb(msg)
            except Exception:
                pass

    def _cand_intervals(c: ClipCandidate) -> list[tuple[float, float]]:
        if c.narrative_spans:
            return [(float(sp.orig_start), float(sp.orig_end)) for sp in c.narrative_spans]
        return [(float(c.orig_start), float(c.orig_end))]

    def _span_list_overlaps(cand_ivs: list[tuple[float, float]], ref_ivs: list[tuple[float, float]], thresh: float) -> bool:
        for s, e in cand_ivs:
            for rs, re_t in ref_ivs:
                if _compute_overlap_ratio(s, e, rs, re_t) > thresh:
                    return True
        return False

    chosen_intervals: list[tuple[float, float]] = list(existing_short_spans)
    short_overlap_thresh = 0.12 if clip_type.startswith("short") else 0.15

    def conflicts(cand_ivs: list[tuple[float, float]], cand_obj: Optional[ClipCandidate] = None) -> bool:
        if _span_list_overlaps(cand_ivs, chosen_intervals, short_overlap_thresh):
            return True
        if not allow_short_as_teaser and clip_type.startswith("short"):
            if _span_list_overlaps(cand_ivs, longform_spans, 0.20):
                return True
        if cand_obj is not None and clip_type.startswith("short"):
            for prev in chosen_topic_clips:
                sim = _compute_topic_similarity(cand_obj, prev, sentences, id_to_idx, global_topic_stopwords)
                if sim >= 0.55:
                    _emit(
                        f"Filtro temático descarta '{cand_obj.angle_theme or cand_obj.slug}' "
                        f"(similitud={sim:.2f} con '{prev.angle_theme or prev.slug}')."
                    )
                    if rejected_records is not None:
                        rejected_records.append(
                            {
                                "candidate_id": cand_obj.candidate_id or cand_obj.id,
                                "title": (cand_obj.metadata.titles[0] if cand_obj.metadata.titles else cand_obj.slug),
                                "clip_type": cand_obj.clip_type,
                                "score": cand_obj.score,
                                "orig_start": cand_obj.orig_start,
                                "orig_end": cand_obj.orig_end,
                                "reason": f"Filtro temático (similitud={sim:.2f} con '{prev.angle_theme or prev.slug}')",
                                "candidate": cand_obj.model_dump(mode="json"),
                            }
                        )
                    return True
        return False

    non_conflicting: list[ClipCandidate] = []
    for c in ordered:
        sig = _cand_sig(c)
        if sig in seen_sigs:
            continue
        if not conflicts(_cand_intervals(c), c):
            non_conflicting.append(c)

    max_candidates = min(len(non_conflicting), max(requested + 4, 6))
    to_audit = non_conflicting[:max_candidates]
    strong_budget = max_strong_repairs if max_strong_repairs is not None else max(2, requested + 1)
    strong_used = [0]
    total_to_audit = max(1, len(to_audit))

    for idx_c, cand in enumerate(to_audit):
        if len(chosen) >= requested:
            break
        sig = _cand_sig(cand)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        if conflicts(_cand_intervals(cand), cand):
            continue
        if progress_cb is not None and progress_range is not None:
            p_lo, p_hi = progress_range
            pct = p_lo + (p_hi - p_lo) * (idx_c / total_to_audit)
            lbl = stage_label or f"Auditando candidato {clip_type}"
            progress_cb(pct, f"{lbl} ({idx_c + 1}/{total_to_audit})...")

        audited = _audit_and_refine_candidate(
            cand=cand,
            sentences=sentences,
            id_to_idx=id_to_idx,
            target_window=window,
            silence_intervals=silence_intervals,
            audio_config=profile.audio,
            energy_blocks=energy_blocks,
            llm=llm,
            show_speakers=show_speakers,
            blacklisted_indices=blocked,
            blocks=blocks,
            block_map=block_map,
            visual_bible=visual_bible,
            allow_strong_escalation=(strong_used[0] < strong_budget),
            used_strong_ref=strong_used,
            profile=profile,
            already_used_intros=already_used_intros,
            log_cb=log_cb,
            telemetry_cb=telemetry_cb,
            rejected_records=rejected_records,
        )
        if audited is not None:
            aud_ivs = _cand_intervals(audited)
            if not conflicts(aud_ivs, audited):
                chosen.append(audited)
                chosen_intervals.extend(aud_ivs)
                chosen_topic_clips.append(audited)

    # Prefer highest-scoring LLM-curated candidates with AI intro framing ONLY when fallback is explicitly allowed
    if allow_fallback and len(chosen) < requested:
        for cand in ordered:
            if len(chosen) >= requested:
                break
            cand_ivs = _cand_intervals(cand)
            if conflicts(cand_ivs, cand):
                continue
            if cand.narrative_spans and cand.estimated_post_cut_sec >= window.min_sec * 0.70:
                if (
                    profile is not None
                    and profile.intro_curtain.enabled
                    and not (cand.intro_curtain_text or "").strip()
                ):
                    try:
                        ic_fb = generate_intro_curtain_script(
                            cand,
                            profile,
                            visual_bible or {},
                            llm,
                            sentences=sentences,
                            id_to_idx=id_to_idx,
                            already_used_intros=already_used_intros,
                        )
                        cand.intro_curtain_enabled = True
                        cand.intro_curtain_text = ic_fb["spoken_hook"]
                        cand.intro_curtain_phrases = ic_fb["phrases"]
                        cand.intro_curtain_badge = ic_fb["badge"]
                        if already_used_intros is not None and cand.intro_curtain_text:
                            already_used_intros.append(cand.intro_curtain_text)
                    except Exception:
                        pass
                cand.standalone_packaging = {
                    **(cand.standalone_packaging or {}),
                    "needs_ai_intro_curtain": True,
                    "audit_action": "best_effort_needs_intro",
                }
                chosen.append(cand)
                chosen_intervals.extend(cand_ivs)
                chosen_topic_clips.append(cand)

    # Guarantee quota on clean non-blacklisted boundaries ONLY if allow_fallback is True
    if allow_fallback and len(chosen) < requested and sentences[-1].end >= window.min_sec:
        step_size = max(1, len(sentences) // (requested + 3))
        for start_idx in range(0, len(sentences), step_size):
            if len(chosen) >= requested:
                break
            cur_s = start_idx
            while cur_s < len(sentences) and cur_s in blocked:
                cur_s += 1
            if cur_s >= len(sentences):
                continue
            cur_s, _ = _snap_span_boundaries(cur_s, min(len(sentences) - 1, cur_s + 12), sentences, blocked, min_bound=cur_s)
            s_idx, e_idx, est = adjust_bounds_to_window(
                cur_s,
                min(len(sentences) - 1, cur_s + 12),
                sentences,
                window,
                silence_intervals,
                profile.audio,
                min_bound_idx=cur_s,
                blacklisted_indices=blocked,
            )
            if s_idx in blocked or e_idx in blocked:
                continue
            s_t = sentences[s_idx].start
            e_t = sentences[e_idx].end
            if est >= window.min_sec * 0.75 and not conflicts([(s_t, e_t)]):
                title = f"Momento destacado {len(chosen) + 1}"
                chosen.append(
                    ClipCandidate(
                        id=f"{clip_type}_{len(chosen) + 1:02d}_respaldo",
                        clip_type=clip_type,
                        aspect_ratio=(
                            profile.visual.shorts_aspect
                            if clip_type.startswith("short")
                            else (profile.visual.medium_aspect if clip_type == "medium" else profile.visual.long_aspect)
                        ),
                        score=65,
                        slug=make_slug(title),
                        summary=sentences[s_idx].text,
                        start_sentence_id=sentences[s_idx].id,
                        end_sentence_id=sentences[e_idx].id,
                        orig_start=s_t,
                        orig_end=e_t,
                        estimated_post_cut_sec=est,
                        standalone_packaging={"needs_ai_intro_curtain": True, "audit_action": "fallback"},
                        narrative_spans=[
                            NarrativeSpan(
                                start_sentence_id=sentences[s_idx].id,
                                end_sentence_id=sentences[e_idx].id,
                                orig_start=s_t,
                                orig_end=e_t,
                                role="core",
                            )
                        ],
                        metadata=ClipMetadata(
                            titles=[title],
                            description=sentences[s_idx].text,
                            hashtags=["#podcast"],
                            thumbnail_text=title,
                            hook_text=" ".join(sentences[s_idx].text.split()[:6]),
                            cliffhanger_question="¿Quieres saber cómo termina esta historia?" if clip_type == "short_cliffhanger" else "",
                        ),
                    )
                )
                chosen_intervals.append((s_t, e_t))

    for idx, c in enumerate(chosen, start=1):
        c.id = f"{clip_type}_{idx:02d}_{c.slug[:16]}"
        c.output_filename = format_output_filename(c.score, c.clip_type, c.metadata.titles[0] if c.metadata.titles else c.slug)

    return chosen


def generate_final_clip_metadata(
    clip: ClipCandidate,
    profile: PodcasterProfile,
    glossary: dict[str, str],
    llm: OpenRouterClient,
    intro_curtain_text: str = "",
) -> ClipMetadata:
    """Generate/re-sync titles, summary, concise hook, chapters, and description AFTER final Pass 2 + EDL cuts."""
    if not clip.mapped_words:
        return clip.metadata

    edited_dur = clip.edl.edited_duration if clip.edl else max(1.0, clip.mapped_words[-1].end)
    is_longform = clip.clip_type in ("medium", "long")

    # Build timestamped chunks from the REAL edited timeline
    lines: list[str] = []
    cur: list[WordTimestamp] = []
    cur_t = clip.mapped_words[0].start
    for w in clip.mapped_words:
        if not cur:
            cur_t = w.start
        cur.append(w)
        if w.word.strip().endswith((".", "?", "!")) or (w.end - cur_t) >= 12.0:
            lines.append(f"[{cur_t:.1f}s] {' '.join(x.word for x in cur)}")
            cur = []
    if cur:
        lines.append(f"[{cur_t:.1f}s] {' '.join(x.word for x in cur)}")

    chapter_rule = (
        f"- Incluye de 3 a 6 'chapters' cubriendo exclusivamente el rango real [0.0s .. {max(1.0, edited_dur - 12.0):.1f}s], ordenados de menor a mayor."
        if is_longform
        else "- 'chapters': lista vacía []."
    )
    intro_section = (
        f"CORTINILLA DE CONTEXTO AL INICIO (voz IA antes del audio):\n\"{intro_curtain_text.strip()}\"\n\n"
        if intro_curtain_text.strip()
        else ""
    )
    spoiler_meta_rule = (
        "- REGLA ANTI-SPOILER (short_cliffhanger): Los títulos, descripción, thumbnail_text y cliffhanger_question "
        "DEBEN generar intriga SIN revelar ni destripar el desenlace/solución."
        if clip.clip_type == "short_cliffhanger"
        else "- Refleja fielmente el clímax y la conclusión real que se oye en el recorte."
    )

    sys_prompt = (
        "Eres Copywriter Senior de YouTube, Reels y TikTok especializado en podcasts narrativos y documentales. "
        "Generas metadatos 100% fieles EXCLUSIVAMENTE al texto final editado que recibirás "
        "(PROHIBIDO mencionar anécdotas, nombres o datos que quedaron fuera del recorte final). Responde en JSON estricto."
    )
    usr_prompt = (
        f"{_profile_editorial_context(profile)}\n"
        f"Tipo: {clip.clip_type} | Duración final exacta del audio del podcast: {edited_dur:.1f} segundos.\n"
        f"Glosario de nombres propios: {json.dumps(glossary, ensure_ascii=False)}\n\n"
        "REGLAS ESTRICTAS (PROHIBIDO INVENTAR O PROMETER ALGO QUE NO SALE EN ESTE GUION):\n"
        "- titles: 3 títulos virales con ángulos distintos (1 curiosidad/misterio, 1 dato/impacto, 1 historia humana) fieles SOLO a este guion final.\n"
        "- summary: 1-2 frases resumiendo con precisión el principio, nudo y cierre/incógnita que SÍ aparecen en este corte.\n"
        "- hook_text: frase gancho ultracorta (MÁXIMO 7 palabras) coherente con la apertura real.\n"
        "- description: resumen atractivo listo para publicar basado solo en este guion, con llamada a comentar/escuchar.\n"
        "- hashtags: 4-6 hashtags relevantes en español sin espacios.\n"
        "- thumbnail_text: 2-4 palabras con impacto visual en mayúsculas.\n"
        "- cliffhanger_question: pregunta abierta máx. 9 palabras si es short_cliffhanger, si no ''.\n"
        f"{spoiler_meta_rule}\n"
        f"{chapter_rule}\n\n"
        "Devuelve JSON exacto con claves:\n"
        '{"titles": ["..."], "summary": "...", "hook_text": "...", "description": "...", '
        '"hashtags": ["#..."], "thumbnail_text": "...", "cliffhanger_question": "...", "chapters": [{"title": "...", "time_sec": 0.0}]}\n\n'
        f"{intro_section}"
        "GUION FINAL EDITADO CON TIEMPOS REALES:\n"
        + "\n".join(lines[:220])
    )

    try:
        resp = llm.chat_json("fast", sys_prompt, usr_prompt, temperature=0.2, max_tokens=2048, stage="final_metadata")
    except Exception:
        return clip.metadata

    if resp.get("summary"):
        clip.summary = str(resp["summary"]).strip()

    titles = [str(t).strip() for t in (resp.get("titles") or []) if str(t).strip()][:3] or clip.metadata.titles
    hook_words = str(resp.get("hook_text") or clip.metadata.hook_text or titles[0]).strip().split()
    hook_text = _clean_trailing_hook_words(" ".join(hook_words[:7]))

    chapters: list[ChapterMark] = []
    if is_longform:
        raw_chaps = resp.get("chapters") or []
        prev_t = -1.0
        max_chap_t = max(0.0, edited_dur - 8.0)
        for ch in raw_chaps:
            if not isinstance(ch, dict):
                continue
            try:
                t_sec = float(ch.get("time_sec", ch.get("edited_time_sec", 0.0)))
            except (TypeError, ValueError):
                continue
            if t_sec > max_chap_t and chapters:
                continue
            t_sec = round(max(0.0, min(max_chap_t, t_sec)), 2)
            if t_sec <= prev_t + 5.0 and chapters:
                continue
            if not chapters:
                t_sec = 0.0
            prev_t = t_sec
            chapters.append(
                ChapterMark(
                    title=str(ch.get("title") or "Sección").strip(),
                    orig_time_sec=t_sec,
                    edited_time_sec=t_sec,
                )
            )

    return ClipMetadata(
        titles=titles,
        description=str(resp.get("description") or clip.metadata.description).strip(),
        hashtags=[str(h).strip() for h in (resp.get("hashtags") or clip.metadata.hashtags) if str(h).strip()],
        thumbnail_text=str(resp.get("thumbnail_text") or clip.metadata.thumbnail_text).strip(),
        cliffhanger_question=(
            str(resp.get("cliffhanger_question") or clip.metadata.cliffhanger_question).strip()
            if clip.clip_type == "short_cliffhanger"
            else ""
        ),
        hook_text=hook_text,
        chapters=chapters,
    )


def _split_hook_into_phrases(text: str, max_phrases: int = 4) -> list[str]:
    """Split a hook sentence into 1-4 readable visual cards without orphan 1-word lines."""
    words = [w for w in str(text or "").split() if w]
    if not words:
        return []
    if len(words) <= 5 or max_phrases <= 1:
        return [" ".join(words)]
    if len(words) <= 10:
        target_n = 2
    elif len(words) <= 15:
        target_n = min(max_phrases, 3)
    else:
        target_n = min(max_phrases, 4)
    per = max(2, len(words) // target_n)
    phrases: list[str] = []
    idx = 0
    for i in range(target_n):
        if i == target_n - 1:
            part = words[idx:]
        else:
            part = words[idx : idx + per]
        if part:
            phrases.append(" ".join(part))
        idx += len(part)
    if len(phrases) >= 2 and len(phrases[-1].split()) == 1:
        phrases[-2] = f"{phrases[-2]} {phrases[-1]}"
        phrases.pop()
    return phrases or [" ".join(words)]


def generate_intro_curtain_script(
    clip: ClipCandidate,
    profile: PodcasterProfile,
    visual_bible: dict[str, Any],
    llm: OpenRouterClient,
    sentences: Optional[list[SentenceSegment]] = None,
    id_to_idx: Optional[dict[str, int]] = None,
    already_used_intros: Optional[list[str]] = None,
    anti_spoiler_note: str = "",
    suggested_intro_hint: str = "",
) -> dict[str, Any]:
    """Generate contextual AI-voiced opening curtain script naming WHO, WHEN/WHERE, and WHAT before podcast audio."""
    ic = profile.intro_curtain
    if clip.mapped_words:
        body_words = [w.word for w in clip.mapped_words if not w.censored]
        opening_quote = " ".join(body_words[:28]).strip()
        body_excerpt = " ".join(body_words[:180]).strip() or clip.summary or (clip.metadata.titles[0] if clip.metadata.titles else "")
    elif sentences and id_to_idx is not None:
        raw_script, _ = _format_candidate_script(clip, sentences, id_to_idx, for_blind_audit=True)
        clean_words = raw_script.replace("\n", " ").split()
        opening_quote = " ".join(clean_words[:28]).strip()
        body_excerpt = " ".join(clean_words[:180]).strip() or clip.summary or (clip.metadata.titles[0] if clip.metadata.titles else "")
    else:
        opening_quote = ""
        body_excerpt = clip.summary or (clip.metadata.titles[0] if clip.metadata.titles else "")

    vb = visual_bible or {}
    pkg = clip.standalone_packaging or {}
    template = (ic.hook_template or "auto").strip()
    custom_note = (ic.custom_hook_prompt or "").strip()
    raw_nouns = vb.get("proper_nouns") or vb.get("key_entities") or []
    entities = ", ".join(str(x) for x in raw_nouns[:10])
    topic = str(vb.get("episode_topic") or vb.get("topic") or clip.summary or "").strip()
    era = str(vb.get("era_or_setting") or "").strip()
    narrative_spine = str(vb.get("narrative_spine") or "").strip()
    missing_items = ", ".join(str(x) for x in (pkg.get("missing_context_items") or []) if str(x).strip())
    opening_issue = str(pkg.get("opening_issue") or "").strip()
    clip_angle = str(clip.angle_theme or clip.summary or "").strip()

    def _fallback_script() -> dict[str, Any]:
        ctx_core = str(
            suggested_intro_hint
            or pkg.get("standalone_summary")
            or clip_angle
            or topic
            or (clip.metadata.titles[0] if clip.metadata.titles else "")
        ).strip()
        ctx_core = re.sub(r"^[¡¿\"']+", "", ctx_core).strip().rstrip(".")
        if era and _strip_accents_lower(era) not in _strip_accents_lower(ctx_core):
            spoken = f"{era}: {ctx_core[0].lower() + ctx_core[1:] if len(ctx_core) > 1 else ctx_core}."
        else:
            spoken = f"{ctx_core}." if ctx_core else "Esta historia oculta cambió el rumbo del episodio."
        spoken = _trim_intro_cleanly(spoken, max_words=22)
        badge = (ic.badge_text or "").strip() or ("CONTEXTO CLAVE" if clip.clip_type.startswith("short") else "CLAVE DEL RELATO")
        return {
            "spoken_hook": spoken,
            "phrases": _split_hook_into_phrases(spoken, max_phrases=4),
            "badge": " ".join(badge.split()[:3]).upper(),
        }

    used_intros_block = ""
    if already_used_intros:
        used_list = "\n".join(f'  - "{u}"' for u in already_used_intros[:8])
        used_intros_block = (
            "\nCORTINILLAS YA USADAS EN OTROS SHORTS (PROHIBIDO repetir estructura, frases o enfoque similar):\n"
            + used_list
            + "\n"
        )
    spoiler_block = f"\n{anti_spoiler_note.strip()}\n" if anti_spoiler_note.strip() else ""
    hint_block = (
        f"\nBORRADOR SUGERIDO POR EL MONTADOR (úsalos como base si encaja y mejora la fluidez): \"{suggested_intro_hint.strip()}\"\n"
        if suggested_intro_hint.strip()
        else ""
    )
    cliff_rule = (
        "\n6. ANTI-SPOILER CLIFFHANGER: Como es un 'short_cliffhanger', presenta al protagonista, época y problema con gancho máximo, pero NUNCA reveles la solución o desenlace.\n"
        if clip.clip_type == "short_cliffhanger"
        else ""
    )

    system = (
        "Eres Guionista Experto en Retención Viral para YouTube Shorts, Reels y Podcasts.\n"
        "Debes escribir la CORTINILLA DE APERTURA (locución IA breve previa al audio del podcast) para que cualquier espectador que NO ha visto el podcast entienda el vídeo al 100% desde el segundo 0.\n"
        "Tu objetivo es situar en una sola frase directa QUIÉN es el protagonista/sujeto exacto, CUÁNDO/DÓNDE ocurre y CUÁL es el problema o enigma de ESTE fragmento.\n"
        "REGLAS ESTRICTAS:\n"
        "1. LONGITUD Y LOCUCIÓN NATURAL: 1 sola frase incisiva de 10 a 15 palabras (~3.5s a 5.2s), en español fluido para voz en off (NUNCA más de 16 palabras ni paréntesis ni abreviaturas).\n"
        "2. FIDELIDAD ABSOLUTA AL TRAMO REAL: Describe SOLO hechos que realmente ocurran o se escuchen en la TRANSCRIPCIÓN DEL TRAMO de abajo. PROHIBIDO inventar conclusiones o dramas que no pasan en el audio.\n"
        "3. ANTI-REDUNDANCIA: PROHIBIDO usar fórmulas vacías ('¿Sabías este dato sobre...?') y PROHIBIDO repetir o parafrasear la primera frase literal del podcast.\n"
        "4. PROHIBIDO dejar pronombres sueltos ('ella', 'esta mujer', 'esto') sin nombrar antes al sujeto real y su contexto.\n"
        "5. PUENTE NATURAL: La última palabra de la cortinilla debe dar paso fluido a la primera frase del podcast.\n"
        "6. `phrases`: divide EXACTAMENTE todas las palabras de `spoken_hook` (sin omitir ni resumir ninguna palabra) en 2 a 4 pantallas cortas de 3–5 palabras."
        f"{cliff_rule}\n"
        'Responde SOLO JSON válido: {"spoken_hook": "...", "phrases": ["...", "..."], "badge": "ETIQUETA 1-3 PALABRAS"}'
    )

    user = (
        f"Podcast: {profile.podcast_name} | Nicho: {profile.niche_description}\n"
        f"Tema global del episodio: {topic}\n"
        f"Hilo narrativo del episodio: {narrative_spine or 'No especificado'}\n"
        f"SUB-ÁNGULO ESPECÍFICO DE ESTE CLIP: {clip_angle}\n"
        f"Época/Escenario: {era or 'No especificada'}\n"
        f"Entidades/Nombres propios reales: {entities}\n"
        f"Tipo de clip: {clip.clip_type} ({clip.aspect_ratio})\n"
        f"Diagnóstico de contexto faltante: {missing_items or opening_issue or 'Situar protagonista, época y problema en los primeros segundos'}\n"
        f"Resumen del tramo: {clip.summary}\n"
        f"Plantilla de gancho pedida: {template}\n"
        f"Instrucción extra del usuario: {custom_note or 'Ninguna'}\n"
        f"{hint_block}{used_intros_block}{spoiler_block}"
        f"EL PODCAST EMPIEZA TEXTUALMENTE CON ESTAS PALABRAS (CONECTA JUSTO ANTES DE ELLAS SIN REPETIRLAS):\n"
        f'"{opening_quote}"\n\n'
        f"TRANSCRIPCIÓN COMPLETA DEL TRAMO:\n{body_excerpt}"
    )

    def _opening_overlap_ratio(cand_text: str) -> float:
        if not opening_quote:
            return 0.0
        stop = {"el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "en", "a", "al", "y", "o", "que", "con", "por", "para", "se", "su", "sus", "es", "fue", "eran", "bueno", "pues"}
        a_toks = {t for t in _normalize_glossary_token(cand_text).split() if len(t) > 2 and t not in stop}
        b_toks = {t for t in _normalize_glossary_token(opening_quote).split() if len(t) > 2 and t not in stop}
        if not a_toks or not b_toks:
            return 0.0
        return len(a_toks & b_toks) / max(1, min(len(a_toks), len(b_toks)))

    resp: Any = None
    for role_tier in ("fast", "editorial"):
        try:
            cand_resp = llm.chat_json(role_tier, system, user, temperature=0.35, max_tokens=800, stage="intro_curtain")
            if isinstance(cand_resp, dict):
                cand_spoken = re.sub(r"\s+", " ", str(cand_resp.get("spoken_hook") or "").strip())
                if len(cand_spoken.split()) >= 6 and "sabías este dato sobre" not in cand_spoken.lower():
                    if _opening_overlap_ratio(cand_spoken) >= 0.55 and not anti_spoiler_note:
                        # Retry once asking explicitly not to paraphrase the opening sentence
                        retry_usr = (
                            user
                            + "\n\nATENCIÓN: Tu borrador anterior repetía casi las mismas palabras que el inicio del podcast. "
                            "Aporta SOLO el contexto previo (año, escenario, quién es el protagonista y qué estaba en juego) SIN parafrasear la apertura."
                        )
                        try:
                            cand_retry = llm.chat_json(role_tier, system, retry_usr, temperature=0.35, max_tokens=800, stage="intro_curtain_dedup")
                            if isinstance(cand_retry, dict) and len(str(cand_retry.get("spoken_hook") or "").split()) >= 6:
                                cand_resp = cand_retry
                        except Exception:
                            pass
                    resp = cand_resp
                    break
        except Exception:
            continue

    if not isinstance(resp, dict):
        return _fallback_script()

    spoken = re.sub(r"\s+", " ", str(resp.get("spoken_hook") or "").strip())
    if not spoken or len(spoken.split()) < 6:
        return _fallback_script()
    spoken = _trim_intro_cleanly(spoken, max_words=17)

    raw_phrases = [str(p).strip() for p in (resp.get("phrases") or []) if str(p).strip()]
    spoken_norm_toks = _normalize_glossary_token(spoken).split()
    phrases_norm_toks = _normalize_glossary_token(" ".join(raw_phrases)).split()
    phrases_match = (
        bool(raw_phrases)
        and 1 <= len(raw_phrases) <= 4
        and spoken_norm_toks == phrases_norm_toks
    )
    if not phrases_match:
        raw_phrases = _split_hook_into_phrases(spoken, max_phrases=4)

    badge = (ic.badge_text or "").strip() or str(resp.get("badge") or "").strip() or "CONTEXTO CLAVE"
    badge = " ".join(badge.split()[:3]).upper()

    return {
        "spoken_hook": spoken,
        "phrases": raw_phrases,
        "badge": badge,
    }


def run_editorial_selection(
    sentences: list[SentenceSegment],
    energy_blocks: list[dict],
    silence_intervals: list[tuple[float, float]],
    profile: PodcasterProfile,
    llm: OpenRouterClient,
    req_cliffhanger: int,
    req_conclusive: int,
    req_medium: int,
    req_long: int,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    shorts_reserve_sec: float = 0.0,
    non_speech_intervals: Optional[list[tuple[float, float]]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    telemetry_cb: Optional[Callable[[dict[str, Any]], None]] = None,
    catalog_out: Optional[dict[str, Any]] = None,
) -> tuple[list[ClipCandidate], list[str], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """Run full adaptive editorial v2: block map, glossary/Visual Bible, distinct sub-angle planning & Blind Director audit."""
    if not sentences:
        raise ValueError("La transcripción borrador no contiene frases.")

    def _emit(msg: str) -> None:
        if log_cb is not None:
            try:
                log_cb(msg)
            except Exception:
                pass

    total_duration = sentences[-1].end
    id_to_idx = {s.id: i for i, s in enumerate(sentences)}
    distinct_speakers = {s.speaker for s in sentences if s.speaker}
    show_speakers = len(distinct_speakers) > 1

    med_win, long_win, eff_med, eff_long, warnings = _resolve_adaptive_windows(
        total_duration,
        profile,
        req_medium,
        req_long,
    )

    block_sec = 120.0 if total_duration < 1500.0 else 300.0
    blocks = build_time_blocks(sentences, energy_blocks, block_sec=block_sec)
    block_map: list[dict[str, Any]] = []

    if progress_cb:
        progress_cb(10.0, f"Mapeando {len(blocks)} bloques temáticos del podcast...")

    if total_duration >= 1500.0 or len(blocks) > 4:
        batches = [blocks[i : i + 5] for i in range(0, len(blocks), 5)]
        total_batches = max(1, len(batches))
        done_batches = 0
        with ThreadPoolExecutor(max_workers=min(4, total_batches)) as pool:
            futs = [pool.submit(_map_single_block_batch, llm, profile, batch, show_speakers) for batch in batches]
            for fut in as_completed(futs):
                block_map.extend(fut.result())
                done_batches += 1
                if progress_cb:
                    progress_cb(
                        10.0 + (16.0 * (done_batches / total_batches)),
                        f"Mapeando bloques temáticos ({done_batches}/{total_batches})...",
                    )
        block_map.sort(key=lambda b: int(b.get("block_index", 0)))

    blacklisted_indices = _detect_blacklisted_indices(sentences, block_map, blocks, id_to_idx)
    if blacklisted_indices:
        _emit(f"Bloqueados editorialmente {len(blacklisted_indices)} segmentos de promo/despedida/anuncio.")

    if progress_cb:
        progress_cb(28.0, "Extrayendo glosario de nombres propios y Biblia Visual...")

    glossary, visual_bible = extract_episode_glossary_and_bible(
        sentences,
        block_map,
        profile,
        llm,
        blacklisted_indices=blacklisted_indices,
    )
    if glossary:
        sentences[:] = apply_glossary_to_sentences(sentences, glossary)
        _emit(f"Glosario aplicado sobre transcripción borrador ({len(glossary)} entradas verificadas).")

    gap_refs = non_speech_intervals if non_speech_intervals is not None else silence_intervals
    eff_cut_intervals = list(gap_refs)
    music_breaks = _compute_music_breaks(sentences, gap_refs)
    if music_breaks:
        _emit(f"Marcadas {len(music_breaks)} pausas musicales/cambios de bloque para el director editorial.")

    global_topic_stopwords = _build_global_entity_stopwords(visual_bible, profile, sentences=sentences)
    already_used_intros: list[str] = []
    audited_signatures: set[str] = set()
    rejected_records: list[dict[str, Any]] = []

    planned_angles: list[dict[str, Any]] = []
    if (req_cliffhanger + req_conclusive) >= 1:
        if progress_cb:
            progress_cb(36.0, "Planificando sub-ángulos y capítulos completamente distintos...")
        planned_angles = _plan_distinct_episode_angles(
            llm=llm,
            profile=profile,
            blocks=blocks,
            block_map=block_map,
            sentences=sentences,
            visual_bible=visual_bible,
            total_shorts=(req_cliffhanger + req_conclusive),
            modality_counts={"short_cliffhanger": req_cliffhanger, "short_conclusive": req_conclusive},
            blacklisted_indices=blacklisted_indices,
            log_cb=log_cb,
        )
    if telemetry_cb is not None:
        try:
            telemetry_cb(
                {
                    "type": "editorial_overview",
                    "ts": time.time(),
                    "blacklisted_count": len(blacklisted_indices),
                    "music_breaks_count": len(music_breaks),
                    "block_map_count": len(block_map),
                    "planned_angles": planned_angles,
                }
            )
        except Exception:
            pass

    if progress_cb and (eff_med > 0 or eff_long > 0):
        progress_cb(42.0, "Nivel 2A: Seleccionando y auditando arcos medios y largos...")

    med_cands = _select_longform_arcs(
        llm,
        profile,
        sentences,
        id_to_idx,
        blocks,
        block_map,
        "medium",
        eff_med,
        med_win,
        eff_cut_intervals,
        energy_blocks,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        visual_bible=visual_bible,
    )
    long_cands = _select_longform_arcs(
        llm,
        profile,
        sentences,
        id_to_idx,
        blocks,
        block_map,
        "long",
        eff_long,
        long_win,
        eff_cut_intervals,
        energy_blocks,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        visual_bible=visual_bible,
    )

    chosen_long = _deduplicate_and_audit(
        long_cands,
        eff_long,
        [],
        [],
        True,
        "long",
        sentences,
        id_to_idx,
        long_win,
        eff_cut_intervals,
        energy_blocks,
        profile,
        llm,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        blocks=blocks,
        block_map=block_map,
        visual_bible=visual_bible,
        allow_fallback=True,
        progress_cb=progress_cb,
        progress_range=(44.0, 53.0),
        stage_label="Auditando vídeo largo",
        already_used_intros=already_used_intros,
        global_topic_stopwords=global_topic_stopwords,
        log_cb=log_cb,
        telemetry_cb=telemetry_cb,
        audited_signatures=audited_signatures,
        rejected_records=rejected_records,
    )
    long_span_ivs = [(sp.orig_start, sp.orig_end) for c in chosen_long for sp in (c.narrative_spans or [])] or [
        (c.orig_start, c.orig_end) for c in chosen_long
    ]
    chosen_med = _deduplicate_and_audit(
        med_cands,
        eff_med,
        long_span_ivs,
        [],
        True,
        "medium",
        sentences,
        id_to_idx,
        med_win,
        eff_cut_intervals,
        energy_blocks,
        profile,
        llm,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        blocks=blocks,
        block_map=block_map,
        visual_bible=visual_bible,
        allow_fallback=True,
        progress_cb=progress_cb,
        progress_range=(53.0, 62.0),
        stage_label="Auditando vídeo medio",
        already_used_intros=already_used_intros,
        global_topic_stopwords=global_topic_stopwords,
        log_cb=log_cb,
        telemetry_cb=telemetry_cb,
        audited_signatures=audited_signatures,
        rejected_records=rejected_records,
    )
    longform_spans = [
        (sp.orig_start, sp.orig_end)
        for c in [*chosen_long, *chosen_med]
        for sp in (c.narrative_spans or [])
    ] or [(c.orig_start, c.orig_end) for c in [*chosen_long, *chosen_med]]

    if progress_cb:
        progress_cb(65.0, "Nivel 2B: Selección narrativa y auditoría de Shorts Cliffhanger vs. Conclusivos...")

    def build_excerpt_for_modality(key: str) -> str:
        clean_all = [s for i, s in enumerate(sentences) if i not in blacklisted_indices]
        if len(clean_all) <= 420:
            return _format_sentences_block(
                clean_all or sentences,
                show_speakers=show_speakers,
                music_breaks=music_breaks,
                include_durations=True,
            )
        if not block_map:
            return _format_sentences_block(
                (clean_all or sentences)[:420],
                show_speakers=show_speakers,
                music_breaks=music_breaks,
                include_durations=True,
            )

        non_ad_blocks = [m for m in block_map if not m.get("is_ad_or_promo")] or block_map
        ranked = sorted(
            non_ad_blocks,
            key=lambda m: float(m.get(key, 5))
            + (1.2 if blocks[min(len(blocks) - 1, int(m.get("block_index", 0)))]["energy"] == "alta" else 0.0),
            reverse=True,
        )
        sorted_non_ad_bis = sorted(
            {min(len(blocks) - 1, int(m.get("block_index", 0))) for m in non_ad_blocks}
        )
        valid_non_ad_set = set(sorted_non_ad_bis)
        early_bis = sorted_non_ad_bis[:2]
        picked_bis: list[int] = list(early_bis)
        for a in planned_angles:
            for bi in (a.get("target_blocks") or []):
                if isinstance(bi, int) and bi in valid_non_ad_set and bi not in picked_bis:
                    picked_bis.append(bi)
        for m in ranked:
            bi = min(len(blocks) - 1, int(m.get("block_index", 0)))
            if bi not in picked_bis:
                picked_bis.append(bi)
            if len(picked_bis) >= min(9, len(sorted_non_ad_bis)):
                break
        top_indices = sorted(set(picked_bis))
        parts = []
        for bi in top_indices:
            b = blocks[bi]
            b_clean = [s for s in b["sentences"] if id_to_idx.get(s.id, -1) not in blacklisted_indices]
            if not b_clean:
                continue
            parts.append(
                f"--- BLOQUE B{bi} ({b_clean[0].start:.0f}s-{b_clean[-1].end:.0f}s, energía={b['energy']}) ---\n"
                + _format_sentences_block(
                    b_clean,
                    show_speakers=show_speakers,
                    music_breaks=music_breaks,
                    include_durations=True,
                )
            )
        return (
            "\n\n".join(parts)
            if parts
            else _format_sentences_block(
                (clean_all or sentences)[:420],
                show_speakers=show_speakers,
                music_breaks=music_breaks,
                include_durations=True,
            )
        )

    eff_shorts_win = DurationWindow(
        min_sec=max(12.0, round(float(profile.quotas.shorts_window.min_sec) - max(0.0, shorts_reserve_sec), 1)),
        max_sec=max(
            max(12.0, round(float(profile.quotas.shorts_window.min_sec) - max(0.0, shorts_reserve_sec), 1)) + 8.0,
            round(float(profile.quotas.shorts_window.max_sec) - max(0.0, shorts_reserve_sec), 1),
        ),
    )

    def _renumber_clips(clips_list: list[ClipCandidate], ctype: ClipType) -> None:
        for idx, c in enumerate(clips_list, start=1):
            c.id = f"{ctype}_{idx:02d}_{c.slug[:16]}"
            c.output_filename = format_output_filename(
                c.score,
                c.clip_type,
                c.metadata.titles[0] if c.metadata.titles else c.slug,
            )

    def _short_topic_labels(clips_list: list[ClipCandidate]) -> list[str]:
        labels: list[str] = []
        for c in clips_list:
            t_lbl = c.angle_theme or (c.metadata.titles[0] if c.metadata.titles else "") or c.summary
            if t_lbl:
                labels.append(str(t_lbl).strip())
        return labels

    def _remaining_angles_for(clips_list: list[ClipCandidate], mod: str) -> list[dict[str, Any]]:
        if not planned_angles:
            return []
        used_ids = {c.angle_id for c in clips_list if c.angle_id}
        avail = [a for a in planned_angles if a.get("angle_id") not in used_ids]
        pref = [a for a in avail if a.get("recommended_modality") == mod]
        other = [a for a in avail if a.get("recommended_modality") != mod]
        return [*pref, *other]

    chosen_short_clips: list[ClipCandidate] = []

    cliff_angles = _remaining_angles_for(chosen_short_clips, "short_cliffhanger")
    cliff_excerpt = build_excerpt_for_modality("potencial_cliffhanger")
    cliff_cands = _select_shorts_by_modality(
        llm,
        profile,
        sentences,
        id_to_idx,
        cliff_excerpt,
        "short_cliffhanger",
        req_cliffhanger,
        eff_shorts_win,
        eff_cut_intervals,
        energy_blocks,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        visual_bible=visual_bible,
        block_map=block_map,
        model_role="editorial",
        assigned_angles=cliff_angles,
    )
    chosen_cliff = _deduplicate_and_audit(
        cliff_cands,
        req_cliffhanger,
        [],
        longform_spans,
        profile.quotas.allow_short_as_teaser,
        "short_cliffhanger",
        sentences,
        id_to_idx,
        eff_shorts_win,
        eff_cut_intervals,
        energy_blocks,
        profile,
        llm,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        blocks=blocks,
        block_map=block_map,
        visual_bible=visual_bible,
        allow_fallback=False,
        progress_cb=progress_cb,
        progress_range=(66.0, 78.0),
        stage_label="Auditando Shorts cliffhanger",
        existing_clips=chosen_short_clips,
        already_used_intros=already_used_intros,
        global_topic_stopwords=global_topic_stopwords,
        log_cb=log_cb,
        telemetry_cb=telemetry_cb,
        audited_signatures=audited_signatures,
        rejected_records=rejected_records,
    )
    chosen_short_clips.extend(chosen_cliff)

    if len(chosen_cliff) < req_cliffhanger:
        if progress_cb:
            progress_cb(79.0, "Buscando otros sub-ángulos cliffhanger con modelo experto...")
        existing_cliff_ivs = [
            (sp.orig_start, sp.orig_end) for c in chosen_short_clips for sp in (c.narrative_spans or [])
        ] or [(c.orig_start, c.orig_end) for c in chosen_short_clips]
        esc_cliff_cands = _select_shorts_by_modality(
            llm,
            profile,
            sentences,
            id_to_idx,
            cliff_excerpt,
            "short_cliffhanger",
            req_cliffhanger - len(chosen_cliff),
            eff_shorts_win,
            eff_cut_intervals,
            energy_blocks,
            show_speakers,
            blacklisted_indices=blacklisted_indices,
            visual_bible=visual_bible,
            block_map=block_map,
            model_role="editorial_strong",
            exclude_intervals=existing_cliff_ivs,
            exclude_topics=_short_topic_labels(chosen_short_clips),
            assigned_angles=_remaining_angles_for(chosen_short_clips, "short_cliffhanger"),
        )
        more_cliff = _deduplicate_and_audit(
            esc_cliff_cands,
            req_cliffhanger - len(chosen_cliff),
            existing_cliff_ivs,
            longform_spans,
            profile.quotas.allow_short_as_teaser,
            "short_cliffhanger",
            sentences,
            id_to_idx,
            eff_shorts_win,
            eff_cut_intervals,
            energy_blocks,
            profile,
            llm,
            show_speakers,
            blacklisted_indices=blacklisted_indices,
            blocks=blocks,
            block_map=block_map,
            visual_bible=visual_bible,
            allow_fallback=True,
            progress_cb=progress_cb,
            progress_range=(80.0, 84.0),
            stage_label="Auditando Shorts cliffhanger (experto)",
            max_strong_repairs=max(1, req_cliffhanger - len(chosen_cliff)),
            existing_clips=chosen_short_clips,
            already_used_intros=already_used_intros,
            global_topic_stopwords=global_topic_stopwords,
            log_cb=log_cb,
            telemetry_cb=telemetry_cb,
            audited_signatures=audited_signatures,
            rejected_records=rejected_records,
        )
        chosen_cliff.extend(more_cliff)
        chosen_short_clips.extend(more_cliff)
        _renumber_clips(chosen_cliff, "short_cliffhanger")
        if len(chosen_cliff) < req_cliffhanger:
            w_msg = (
                f"Cuota short_cliffhanger parcial ({len(chosen_cliff)}/{req_cliffhanger}): "
                "el Juez Ciego descartó candidatos que no garantizaban contexto autónomo sin spoilers."
            )
            warnings.append(w_msg)
            _emit(w_msg)

    if progress_cb and req_conclusive > 0:
        progress_cb(85.0, "Auditando Shorts conclusivos y coherencia final...")

    cliff_span_ivs = [
        (sp.orig_start, sp.orig_end)
        for c in chosen_cliff
        for sp in (c.narrative_spans or [])
    ] or [(c.orig_start, c.orig_end) for c in chosen_cliff]

    concl_excerpt = build_excerpt_for_modality("potencial_conclusivo")
    concl_cands = _select_shorts_by_modality(
        llm,
        profile,
        sentences,
        id_to_idx,
        concl_excerpt,
        "short_conclusive",
        req_conclusive,
        eff_shorts_win,
        eff_cut_intervals,
        energy_blocks,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        visual_bible=visual_bible,
        block_map=block_map,
        model_role="editorial",
        exclude_intervals=cliff_span_ivs,
        exclude_topics=_short_topic_labels(chosen_short_clips),
        assigned_angles=_remaining_angles_for(chosen_short_clips, "short_conclusive"),
    )
    chosen_concl = _deduplicate_and_audit(
        concl_cands,
        req_conclusive,
        cliff_span_ivs,
        longform_spans,
        profile.quotas.allow_short_as_teaser,
        "short_conclusive",
        sentences,
        id_to_idx,
        eff_shorts_win,
        eff_cut_intervals,
        energy_blocks,
        profile,
        llm,
        show_speakers,
        blacklisted_indices=blacklisted_indices,
        blocks=blocks,
        block_map=block_map,
        visual_bible=visual_bible,
        allow_fallback=False,
        progress_cb=progress_cb,
        progress_range=(86.0, 95.0),
        stage_label="Auditando Shorts conclusivos",
        existing_clips=chosen_short_clips,
        already_used_intros=already_used_intros,
        global_topic_stopwords=global_topic_stopwords,
        log_cb=log_cb,
        telemetry_cb=telemetry_cb,
        audited_signatures=audited_signatures,
        rejected_records=rejected_records,
    )
    chosen_short_clips.extend(chosen_concl)

    if len(chosen_concl) < req_conclusive:
        if progress_cb:
            progress_cb(96.0, "Buscando sub-ángulos conclusivos adicionales con modelo experto...")
        existing_concl_ivs = [
            *cliff_span_ivs,
            *(
                [(sp.orig_start, sp.orig_end) for c in chosen_concl for sp in (c.narrative_spans or [])]
                or [(c.orig_start, c.orig_end) for c in chosen_concl]
            ),
        ]
        esc_concl_cands = _select_shorts_by_modality(
            llm,
            profile,
            sentences,
            id_to_idx,
            concl_excerpt,
            "short_conclusive",
            req_conclusive - len(chosen_concl),
            eff_shorts_win,
            eff_cut_intervals,
            energy_blocks,
            show_speakers,
            blacklisted_indices=blacklisted_indices,
            visual_bible=visual_bible,
            block_map=block_map,
            model_role="editorial_strong",
            exclude_intervals=existing_concl_ivs,
            exclude_topics=_short_topic_labels(chosen_short_clips),
            assigned_angles=_remaining_angles_for(chosen_short_clips, "short_conclusive"),
        )
        more_concl = _deduplicate_and_audit(
            esc_concl_cands,
            req_conclusive - len(chosen_concl),
            existing_concl_ivs,
            longform_spans,
            profile.quotas.allow_short_as_teaser,
            "short_conclusive",
            sentences,
            id_to_idx,
            eff_shorts_win,
            eff_cut_intervals,
            energy_blocks,
            profile,
            llm,
            show_speakers,
            blacklisted_indices=blacklisted_indices,
            blocks=blocks,
            block_map=block_map,
            visual_bible=visual_bible,
            allow_fallback=True,
            progress_cb=progress_cb,
            progress_range=(96.5, 99.0),
            stage_label="Auditando Shorts conclusivos (experto)",
            max_strong_repairs=max(1, req_conclusive - len(chosen_concl)),
            existing_clips=chosen_short_clips,
            already_used_intros=already_used_intros,
            global_topic_stopwords=global_topic_stopwords,
            log_cb=log_cb,
            telemetry_cb=telemetry_cb,
            audited_signatures=audited_signatures,
            rejected_records=rejected_records,
        )
        chosen_concl.extend(more_concl)
        chosen_short_clips.extend(more_concl)
        _renumber_clips(chosen_concl, "short_conclusive")
        if len(chosen_concl) < req_conclusive:
            w_msg = (
                f"Cuota short_conclusive parcial ({len(chosen_concl)}/{req_conclusive}): "
                "se priorizó no emitir rellenos ni temas clonados sin contexto autónomo."
            )
            warnings.append(w_msg)
            _emit(w_msg)

    all_chosen = [*chosen_cliff, *chosen_concl, *chosen_med, *chosen_long]
    all_chosen.sort(key=lambda c: c.score, reverse=True)

    if catalog_out is not None:
        chosen_cids = {c.candidate_id for c in all_chosen if c.candidate_id}
        seen_rej_cids: set[str] = set()
        dedup_rejected: list[dict[str, Any]] = []
        for rr in rejected_records:
            rcid = str(rr.get("candidate_id") or "")
            if rcid and (rcid in chosen_cids or rcid in seen_rej_cids):
                continue
            if rcid:
                seen_rej_cids.add(rcid)
            dedup_rejected.append(rr)
        catalog_out["candidate_pool"] = all_chosen
        catalog_out["rejected_candidates"] = dedup_rejected
        catalog_out["planned_angles"] = planned_angles

    if progress_cb:
        progress_cb(100.0, f"Selección editorial completada: {len(all_chosen)} vídeos auditados.")

    return all_chosen, warnings, block_map, glossary, visual_bible


def propose_clip_from_block(
    block_index: int,
    clip_type: ClipType,
    sentences: list[SentenceSegment],
    energy_blocks: list[dict],
    silence_intervals: list[tuple[float, float]],
    profile: PodcasterProfile,
    llm: OpenRouterClient,
    visual_bible: Optional[dict[str, Any]] = None,
    block_map: Optional[list[dict[str, Any]]] = None,
    blacklisted_indices: Optional[set[int]] = None,
) -> ClipCandidate:
    """Create and frame a ready-to-produce ClipCandidate focused on a specific episode block."""
    if not sentences:
        raise ValueError("No hay frases transcritas disponibles.")

    id_to_idx = {s.id: i for i, s in enumerate(sentences)}
    distinct_speakers = {s.speaker for s in sentences if s.speaker}
    show_speakers = len(distinct_speakers) > 1
    blocked = blacklisted_indices or set()

    total_duration = sentences[-1].end
    block_sec = 120.0 if total_duration < 1500.0 else 300.0
    blocks = build_time_blocks(sentences, energy_blocks, block_sec=block_sec)
    if not blocks:
        raise ValueError("No se pudieron construir bloques temporales del episodio.")
    bi = max(0, min(len(blocks) - 1, int(block_index)))

    # Include block bi plus small surrounding buffer (e.g., for medium/long or payoff finishing in next block)
    b_cur = blocks[bi]
    b_start_idx = max(0, id_to_idx.get(b_cur["first_id"], 0) - 12)
    extra_end = 45 if clip_type in ("medium", "long") else 20
    b_end_idx = min(len(sentences) - 1, id_to_idx.get(b_cur["last_id"], len(sentences) - 1) + extra_end)
    excerpt_sents = [sentences[k] for k in range(b_start_idx, b_end_idx + 1) if k not in blocked]
    if not excerpt_sents:
        excerpt_sents = sentences[b_start_idx : b_end_idx + 1]

    med_win, long_win, _, _, _ = _resolve_adaptive_windows(total_duration, profile, 1, 1)
    if clip_type.startswith("short"):
        aspect = profile.visual.shorts_aspect
        target_win = DurationWindow(
            min_sec=max(15.0, profile.quotas.shorts_window.min_sec - 8.0),
            max_sec=max(35.0, profile.quotas.shorts_window.max_sec - 10.0),
        )
        cands = _select_shorts_by_modality(
            llm=llm,
            profile=profile,
            sentences=sentences,
            id_to_idx=id_to_idx,
            transcript_excerpt=_format_sentences_block(excerpt_sents, show_speakers=show_speakers, include_durations=True),
            modality=clip_type,  # type: ignore[arg-type]
            count=1,
            window=target_win,
            silence_intervals=silence_intervals,
            energy_blocks=energy_blocks,
            show_speakers=show_speakers,
            blacklisted_indices=blocked,
            visual_bible=visual_bible,
            block_map=block_map,
            model_role="editorial",
        )
    else:
        aspect = profile.visual.medium_aspect if clip_type == "medium" else profile.visual.long_aspect
        target_win = med_win if clip_type == "medium" else long_win
        end_bi = min(len(blocks) - 1, bi + (1 if clip_type == "medium" else 2))
        b_meta = next((m for m in (block_map or []) if int(m.get("block_index", -1)) == bi), {})
        cands = _select_longform_arcs(
            llm=llm,
            profile=profile,
            sentences=sentences,
            id_to_idx=id_to_idx,
            blocks=blocks,
            block_map_summaries=[
                {"block_index": bi, "tema": b_meta.get("tema", f"Bloque {bi}"), "resumen_breve": b_meta.get("resumen_breve", "")}
            ],
            clip_type=clip_type,  # type: ignore[arg-type]
            count=1,
            window=target_win,
            silence_intervals=silence_intervals,
            energy_blocks=energy_blocks,
            show_speakers=show_speakers,
            blacklisted_indices=blocked,
            visual_bible=visual_bible,
        )
        if not cands:
            cands = []

    if not cands:
        # Deterministic fallback bounded to the chosen block
        s_i = max(0, id_to_idx.get(b_cur["first_id"], 0))
        e_i = min(len(sentences) - 1, s_i + 15)
        adj_s, adj_e, est = adjust_bounds_to_window(s_i, e_i, sentences, target_win, silence_intervals, profile.audio, blacklisted_indices=blocked)
        b_meta = next((m for m in (block_map or []) if int(m.get("block_index", -1)) == bi), {})
        title_fb = str(b_meta.get("tema") or f"Historia del bloque B{bi}").strip()
        cand = ClipCandidate(
            id=f"{clip_type}_b{bi}_{make_slug(title_fb)[:14]}",
            candidate_id=f"cand_{uuid.uuid4().hex[:8]}",
            original_title=title_fb,
            clip_type=clip_type,
            aspect_ratio=aspect,
            score=82,
            slug=make_slug(title_fb),
            summary=str(b_meta.get("resumen_breve") or sentences[adj_s].text),
            start_sentence_id=sentences[adj_s].id,
            end_sentence_id=sentences[adj_e].id,
            orig_start=sentences[adj_s].start,
            orig_end=sentences[adj_e].end,
            estimated_post_cut_sec=est,
            narrative_spans=[
                NarrativeSpan(
                    start_sentence_id=sentences[adj_s].id,
                    end_sentence_id=sentences[adj_e].id,
                    orig_start=sentences[adj_s].start,
                    orig_end=sentences[adj_e].end,
                    role="core",
                )
            ],
            metadata=ClipMetadata(titles=[title_fb], description=str(b_meta.get("resumen_breve") or "")),
        )
    else:
        cand = cands[0]
        if not cand.candidate_id:
            cand.candidate_id = f"cand_{uuid.uuid4().hex[:8]}"
        if not cand.original_title:
            cand.original_title = cand.metadata.titles[0] if cand.metadata.titles else cand.slug

    if profile.intro_curtain.enabled:
        ic_script = generate_intro_curtain_script(
            cand,
            profile,
            visual_bible or {},
            llm,
            sentences=sentences,
            id_to_idx=id_to_idx,
        )
        cand.intro_curtain_enabled = True
        cand.intro_curtain_text = ic_script["spoken_hook"]
        cand.intro_curtain_phrases = ic_script["phrases"]
        cand.intro_curtain_badge = ic_script["badge"]

    cand.user_rescued = True
    cand.enabled = True
    cand.output_filename = format_output_filename(cand.score, cand.clip_type, cand.metadata.titles[0] if cand.metadata.titles else cand.slug)
    return cand
