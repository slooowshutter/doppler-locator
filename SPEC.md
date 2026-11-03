# Doppler Locator — build spec

Replicates https://x.com/paraschopra/status/2117561731065274562 (quoting https://x.com/0x45o/status/2117332304880169152):
estimate WHERE an audio recording was made from the Doppler curves of passing aircraft engine tones,
matched against open ADS-B aircraft tracks. Reference output images are in ref/ (a.jpg = ridge score plot,
spectrogram crops with predicted curve overlaid, and a "where must the listener be for this ridge to fit" heatmap
with true point star and aircraft track line; b.jpg = spectrogram with aircraft tone ridges 80–200 Hz).

## Stack (fixed)
- Python 3.13, FastAPI + uvicorn, numpy, scipy, httpx, soundfile (already installed in ./.venv; ffmpeg at /usr/bin/ffmpeg).
  Do NOT add paid services or API keys. No Node build step: frontend is static HTML/CSS/vanilla JS served by FastAPI
  from ./static, with Leaflet 1.9 + OSM tiles loaded from unpkg CDN.
- Single process: `uvicorn app.main:app --host 127.0.0.1 --port 8787`. Data dir ./data (gitignored) for sqlite cache.
- Low memory box (~2 GB free): keep numpy arrays modest; analysis of 3 min audio must run in < 30 s on 2 cores.

## Aircraft data (free, no key)
- Primary: `https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}` (radius max 250 nm). Fallback:
  `https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}`. Both return a list of aircraft (`ac` or `aircraft`)
  with hex, flight, t (type), lat, lon, alt_baro (ft or "ground"), alt_geom, gs (kt), track, seen_pos (s since pos).
  Position time = response `now` (ms in adsb.lol `now`, seconds in adsb.fi — handle both) minus seen_pos.
- Set a descriptive User-Agent, timeouts, and back off on 429/5xx. Never poll faster than once per 3 s per region.
- `app/tracks.py`: a TrackStore (sqlite, table positions(hex, t_unix, lat, lon, alt_m, gs, track, flight, type, src)),
  de-dup on (hex, t rounded 0.5 s). Retain 14 days.
- Pollers:
  - Session poller: when a client starts a recording session (POST /api/session/start with region center+radius_km),
    poll that region every 4 s from start-60 s until stop+120 s (cap 10 min), so tracks cover the window.
  - Watch regions: env var WATCH_REGIONS="name:lat:lon:radius_km;..." (default "Paris:48.86:2.35:60") polled
    continuously every 8 s in a background asyncio task, so later file uploads for those regions/time windows work.
    GET /api/coverage returns watch regions and for each the earliest/latest cached time.
- For a query window [t0-30s, t1+30s] in region, return per-aircraft tracks; interpolate positions in local ENU
  (meters, flat-earth around region center is fine for <100 km) with linear interpolation, plus dead-reckoning
  extrapolation using gs/track up to 30 s beyond the last fix. Altitude: alt_geom if present else alt_baro; "ground" => 0
  (but ground aircraft can still be loud — keep them). Convert ft->m, kt->m/s.
- If no cached data for the window: if window end is within the last 60 s, do one live fetch; otherwise return a clear
  error "No aircraft history cached for that time/region — recordings must be made via Record mode or inside a watch
  region while the server was running" plus the coverage info.

## Audio & spectrogram (`app/audio.py`)
- Accept WAV/any format: decode via soundfile, fallback to ffmpeg subprocess (`ffmpeg -i in -ac 1 -ar 8000 -f wav -`) for
  webm/opus, mp4/m4a (iOS), mov/mp4 video. Resample to 8000 Hz mono. Max 6 min, max 60 MB upload.
- Spectrogram: STFT with ~1.0 s Hann window (nfft 8192 at 8 kHz) hop 0.25 s → good frequency resolution (~0.12 Hz bins
  before resampling). Restrict 40–2000 Hz. Then resample magnitude onto a LOG-frequency axis (e.g. 600 bins from 40 to
  2000 Hz, ratio step r). Take log power, then whiten: subtract per-frequency-bin median over time AND per-frame median
  over frequency, clip negatives to 0 (this removes stationary hums so only moving tones remain). Store as float32.

## Physics & scoring (`app/locate.py`)
- Speed of sound c = 340 m/s (optionally temperature-adjusted; fine to fix).
- For a listener at ground point P (alt 0) and aircraft position A(te) at emission time te, received time
  ta = te + |A(te)-P|/c. Received frequency f(ta) = f0 * c / (c + dR/dte) where R = |A(te)-P| (dR/dte = range rate).
  Compute on a fine te grid then map onto the spectrogram frame times ta (np.interp). Ignore frames where slant range
  > MAX_RANGE (default 12 km) — the plane is inaudible there (those frames contribute nothing).
- On the log-frequency axis, f0*g(t) is log(f0) + log(g(t)): so for each (cell, aircraft) compute per-frame bin offset
  o_t = log(g(t))/log(r), and the score over ALL f0 candidates at once is S[k] = mean_t W[t, k + o_t] for the frames in
  range (gather with integer offsets, vectorized). Take the best k (f0) per aircraft; require >= 20 s of audible frames.
  Allow a small clock-offset search (default {-4,-2,0,2,4} s; upload mode wider, ±20 s, configurable).
- Normalize: z = (best - mean_k(S)) / std_k(S) or compare to the same aircraft's score with a constant-frequency curve
  (g=1) so cells far from any plane (flat curve) don't win. Cell score = max over aircraft of z (also keep sum of top-2).
