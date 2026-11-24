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
