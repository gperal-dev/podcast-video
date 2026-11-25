"""Local two-pass transcription via faster-whisper (Fine-grained clauses + Precision word-level + Glossary)."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel

from backend.config import Settings, get_settings
from backend.core.audio_analyzer import get_audio_duration
from backend.core.diagnostics import resolve_whisper_runtime
from backend.models.job import SentenceSegment, WordTimestamp


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str, str], WhisperModel] = {}


def get_whisper_model(
    model_name: str,
    settings: Optional[Settings] = None,
) -> WhisperModel:
    """Load and cache a WhisperModel using safe runtime device resolution and bounded CPU threads."""
    cfg = settings or get_settings()
    rt = resolve_whisper_runtime(cfg)
    key = (model_name, rt["resolved_device"], rt["resolved_compute_type"])
    with _MODEL_LOCK:
        if key not in _MODEL_CACHE:
            kwargs: dict[str, Any] = {
                "device": rt["resolved_device"],
                "compute_type": rt["resolved_compute_type"],
            }
            if rt["resolved_device"] == "cpu":
                # Bound CPU threads so Whisper runs steadily without saturating or overheating the host
                kwargs["cpu_threads"] = min(4, max(2, (os.cpu_count() or 4) // 2))
                kwargs["num_workers"] = 1
            _MODEL_CACHE[key] = WhisperModel(model_name, **kwargs)
        return _MODEL_CACHE[key]


def compute_audio_draft_cache_key(
    audio_path: Path,
    language: Optional[str],
    remove_silences: bool,
    silence_threshold_db: float,
    min_silence_sec: float,
    settings: Optional[Settings] = None,
) -> str:
    """Build deterministic cache key for Pass-1 draft transcript + acoustic scan."""
    cfg = settings or get_settings()
    st = audio_path.stat()
    h = hashlib.sha256()
    h.update(
        json.dumps(
            {
                "size": st.st_size,
                "name": audio_path.name,
                "draft_model": cfg.whisper_draft_model,
                "lang": language or "auto",
                "remove_silences": bool(remove_silences),
                "silence_threshold_db": round(float(silence_threshold_db), 2),
                "min_silence_sec": round(float(min_silence_sec), 2),
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    # Sample first + middle + last 64KB to distinguish files with same size/name rapidly
    try:
        with audio_path.open("rb") as fh:
            h.update(fh.read(65536))
            if st.st_size > 131072:
                fh.seek(st.st_size // 2)
                h.update(fh.read(65536))
                fh.seek(max(0, st.st_size - 65536))
                h.update(fh.read(65536))
    except OSError:
        pass
    return h.hexdigest()[:20]


def load_cached_draft(cache_key: str, settings: Optional[Settings] = None) -> Optional[dict[str, Any]]:
    cfg = settings or get_settings()
    p = cfg.base_dir / "cache" / "transcripts" / f"{cache_key}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("sentences"):
            return data
    except Exception:
        pass
    return None


def save_cached_draft(cache_key: str, payload: dict[str, Any], settings: Optional[Settings] = None) -> None:
    cfg = settings or get_settings()
    cdir = cfg.base_dir / "cache" / "transcripts"
    cdir.mkdir(parents=True, exist_ok=True)
    tmp = cdir / f"{cache_key}.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(cdir / f"{cache_key}.json")


def unload_whisper_models() -> None:
    """Release cached Whisper models to free RAM before rendering."""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()
    gc.collect()


def _normalize_glossary_token(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9ñ]+", stripped.lower())
    return " ".join(tokens)


DANGLING_END_WORDS: frozenset[str] = frozenset(
    {
        # Spanish articles, contractions, prepositions, conjunctions, clitics & auxiliaries
        "el", "la", "los", "las", "lo", "un", "una", "unos", "unas",
        "de", "del", "a", "al", "en", "con", "por", "para", "sin", "sobre",
        "entre", "hacia", "desde", "hasta", "tras", "ante", "antes", "bajo", "contra",
        "despues", "durante", "mediante", "segun",
        "y", "e", "o", "u", "ni", "pero", "sino", "aunque", "que", "si",
        "porque", "pues", "cuando", "donde", "como", "mientras",
        "cuyo", "cuya", "cuyos", "cuyas", "su", "sus", "mi", "mis", "tu", "tus",
        "se", "le", "les", "me", "te", "nos", "os",
        "muy", "mas", "menos", "tan", "ya", "no", "es", "son", "era", "fue",
        "ha", "han", "he", "hay", "esta", "estan", "este", "esta", "estos", "estas",
        "ese", "esa", "esos", "esas", "aquel", "aquella",
        # English articles, prepositions, conjunctions & auxiliaries
        "the", "an", "of", "to", "in", "on", "for", "with", "by", "from", "at",
        "and", "or", "but", "if", "that", "which", "who", "when", "where", "as",
        "is", "are", "was", "were", "be", "been", "have", "has", "had",
    }
)

_NON_TERMINAL_ABBREVS: frozenset[str] = frozenset(
    {"dr.", "sr.", "sra.", "srta.", "prof.", "etc.", "ee.", "uu.", "p.ej.", "ej.", "vs.", "mr.", "mrs."}
)


def _norm_last_word(raw_token: str) -> str:
    """Normalize a single token (or trailing word of a string) without accents for dangling-word checks."""
    decomposed = unicodedata.normalize("NFKD", (raw_token or "").strip())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    parts = re.findall(r"[a-z0-9ñ]+", stripped.lower())
    return parts[-1] if parts else ""


def apply_glossary_to_words(
    words: list[WordTimestamp],
    glossary: dict[str, str],
) -> list[WordTimestamp]:
    """Repair Whisper mishearings using episode glossary while preserving exact timestamps & speakers."""
    if not words or not glossary:
        return words

    rules: list[tuple[list[str], str]] = []
    for wrong, right in glossary.items():
        norm_parts = _normalize_glossary_token(wrong).split()
        clean_right = (right or "").strip()
        if norm_parts and clean_right and _normalize_glossary_token(clean_right) != " ".join(norm_parts):
            rules.append((norm_parts, clean_right))

    if not rules:
        return words

    rules.sort(key=lambda r: len(r[0]), reverse=True)
    norm_words = [_normalize_glossary_token(w.word) for w in words]

    out: list[WordTimestamp] = []
    i = 0
    n = len(words)
    while i < n:
        matched = False
        for wrong_parts, right_phrase in rules:
            k = len(wrong_parts)
            if i + k <= n and norm_words[i : i + k] == wrong_parts:
                span_start = words[i].start
                span_end = words[i + k - 1].end
                span_speaker = words[i].speaker
                span_censored = any(words[j].censored for j in range(i, i + k))

                # Preserve trailing punctuation from the last matched word
                last_raw = words[i + k - 1].word.strip()
                trailing_m = re.search(r"([.,;:!?]+)$", last_raw)
                trailing = trailing_m.group(1) if trailing_m else ""

                rep_tokens = right_phrase.split()
                if out and len(rep_tokens) >= 2:
                    prev_norm = _normalize_glossary_token(out[-1].word)
                    if prev_norm and _normalize_glossary_token(rep_tokens[0]) == prev_norm:
                        rep_tokens = rep_tokens[1:]
                if rep_tokens and trailing and not rep_tokens[-1].endswith(tuple(".,;:!?")):
                    rep_tokens[-1] = rep_tokens[-1] + trailing

                total_chars = max(1, sum(len(t) for t in rep_tokens))
                dur = max(0.04, span_end - span_start)
                cursor = span_start
                for r_idx, r_tok in enumerate(rep_tokens):
                    frac = len(r_tok) / total_chars
                    r_end = span_end if r_idx == len(rep_tokens) - 1 else min(span_end, cursor + dur * frac)
                    out.append(
                        WordTimestamp(
                            word=r_tok,
                            start=round(cursor, 3),
                            end=round(max(cursor + 0.02, r_end), 3),
                            probability=words[i].probability,
                            censored=span_censored,
                            speaker=span_speaker,
                        )
                    )
                    cursor = r_end
                i += k
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return out


def _split_segment_into_sentences(
    seg_start: float,
    seg_end: float,
    seg_text: str,
    words: Optional[list[WordTimestamp]] = None,
) -> list[tuple[float, float, str]]:
    """Split a Whisper segment only at complete sentences or natural non-dangling pauses."""
    clean_text = " ".join(seg_text.strip().split())
    if not clean_text:
        return []

    if words:
        results: list[tuple[float, float, str]] = []
        buf_words: list[WordTimestamp] = []
        for idx, w in enumerate(words):
            token = w.word.strip()
            if not token:
                continue

            # 1. Split BEFORE `w` only on a genuine acoustic pause after a complete/non-dangling phrase
            if buf_words:
                gap = w.start - buf_words[-1].end
                prev_norm = _norm_last_word(buf_words[-1].word)
                if gap >= 0.55 and len(buf_words) >= 5 and prev_norm not in DANGLING_END_WORDS:
                    results.append(
                        (
                            buf_words[0].start,
                            buf_words[-1].end,
                            " ".join(x.word.strip() for x in buf_words),
                        )
                    )
                    buf_words = []

            buf_words.append(w)
            buf_dur = buf_words[-1].end - buf_words[0].start
            buf_text = " ".join(x.word.strip() for x in buf_words)
            cur_norm = _norm_last_word(token)

            leading_lowercase = (not results) and _starts_with_lowercase(buf_text)
            min_term_words = 2 if leading_lowercase else 3
            min_term_chars = 6 if leading_lowercase else 10
            is_terminal = (
                token.endswith((".", "?", "!"))
                and token.lower() not in _NON_TERMINAL_ABBREVS
                and len(buf_words) >= min_term_words
                and len(buf_text) >= min_term_chars
            )
            is_long_clause = (
                token.endswith((",", ";", ":"))
                and (buf_dur >= 7.0 or len(buf_words) >= 18)
                and cur_norm not in DANGLING_END_WORDS
            )
            # Hard safety cap only at >= 32 words or >= 14s, waiting up to 5 words past dangling tokens
            over_hard_cap = (len(buf_words) >= 32 or buf_dur >= 14.0) and (
                cur_norm not in DANGLING_END_WORDS or len(buf_words) >= 37
            )
            if is_terminal or is_long_clause or over_hard_cap:
                results.append((buf_words[0].start, buf_words[-1].end, buf_text))
                buf_words = []

        if buf_words:
            text = " ".join(x.word.strip() for x in buf_words)
            results.append((buf_words[0].start, buf_words[-1].end, text))
        if results:
            return results

    # Fallback: character-proportional sentence splitting
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", clean_text) if p.strip()]
    if len(parts) == 1 and (seg_end - seg_start) > 12.0:
        parts = [p.strip() for p in re.split(r"(?<=[,;:])\s+", clean_text) if p.strip()]
    if len(parts) <= 1:
        return [(seg_start, seg_end, clean_text)]

    total_chars = max(1, sum(len(p) for p in parts))
    dur = max(0.05, seg_end - seg_start)
    cursor = seg_start
    out: list[tuple[float, float, str]] = []
    for idx, part in enumerate(parts):
        frac = len(part) / total_chars
        part_end = seg_end if idx == len(parts) - 1 else min(seg_end, cursor + dur * frac)
        out.append((round(cursor, 3), round(max(cursor + 0.05, part_end), 3), part))
        cursor = part_end
    return out


def _starts_with_lowercase(text: str) -> bool:
    """Return True if the first alphabetic character in text is lowercase."""
    for ch in (text or "").strip():
        if ch.isalpha():
            return ch.islower()
    return False


def _merge_orphan_fragments(sentences: list[SentenceSegment]) -> list[SentenceSegment]:
    """Attach ultra-short or dangling-word Whisper fragments to their syntactic neighbors and reindex S####."""
    if len(sentences) <= 1:
        return sentences

    items = [s.model_copy() for s in sentences if s.text.strip()]
    changed = True
    passes = 0
    while changed and len(items) > 1 and passes < 6:
        changed = False
        passes += 1
        merged: list[SentenceSegment] = []
        i = 0
        while i < len(items):
            cur = items[i]
            txt = " ".join(cur.text.split())
            words = txt.split()
            dur = cur.end - cur.start
            ends_terminal = txt.endswith((".", "?", "!"))
            last_norm = _norm_last_word(txt)
            is_short_or_dangling = (
                len(words) <= 3
                or dur < 1.2
                or (not ends_terminal and last_norm in DANGLING_END_WORDS)
            )

            # Case 1: Ends with a dangling function word (e.g. "se acercó ella a los") or short lowercase non-terminal fragment -> attach forward
            if (
                i + 1 < len(items)
                and not ends_terminal
                and (items[i + 1].start - cur.end) < 0.75
                and (len(words) <= 4 or (len(words) + len(items[i + 1].text.split())) <= 60)
                and (last_norm in DANGLING_END_WORDS or (len(words) <= 3 and _starts_with_lowercase(txt)) or (len(words) <= 2 and dur < 1.2))
            ):
                nxt = items[i + 1]
                items[i + 1] = nxt.model_copy(
                    update={
                        "start": round(cur.start, 3),
                        "text": f"{txt} {nxt.text.strip()}".strip(),
                    }
                )
                changed = True
                i += 1
                continue

            # Case 2: Short fragment (<= 3 words) or lowercase-started closing tail (<= 6 words ending in .?!) continuing an incomplete previous sentence -> attach backward
            is_lowercase_closing_tail = _starts_with_lowercase(txt) and ends_terminal and len(words) <= 6
            if (
                merged
                and (is_short_or_dangling or is_lowercase_closing_tail)
                and (len(words) <= 3 or is_lowercase_closing_tail)
                and (cur.start - merged[-1].end) < 0.75
                and not merged[-1].text.strip().endswith((".", "?", "!"))
            ):
                prev = merged[-1]
                merged[-1] = prev.model_copy(
                    update={
                        "end": round(max(prev.end, cur.end), 3),
                        "text": f"{prev.text.strip()} {txt}".strip(),
                    }
                )
                changed = True
                i += 1
                continue

            # Case 3: Lowercase run-on continuation after a non-terminal previous segment split by a short Whisper pause
            if (
                merged
                and not merged[-1].text.strip().endswith((".", "?", "!"))
                and _starts_with_lowercase(txt)
                and (cur.start - merged[-1].end) < 0.65
                and (len(merged[-1].text.split()) + len(words)) <= 32
                and (max(merged[-1].end, cur.end) - merged[-1].start) <= 12.0
            ):
                prev = merged[-1]
                merged[-1] = prev.model_copy(
                    update={
                        "end": round(max(prev.end, cur.end), 3),
                        "text": f"{prev.text.strip()} {txt}".strip(),
                    }
                )
                changed = True
                i += 1
                continue

            merged.append(cur.model_copy(update={"text": txt}))
            i += 1
        items = merged

    return [
        s.model_copy(update={"id": f"S{idx:04d}"})
        for idx, s in enumerate(items, start=1)
    ]


