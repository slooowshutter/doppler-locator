import base64
import math
import struct
import subprocess
import zlib
import numpy as np
import soundfile as sf
import os

SR = 8000


def decode(path):
    """Decode audio to finite mono samples at 8 kHz."""
    from scipy import signal

    try:
        info = sf.info(path)
        if info.duration > 360:
            raise ValueError("Audio must be at most 6 minutes")
        y, sr = sf.read(path, dtype="float32", always_2d=True)
        y = y.mean(axis=1)
    except ValueError as exc:
        if str(exc) == "Audio must be at most 6 minutes":
            raise
        y, sr = _ffmpeg(path), SR
    except (RuntimeError, OSError):
        y, sr = _ffmpeg(path), SR
    if len(y) > sr * 360:
        raise ValueError("Audio must be at most 6 minutes")
    if len(y) < sr * 20:
        raise ValueError("Record at least 20 seconds of audio")
    if sr != SR:
        g = math.gcd(sr, SR)
        y = signal.resample_poly(y, SR // g, sr // g)
    return np.nan_to_num(y).astype("float32")


def _ffmpeg(path):
    if os.getenv("VERCEL") is not None or os.getenv("DOPPLER_STATELESS") == "1":
        raise ValueError(
            "Hosted mode requires browser-decodable audio converted to WAV. Try another browser or upload a PCM WAV."
        )
    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-t",
                "361",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            timeout=40,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Audio decoding timed out") from None
    if p.returncode:
        raise ValueError("Could not decode this audio or video file")
    return np.frombuffer(p.stdout, dtype="<f4")


def spectrogram(y):
    """Build log-frequency raw and whitened spectrograms."""
    from scipy import signal

    f, t, z = signal.stft(
        y, SR, window="hann", nperseg=8192, noverlap=6192, boundary=None
    )
    freq = np.geomspace(40, 2000, 600)
    raw = np.array(
        [np.interp(freq, f, np.abs(z[:, i]) ** 2) for i in range(len(t))],
        dtype="float32",
    )
    raw = np.log(raw + 1e-13)
    w = raw - np.median(raw, axis=0)
    w -= np.median(w, axis=1, keepdims=True)
    return dict(t=t, freq=freq, w=np.maximum(w, 0).astype("float32"), raw=raw)


def png_image(values, curve=None):
    """Render a compact spectrogram PNG with an optional predicted ridge."""
    a = values.T[::-1]
    a = a[:: max(1, a.shape[0] // 300), :: max(1, a.shape[1] // 700)]
    a = np.clip(
        (a - np.percentile(a, 10))
        / (np.percentile(a, 99) - np.percentile(a, 10) + 1e-6),
        0,
        1,
    )
    stops = np.array(
        [[7, 5, 24], [57, 15, 94], [144, 38, 103], [230, 94, 66], [252, 226, 153]]
    )
    v = a * 4
    lo = np.minimum(v.astype(int), 3)
    rgb = (
        stops[lo] * (1 - (v - lo)[..., None]) + stops[lo + 1] * (v - lo)[..., None]
    ).astype("uint8")
    if curve is not None:
        for x, y in curve:
            ix = int(x * (rgb.shape[1] - 1))
            iy = int((1 - y) * (rgb.shape[0] - 1))
            if 0 <= iy < rgb.shape[0] and 0 <= ix < rgb.shape[1]:
                rgb[max(0, iy - 1) : iy + 2, ix] = [50, 240, 255]
    h, w = rgb.shape[:2]

    def chunk(k, d):
        return (
            struct.pack("!I", len(d))
            + k
            + d
            + struct.pack("!I", zlib.crc32(k + d) & 0xFFFFFFFF)
        )

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + r.tobytes() for r in rgb)))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(data).decode()
