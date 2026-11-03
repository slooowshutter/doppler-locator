import asyncio
import contextlib
import os
import time
import uuid
import tempfile
import json
import io
import base64
import math
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import anyio
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute
from starlette.requests import Request
from pydantic import BaseModel, Field
from .tracks import TrackStore, Fetcher, HISTORY_ERROR, latlon, interpolate, enu
from .audio import decode
from .locate import locate, synthesize


def is_stateless():
    """Select hosted mode from deployment environment."""
    return os.getenv("VERCEL") is not None or os.getenv("DOPPLER_STATELESS") == "1"


store = TrackStore(":memory:" if is_stateless() else None)
fetcher = Fetcher(store)
sessions = {}
jobs = set()
busy = asyncio.Semaphore(1)
regions = []
for item in os.environ.get("WATCH_REGIONS", "Paris:48.86:2.35:60").split(";"):
    if item.strip():
        name, lat, lon, radius = item.split(":")
        regions.append(
            dict(name=name, lat=float(lat), lon=float(lon), radius_km=float(radius))
        )


async def poll(region, session=None):
    """Poll a watch region or a bounded recording session."""
    while session is None or time.time() < min(
        session["start"] + 600, session.get("stop", float("inf")) + 120
    ):
        await fetcher.fetch(region["lat"], region["lon"], region["radius_km"])
        await asyncio.sleep(8 if session is None else 4)


def task(coro):
    t = asyncio.create_task(coro)
    jobs.add(t)
    t.add_done_callback(jobs.discard)


@asynccontextmanager
async def lifespan(app):
    if not is_stateless():
        for r in regions:
            task(poll(r))
    yield
    for t in list(jobs):
        t.cancel()
    await asyncio.gather(*list(jobs), return_exceptions=True)
    store.db.close()