def transcribe_draft(
    audio_path: Path,
    language: Optional[str] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    settings: Optional[Settings] = None,
) -> tuple[list[SentenceSegment], str]:
    """Pass 1: Fast local transcription with VAD across the full podcast -> fine-grained [S####]."""
    cfg = settings or get_settings()
    total_dur = max(1.0, get_audio_duration(audio_path, cfg))
    model = get_whisper_model(cfg.whisper_draft_model, cfg)

    if progress_cb:
        progress_cb(2.0, f"Cargando modelo Whisper borrador ({cfg.whisper_draft_model})...")

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language or None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 450},
        word_timestamps=True,
        beam_size=1,
        condition_on_previous_text=False,
        compression_ratio_threshold=2.2,
        no_speech_threshold=0.6,
    )
    detected_lang = getattr(info, "language", None) or language or "es"

    sentences: list[SentenceSegment] = []
    idx = 1
    last_reported = -5.0

    for seg in segments_iter:
        seg_words: list[WordTimestamp] = []
        if getattr(seg, "words", None):
            for w in seg.words:
                w_text = (w.word or "").strip()
                if w_text:
                    seg_words.append(
                        WordTimestamp(
                            word=w_text,
                            start=round(float(w.start), 3),
                            end=round(max(float(w.start) + 0.02, float(w.end)), 3),
                            probability=float(getattr(w, "probability", 1.0) or 1.0),
                        )
                    )

        pieces = _split_segment_into_sentences(
            float(seg.start),
            float(seg.end),
            seg.text or "",
            seg_words or None,
        )
        for s_start, s_end, s_text in pieces:
            sentences.append(
                SentenceSegment(
                    id=f"S{idx:04d}",
                    start=round(s_start, 3),
                    end=round(max(s_start + 0.05, s_end), 3),
                    text=s_text,
                )
            )
            idx += 1

        pct = min(99.0, max(2.0, (float(seg.end) / total_dur) * 100.0))
        if progress_cb and pct - last_reported >= 3.0:
            last_reported = pct
            progress_cb(pct, f"Transcribiendo borrador: {pct:.0f}% ({idx - 1} frases)")

    sentences = _merge_orphan_fragments(sentences)
    if progress_cb:
        progress_cb(100.0, f"Pasada 1 completada: {len(sentences)} oraciones indexadas ({detected_lang}).")

    return sentences, detected_lang


