"""Unit tests for strict Public Domain/CC0 archive verification, orientation validation, and zero gap-fillers."""

from __future__ import annotations

from pathlib import Path

from backend.core.archive_fetcher import is_strictly_public_domain
from backend.core.stock_fetcher import StockFetcher, _is_valid_resolution, write_clip_license_manifest
from backend.core.visual_director import _normalize_storyboard_coverage
from backend.models.job import StoryboardShot, WordTimestamp


def test_strict_public_domain_license_filter() -> None:
    # Allowed Public Domain / CC0
    assert is_strictly_public_domain("Public domain", "https://creativecommons.org/publicdomain/mark/1.0/")[0] is True
    assert is_strictly_public_domain("CC0", "https://creativecommons.org/publicdomain/zero/1.0/")[0] is True
    assert is_strictly_public_domain("PD-USGov", "", "United States federal government work")[0] is True
    assert is_strictly_public_domain("PD-old-100")[0] is True

    # Rejected non-PD / attribution / sharealike / noncommercial / fair use / restricted
    assert is_strictly_public_domain("CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/")[0] is False
    assert is_strictly_public_domain("CC BY-SA 3.0")[0] is False
    assert is_strictly_public_domain("CC-BY-NC-ND 2.0")[0] is False
    assert is_strictly_public_domain("Fair use")[0] is False
    assert is_strictly_public_domain("GFDL")[0] is False
    assert is_strictly_public_domain("Public domain", "", "", "False", "Personality rights")[0] is False
    assert is_strictly_public_domain("", "", "", "True", "")[0] is False


def test_strict_orientation_and_hd_check() -> None:
    # 9:16 portrait requires h > w and >= 720p short side
    assert _is_valid_resolution(1080, 1920, "portrait") is True
    assert _is_valid_resolution(540, 960, "portrait") is False
    assert _is_valid_resolution(1920, 1080, "portrait") is False

    # 16:9 landscape requires w > h and >= 720p short side
    assert _is_valid_resolution(1920, 1080, "landscape") is True
    assert _is_valid_resolution(1080, 1920, "landscape") is False


def test_storyboard_normalization_absorbs_gaps_without_filler_flashes() -> None:
    words = [
        WordTimestamp(word="Uno", start=0.2, end=0.8),
        WordTimestamp(word="dos.", start=1.0, end=4.1),
        WordTimestamp(word="Tres", start=4.6, end=5.5),
        WordTimestamp(word="cuatro.", start=6.0, end=9.8),
    ]
    shots = [
        StoryboardShot(id="s1", start=0.0, end=3.8, source_type="stock_video", stock_query="airfield night"),
        # Gap between 3.8 and 4.4 must be absorbed cleanly without adding a filler shot!
        StoryboardShot(id="s2", start=4.4, end=10.0, source_type="stock_video", stock_query="bomber cockpit"),
    ]
    norm = _normalize_storyboard_coverage(
        shots,
        duration=10.0,
        aspect_ratio="9:16",
        words=words,
        fallback_query="vintage airplane",
        min_shot_sec=3.0,
    )
    assert len(norm) == 2
    assert norm[0].start == 0.0
    assert norm[0].end == norm[1].start
    assert norm[-1].end == 10.0
    assert all("Relleno" not in sh.reason for sh in norm)


def test_license_audit_and_manifest_download_with_warning(tmp_path: Path) -> None:
    # 1. Free Pexels & PD Wikimedia -> 0 warnings, downloadable certificate created
    free_audit_1 = StockFetcher._build_stock_license_audit(
        asset_id="pexels_video_123",
        provider="pexels",
        title="Night radar tower",
        author="Jane Doe",
        source_url="https://www.pexels.com/video/123/",
        query="radar tower",
    )
    assert free_audit_1["verified_public_domain"] is True
    assert free_audit_1["requires_license_warning"] is False

    manifest_ok = tmp_path / "clip_free.licencias.txt"
    _, warns_ok = write_clip_license_manifest(
        dest_path=manifest_ok,
        clip_id="short_01",
        episode_name="Episodio Radar",
        visual_credits_audit=[{"shot_id": "shot_01", **free_audit_1}],
    )
    assert warns_ok == []
    assert manifest_ok.exists()
    text_ok = manifest_ok.read_text(encoding="utf-8")
    assert "100% VERIFICADO LIBRE Y GRATUITO" in text_ok
    assert "Pexels Free License" in text_ok

    # 2. Hypothetical non-PD license -> must emit explicit warning AND download full license certificate
    non_pd_audit = StockFetcher._build_stock_license_audit(
        asset_id="ext_999",
        provider="external_archive",
        title="Restricted photo",
        author="John Smith",
        source_url="https://example.org/photo/999",
        query="historical photo",
        license_override="CC-BY-SA 4.0",
        license_url_override="https://creativecommons.org/licenses/by-sa/4.0/",
    )
    assert non_pd_audit["verified_public_domain"] is False
    assert non_pd_audit["requires_license_warning"] is True

    manifest_warn = tmp_path / "clip_warn.licencias.txt"
    _, warns_bad = write_clip_license_manifest(
        dest_path=manifest_warn,
        clip_id="short_02",
        episode_name="Episodio Radar",
        visual_credits_audit=[{"shot_id": "shot_02", **non_pd_audit}],
    )
    assert len(warns_bad) == 1
    assert "AVISO DE LICENCIA" in warns_bad[0]
    assert manifest_warn.exists()
    text_warn = manifest_warn.read_text(encoding="utf-8")
    assert "ALERTA DE LICENCIA DETECTADA" in text_warn
    assert "https://creativecommons.org/licenses/by-sa/4.0/" in text_warn
