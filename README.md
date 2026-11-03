# Doppler Locator

Estimate a recording's location by matching moving aircraft tones to free ADS-B tracks. Python 3.13, FastAPI, NumPy/SciPy and a static Leaflet interface. No API keys or telemetry.

## Run

```sh
. .venv/bin/activate
# Only if dependencies are missing: uv pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Open http://127.0.0.1:8787. Leave the server running to build history. Paris (60 km radius) is watched by default. Configure additional regions with `WATCH_REGIONS="Paris:48.86:2.35:60;London:51.51:-0.12:60"`. SQLite data lives in `data/`, retaining 14 days. Sessions poll every 4 seconds and continue for 120 seconds after stopping, with a 10-minute total cap. They use existing cached history for the minute before start; free live feeds cannot retrieve uncached past fixes.

Record mode asks for microphone access and disables speech processing. Choose the search region manually. Upload accepts audio and video, a recording time and timezone, clock uncertainty, and an optional saved tracks JSON file. Both modes convert browser-decodable media to mono 8 kHz 16-bit PCM WAV before upload (maximum 4.5 minutes). Unsupported browser codecs fall back to the original file only in box mode, which can use ffmpeg. Old uploads need aircraft history captured while this server was running or supplied in a saved tracks file. While recording, the browser polls the frozen region every four seconds and continues for 20 seconds after stopping. Results offer separate WAV and tracks JSON download links; save both to reanalyze later. The JSON bundle includes recording start time and search region. Audio uploads are temporary and deleted after analysis. Synthetic demo WAVs are retained locally for up to a day (cleaned when the next demo is generated).

## Physics and fit

For emission time `te`, slant range `R` and sound speed `c = 340 m/s`, arrival time is `ta = te + R/c` and received frequency is `f0*c/(c + dR/dte)`. The solver interpolates real tracks, including at most 30 seconds of dead reckoning, and maps emitted curves into arrival time. It builds a whitened log-frequency STFT, scans fundamental tones and clock offsets, evaluates a 1 km grid, then refines three peaks at 200 m. Frames beyond 12 km are excluded. Fit scores combine ridge support, harmonic support and z-score, and require improvement over the strongest constant-frequency curve on the same frames. A broad initial scan and per-aircraft refinement keep sharp low-altitude ridges from falling between coarse cells. The cyan overlay shows the winning prediction; the raw spectrogram is shown separately.

Straight paths have a fundamental mirror ambiguity. Numerically tied peaks prefer the search center, which is a prior rather than additional acoustic evidence. Engine RPM changes, multipath, wind, altitude errors, missing ADS-B fixes and timestamp errors can defeat this method. Confidence is a heuristic, not a calibrated probability. Synthetic tests establish internal consistency, not accuracy on arbitrary real recordings. The demo chooses a low aircraft and a listener 0.5–0.85 km to one side of its path; this keeps the unavoidable mirror alternative near the true point. The specified 8192-point FFT at 8 kHz has **0.977 Hz** native bin spacing (not 0.12 Hz); log-axis interpolation does not add physical resolution.

## Verify

```sh
. .venv/bin/activate
pytest -q
# With box mode running and at least ~60 seconds of cache:
python scripts/smoke_live.py http://127.0.0.1:8787
```

Tests use only fake tracks and generated audio; no network. The live script uses public feeds, creates a demo, analyzes its WAV and asserts a location error below 2 km.

## Deploy to Vercel

Import this repository into Vercel as a Python project (no frontend build command). The root `index.py` exposes the FastAPI app (Vercel's zero-config FastAPI detection); `vercel.json` gives that function a 60-second max duration. Python 3.12 is pinned in `.python-version`; the dependency bundle is about 185 MB (under the 500 MB limit). Runtime dependencies are in `requirements.txt`; development dependencies are in `requirements-dev.txt`. `.vercelignore` excludes local caches, media, tests and the virtual environment. No keys or paid APIs are required.

Vercel sets `VERCEL`, which selects stateless mode. Test that mode locally with:

```sh
DOPPLER_STATELESS=1 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8788
.venv/bin/python scripts/smoke_live.py http://127.0.0.1:8788
PYTHONPATH=. .venv/bin/python scripts/benchmark_stateless.py
```

Hosted mode starts no pollers, creates no sessions or persistent history, and uses request-local in-memory track stores. The default SQLite path for any temporary on-disk store is `/tmp`. Warm instances cache only recent live snapshots to respect feed polling limits; separate serverless instances cannot coordinate that cache. Record mode supplies its own collected fixes. Without a tracks file, hosted uploads must end within roughly the last 60 seconds; the function projects one live snapshot ±60 seconds. This is an approximation and cannot reconstruct arbitrary past videos. Hosted demos project real live airborne states from −60 to +180 seconds, and return WAV and all sampled tracks inline. Box demos retain cached-track behavior and disk WAV URLs, plus the same portable fields.

The hosted request budget is 4.5 MB including tracks and multipart overhead; WAV files alone must stay below 4.4 MB. The browser trims to at most 270 seconds and reserves room for the track bundle. Hosted mode has no ffmpeg, so media must decode in the browser or already be a supported WAV. Its solver bounds coarse-grid density, uses 400 m shared refinement followed by 200 m aircraft refinement, and tests at most five clock offsets. Wide timestamp uncertainty therefore has lower clock resolution than box mode. The reproducible benchmark uses 180 seconds and 40 aircraft; run the command above for timing on your machine. Live-feed availability, cold starts and larger workloads can still affect total request time.

## Box mode and limits

Microphone access needs localhost or HTTPS. Unset `VERCEL` and `DOPPLER_STATELESS` (or set the latter to `0`) to use the original box mode. Run one uvicorn worker; the cache pollers and analysis semaphore are process-local. This is a research prototype, without authentication or production abuse controls. Do not expose it unattended. Box API maximum upload: 60 MB, six minutes; the browser normalizes and caps uploads at 4.5 minutes. One analysis at a time; concurrent requests receive 429. Browser assets/tiles use unpkg/OpenStreetMap and city searches go to Nominatim once per submitted search. GPS is requested only by the optional comparison button and never sent to this server. Real-device recording on iOS Safari and Android Chrome requires manual verification.

Credits: [paraschopra](https://x.com/paraschopra/status/2117561731065274562), quoting [0x45o](https://x.com/0x45o/status/2117332304880169152). Aircraft positions: ADSB.lol, fallback ADSB.fi. Maps: Leaflet and OpenStreetMap contributors.