def detect_pass2_hallucination(
    words: list[WordTimestamp],
    span_duration: float,
    reference_text: str = "",
    clip_start: Optional[float] = None,
    clip_end: Optional[float] = None,
    reference_sentences: Optional[list[SentenceSegment]] = None,
) -> tuple[bool, str]:
    """Detect Whisper Pass 2 hallucination loops, severe word drops, low-prob bursts, or missing speech gaps."""
    dur = max(0.2, float(span_duration))
    ref_tokens = _normalize_glossary_token(reference_text).split() if reference_text else []
    ref_count = len(ref_tokens)

    if not words:
        if dur >= 4.0 and ref_count >= 6:
            return True, f"Pasada 2 devolvió 0 palabras frente a {ref_count} en Pasada 1"
        return False, ""

    norm_tokens = [
        t
        for w in words
        for t in _normalize_glossary_token(w.word).split()
        if t
    ]

    # (a) Repetitive loop detection via unique 3-grams and back-to-back repeated n-grams
    if len(norm_tokens) >= 12 and len(words) >= 12:
        trigrams = [tuple(norm_tokens[i : i + 3]) for i in range(len(norm_tokens) - 2)]
        if trigrams:
            uniq_ratio = len(set(trigrams)) / len(trigrams)
            if uniq_ratio < 0.55:
                return True, f"bucle repetitivo de 3-gramas ({len(set(trigrams))}/{len(trigrams)} únicos)"

    if len(norm_tokens) >= 9:
        for k in (2, 3, 4):
            for i in range(0, len(norm_tokens) - (3 * k) + 1):
                g1 = norm_tokens[i : i + k]
                g2 = norm_tokens[i + k : i + 2 * k]
                g3 = norm_tokens[i + 2 * k : i + 3 * k]
                if g1 == g2 == g3:
                    return True, f"repetición en bucle de '{' '.join(g1)}'"

    # (b) Severe coverage drop compared to Pass 1 reference sentences
    if ref_count >= 12 and dur >= 8.0:
        min_expected = max(6, int(ref_count * 0.45))
        if len(words) < min_expected:
            return True, f"caída de cobertura ({len(words)} palabras vs {ref_count} en Pasada 1)"
    wps = len(words) / dur
    ref_wps = ref_count / dur if dur > 0 else 0.0
    if dur >= 12.0 and ref_wps >= 1.3 and wps < 0.65:
        return True, f"densidad muy baja ({wps:.2f} w/s vs {ref_wps:.2f} w/s en Pasada 1)"

    # (c) Low-probability burst
    if len(words) >= 8:
        low_count = sum(1 for w in words if float(w.probability) < 0.25)
        if (low_count / len(words)) >= 0.35:
            return True, f"ráfaga de baja confianza ({low_count}/{len(words)} palabras con prob < 0.25)"

    # (d) Leading, internal, or trailing dead gaps when Pass 1 has real speech in those intervals
    def _ref_words_in_window(w_s: float, w_e: float) -> int:
        if not reference_sentences or w_e <= w_s + 0.2:
            return 0
        cnt = 0
        for rs in reference_sentences:
            if rs.end > w_s + 0.1 and rs.start < w_e - 0.1:
                cnt += len(rs.text.split())
        return cnt

    if clip_start is not None and (words[0].start - float(clip_start)) > 4.5:
        lead_gap = words[0].start - float(clip_start)
        if _ref_words_in_window(float(clip_start), words[0].start - 0.5) >= 5:
            return True, f"hueco inicial de {lead_gap:.1f}s sin subtítulos"

    if clip_end is not None and (float(clip_end) - words[-1].end) > 4.5:
        trail_gap = float(clip_end) - words[-1].end
        if _ref_words_in_window(words[-1].end + 0.5, float(clip_end)) >= 5:
            return True, f"hueco final de {trail_gap:.1f}s sin subtítulos"

    if ref_wps >= 1.3 and len(words) >= 2:
        max_gap = 0.0
        gap_s = 0.0
        gap_e = 0.0
        for i in range(1, len(words)):
            g = words[i].start - words[i - 1].end
            if g > max_gap:
                max_gap = g
                gap_s = words[i - 1].end + 0.4
                gap_e = words[i].start - 0.4
        internal_limit = max(5.5, min(10.0, dur * 0.15))
        if max_gap > internal_limit and _ref_words_in_window(gap_s, gap_e) >= 5:
            return True, f"hueco interno de {max_gap:.1f}s sin subtítulos"

    return False, ""


