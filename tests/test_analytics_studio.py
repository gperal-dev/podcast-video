"""Tests for PodcastForge Analytics Studio exporter, server, and opt-in telemetry default."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from analytics_studio.exporter import export_job_report, import_all_historical_jobs
from analytics_studio.server import create_analytics_app
from backend.config import get_settings
from backend.core.pipeline import JobManager


def test_exporter_handles_prueba6_anomalies_and_all_historical_jobs() -> None:
    root = Path(__file__).resolve().parent.parent
    summaries = import_all_historical_jobs(root)
    assert len(summaries) >= 13

    rep = export_job_report("job_20261124_203725_8304d3", root)
    assert rep["status"] == "completed"
    assert len(rep["clips"]) == 4
    assert rep["judge_funnel"]["total_candidates_evaluated"] >= 10

    codes = {a["code"] for a in rep["anomalies"]}
    assert "residual_silence_gap" in codes
    assert "truncated_hook_text" in codes
    assert any(len(c.get("splices") or []) >= 1 for c in rep["clips"])
    assert any("phase_durations" in s for s in summaries)


def test_analytics_server_endpoints_and_path_safety() -> None:
    root = Path(__file__).resolve().parent.parent
    app = create_analytics_app(base_dir=root)
    client = TestClient(app)

    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "PodcastForge Studio" in r_home.text
    assert "Cadena de Trabajo" in r_home.text

    r_jobs = client.get("/api/jobs")
    assert r_jobs.status_code == 200
    items = r_jobs.json()
    assert any(j["job_id"] == "job_20261124_203725_8304d3" for j in items)

    r_rep = client.get("/api/jobs/job_20261124_203725_8304d3")
    assert r_rep.status_code == 200
    rep = r_rep.json()
    assert len(rep["clips"]) == 4

    vid_fn = rep["clips"][0]["video_filename"]
    assert vid_fn
    r_vid = client.get(f"/media/video/job_20261124_203725_8304d3/{vid_fn}", headers={"Range": "bytes=0-1023"})
    assert r_vid.status_code in (200, 206)

    r_aud = client.get("/media/audio/job_20261124_203725_8304d3", headers={"Range": "bytes=0-1023"})
    assert r_aud.status_code in (200, 206)

    # Path traversal rejected
    r_bad = client.get("/media/video/job_20261124_203725_8304d3/..%2Fjob.json")
    assert r_bad.status_code in (400, 404)


def test_notes_persistence_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "analytics_studio" / "data").mkdir(parents=True, exist_ok=True)
    app = create_analytics_app(base_dir=tmp_path)
    client = TestClient(app)

    r_post = client.post(
        "/api/notes",
        json={"job_id": "job_test", "clip_id": "clip_1", "tags": ["buen_gancho"], "note": "Excelente ritmo"},
    )
    assert r_post.status_code == 200
    r_get = client.get("/api/notes?job_id=job_test")
    assert r_get.status_code == 200
    assert r_get.json()["clip_1"]["note"] == "Excelente ritmo"
    assert r_get.json()["clip_1"]["tags"] == ["buen_gancho"]


def test_job_manager_save_analytics_default_false(tmp_path: Path) -> None:
    cfg = get_settings().model_copy(update={"base_dir": tmp_path})
    audio = tmp_path / "ep.mp3"
    audio.write_bytes(b"fake")
    jm = JobManager(cfg)
    p = jm.store.list_profiles()[0]
    job = jm.create_job(p.id, audio)
    assert job.save_analytics_report is False


def test_exporter_dedups_judge_cards_and_detects_short_duration_and_phrase_mismatch(tmp_path: Path) -> None:
    import json

    job_id = "job_test_dedup"
    proc_dir = tmp_path / "processing" / job_id
    proc_dir.mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "output" / "ep"
    out_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "analytics_studio" / "data").mkdir(parents=True, exist_ok=True)

    job_data = {
        "job_id": job_id,
        "profile_id": "podcast_2",
        "episode_name": "Ep Test",
        "audio_duration_sec": 600.0,
        "status": "completed",
        "output_dir": str(out_dir),
        "created_at": "2026-11-25T10:00:00+00:00",
        "updated_at": "2026-11-25T10:05:00+00:00",
        "phases": [
            {"phase": "transcription", "status": "completed", "duration_sec": 45.0, "detail": "OK"},
            {"phase": "editorial", "status": "completed", "duration_sec": 30.0, "detail": "OK"},
            {"phase": "audio_edit", "status": "completed", "duration_sec": 20.0, "detail": "OK"},
            {"phase": "visual_direction", "status": "completed", "duration_sec": 15.0, "detail": "OK"},
            {"phase": "rendering", "status": "completed", "duration_sec": 25.0, "detail": "OK"},
        ],
        "clips": [
            {
                "id": "short_conclusive_01",
                "candidate_id": "cand_x1",
                "original_title": "Título Original Fase 2",
                "clip_type": "short_conclusive",
                "aspect_ratio": "9:16",
                "score": 92,
                "slug": "test",
                "summary": "Resumen",
                "intro_curtain_enabled": True,
                "intro_curtain_text": "En junio de 1942 ocurrió esto.",
                "intro_curtain_phrases": ["Junio de 1942", "otro texto"],
                "intro_curtain_duration_sec": 5.0,
                "outro_curtain_enabled": True,
                "outro_curtain_duration_sec": 6.0,
                "edl": {"orig_duration": 88.0, "edited_duration": 85.0, "segments": []},
                "metadata": {"titles": ["Título Nuevo Fase 3"]},
            }
        ],
        "cost": {"total_calls": 1, "cost_usd": 0.01, "cost_eur": 0.009},
        "logs": [],
    }
    (proc_dir / "job.json").write_text(json.dumps(job_data), encoding="utf-8")
    evs = [
        {
            "type": "judge_audit",
            "candidate_id": "cand_x1",
            "clip_title": "Título Original Fase 2",
            "clip_type": "short_conclusive",
            "stage": "initial",
            "comprehension_score": 80,
            "context_clarity": 85,
            "clean_boundaries": 85,
            "resolution_score": 90,
        },
        {
            "type": "candidate_outcome",
            "candidate_id": "cand_x1",
            "clip_title": "Título Original Fase 2",
            "clip_type": "short_conclusive",
            "accepted": True,
            "audit_action": "approve_needs_intro",
            "reason": "OK",
        },
    ]
    (proc_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n", encoding="utf-8")

    rep = export_job_report(job_id, tmp_path)
    assert len(rep["judge_candidates"]) == 1
    assert rep["judge_candidates"][0]["final_title"] == "Título Nuevo Fase 3"
    assert rep["phase_timings"][0]["duration_sec"] == 45.0
    codes = {a["code"] for a in rep["anomalies"]}
    assert "short_exceeds_max_sec" in codes
    assert "intro_phrases_mismatch" in codes
