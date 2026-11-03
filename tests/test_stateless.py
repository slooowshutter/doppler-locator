"""Hosted-mode API contracts, using only deterministic local ADS-B states."""

import base64
import io
import json
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import main
from app.tracks import TrackStore


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("DOPPLER_STATELESS", "1")
    store = TrackStore(":memory:")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "snapshots", {})

    async def fake_fetch(self, lat, lon, radius):
        self.store.ingest(
            dict(
                now=time.time() * 1000,
                ac=[
                    dict(
                        hex="abc123",
                        flight="TEST",
                        t="ATR72",
                        lat=48.86,
                        lon=2.35,
                        alt_geom=1968.5,
                        gs=136.07,
                        track=90,
                        seen_pos=2,
                    ),
                    dict(
                        hex="def456",
                        flight="HIGH",
                        t="A320",
                        lat=48.9,
                        lon=2.4,
                        alt_geom=30000,
                        gs=400,
                        track=45,
                        seen_pos=0,
                    ),
                ],
            ),
            "fixture",
        )

    monkeypatch.setattr(main.Fetcher, "fetch", fake_fetch)
    yield TestClient(main.app)
    store.db.close()


def wav():
    out = io.BytesIO()
    sf.write(out, np.zeros(21 * 8000), 8000, format="WAV")
    return out.getvalue()


def position(hex="client", t=1000):
    return dict(
        hex=hex,
        flight="CLIENT",
        type="TEST",
        t_unix=t,
        lat=48.86,
        lon=2.35,
        alt_m=600,
        gs=70,
        track=90,
    )


def test_tracks_merge(hosted, monkeypatch):
    main.store.merge([position("cached")])
    observed = []

    def locate(y, start, tracks, *args, **kwargs):
        observed.extend(tracks)
        assert kwargs["stateless"]
        return dict(ok=True)

    monkeypatch.setattr(main, "locate", locate)
    response = hosted.post(
        "/api/analyze",
        files={"file": ("a.wav", wav())},
        data=dict(
            start_utc="1000",
            lat=48.86,
            lon=2.35,
            tracks_json=json.dumps([position(), position()]),
        ),
    )
    assert response.status_code == 200, response.text
    assert {a["hex"] for a in observed} == {"cached", "client"}
    assert all(len(a["t"]) == 1 for a in observed)
    # Client positions remain request-local.
    assert main.store.count() == 1


@pytest.mark.parametrize(
    "positions",
    [
        dict(),
        [dict(position(), lat=91)],
        [dict(position(), gs="70")],
        [dict(position(), track=float("nan"))],
        [position()] * 20001,
    ],
)
def test_invalid_tracks(hosted, positions):
    response = hosted.post(
        "/api/analyze",
        files={"file": ("a.wav", wav())},
        data=dict(
            start_utc="1000", lat=48.86, lon=2.35, tracks_json=json.dumps(positions)
        ),
    )
    assert response.status_code == 422


def test_size_guard(hosted):
    response = hosted.post(
        "/api/analyze",
        files={"file": ("large.wav", b"0" * 4_400_001)},
        data=dict(start_utc="1000", lat=48.86, lon=2.35),
    )
    assert response.status_code == 413
    assert "4.4 MB" in response.json()["detail"]
    response = hosted.post(
        "/api/analyze",
        files={"file": ("large.wav", b"0" * 4_500_001)},
        data=dict(start_utc="1000", lat=48.86, lon=2.35),
    )
    assert response.status_code == 413


def test_demo_and_aircraft(hosted):
    before = time.time()
    response = hosted.get(
        "/api/aircraft", params=dict(lat=48.86, lon=2.35, radius_km=60)
    )
    p = response.json()["aircraft"][0]
    assert abs(p["t_unix"] - (before - 2)) < 1
    assert set(position()) <= set(p)
    response = hosted.post("/api/demo/synthetic", json={})
    assert response.status_code == 200, response.text
    demo = response.json()
    audio, sr = sf.read(io.BytesIO(base64.b64decode(demo["wav_b64"])))
    assert sr == 8000 and len(audio) == 120 * 8000
    assert "wav_url" not in demo
    assert {"abc123", "def456"} == {p["hex"] for p in demo["tracks_json"]}
    main.validate_positions(json.dumps(demo["tracks_json"]))
    # Verify that the portable tracks are sufficient with no persistent history.
    response = hosted.post(
        "/api/analyze",
        files={"file": ("demo.wav", base64.b64decode(demo["wav_b64"]))},
        data=dict(
            start_utc=demo["start_utc"],
            **demo["region"],
            clock_search_s=4,
            tracks_json=json.dumps(demo["tracks_json"]),
        ),
    )
    assert response.status_code == 200, response.text
    from app.tracks import enu

    point = response.json()["best_guess"]
    assert (
        np.linalg.norm(
            enu(point["lat"], point["lon"], (demo["true_lat"], demo["true_lon"]))
        )
        < 2000
    )
    assert main.store.count() == 0