def _patch_missing_word_gaps(
    words: list[WordTimestamp],
    clip_start: float,
    clip_end: float,
    reference_sentences: list[SentenceSegment],
    min_gap_sec: float = 4.0,
) -> tuple[list[WordTimestamp], int]:
    """Surgically fill any leading, internal, or trailing >=4s speech gap using Pass 1 reference sentences."""
    if not reference_sentences:
        return words, 0
    if not words:
        fb = _fallback_words_from_sentences(clip_start, clip_end, reference_sentences)
        return fb, (1 if fb else 0)

    gaps: list[tuple[float, float]] = []
    if words[0].start - float(clip_start) >= min_gap_sec:
        gaps.append((float(clip_start), max(float(clip_start) + 0.1, words[0].start - 0.03)))
    for i in range(1, len(words)):
        g_s = words[i - 1].end + 0.03
        g_e = words[i].start - 0.03
        if g_e - g_s >= min_gap_sec:
            gaps.append((g_s, g_e))
    if float(clip_end) - words[-1].end >= min_gap_sec:
        gaps.append((min(float(clip_end) - 0.1, words[-1].end + 0.03), float(clip_end)))

    if not gaps:
        return words, 0

    inserted: list[WordTimestamp] = []
    patched = 0
    for g_s, g_e in gaps:
        patch_words = _fallback_words_from_sentences(g_s, g_e, reference_sentences)
        if len(patch_words) >= 3:
            inserted.extend(patch_words)
            patched += 1

    if not inserted:
        return words, 0

    merged = [*words, *inserted]
    merged.sort(key=lambda w: w.start)
    return merged, patched


