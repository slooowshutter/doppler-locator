import numpy as np
import pytest
import soundfile as sf
from app.audio import decode, spectrogram
from app.main import parse_time
from app.tracks import interpolate


def test_decode_resample_stereo(tmp_path):
    p = tmp_path / "tone.wav"
    t = np.arange(44100 * 21) / 44100
    y = np.sin(2 * np.pi * 180 * t).astype("float32")
    sf.write(p, np.column_stack([y, y]), 44100)
    decoded = decode(p)
    assert len(decoded) == 21 * 8000
    assert decoded.ndim == 1
    s = spectrogram(decoded)
    assert s["w"].dtype == np.float32
    assert np.isfinite(s["w"]).all() and s["w"].min() >= 0


def test_short_decode(tmp_path):
    p = tmp_path / "short.wav"
    sf.write(p, np.zeros(100), 8000)
    with pytest.raises(ValueError, match="at least 20"):
        decode(p)


def test_times():
    assert parse_time("2026-01-01T00:00:00Z") == parse_time("1767225600000")
    with pytest.raises(ValueError, match="timezone"):
        parse_time("2026-01-01T00:00:00")


def test_dead_reckoning():
    a = dict(
        t=np.array([100.0, 110.0]),
        xyz=np.array([[0.0, 0.0, 1000.0], [700.0, 0.0, 1000.0]]),
        gs=np.array([70.0, 70.0]),
        heading=np.array([90.0, 90.0]),
    )
    p, ok = interpolate(a, np.array([105.0, 120.0, 141.0]))
    assert p[0, 0] == 350 and p[1, 0] == 1400
    assert ok.tolist() == [True, True, False]


def test_ffmpeg_webm_fallback(tmp_path):
    import subprocess

    p = tmp_path / "recording.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=180:sample_rate=8000:duration=21",
            "-c:a",
            "libopus",
            str(p),
        ],
        check=True,
        capture_output=True,
    )
    y = decode(p)
    assert abs(len(y) - 21 * 8000) < 800
    assert np.isfinite(y).all()
