"""Surgical audio editing: silence trimming, word censorship with crossfade, EDL, and TimeMapper."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from backend.config import Settings, get_settings
from backend.models.job import ChapterMark, EDLPlan, EDLSegment, WordTimestamp
from backend.models.profile import AudioEditConfig


def normalize_token(text: str) -> str:
    """Normalize text for case-insensitive, accent-insensitive, punctuation-free matching."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower().strip()
    tokens = re.findall(r"[a-z0-9ñ]+", lowered)
    return " ".join(tokens)


def _merge_intervals(
    intervals: Iterable[tuple[float, float]],
    gap_tolerance: float = 0.02,
) -> list[tuple[float, float]]:
    ordered = sorted((float(s), float(e)) for s, e in intervals if e > s)
    if not ordered:
        return []
    merged: list[tuple[float, float]] = [ordered[0]]
    for s, e in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e + gap_tolerance:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged


def _subtract_intervals(
    base: tuple[float, float],
    blocks: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Subtract sorted blocking intervals from a single base interval."""
    cur_s, cur_e = base
    if cur_e <= cur_s:
        return []
    remaining: list[tuple[float, float]] = []
    cursor = cur_s
    for bs, be in blocks:
        if be <= cursor:
            continue
        if bs >= cur_e:
            break
        if bs > cursor:
            remaining.append((cursor, min(bs, cur_e)))
        cursor = max(cursor, be)
        if cursor >= cur_e:
            break
    if cursor < cur_e:
        remaining.append((cursor, cur_e))
    return remaining


_EDGE_NON_TERMINAL_ABBREVS: frozenset[str] = frozenset(
    {"dr.", "sr.", "sra.", "srta.", "prof.", "etc.", "ee.", "uu.", "p.ej.", "ej.", "vs.", "mr.", "mrs."}
)


def _clean_ordered_spans(
    spans: Iterable[tuple[float, float]],
    gap_tolerance: float = 0.04,
) -> list[tuple[float, float]]:
    """Preserve exact playback order while merging adjacent extensions and stripping any prior overlap."""
    raw = [(max(0.0, float(s)), float(e)) for s, e in spans if float(e) > float(s) + 0.05]
    if not raw:
        return []

    accepted: list[tuple[float, float]] = []
    for sp_s, sp_e in raw:
        if not accepted:
            accepted.append((round(sp_s, 4), round(sp_e, 4)))
            continue

        prev_s, prev_e = accepted[-1]
        if prev_s - 0.02 <= sp_s <= prev_e + gap_tolerance:
            prior_blocks = _merge_intervals(
                [(max(0.0, a - gap_tolerance), b + gap_tolerance) for a, b in accepted[:-1]],
                gap_tolerance=0.0,
            )
            pieces = _subtract_intervals((sp_s, sp_e), prior_blocks) if prior_blocks else [(sp_s, sp_e)]
            for ps, pe in pieces:
                if pe - ps <= 0.05:
                    continue
                cur_ps, cur_pe = accepted[-1]
                if cur_ps - 0.02 <= ps <= cur_pe + gap_tolerance:
                    accepted[-1] = (round(min(cur_ps, ps), 4), round(max(cur_pe, pe), 4))
                else:
                    accepted.append((round(ps, 4), round(pe, 4)))
        else:
            all_blocks = _merge_intervals(
                [(max(0.0, a - gap_tolerance), b + gap_tolerance) for a, b in accepted],
                gap_tolerance=0.0,
            )
            for ps, pe in _subtract_intervals((sp_s, sp_e), all_blocks):
                if pe - ps > 0.05:
                    accepted.append((round(ps, 4), round(pe, 4)))

    return accepted


def find_censored_intervals(
    words: list[WordTimestamp],
    censored_terms: list[str],
    pad_sec: float = 0.025,
) -> tuple[list[tuple[float, float]], set[int], list[str]]:
    """Find exact intervals for single censored words or multi-word censored phrases."""
    if not words or not censored_terms:
        return [], set(), []

    parsed_terms: list[tuple[str, list[str]]] = []
    for raw in censored_terms:
        norm = normalize_token(raw)
        if norm:
            parts = norm.split()
            parsed_terms.append((raw.strip(), parts))

    if not parsed_terms:
        return [], set(), []

    norm_words = [normalize_token(w.word) for w in words]
    censored_indices: set[int] = set()
    matched_terms: list[str] = []
    raw_spans: list[tuple[int, int]] = []

    i = 0
    n = len(words)
    # Sort longer phrases first so multi-word matches take precedence
    parsed_terms.sort(key=lambda item: len(item[1]), reverse=True)

    while i < n:
        matched_here = False
        for orig_term, parts in parsed_terms:
            k = len(parts)
            if i + k <= n and norm_words[i : i + k] == parts:
                for idx in range(i, i + k):
                    censored_indices.add(idx)
                raw_spans.append((i, i + k - 1))
                if orig_term not in matched_terms:
                    matched_terms.append(orig_term)
                i += k
                matched_here = True
                break
        if not matched_here:
            i += 1

    intervals: list[tuple[float, float]] = []
    for start_idx, end_idx in raw_spans:
        raw_s = words[start_idx].start - pad_sec
        raw_e = words[end_idx].end + pad_sec

        # Clamp against adjacent non-censored speech so neighbors are not clipped
        if start_idx > 0 and (start_idx - 1) not in censored_indices:
            prev_end = words[start_idx - 1].end
            if prev_end <= words[start_idx].start:
                raw_s = max(raw_s, prev_end)
            else:
                raw_s = max(raw_s, words[start_idx].start)
        if end_idx + 1 < n and (end_idx + 1) not in censored_indices:
            next_start = words[end_idx + 1].start
            if next_start >= words[end_idx].end:
                raw_e = min(raw_e, next_start)
            else:
                raw_e = min(raw_e, words[end_idx].end)

        raw_s = max(0.0, raw_s)
        if raw_e > raw_s + 0.01:
            intervals.append((round(raw_s, 4), round(raw_e, 4)))

    return _merge_intervals(intervals, gap_tolerance=0.02), censored_indices, matched_terms


class TimeMapper:
    """Deterministic mapping from original audio timestamps to edited EDL timeline."""

    def __init__(self, edl: EDLPlan) -> None:
        self.edl = edl
        self.segments = edl.segments
        self.total_duration = edl.edited_duration

    def map_time(self, orig_t: float) -> float:
        """Map an original timeline timestamp to the clean edited timeline (supporting non-linear cold opens)."""
        if not self.segments:
            return 0.0

        t = float(orig_t)
        for seg in self.segments:
            if seg.orig_start <= t <= seg.orig_end:
                mapped = seg.out_start + (t - seg.orig_start)
                return round(min(self.total_duration, max(0.0, mapped)), 4)

        if len(self.segments) == 1 and t <= self.segments[0].orig_start:
            return 0.0

        for idx, seg in enumerate(self.segments):
            if idx > 0 and self.segments[idx - 1].orig_end < t < seg.orig_start:
                # Fell inside a cut gap between adjacent chronological segments
                seam = seg.out_start + (seg.crossfade_in_sec * 0.5)
                return round(min(self.total_duration, max(0.0, seam)), 4)

        # Fallback: snap to closest segment boundary
        best_seg = min(
            self.segments,
            key=lambda sg: min(abs(t - sg.orig_start), abs(t - sg.orig_end)),
        )
        if abs(t - best_seg.orig_start) <= abs(t - best_seg.orig_end):
            return round(best_seg.out_start, 4)
        return round(best_seg.out_end, 4)

    def map_words(self, words: list[WordTimestamp], censored_indices: Optional[set[int]] = None) -> list[WordTimestamp]:
        """Remap surviving words onto the edited timeline (supporting chronological & non-linear cold-open stitching)."""
        blocked = censored_indices or set()
        mapped_words: list[WordTimestamp] = []

        # Iterate EDL segments in output order so non-linear / repeated cold-open spans map cleanly in output sequence
        for seg in self.segments:
            seg_len = seg.orig_end - seg.orig_start
            for idx, w in enumerate(words):
                if idx in blocked or w.censored:
                    continue
                mid = (w.start + w.end) * 0.5
                if not ((seg.orig_start - 0.04) <= mid <= (seg.orig_end + 0.04)):
                    continue
                rel_s = max(0.0, w.start - seg.orig_start)
                rel_e = max(rel_s + 0.02, min(seg_len, w.end - seg.orig_start))
                new_s = round(min(self.total_duration, max(0.0, seg.out_start + rel_s)), 4)
                new_e = round(min(self.total_duration, max(new_s + 0.02, seg.out_start + rel_e)), 4)
                if new_s >= self.total_duration:
                    continue
                if mapped_words and abs(new_s - mapped_words[-1].start) < 0.05 and w.word.lower() == mapped_words[-1].word.lower():
                    continue
                mapped_words.append(
                    WordTimestamp(
                        word=w.word,
                        start=new_s,
                        end=new_e,
                        probability=w.probability,
                        censored=False,
                        speaker=w.speaker,
                    )
                )
        mapped_words.sort(key=lambda x: x.start)
        return mapped_words

    def map_chapters(self, chapters: list[ChapterMark]) -> list[ChapterMark]:
        """Recalculate chapter timestamps onto the edited timeline."""
        out: list[ChapterMark] = []
        for ch in chapters:
            out.append(
                ch.model_copy(
                    update={"edited_time_sec": round(self.map_time(ch.orig_time_sec), 2)}
                )
            )
        return out


def snap_spans_to_word_edges(
    spans: list[tuple[float, float]],
    words: list[WordTimestamp],
    pad_before_sec: float = 0.055,
    pad_after_sec: float = 0.085,
    min_word_coverage: float = 0.45,
    preserve_order: bool = True,
) -> list[tuple[float, float]]:
    """Snap edit spans to intact word boundaries so cuts never slice a word mid-syllable or repeat audio."""
    raw = [(max(0.0, float(s)), float(e)) for s, e in spans if float(e) > float(s) + 0.05]
    if not preserve_order:
        raw.sort()
    if not raw:
        return []
    if not words:
        return _clean_ordered_spans(raw, gap_tolerance=0.05)

    ordered_words = sorted((w for w in words if w.end > w.start), key=lambda w: (w.start, w.end))
    if not ordered_words:
        return _clean_ordered_spans(raw, gap_tolerance=0.05)

    snapped: list[tuple[float, float]] = []
    for sp_s, sp_e in raw:
        # Locate words near/inside [sp_s, sp_e]
        new_s = sp_s
        for idx_w, w in enumerate(ordered_words):
            if w.end <= sp_s - 0.35:
                continue
            if w.start > sp_e:
                break
            dur = max(0.04, w.end - w.start)
            if w.start <= sp_s < w.end:
                cov = (w.end - sp_s) / dur
                allow_onset_snap = (sp_s - w.start) <= 0.18 and sp_s < (w.end - 0.12)
                if cov >= min_word_coverage or allow_onset_snap:
                    new_s = max(0.0, w.start - pad_before_sec)
                else:
                    # Drop clipped trailing fragment; find next full word
                    if idx_w + 1 < len(ordered_words) and ordered_words[idx_w + 1].start < sp_e:
                        new_s = max(0.0, ordered_words[idx_w + 1].start - pad_before_sec)
                break
            if w.start >= sp_s:
                new_s = max(sp_s - 0.02, w.start - pad_before_sec) if (w.start - sp_s) <= 0.25 else sp_s
                break

        new_e = sp_e
        for idx in range(len(ordered_words) - 1, -1, -1):
            w = ordered_words[idx]
            if w.start >= sp_e + 0.35:
                continue
            if w.end <= new_s:
                break
            dur = max(0.04, w.end - w.start)
            if w.start < sp_e <= w.end:
                cov = (sp_e - w.start) / dur
                if cov >= 0.30 or (w.end - sp_e) <= 0.22:
                    new_e = w.end + pad_after_sec
                elif idx - 1 >= 0 and ordered_words[idx - 1].end > new_s + 0.15:
                    new_e = ordered_words[idx - 1].end + pad_after_sec
                break
            if w.end <= sp_e:
                new_e = min(sp_e + 0.02, w.end + pad_after_sec) if (sp_e - w.end) <= 0.25 else sp_e
                break

        if new_e <= new_s + 0.08:
            new_s, new_e = sp_s, sp_e

        if snapped and snapped[-1][0] - 0.06 <= new_s <= snapped[-1][1] + 0.06 and new_e >= snapped[-1][0] - 0.06:
            prev_s, prev_e = snapped[-1]
            prior_blocks = _merge_intervals([(max(0.0, a - 0.02), b + 0.02) for a, b in snapped[:-1]], gap_tolerance=0.0)
            pieces = _subtract_intervals((new_s, new_e), prior_blocks) if prior_blocks else [(new_s, new_e)]
            for ps, pe in pieces:
                if pe - ps <= 0.08:
                    continue
                cur_ps, cur_pe = snapped[-1]
                if cur_ps - 0.06 <= ps <= cur_pe + 0.06:
                    snapped[-1] = (round(min(cur_ps, ps), 4), round(max(cur_pe, pe), 4))
                else:
                    snapped.append((round(max(0.0, ps), 4), round(pe, 4)))
        elif snapped:
            all_blocks = _merge_intervals([(max(0.0, a - 0.02), b + 0.02) for a, b in snapped], gap_tolerance=0.0)
            for ps, pe in _subtract_intervals((new_s, new_e), all_blocks):
                if pe - ps > 0.08:
                    snapped.append((round(max(0.0, ps), 4), round(pe, 4)))
        else:
            snapped.append((round(max(0.0, new_s), 4), round(new_e, 4)))

    return snapped


def trim_pass2_edge_orphans(
    spans: list[tuple[float, float]],
    words: list[WordTimestamp],
) -> list[tuple[float, float]]:
    """Trim lowercase/fragment orphan tails at the start or dangling cut-offs at the end of EVERY span."""
    if not spans or not words:
        return spans

    ordered_words = sorted((w for w in words if w.end > w.start), key=lambda w: (w.start, w.end))
    if not ordered_words:
        return spans

    out = [(float(s), float(e)) for s, e in spans]

    for span_i in range(len(out)):
        sp_s, sp_e = out[span_i]
        orig_dur = sp_e - sp_s
        if orig_dur < 2.2:
            continue

        # 1. Trim leading lowercase orphan tail on this span
        sp_words = [
            w for w in ordered_words
            if (sp_s - 0.05) <= (w.start + w.end) * 0.5 <= (sp_e + 0.05)
        ]
        if len(sp_words) >= 3:
            first_alpha = next((ch for ch in sp_words[0].word if ch.isalpha()), "")
            if first_alpha and first_alpha.islower():
                limit_k = min(7, len(sp_words) - 2)
                for k in range(0, limit_k + 1):
                    w = sp_words[k]
                    if (w.start - sp_s) > 3.8:
                        break
                    tok = w.word.strip()
                    if tok.endswith((".", "?", "!")) and tok.lower() not in _EDGE_NON_TERMINAL_ABBREVS:
                        nxt = sp_words[k + 1]
                        nxt_raw = nxt.word.strip().lstrip("\"'(- ")
                        nxt_first = next((ch for ch in nxt_raw if ch.isalpha()), "")
                        if nxt_raw.startswith(("¿", "¡")) or (nxt_first and nxt_first.isupper()):
                            cand_s = round(max(sp_s, nxt.start - 0.055), 4)
                            new_dur = sp_e - cand_s
                            if new_dur >= 1.8 and new_dur >= orig_dur * 0.55:
                                out[span_i] = (cand_s, sp_e)
                                sp_s = cand_s
                            break

        # 2. Trim dangling incomplete tail on this span
        sp_s, sp_e = out[span_i]
        sp_words = [
            w for w in ordered_words
            if (sp_s - 0.05) <= (w.start + w.end) * 0.5 <= (sp_e + 0.05)
        ]
        if len(sp_words) >= 3:
            last_tok = sp_words[-1].word.strip()
            if not last_tok.endswith((".", "?", "!")):
                low_k = max(-1, len(sp_words) - 8)
                for k in range(len(sp_words) - 2, low_k, -1):
                    w = sp_words[k]
                    if (sp_e - w.end) > 2.8:
                        break
                    tok = w.word.strip()
                    if tok.endswith((".", "?", "!")) and tok.lower() not in _EDGE_NON_TERMINAL_ABBREVS:
                        cand_e = round(min(sp_e, w.end + 0.085), 4)
                        new_dur = cand_e - sp_s
                        if new_dur >= 1.8 and new_dur >= (sp_e - sp_s) * 0.65:
                            out[span_i] = (sp_s, cand_e)
                        break

    return out


def verify_edl_no_duplicates(
    edl: EDLPlan,
    mapped_words: list[WordTimestamp],
) -> list[str]:
    """Verify pairwise non-overlapping EDL spans and detect repeated phrases/ngrams."""
    issues: list[str] = []
    segs = edl.segments
    for i in range(len(segs)):
        a = segs[i]
        for j in range(i + 1, len(segs)):
            b = segs[j]
            overlap = min(a.orig_end, b.orig_end) - max(a.orig_start, b.orig_start)
            if overlap > 0.03:
                issues.append(
                    f"Solapamiento EDL entre seg {a.index} ([{a.orig_start:.2f}s, {a.orig_end:.2f}s]) "
                    f"y seg {b.index} ([{b.orig_start:.2f}s, {b.orig_end:.2f}s])."
                )

    tokens = [normalize_token(w.word) for w in mapped_words if normalize_token(w.word)]
    if len(tokens) >= 6:
        seen_trigrams: dict[tuple[str, str, str], int] = {}
        for i in range(len(tokens) - 2):
            tri = (tokens[i], tokens[i + 1], tokens[i + 2])
            # Ignore filler 3-grams of tiny words (<=2 chars each)
            if sum(len(x) for x in tri) <= 7:
                continue
            prev_i = seen_trigrams.get(tri)
            if prev_i is not None and (i - prev_i) <= 8:
                issues.append(f"Posible repetición de frase detectada tras corte EDL: '{' '.join(tri)}'.")
                break
            seen_trigrams[tri] = i
    return issues


def _intervals_overlap(a: tuple[float, float], b: tuple[float, float], min_overlap: float = 0.05) -> bool:
    return (min(a[1], b[1]) - max(a[0], b[0])) >= min_overlap


def build_edl_for_spans(
    clip_id: str,
    spans: list[tuple[float, float]],
    words: list[WordTimestamp],
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    non_speech_intervals: Optional[list[tuple[float, float]]] = None,
    music_intervals: Optional[list[tuple[float, float]]] = None,
) -> tuple[EDLPlan, TimeMapper, list[WordTimestamp]]:
    """Compute exact EDL across stitched narrative spans, removing silences AND music interludes organically."""
    clean_spans = _clean_ordered_spans(spans, gap_tolerance=0.04)
    if not clean_spans:
        raise ValueError("build_edl_for_spans requiere al menos un tramo válido.")

    censored_cuts, censored_indices, matched_terms = find_censored_intervals(
        words,
        audio_config.censored_words,
    )

    speech_guards = _merge_intervals(
        [
            (max(0.0, w.start - 0.025), w.end + 0.025)
            for idx, w in enumerate(words)
            if idx not in censored_indices and w.end > w.start
        ],
        gap_tolerance=0.01,
    )

    music_ivs = [(float(s), float(e)) for s, e in (music_intervals or []) if float(e) > float(s)]
    unified_raw_gaps = _merge_intervals(
        [*(silence_intervals or []), *(non_speech_intervals or [])],
        gap_tolerance=0.04,
    )

    # Kept items carry: (orig_start, orig_end, span_idx, preceded_by_music_cut)
    kept_with_origin: list[tuple[float, float, int, bool]] = []
    silences_removed_count = 0
    silences_removed_sec = 0.0
    music_gaps_removed_count = 0
    music_gaps_removed_sec = 0.0
    censored_hits_count = 0

    for sp_idx, (sp_start, sp_end) in enumerate(clean_spans):
        sp_silence_cuts: list[tuple[float, float]] = []
        sp_music_cuts: list[tuple[float, float]] = []

        if audio_config.remove_silences:
            keep_sil_base = max(0.0, float(audio_config.keep_silence_sec))
            min_sil = max(float(audio_config.min_silence_sec), keep_sil_base + 0.06)

            # Supplement with word-to-word dead gaps >= 2.0s inside the span (only if not already covered)
            span_words = sorted(
                (
                    w
                    for idx_w, w in enumerate(words)
                    if idx_w not in censored_indices
                    and w.end > w.start
                    and (sp_start - 0.05) <= (w.start + w.end) * 0.5 <= (sp_end + 0.05)
                ),
                key=lambda x: x.start,
            )
            extra_word_gaps: list[tuple[float, float]] = []
            for wi in range(1, len(span_words)):
                g_s = span_words[wi - 1].end
                g_e = span_words[wi].start
                if (g_e - g_s) >= max(2.0, min_sil + 0.4):
                    if not any(_intervals_overlap((g_s, g_e), ug, min_overlap=0.15) for ug in unified_raw_gaps):
                        if not any(_intervals_overlap((g_s, g_e), cc, min_overlap=0.10) for cc in censored_cuts):
                            extra_word_gaps.append((round(g_s + 0.04, 4), round(g_e - 0.04, 4)))

            cand_gaps = _merge_intervals([*unified_raw_gaps, *extra_word_gaps], gap_tolerance=0.04)

            for raw_s, raw_e in cand_gaps:
                cs = max(sp_start, float(raw_s))
                ce = min(sp_end, float(raw_e))
                if ce <= cs:
                    continue
                sub_silences = _subtract_intervals((cs, ce), speech_guards) if speech_guards else [(cs, ce)]
                for ss, se in sub_silences:
                    dur = se - ss
                    overlaps_music = any(_intervals_overlap((ss, se), mv, min_overlap=0.20) for mv in music_ivs)
                    is_long_interlude = overlaps_music or dur >= 2.2
                    keep_total = max(keep_sil_base, 0.22) if is_long_interlude else keep_sil_base
                    eff_min_dur = max(min_sil, keep_total + 0.06)
                    if dur < eff_min_dur:
                        continue
                    if ss <= sp_start + 0.01:
                        cut_s = ss
                        cut_e = se - min(0.08, keep_total * 0.5)
                    elif se >= sp_end - 0.01:
                        cut_s = ss + min(0.08, keep_total * 0.5)
                        cut_e = se
                    else:
                        cut_s = ss + (keep_total * 0.5)
                        cut_e = se - (keep_total * 0.5)
                    if cut_e > cut_s + 0.04:
                        cut_pair = (round(cut_s, 4), round(cut_e, 4))
                        cut_dur = cut_e - cut_s
                        if overlaps_music:
                            sp_music_cuts.append(cut_pair)
                            music_gaps_removed_sec += cut_dur
                            music_gaps_removed_count += 1
                        else:
                            if is_long_interlude:
                                sp_music_cuts.append(cut_pair)
                            else:
                                sp_silence_cuts.append(cut_pair)
                            silences_removed_sec += cut_dur
                            silences_removed_count += 1

        sp_censored = [
            (max(sp_start, cs), min(sp_end, ce))
            for cs, ce in censored_cuts
            if ce > sp_start and cs < sp_end
        ]
        censored_hits_count += len(sp_censored)

        all_cuts = _merge_intervals([*sp_silence_cuts, *sp_music_cuts, *sp_censored], gap_tolerance=0.03)
        cursor = sp_start
        span_kept: list[tuple[float, float, bool]] = []
        for cs, ce in all_cuts:
            cs_c = max(sp_start, cs)
            ce_c = min(sp_end, ce)
            is_mus = any(_intervals_overlap((cs_c, ce_c), mc, min_overlap=0.04) for mc in sp_music_cuts)
            if cs_c > cursor + 0.04:
                prev_cut_mus = bool(
                    span_kept
                    and any(_intervals_overlap((span_kept[-1][1], cursor), mc, min_overlap=0.04) for mc in sp_music_cuts)
                )
                span_kept.append((round(cursor, 4), round(cs_c, 4), prev_cut_mus))
            cursor = max(cursor, ce_c)
            if span_kept and is_mus:
                # Next segment will check gap [span_kept[-1][1], next_start]
                pass
        if sp_end > cursor + 0.04:
            prev_cut_mus = bool(
                span_kept
                and any(_intervals_overlap((span_kept[-1][1], cursor), mc, min_overlap=0.04) for mc in sp_music_cuts)
            )
            span_kept.append((round(cursor, 4), round(sp_end, 4), prev_cut_mus))

        if not span_kept:
            span_kept = [(round(sp_start, 4), round(sp_end, 4), False)]
        for ks, ke, pmus in span_kept:
            kept_with_origin.append((ks, ke, sp_idx, pmus))

    cf_cut = max(0.005, float(audio_config.crossfade_ms) / 1000.0)
    cf_stitch = max(cf_cut, 0.045)  # smoother crossfade when joining distinct narrative spans
    cf_music = max(cf_cut, 0.13)    # equal-power crossfade when extirpating background music
    segments: list[EDLSegment] = []
    out_cursor = 0.0
    prev_sp_idx = -1

    for idx, (seg_s, seg_e, sp_idx, preceded_by_music) in enumerate(kept_with_origin):
        dur = seg_e - seg_s
        if idx == 0:
            cf = 0.0
            curve: str = "tri"
            out_s = 0.0
        else:
            prev_seg = segments[-1]
            prev_dur = prev_seg.out_end - prev_seg.out_start
            if preceded_by_music:
                nom = cf_music
                curve = "qsin"
            elif sp_idx != prev_sp_idx:
                nom = cf_stitch
                curve = "qsin" if music_ivs else "tri"
            else:
                nom = cf_cut
                curve = "tri"
            cf = round(min(nom, prev_dur * 0.25, dur * 0.25), 3)
            cf = max(0.004, cf) if dur >= 0.02 and prev_dur >= 0.02 else 0.0
            out_s = round(prev_seg.out_end - cf, 4)

        out_e = round(out_s + dur, 4)
        segments.append(
            EDLSegment(
                index=idx,
                orig_start=seg_s,
                orig_end=seg_e,
                out_start=out_s,
                out_end=out_e,
                crossfade_in_sec=cf,
                crossfade_curve="qsin" if curve == "qsin" else "tri",
            )
        )
        out_cursor = out_e
        prev_sp_idx = sp_idx

    raw_orig_dur = sum(e - s for s, e in clean_spans)
    edl = EDLPlan(
        clip_id=clip_id,
        orig_clip_start=round(min(s for s, _ in clean_spans), 4),
        orig_clip_end=round(max(e for _, e in clean_spans), 4),
        orig_duration=round(raw_orig_dur, 4),
        edited_duration=round(out_cursor, 4),
        silences_removed_count=silences_removed_count,
        silences_removed_sec=round(silences_removed_sec, 3),
        music_gaps_removed_count=music_gaps_removed_count,
        music_gaps_removed_sec=round(music_gaps_removed_sec, 3),
        censored_hits_count=censored_hits_count,
        censored_terms_matched=matched_terms,
        segments=segments,
    )

    mapper = TimeMapper(edl)
    mapped_words = mapper.map_words(words, censored_indices=censored_indices)
    return edl, mapper, mapped_words


def build_edl(
    clip_id: str,
    clip_start: float,
    clip_end: float,
    words: list[WordTimestamp],
    silence_intervals: list[tuple[float, float]],
    audio_config: AudioEditConfig,
    non_speech_intervals: Optional[list[tuple[float, float]]] = None,
    music_intervals: Optional[list[tuple[float, float]]] = None,
) -> tuple[EDLPlan, TimeMapper, list[WordTimestamp]]:
    """Compute exact EDL for a single contiguous span."""
    return build_edl_for_spans(
        clip_id=clip_id,
        spans=[(clip_start, clip_end)],
        words=words,
        silence_intervals=silence_intervals,
        audio_config=audio_config,
        non_speech_intervals=non_speech_intervals,
        music_intervals=music_intervals,
    )


def _build_acrossfade_tree(
    segments: list[EDLSegment],
    lines: list[str],
    counter_ref: list[int],
) -> str:
    """Recursively join segments as a balanced binary tree preserving exact seam crossfades."""
    items: list[tuple[str, int]] = [(f"seg{s.index}", s.index) for s in segments]

    while len(items) > 1:
        next_items: list[tuple[str, int]] = []
        i = 0
        while i < len(items):
            if i + 1 >= len(items):
                next_items.append(items[i])
                i += 1
                continue
            left_lbl, _ = items[i]
            right_lbl, right_first_seg_idx = items[i + 1]
            cf = segments[right_first_seg_idx].crossfade_in_sec
            curve = getattr(segments[right_first_seg_idx], "crossfade_curve", "tri") or "tri"
            counter_ref[0] += 1
            out_lbl = f"m{counter_ref[0]}"
            if cf > 0:
                lines.append(
                    f"[{left_lbl}][{right_lbl}]acrossfade=d={cf:.3f}:c1={curve}:c2={curve}[{out_lbl}]"
                )
            else:
                lines.append(f"[{left_lbl}][{right_lbl}]concat=n=2:v=0:a=1[{out_lbl}]")
            next_items.append((out_lbl, items[i][1]))
            i += 2
        items = next_items

    return items[0][0]


def render_clean_audio(
    source_audio: Path,
    edl_plan: EDLPlan,
    output_wav: Path,
    audio_config: AudioEditConfig,
    bg_music_path: Optional[Path] = None,
    preview_mp3_path: Optional[Path] = None,
    settings: Optional[Settings] = None,
) -> Path:
    """Render clean EDL audio with micro-crossfades, -14 LUFS normalization, and optional bg music."""
    cfg = settings or get_settings()
    if not cfg.ffmpeg_path or not cfg.ffmpeg_path.exists():
        raise RuntimeError("FFmpeg no encontrado en el sistema.")

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    segments = edl_plan.segments
    if not segments:
        raise ValueError("EDLPlan no contiene segmentos para renderizar.")

    filter_lines: list[str] = []
    for seg in segments:
        seg_dur = max(0.05, float(seg.orig_end) - float(seg.orig_start))
        edge_fade_in = "afade=t=in:st=0:d=0.012," if (seg.index == 0 or seg.crossfade_in_sec <= 0.0) and seg_dur >= 0.06 else ""
        filter_lines.append(
            f"[0:a]atrim=start={seg.orig_start:.4f}:end={seg.orig_end:.4f},"
            f"asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"{edge_fade_in}asetpts=PTS-STARTPTS[seg{seg.index}]"
        )

    counter = [0]
    joined_label = _build_acrossfade_tree(segments, filter_lines, counter)

    target_lufs = float(audio_config.target_lufs)
    fade_out_st = max(0.0, float(edl_plan.edited_duration) - 0.018)
    tail_fade = f"afade=t=out:st={fade_out_st:.4f}:d=0.018," if edl_plan.edited_duration > 0.08 else ""
    if audio_config.level_multi_speaker_voices:
        filter_lines.append(
            f"[{joined_label}]{tail_fade}dynaudnorm=f=150:g=15:p=0.9:m=10,"
            f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11[voice_norm]"
        )
    else:
        filter_lines.append(
            f"[{joined_label}]{tail_fade}loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11[voice_norm]"
        )

    inputs: list[str] = ["-i", str(source_audio)]
    final_label = "voice_norm"

    if bg_music_path and bg_music_path.exists():
        inputs.extend(["-stream_loop", "-1", "-i", str(bg_music_path)])
        vol_db = float(audio_config.bg_music_volume_db)
        filter_lines.append(
            f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={vol_db:.1f}dB[bgm]"
        )
        filter_lines.append(
            "[voice_norm][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
        )
        final_label = "aout"

    filter_script_text = ";\n".join(filter_lines)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="pf_edl_",
        suffix=".txt",
        dir=output_wav.parent,
        delete=False,
    ) as tf:
        tf.write(filter_script_text)
        script_path = Path(tf.name)

    try:
        base_cmd = [
            str(cfg.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            str(cfg.ffmpeg_threads),
            *inputs,
        ]
        tail_cmd = [
            "-map",
            f"[{final_label}]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_wav),
        ]
        # FFmpeg 7.1+/9.x uses -/filter_complex <file>; older FFmpeg uses -filter_complex_script
        proc = subprocess.run(
            [*base_cmd, "-/filter_complex", str(script_path), *tail_cmd],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if proc.returncode != 0 and "Unrecognized option '/filter_complex'" in proc.stderr:
            proc = subprocess.run(
                [*base_cmd, "-filter_complex_script", str(script_path), *tail_cmd],
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg falló renderizando audio limpio: {proc.stderr.strip()}")

        if preview_mp3_path is not None:
            preview_mp3_path.parent.mkdir(parents=True, exist_ok=True)
            mp3_cmd = [
                str(cfg.ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(output_wav),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(preview_mp3_path),
            ]
            mp3_proc = subprocess.run(mp3_cmd, capture_output=True, text=True, timeout=120, check=False)
            if mp3_proc.returncode != 0:
                raise RuntimeError(f"FFmpeg falló creando MP3 preview: {mp3_proc.stderr.strip()}")

        return output_wav
    finally:
        try:
            if script_path.exists():
                script_path.unlink()
        except OSError:
            pass