def _fallback_words_from_sentences(
    clip_start: float,
    clip_end: float,
    reference_sentences: list[SentenceSegment],
) -> list[WordTimestamp]:
    """Generate clean character-proportional word timestamps from Pass 1 sentences as a hallucination-proof fallback."""
    out: list[WordTimestamp] = []
    cs = float(clip_start)
    ce = float(clip_end)
    for sent in reference_sentences:
        if sent.end <= cs + 0.04 or sent.start >= ce - 0.04:
            continue
        s_t = max(cs, float(sent.start))
        e_t = min(ce, float(sent.end))
        if e_t <= s_t + 0.03:
            continue
        tokens = [t for t in sent.text.strip().split() if t]
        if not tokens:
            continue
        total_chars = max(1, sum(len(t) for t in tokens))
        dur = e_t - s_t
        cursor = s_t
        for i, tok in enumerate(tokens):
            frac = len(tok) / total_chars
            w_end = e_t if i == len(tokens) - 1 else min(e_t, cursor + dur * frac)
            out.append(
                WordTimestamp(
                    word=tok,
                    start=round(cursor, 3),
                    end=round(max(cursor + 0.02, w_end), 3),
                    probability=0.85,
                    speaker=sent.speaker,
                )
            )
            cursor = w_end
    return out