class TrackFormRoute(APIRoute):
    """Allow portable tracks within the serverless request budget."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_form(request: Request):
            if request.url.path == "/api/analyze":
                limit = 4_500_000 if is_stateless() else 70_000_000
                if int(request.headers.get("content-length", "0")) > limit:
                    raise HTTPException(
                        413,
                        (
                            "Request exceeds hosted 4.5 MB limit"
                            if is_stateless()
                            else "Request too large"
                        ),
                    )
                await request.form(max_part_size=8_000_000)
            return await handler(request)

        return bounded_form


snapshots = {}
snapshot_locks = {}


async def live_positions(lat, lon, radius):
    """Cache only the latest feed snapshot, limiting warm-instance polling."""
    key = (round(lat, 2), round(lon, 2), round(radius))
    async with snapshot_locks.setdefault(key, asyncio.Lock()):
        stamp, positions = snapshots.get(key, (0, []))
        if time.monotonic() - stamp < (3 if positions else 24):
            return positions
        local = TrackStore(":memory:")
        try:
            await Fetcher(local).fetch(lat, lon, radius)
            positions = local.positions()
            if len(snapshots) >= 64:
                snapshots.pop(next(iter(snapshots)))
            snapshots[key] = (time.monotonic(), positions)
            return positions
        finally:
            local.db.close()


app = FastAPI(lifespan=lifespan)
app.router.route_class = TrackFormRoute
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/healthz")
def health():
    return dict(
        ok=True,
        tracks_cached=store.count(),
        regions=len(regions),
        feed_status=fetcher.status,
        stateless=is_stateless(),
    )


@app.get("/api/coverage")
def coverage():
    return dict(
        regions=store.coverage(regions),
        feed_status=fetcher.status,
        stateless=is_stateless(),
    )


class Region(BaseModel):
    lat: float = Field(ge=-85, le=85)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=25, ge=1, le=100)
    client_time_ms: float | None = None


@app.post("/api/session/start")
async def start_session(r: Region):
    if is_stateless():
        return dict(session_id="stateless")
    sid = uuid.uuid4().hex
    s = dict(start=time.time(), region=r.model_dump())
    sessions[sid] = s
    task(poll(s["region"], s))
    return dict(session_id=sid)


@app.post("/api/session/{sid}/stop")
def stop_session(sid: str):
    if is_stateless():
        return dict(ok=True, tail_seconds=20)
    if sid not in sessions:
        raise HTTPException(404, "Session not found")
    sessions[sid]["stop"] = time.time()
    return dict(ok=True, tail_seconds=120)


@app.get("/api/aircraft")
async def aircraft(
    lat: float = Query(..., ge=-85, le=85),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(25, ge=1, le=100),
):
    local = TrackStore(":memory:") if is_stateless() else store
    try:
        if is_stateless():
            local.merge(await live_positions(lat, lon, radius_km))
        else:
            await fetcher.fetch(lat, lon, radius_km)
        positions = [
            p
            for p in local.positions(time.time() - 30)
            if p["t_unix"] >= time.time() - 30
            and np.linalg.norm(enu(p["lat"], p["lon"], (lat, lon))) <= radius_km * 1000
        ]
        latest = {p["hex"]: p for p in positions}
        return dict(count=len(latest), aircraft=list(latest.values()))
    finally:
        if local is not store:
            local.db.close()


def validate_positions(value):
    """Validate untrusted portable track JSON before merging it."""
    if len(value) > 8_000_000:
        raise ValueError("Tracks JSON is too large")
    try:
        positions = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError("Invalid tracks JSON") from None
    if not isinstance(positions, list) or len(positions) > 20000:
        raise ValueError("Tracks JSON must be a list of at most 20000 positions")
    bounds = dict(
        t_unix=(0, 4102444800),
        lat=(-85, 85),
        lon=(-180, 180),
        alt_m=(-500, 25000),
        gs=(0, 700),
        track=(0, 360),
    )
    for p in positions:
        if not isinstance(p, dict):
            raise ValueError("Each track position must be an object")
        for key, (low, high) in bounds.items():
            v = p.get(key)
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or not low <= v <= high
            ):
                raise ValueError("Invalid track position field: " + key)
        for key in ("hex", "flight", "type"):
            v = p.get(key, "")
            if not isinstance(v, str) or len(v) > 64 or (key == "hex" and not v):
                raise ValueError("Invalid track identity: " + key)
    return positions


def project_positions(positions, offsets):
    """Dead-reckon real ADS-B states along their measured ground velocity."""
    projected = []
    from .tracks import latlon

    for p in positions:
        for dt in offsets:
            heading = math.radians(p["track"])
            lat, lon = latlon(
                math.sin(heading) * p["gs"] * dt,
                math.cos(heading) * p["gs"] * dt,
                (p["lat"], p["lon"]),
            )
            if -85 <= lat <= 85 and -180 <= lon <= 180:
                projected.append(dict(p, lat=lat, lon=lon, t_unix=p["t_unix"] + dt))
    return projected


def parse_time(value):
    """Parse timezone-aware ISO dates or Unix seconds/milliseconds."""
    try:
        n = float(value)
        return n / 1000 if n > 1e11 else n
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Recording time must include a timezone")
        return dt.timestamp()


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    start_utc: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    radius_km: float = Form(25),
    session_id: str | None = Form(None),
    clock_search_s: float = Form(20),
    tracks_json: str | None = Form(None),
):
    """Decode an upload, merge tracks, and run one guarded solver job."""
    if busy.locked():
        raise HTTPException(
            429, "An analysis is already running. Please try again shortly."
        )
    async with busy:
        path = None
        local = TrackStore(":memory:") if is_stateless() else store
        try:
            if local is not store:
                local.merge(store.positions())
            if tracks_json:
                local.merge(validate_positions(tracks_json))
            Region(lat=lat, lon=lon, radius_km=radius_km)
            if not 0 <= clock_search_s <= 60:
                raise ValueError("Clock search must be between 0 and 60 seconds")
            start = parse_time(start_utc)
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(file.filename or ".audio").suffix
            ) as f:
                path = Path(f.name)
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if is_stateless() and total > 4_400_000:
                        raise HTTPException(
                            413,
                            "Hosted WAV upload must be under 4.4 MB (maximum 4.5 minutes at 8 kHz mono)",
                        )
                    if total > 60 * 1024**2:
                        raise HTTPException(413, "Maximum upload size is 60 MB")
                    f.write(chunk)
            y = await anyio.to_thread.run_sync(decode, path)
            end = start + len(y) / 8000
            if session_id and not is_stateless():
                if session_id not in sessions:
                    raise ValueError("Unknown session")
                if time.time() - end < 20:
                    await asyncio.sleep(4)
                    await fetcher.fetch(lat, lon, radius_km)
            tracks = local.query(start, end, (lat, lon), radius_km)
            if not tracks and -10 < time.time() - end < 60:
                if is_stateless():
                    local.merge(await live_positions(lat, lon, radius_km))
                else:
                    await fetcher.fetch(lat, lon, radius_km)
                if is_stateless():
                    local.merge(project_positions(local.positions(), range(-60, 61, 4)))
                tracks = local.query(start, end, (lat, lon), radius_km)
            if not tracks:
                return JSONResponse(
                    status_code=422,
                    content=dict(detail=HISTORY_ERROR, coverage=coverage()),
                )
            return await anyio.to_thread.run_sync(
                lambda: locate(
                    y,
                    start,
                    tracks,
                    (lat, lon),
                    radius_km,
                    clock_search_s,
                    stateless=is_stateless(),
                )
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        finally:
            if path:
                path.unlink(missing_ok=True)
            await file.close()
            if local is not store:
                local.db.close()


class DemoRequest(BaseModel):
    lat: float | None = Field(default=None, ge=-85, le=85)
    lon: float | None = Field(default=None, ge=-180, le=180)


@app.post("/api/demo/synthetic")
async def demo(req: DemoRequest = DemoRequest()):
    """Build a portable synthetic recording from real ADS-B states."""
    region = (
        min(
            regions,
            key=lambda r: abs(r["lat"] - (req.lat or r["lat"]))
            + abs(r["lon"] - (req.lon or r["lon"])),
        )
        if regions
        else dict(lat=48.86, lon=2.35, radius_km=60)
    )
    if is_stateless():
        region = dict(lat=48.86, lon=2.35, radius_km=60)
    if req.lat is not None and req.lon is not None:
        region = Region(lat=req.lat, lon=req.lon, radius_km=60).model_dump()
    center = (region["lat"], region["lon"])
    now = time.time()
    local = TrackStore(":memory:") if is_stateless() else store
    if is_stateless():
        local.merge(await live_positions(*center, region["radius_km"]))
        local.merge(
            project_positions(
                [p for p in local.positions() if p["alt_m"] > 0],
                range(-60, 181, 4),
            )
        )
    tracks = local.query(
        now - 600, now + 180 if is_stateless() else now, center, region["radius_km"]
    )
    candidates = [
        a
        for a in tracks
        if np.ptp(a["t"]) >= 60
        and 200 < np.median(a["xyz"][-8:, 2]) < 2800
        and np.median(a["gs"][-8:]) > 40
    ]
    if not candidates:
        if local is not store:
            local.db.close()
        raise HTTPException(
            503,
            (
                "No suitable airborne aircraft in the live feed. Retry shortly. "
                if is_stateless()
                else "Need at least 60 seconds of recent moving aircraft fixes. Wait for the watch poller, then retry. "
            )
            + fetcher.status,
        )
    # Use actual cached fixes plus the same bounded 30-second extrapolation as analysis.
    a = min(candidates, key=lambda a: np.median(a["xyz"][-8:, 2]))
    start = float(now if is_stateless() else a["t"][-1] - 90)
    if start < a["t"][0] - 30:
        start = float(a["t"][0] - 30)
    duration = min(120, float(a["t"][-1] + 30 - start))
    midpoint = start + duration / 2 - 5
    mid = interpolate(a, np.array([midpoint]))[0][0, :2]
    rng = np.random.default_rng()
    heading = np.radians(a["heading"][np.argmin(abs(a["t"] - midpoint))])
    normal = np.array([np.cos(heading), -np.sin(heading)])
    truth = mid + normal * float(rng.uniform(500, 850))
    true = latlon(*truth, center)
    offset = rng.uniform(5000, 15000)
    angle = rng.uniform(0, 2 * np.pi)
    search = latlon(
        *(truth + offset * np.array([np.cos(angle), np.sin(angle)])), center
    )
    y = await anyio.to_thread.run_sync(
        lambda: synthesize(a, truth, start, duration, seed=int(rng.integers(1, 2**31)))
    )
    buffer = io.BytesIO()
    sf.write(buffer, y, 8000, format="WAV", subtype="PCM_16")
    portable = [
        p
        for p in local.positions(start - 90, start + duration + 30)
        if start - 90 <= p["t_unix"] <= start + duration + 30
        and np.linalg.norm(enu(p["lat"], p["lon"], center))
        < region["radius_km"] * 1000 + 15000
    ]
    if local is not store:
        local.db.close()
    inline = dict(
        wav_b64=base64.b64encode(buffer.getvalue()).decode(), tracks_json=portable
    )
    if is_stateless():
        return dict(
            **inline,
            source_aircraft=a["hex"],
            duration_s=duration,
            start_utc=start,
            region=dict(lat=search[0], lon=search[1], radius_km=25),
            true_lat=true[0],
            true_lon=true[1],
        )
    did = uuid.uuid4().hex
    folder = Path("data/demo")
    folder.mkdir(parents=True, exist_ok=True)
    sf.write(folder / f"{did}.wav", y, 8000)
    for old in folder.glob("*.wav"):
        if old.stat().st_mtime < time.time() - 86400:
            old.unlink()
    return dict(
        **inline,
        source_aircraft=a["hex"],
        duration_s=duration,
        demo_id=did,
        wav_url=f"/api/demo/{did}.wav",
        start_utc=start,
        region=dict(lat=search[0], lon=search[1], radius_km=25),
        true_lat=true[0],
        true_lon=true[1],
    )


@app.get("/api/demo/{did}.wav")
def demo_wav(did: str):
    if len(did) != 32 or any(c not in "0123456789abcdef" for c in did):
        raise HTTPException(404)
    p = Path("data/demo") / f"{did}.wav"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")
