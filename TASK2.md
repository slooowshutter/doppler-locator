# Task 2 — make the app deployable on Vercel (stateless serverless) while keeping box mode

Context: the box cannot expose a tunnel, so the public deploy target is Vercel Python serverless functions
(FastAPI). Serverless = no background pollers, no persistent sqlite, 4.5 MB max request body, no ffmpeg,
function maxDuration 60 s. Keep the existing long-running "box mode" working exactly as now (pollers, sqlite,
sessions). Select mode with env: stateless when `VERCEL` env var is set or `DOPPLER_STATELESS=1`.

Do all of this:
1. Readability: the current Python/JS/CSS is written as very long one-liners. Reformat app/*.py, static/*,
   tests/*, scripts/* into normal readable code (one statement per line, docstrings on main functions, PEP8-ish,
   JS formatted). Behaviour unchanged; tests must still pass.
2. Client-side audio normalisation (both modes): in the browser, before upload, decode the recorded blob or the
   chosen audio/video file with AudioContext.decodeAudioData, downmix to mono, resample to 8000 Hz with
   OfflineAudioContext, encode 16-bit PCM WAV, cap at 6 min (≈5.8 MB? -> cap at 4.5 min so WAV < 4.4 MB) and upload
   that WAV. If decoding fails, fall back to uploading the original file (box mode only can handle it via ffmpeg;
   show a clear error in stateless mode if > 4.4 MB). Must work on iOS Safari (MediaRecorder audio/mp4) and Android
   Chrome (webm/opus). Use webkitAudioContext fallback.
3. Client-collected tracks (stateless record mode): while recording, the page polls GET /api/aircraft for the frozen
   search region every 4 s (start polling 0 s before recording, continue 20 s after stop, then analyze) and accumulates
   raw positions [{hex, flight, type, t_unix, lat, lon, alt_m, gs, track}] de-duplicated client-side. Make
   /api/aircraft return per-aircraft position time (now - seen_pos) and all those fields. POST /api/analyze accepts an
   optional form field `tracks_json` (JSON list of such positions, max 20k entries, validate types/ranges) which is
   merged with whatever the TrackStore has; in stateless mode it is the only source besides one live fetch.
   Show "N aircraft / M positions collected" live during recording. Do this in box mode too (harmless, more robust).
4. Stateless demo: /api/demo/synthetic in stateless mode does one live fetch for the region (default Paris
   48.86,2.35 r 60 km, or lat/lon provided), dead-reckons every airborne aircraft straight at its gs/track/alt for
   t in [-60, +180] s around the fetch time to build tracks (so tracks are seeded from REAL ADS-B states), picks one
   that passes within 3 km of a ground point, synthesizes as now, and returns the WAV inline as base64 (`wav_b64`)
   plus `tracks_json` (positions sampled every 4 s from those tracks, all aircraft) instead of writing to disk.
   Frontend demo flow passes tracks_json to /api/analyze. Box mode keeps current behaviour but also returns the same
   fields so one frontend path works. Update scripts/smoke_live.py to use wav_b64 + tracks_json when present.
5. Upload mode in stateless mode: also accept an optional "aircraft tracks file" (the JSON bundle the app offers to
   download after a record-mode analysis: add a "Download recording bundle (audio WAV + tracks JSON)" link in results
   — actually make two download links: WAV and tracks JSON). Explain in the UI that arbitrary past videos need
   historical ADS-B which the hosted version doesn't store; it works for recordings made in the last ~60 s (one live
   fetch with dead reckoning ±60 s) or with a tracks file.
6. Vercel packaging: add `api/index.py` that imports `app.main:app`; `vercel.json` with rewrites so every path goes to
   the FastAPI function (static files served by FastAPI), `functions` config: maxDuration 60, memory 2048,
   includeFiles static/** and app/**; requirements.txt must not include pytest (move to requirements-dev.txt). Ensure
   sqlite path uses /tmp in stateless mode. Add `.vercelignore` (exclude .venv, data, tests, ref, *.log, TASK*.md,
   codex_run*.log). Keep import-time work small (lazy-import scipy inside functions if it helps cold start).
   Analysis must finish < 45 s: in stateless mode cap the coarse grid and fine refinement so a 3-minute recording with
   40 aircraft finishes in < 25 s on this box (measure it and print timing).
7. Tests: keep existing ones passing; add tests for tracks_json merge in /api/analyze (stateless env), for stateless
   demo using a monkeypatched live fetch with fake aircraft states, and for the WAV size guard. Run pytest.
8. Verify: run the server in stateless mode on port 8788 (`DOPPLER_STATELESS=1`), run scripts/smoke_live.py against it
   (must locate < 2 km), also run it against box mode on 8789 briefly. Kill servers you start (do NOT touch any
   process on port 8787). No git push, no secrets, no paid APIs. Update README (deploy: Vercel; box mode; limits).
Finish with a short summary.
