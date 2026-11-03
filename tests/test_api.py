import io
import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient
from app import main
from app.tracks import TrackStore


def test_api_errors_and_history(tmp_path, monkeypatch):
    store = TrackStore(str(tmp_path / "empty.sqlite"))
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json()["tracks_cached"] == 0
    assert client.get("/api/demo/not-a-demo.wav").status_code == 404
    assert client.post("/api/session/not-a-session/stop").status_code == 404
    data = dict(start_utc="2020-01-01T00:00:00Z", lat=48.86, lon=2.35, radius_km=25)
    out = io.BytesIO()
    sf.write(out, np.zeros(8000 * 21), 8000, format="WAV")
    response = client.post(
        "/api/analyze",
        data=data,
        files={"file": ("silent.wav", out.getvalue(), "audio/wav")},
    )
    assert response.status_code == 422
    assert "No aircraft history cached" in response.json()["detail"]
    assert "coverage" in response.json()
    assert main.busy._value == 1
    store.db.close()