def _run_whisper_pass2_attempt(
    model: WhisperModel,
    wav_path: Path,
    clip_start: float,
    clip_end: float,
    extract_start: float,
    language: Optional[str],
    beam_size: int,
    vad_filter: bool,
    no_speech_threshold: float,
    initial_prompt: Optional[str],
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> tuple[list[WordTimestamp], list[SentenceSegment]]:
    transcribe_kwargs: dict[str, object] = {
        "language": language or None,
        "vad_filter": vad_filter,
        "word_timestamps": True,
        "beam_size": beam_size,
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.2,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": no_speech_threshold,
        "hallucination_silence_threshold": 1.5,
    }
    if vad_filter:
        transcribe_kwargs["vad_parameters"] = {"min_silence_duration_ms": 320}
    if initial_prompt and initial_prompt.strip():
        transcribe_kwargs["initial_prompt"] = initial_prompt.strip()[:450]

    segments_iter, _ = model.transcribe(str(wav_path), **transcribe_kwargs)  # type: ignore[arg-type]

    words: list[WordTimestamp] = []
    sentences: list[SentenceSegment] = []
    s_idx = 1
    span_dur = max(0.5, float(clip_end) - float(clip_start))
    last_pct = -10.0

    for seg in segments_iter:
        seg_words: list[WordTimestamp] = []
        if getattr(seg, "words", None):
            for w in seg.words:
                txt = (w.word or "").strip()
                if not txt:
                    continue
                abs_s = float(w.start) + extract_start
                abs_e = float(w.end) + extract_start
                mid = (abs_s + abs_e) * 0.5
                if clip_start - 0.12 <= mid <= clip_end + 0.12:
                    clamped_s = max(clip_start, round(abs_s, 3))
                    clamped_e = min(clip_end, max(clamped_s + 0.02, round(abs_e, 3)))
                    wt = WordTimestamp(
                        word=txt,
                        start=clamped_s,
                        end=clamped_e,
                        probability=float(getattr(w, "probability", 1.0) or 1.0),
                    )
                    words.append(wt)
                    seg_words.append(wt)

        if seg_words:
            for ps, pe, pt in _split_segment_into_sentences(
                seg_words[0].start,
                seg_words[-1].end,
                " ".join(x.word for x in seg_words),
                seg_words,
            ):
                sentences.append(
                    SentenceSegment(
                        id=f"P{s_idx:04d}",
                        start=round(ps, 3),
                        end=round(pe, 3),
                        text=pt,
                    )
                )
                s_idx += 1

        if progress_cb is not None:
            abs_pos = float(seg.end) + extract_start
            pct = min(99.0, max(1.0, ((abs_pos - clip_start) / span_dur) * 100.0))
            if pct - last_pct >= 6.0:
                last_pct = pct
                progress_cb(pct, f"Pasada 2 precisión: {pct:.0f}% ({len(words)} palabras)")

    words.sort(key=lambda w: w.start)
    return words, sentences


def transcribe_clip_precision(
    audio_path: Path,
    clip_start: float,
    clip_end: float,
    language: Optional[str] = None,
    pad_sec: float = 1.5,
    initial_prompt: Optional[str] = None,
    work_dir: Optional[Path] = None,
    settings: Optional[Settings] = None,
    reference_sentences: Optional[list[SentenceSegment]] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> tuple[list[WordTimestamp], list[SentenceSegment]]:
    """Pass 2: Precision word-level transcription with hallucination detection, retry, and Pass 1 fallback."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

    extract_start = max(0.0, float(clip_start) - pad_sec)
    extract_end = float(clip_end) + pad_sec
    extract_dur = max(0.2, extract_end - extract_start)

    tmp_root = work_dir or Path(tempfile.gettempdir())
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="pf_pass2_",
        suffix=".wav",
        dir=tmp_root,
        delete=False,
    ) as tf:
        tmp_wav = Path(tf.name)

    overlapping_refs = (
        [
            s
            for s in reference_sentences
            if s.end > clip_start + 0.04 and s.start < clip_end - 0.04
        ]
        if reference_sentences
        else []
    )
    reference_text = " ".join(s.text.strip() for s in overlapping_refs if s.text.strip())
    span_dur = max(0.2, float(clip_end) - float(clip_start))

    try:
        cmd = [
            str(cfg.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{extract_start:.3f}",
            "-t",
            f"{extract_dur:.3f}",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(tmp_wav),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg falló extrayendo tramo para Pasada 2: {proc.stderr.strip()}")

        rt = resolve_whisper_runtime(cfg)
        model = get_whisper_model(cfg.whisper_precision_model, cfg)
        primary_beam = 1 if rt["resolved_device"] == "cpu" else 3

        # Attempt 1: Precision with anti-hallucination parameters (beam=1 on CPU for ~2.5x speed without CPU spike)
        words, sentences = _run_whisper_pass2_attempt(
            model=model,
            wav_path=tmp_wav,
            clip_start=clip_start,
            clip_end=clip_end,
            extract_start=extract_start,
            language=language,
            beam_size=primary_beam,
            vad_filter=True,
            no_speech_threshold=0.6,
            initial_prompt=initial_prompt,
            progress_cb=progress_cb,
        )
        bad, _ = detect_pass2_hallucination(
            words,
            span_dur,
            reference_text,
            clip_start=clip_start,
            clip_end=clip_end,
            reference_sentences=overlapping_refs,
        )

        # Attempt 2: Retry WITHOUT initial_prompt, relaxed VAD & beam=1 if hallucination/drop detected
        if bad:
            retry_words, retry_sents = _run_whisper_pass2_attempt(
                model=model,
                wav_path=tmp_wav,
                clip_start=clip_start,
                clip_end=clip_end,
                extract_start=extract_start,
                language=language,
                beam_size=1,
                vad_filter=False,
                no_speech_threshold=0.8,
                initial_prompt=None,
                progress_cb=progress_cb,
            )
            retry_bad, _ = detect_pass2_hallucination(
                retry_words,
                span_dur,
                reference_text,
                clip_start=clip_start,
                clip_end=clip_end,
                reference_sentences=overlapping_refs,
            )
            if not retry_bad or len(retry_words) > len(words):
                words, sentences = retry_words, retry_sents
                bad = retry_bad

        # Attempt 3: Guaranteed Pass 1 word-aligned fallback if Pass 2 still hallucinated
        if bad and overlapping_refs:
            words = _fallback_words_from_sentences(clip_start, clip_end, overlapping_refs)
            sentences = [
                SentenceSegment(id=f"P{i + 1:04d}", start=s.start, end=s.end, text=s.text, speaker=s.speaker)
                for i, s in enumerate(overlapping_refs)
            ]

        # Surgical final guarantee: never leave a >=4s speech gap without subtitle words
        if overlapping_refs:
            words, patched_gaps = _patch_missing_word_gaps(
                words,
                clip_start=clip_start,
                clip_end=clip_end,
                reference_sentences=overlapping_refs,
                min_gap_sec=4.0,
            )
            if patched_gaps > 0 and words:
                sentences = [
                    SentenceSegment(id=f"P{i + 1:04d}", start=ps, end=pe, text=pt)
                    for i, (ps, pe, pt) in enumerate(
                        _split_segment_into_sentences(
                            words[0].start,
                            words[-1].end,
                            " ".join(w.word for w in words),
                            words,
                        )
                    )
                ]

        return words, sentences
    finally:
        try:
            if tmp_wav.exists():
                tmp_wav.unlink()
        except OSError:
            pass


def transcribe_spans_precision(
    audio_path: Path,
    spans: list[tuple[float, float]],
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
    work_dir: Optional[Path] = None,
    settings: Optional[Settings] = None,
    reference_sentences: Optional[list[SentenceSegment]] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> list[WordTimestamp]:
    """Run Pass 2 precision Whisper across one or more narrative spans, validating each span."""
    ordered = sorted((max(0.0, float(s)), max(float(s) + 0.1, float(e))) for s, e in spans)
    if not ordered:
        return []

    merged: list[tuple[float, float]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s <= pe + 3.0:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))

    total_dur = max(0.5, sum(e - s for s, e in merged))
    done_dur = 0.0
    all_words: list[WordTimestamp] = []
    for s, e in merged:
        span_d = max(0.2, e - s)

        def _on_span_pct(p: float, m: str, base_d: float = done_dur, cur_d: float = span_d) -> None:
            if progress_cb is not None:
                overall = min(99.0, ((base_d + (p / 100.0) * cur_d) / total_dur) * 100.0)
                progress_cb(overall, m)

        w_list, _ = transcribe_clip_precision(
            audio_path=audio_path,
            clip_start=s,
            clip_end=e,
            language=language,
            initial_prompt=initial_prompt,
            work_dir=work_dir,
            settings=settings,
            reference_sentences=reference_sentences,
            progress_cb=_on_span_pct if progress_cb is not None else None,
        )
        all_words.extend(w_list)
        done_dur += span_d

    all_words.sort(key=lambda w: w.start)
    # Deduplicate near-identical overlapping boundary tokens
    deduped: list[WordTimestamp] = []
    for w in all_words:
        if deduped and abs(w.start - deduped[-1].start) < 0.08 and w.word.lower() == deduped[-1].word.lower():
            continue
        deduped.append(w)
    return deduped
