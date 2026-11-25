"""Integration tests for FastAPI endpoints, profile CRUD, watcher API, and gallery API."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app


def test_fastapi_endpoints_lifecycle() -> None:
    with TestClient(app) as client:
        r_status = client.get("/api/system/status")
        assert r_status.status_code == 200
        st = r_status.json()
        assert "ffmpeg_ok" in st
        assert "openrouter_api_key" not in st  # Secret key must never leak

        r_profiles = client.get("/api/profiles")
        assert r_profiles.status_code == 200
        profiles = r_profiles.json()
        assert any(p["id"] == "ejemplo_podcast" for p in profiles)

        # Duplicate and delete test profile cleanly
        r_dup = client.post("/api/profiles/ejemplo_podcast/duplicate", json={"new_name": "Perfil Test Temp"})
        assert r_dup.status_code == 200
        dup_id = r_dup.json()["id"]
        assert dup_id.startswith("perfil_test_temp")

        r_del = client.delete(f"/api/profiles/{dup_id}")
        assert r_del.status_code == 200

        r_fonts = client.get("/api/fonts")
        assert r_fonts.status_code == 200
        assert len(r_fonts.json()) >= 4

        r_voices = client.get("/api/tts/voices")
        assert r_voices.status_code == 200
        v_ids = {e["id"] for e in r_voices.json()["engines"]}
        assert v_ids == {"edge_tts", "openrouter_audio", "offline_local"}

        # Verify intro_curtain & outro_curtain persistence on a temporary profile
        r_dup2 = client.post("/api/profiles/ejemplo_podcast/duplicate", json={"new_name": "Cortinilla Test"})
        assert r_dup2.status_code == 200
        p2 = r_dup2.json()
        p2["intro_curtain"]["display_mode"] = "single_screen"
        p2["intro_curtain"]["hook_template"] = "¿Sabías que...?"
        p2["outro_curtain"]["audio_mode"] = "duck_podcast_bg"
        r_put = client.put(f"/api/profiles/{p2['id']}", json=p2)
        assert r_put.status_code == 200
        saved_p2 = r_put.json()
        assert saved_p2["intro_curtain"]["display_mode"] == "single_screen"
        assert saved_p2["outro_curtain"]["audio_mode"] == "duck_podcast_bg"
        client.delete(f"/api/profiles/{p2['id']}")

        r_jobs = client.get("/api/jobs")
        assert r_jobs.status_code == 200

        r_watcher = client.get("/api/watcher")
        assert r_watcher.status_code == 200
        assert r_watcher.json()["running"] is False

        r_gallery = client.get("/api/media/gallery")
        assert r_gallery.status_code == 200

        r_root = client.get("/")
        assert r_root.status_code == 200
        assert "PodcastForge Studio" in r_root.text
        assert "text/html" in r_root.headers.get("content-type", "")


def test_semi_auto_pause_select_propose_and_source_audio(tmp_path, monkeypatch) -> None:
    from pathlib import Path
    import backend.core.pipeline as pl_mod
    from backend.config import get_settings
    from backend.core.pipeline import JobManager
    from backend.models.job import ClipCandidate, ClipMetadata, NarrativeSpan, SentenceSegment

    base = Path(tmp_path)
    src_audio = base / "ep_semi.mp3"
    src_audio.write_bytes(b"ID3" + b"\x00" * 1024)
    cfg = get_settings().model_copy(
        update={
            "base_dir": base,
            "processing_dir": base / "processing",
            "output_dir": base / "output",
            "cache_dir": base / "cache",
        }
    )
    jm = JobManager(cfg)
    prof = jm.store.list_profiles()[0]

    # Avoid slow external calls: mock Phase 1 & Phase 2 helpers
    sents = [
        SentenceSegment(id="S0001", start=0.0, end=20.0, text="En 1942 Rommel avanzó hacia el este."),
        SentenceSegment(id="S0002", start=20.0, end=45.0, text="Los defensores frenaron el ataque nocturno."),
    ]
    monkeypatch.setattr(pl_mod, "get_audio_duration", lambda *a, **k: 60.0)
    monkeypatch.setattr(pl_mod, "compute_rms_energy_blocks", lambda *a, **k: [{"block_index": 0, "start": 0.0, "end": 60.0, "energy": "alta"}])
    monkeypatch.setattr(pl_mod, "detect_silences", lambda *a, **k: [])
    monkeypatch.setattr(pl_mod, "detect_non_speech_intervals", lambda *a, **k: ([], []))
    monkeypatch.setattr(pl_mod, "transcribe_draft", lambda *a, **k: (sents, "es"))
    monkeypatch.setattr(pl_mod, "diarize_draft_sentences", lambda src, ss, **k: (ss, ["Hablante 1"]))

    cand_rec = ClipCandidate(
        id="short_conclusive_01",
        candidate_id="cand_rec1",
        original_title="Avance de Rommel",
        clip_type="short_conclusive",
        aspect_ratio="9:16",
        score=91,
        slug="avance-rommel",
        start_sentence_id="S0001",
        end_sentence_id="S0002",
        orig_start=0.0,
        orig_end=45.0,
        estimated_post_cut_sec=44.0,
        narrative_spans=[NarrativeSpan(start_sentence_id="S0001", end_sentence_id="S0002", orig_start=0.0, orig_end=45.0, role="core")],
        metadata=ClipMetadata(titles=["Avance de Rommel"]),
    )
    cand_rej = cand_rec.model_copy(
        update={
            "id": "short_cliff_rej",
            "candidate_id": "cand_rej1",
            "clip_type": "short_cliffhanger",
            "metadata": ClipMetadata(titles=["Descartado Rescatable"]),
        }
    )

    def _fake_editorial(**kwargs):
        cat = kwargs.get("catalog_out")
        if isinstance(cat, dict):
            cat["candidate_pool"] = [cand_rec]
            cat["rejected_candidates"] = [
                {
                    "candidate_id": "cand_rej1",
                    "title": "Descartado Rescatable",
                    "clip_type": "short_cliffhanger",
                    "score": 85,
                    "orig_start": 10.0,
                    "orig_end": 40.0,
                    "reason": "Prueba de rescate",
                    "candidate": cand_rej.model_dump(mode="json"),
                }
            ]
        return [cand_rec], [], [{"block_index": 0, "tema": "Rommel 1942", "start": 0.0, "end": 60.0, "interes_general": 90}], {}, {}

    monkeypatch.setattr(pl_mod, "run_editorial_selection", _fake_editorial)

    job = jm.create_job(prof.id, src_audio, execution_mode="semi_auto")
    paused = jm.run_job_sync(job.job_id)
    assert paused.status == "awaiting_topic_selection"
    assert paused.topics_confirmed is False
    assert len(paused.editorial_blocks) == 1
    assert len(paused.rejected_candidates) == 1

    # Mock start_job_async & propose_clip_from_block for API tests
    monkeypatch.setattr(jm, "start_job_async", lambda *a, **k: None)
    monkeypatch.setattr(
        pl_mod,
        "propose_clip_from_block",
        lambda **k: cand_rec.model_copy(update={"candidate_id": "cand_prop1", "user_rescued": True, "metadata": ClipMetadata(titles=["Propuesto B0"])}),
    )

    import backend.api.routes_media as rm_mod

    monkeypatch.setattr(rm_mod, "get_settings", lambda: cfg)

    old_jm = app.state.job_manager if hasattr(app.state, "job_manager") else None
    with TestClient(app) as client:
        app.state.job_manager = jm
        app.state.settings = cfg
        try:
            r_src = client.get(f"/api/media/processing/{job.job_id}/source-audio")
            assert r_src.status_code == 200
            assert r_src.content.startswith(b"ID3")

            r_prop = client.post(f"/api/jobs/{job.job_id}/propose-block", json={"block_index": 0, "clip_type": "short_conclusive"})
            assert r_prop.status_code == 200
            assert r_prop.json()["candidate_id"] == "cand_prop1"

            r_logs = client.get(f"/api/jobs/{job.job_id}/logs")
            assert r_logs.status_code == 200
            assert len(r_logs.json()["events"]) >= 1

            r_sel = client.post(
                f"/api/jobs/{job.job_id}/select-topics",
                json={"selections": [{"candidate_id": "cand_rej1", "clip_type": "short_conclusive", "intro_curtain_enabled": True}], "pause_before_render": True},
            )
            assert r_sel.status_code == 200
            body_sel = r_sel.json()
            assert body_sel["topics_confirmed"] is True
            assert body_sel["clips"][0]["user_rescued"] is True
            assert body_sel["auto_mode"] is False
        finally:
            if old_jm is not None:
                app.state.job_manager = old_jm
