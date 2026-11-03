import time
import numpy as np
import pytest
from app.locate import doppler, locate, synthesize
from app.tracks import TrackStore, enu


@pytest.fixture
def flight():
    t = np.arange(-100, 241, 4.0)
    return dict(
        hex="abc123",
        flight="TEST123",
        type="ATR72",
        t=t,
        xyz=np.column_stack(
            [(t - 60) * 70, np.full_like(t, 1000), np.full_like(t, 600)]
        ),
        gs=np.full_like(t, 70),
        heading=np.full_like(t, 90),
    )


def test_physics(flight):
    g, r, ok = doppler(
        flight, [0, 0], np.array([10.0, 60 + np.hypot(1000, 600) / 340, 110.0])
    )
    assert g[0] > 1 and g[2] < 1
    assert abs(g[1] - 1) < 0.001


def test_store(tmp_path):
    store = TrackStore(str(tmp_path / "tracks.db"))
    now = time.time()
    p = dict(
        now=now * 1000,
        ac=[
            dict(
                hex="abc",
                lat=48.86,
                lon=2.35,
                alt_geom=1000,
                gs=100,
                track=90,
                seen_pos=2,
            )
        ],
    )
    store.ingest(p, "test")
    store.ingest(p, "test")
    assert store.count() == 1
    track = store.query(now - 10, now, (48.86, 2.35), 25)[0]
    assert track["xyz"][0, 2] == pytest.approx(304.8)
    assert abs(track["t"][0] - (now - 2)) < 0.26


def test_synthetic(flight):
    tracks = [flight]
    for i in range(5):
        a = {
            **flight,
            "hex": f"distractor{i}",
            "xyz": flight["xyz"]
            + np.array([6000 + i * 2000, 4000 + i * 3000, 1500 + i * 1000]),
        }
        tracks.append(a)
    audio = synthesize(flight, [0, 0], 0, 120)
    result = locate(audio, 0, tracks, (48.86, 2.35), 8, 4)
    error = np.linalg.norm(
        enu(result["best_guess"]["lat"], result["best_guess"]["lon"], (48.86, 2.35))
    )
    assert error < 1500, (error, result["best_guess"])
    import json
    from fastapi.encoders import jsonable_encoder

    json.dumps(jsonable_encoder(result))
    assert result["heatmap"] and result["spectrogram"].startswith(
        "data:image/png;base64,"
    )