- Grid: coarse-to-fine. Region = center + radius (default 25 km for record mode, user-set for upload). Coarse 1 km grid,
  then refine 200 m grid in a 4 km box around the top 3 coarse peaks. Output heatmap values on the coarse grid + refined
  patch, the best-guess point, top-3 peaks, and for the winning aircraft: hex, flight, type, f0 estimate, z-score,
  closest-approach distance from best guess, and the predicted curve (time, freq) at best guess and for comparison.
- Confidence: from peak z and peak sharpness (ratio of best score to 90th percentile across grid, and area of cells
  within 90% of max). Labels: "high" / "medium" / "low" / "no fit". Honest failures: no aircraft in window → error;
  aircraft present but none within MAX_RANGE of any grid cell → "no plane close enough"; peak z < threshold → "no
  Doppler curve detected — no aircraft clearly heard".
- Also return a downsampled spectrogram image (PNG, base64, magma-like colormap, made with numpy + a tiny PNG writer or
  zlib — no matplotlib needed, or add matplotlib only if light) with the predicted best-guess curve overlaid as cyan
  line, and the raw (unwhitened) log spectrogram 40–600 Hz for display.

## API (`app/main.py`)
- GET / → static/index.html. GET /healthz → {ok, tracks_cached, regions}.
- POST /api/session/start {lat, lon, radius_km, client_time_ms} → {session_id}. Starts session poller.
- POST /api/session/{id}/stop → stops poller after +120 s tail (analysis can start right away: if the window end is
  < 20 s ago, wait a few seconds for one more poll).
- POST /api/analyze multipart: file, start_utc (ISO or unix ms; for record mode = recording start time measured on
  client), region lat, lon, radius_km, optional session_id, optional clock_search_s. Returns JSON result as above.
  Run analysis in a thread (anyio.to_thread) and guard with a semaphore (1 concurrent analysis); return 429 politely.
- POST /api/demo/synthetic {lat?, lon?} → picks a real recent cached aircraft track in a watch region (one that passes
  within 3 km of some ground point during a 120 s window in the cache), picks a hidden "true" point near its closest
  approach (offset 0.5–3 km), synthesizes audio: f0 ~ 120–400 Hz harmonic tone + 2 harmonics with the exact Doppler
  curve and 1/R amplitude, plus pink-ish noise and a stationary 50 Hz hum + random stationary tone, writes WAV to
  data/demo/{id}.wav, and returns {demo_id, wav_url, start_utc, region (center offset randomly 5–15 km from truth,
  radius 25 km), true_lat, true_lon}. Also GET /api/demo/{id}.wav. Used for smoke tests and a "Try demo" button.
- Never log audio contents or user GPS. Uploaded audio deleted after analysis.

## Frontend (static/, mobile-first, works on iOS Safari + Android Chrome, one page, dark theme)
- Header: what it is (1 line), privacy note.
- Tabs: "Record now", "Upload file", "Demo".
- Record now: region picker = Leaflet map; user taps the map or types a city (Nominatim search
  https://nominatim.openstreetmap.org/search?format=json&q=..., debounce, respect usage policy, 1 req per submit) to set
  a SEARCH REGION center with a radius slider (10–50 km, default 25). Do NOT use GPS to set it. Big Record button
  (getUserMedia audio with echoCancellation/noiseSuppression/autoGainControl all FALSE — critical, those kill tones),
  MediaRecorder (pick supported mimeType: audio/webm;codecs=opus, audio/mp4, …), live level meter + elapsed timer, live
  mini scrolling spectrogram via AnalyserNode (nice to have), auto-stop at 3 min, hint "wait until you hear a plane;
  best 60–180 s while it passes". start time = Date.now() at recorder start (send ISO UTC). On stop: upload to
  /api/analyze with session_id.
  Also show "planes currently near region" count from GET /api/aircraft?lat&lon&radius_km (cached, live) so user knows.
- Upload file: file input (audio/*,video/*), datetime-local for recording start + timezone select defaulting to the
  browser zone (convert to UTC), region via the same map picker + radius, clock uncertainty slider. Show coverage info.
- Demo: button "Generate synthetic test from real ADS-B" → calls /api/demo/synthetic, plays audio element, then runs
  analyze with the returned start/region, and shows true point.
- Results view: Leaflet map with heatmap as semi-transparent colored rectangles or a canvas ImageOverlay (no heatmap
  plugin needed), best-guess marker, top-3 peaks, aircraft track polylines in window (winning one highlighted), confidence
  badge + plain-English explanation, the spectrogram PNG with curve overlay, table of aircraft matched. Optional button
  "Compare with my actual location" → asks geolocation permission only then, draws actual point and distance error
  (only client-side, never sent to server).
- Clear error states. Everything must be usable at 375 px width; viewport meta set; tap targets >= 44 px.

## Tests (`tests/`, pytest) — MUST pass
- Unit: doppler formula sanity (approaching → higher f, receding → lower, at closest approach ≈ f0 with delay).
- Synthetic end-to-end WITHOUT network: build a fake straight-line track (alt 600 m, 70 m/s, passes 1 km from truth),
  plus 5 distractor tracks, synthesize 120 s audio with noise, run locate() → best guess within 1.5 km of truth.
- tests use fixtures only; a separate script `scripts/smoke_live.py BASE_URL` hits a running server: /healthz,
  /api/demo/synthetic, download wav, /api/analyze, asserts heatmap present and prints error distance in km.

## Repo hygiene
- README.md: what it is, how it works (physics), how to run, deploy notes (cloudflared quick tunnel), limits, credits to
  the two X posts. .gitignore: .venv, data/, __pycache__, *.wav except tests fixtures. requirements.txt pinned-ish.
- No secrets anywhere. No telemetry.
