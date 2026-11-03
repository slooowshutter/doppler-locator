import asyncio
import math
import sqlite3
import time
import os
from pathlib import Path
import httpx
import numpy as np

HISTORY_ERROR = "No aircraft history cached for that time/region — recordings must be made via Record mode or inside a watch region while the server was running"


def enu(lat, lon, center):
    """Convert latitude and longitude to local east/north meters."""
    return np.asarray(
        [
            (np.asarray(lon) - center[1]) * 111320 * math.cos(math.radians(center[0])),
            (np.asarray(lat) - center[0]) * 111320,
        ]
    ).T


def latlon(x, y, center):
    """Convert local east/north meters to geographic coordinates."""
    return float(center[0] + y / 111320), float(
        center[1] + x / (111320 * math.cos(math.radians(center[0])))
    )


class TrackStore:
    def __init__(self, path=None):
        path = path or (
            "/tmp/doppler-tracks.sqlite"
            if os.getenv("VERCEL") is not None or os.getenv("DOPPLER_STATELESS") == "1"
            else "data/tracks.sqlite"
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS positions(hex TEXT,t_unix REAL,lat REAL,lon REAL,alt_m REAL,gs REAL,track REAL,flight TEXT,type TEXT,src TEXT,PRIMARY KEY(hex,t_unix))"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS position_time ON positions(t_unix)")

    def ingest(self, payload, src):
        """Normalize feed timestamps, altitude and velocity before storing fixes."""
        now = float(payload.get("now", time.time()))
        if now > 1e11:
            now /= 1000
        rows = []
        for a in payload.get("ac", payload.get("aircraft", [])):
            if "lat" not in a or "lon" not in a:
                continue
            alt = a.get("alt_geom", a.get("alt_baro", 0))
            try:
                rows.append(
                    (
                        a["hex"],
                        round((now - float(a.get("seen_pos", 0))) * 2) / 2,
                        a["lat"],
                        a["lon"],
                        0 if alt == "ground" else float(alt or 0) * 0.3048,
                        float(a.get("gs", 0) or 0) * 0.514444,
                        float(a.get("track", 0) or 0),
                        a.get("flight", "").strip(),
                        a.get("t", ""),
                        src,
                    )
                )
            except (ValueError, TypeError, KeyError):
                continue
        self.db.executemany(
            "INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.db.execute(
            "DELETE FROM positions WHERE t_unix < ?", (time.time() - 14 * 86400,)
        )
        self.db.commit()
        return len(rows)

    def positions(self, t0=0, t1=4102444800):
        """Return portable positions in SI units, with actual position timestamps."""
        keys = ("hex", "t_unix", "lat", "lon", "alt_m", "gs", "track", "flight", "type")
        return [
            dict(zip(keys, row))
            for row in self.db.execute(
                "SELECT hex,t_unix,lat,lon,alt_m,gs,track,flight,type FROM positions WHERE t_unix BETWEEN ? AND ? ORDER BY t_unix",
                (t0, t1),
            )
        ]

    def merge(self, positions):
        """Merge validated client positions without altering their units."""
        self.db.executemany(
            "INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    p["hex"],
                    round(p["t_unix"] * 2) / 2,
                    p["lat"],
                    p["lon"],
                    p["alt_m"],
                    p["gs"],
                    p["track"],
                    p.get("flight", ""),
                    p.get("type", ""),
                    "client",
                )
                for p in positions
            ],
        )
        self.db.commit()

    def query(self, t0, t1, center, radius_km):
        """Group fixes in a buffered time window into local aircraft tracks."""
        rows = self.db.execute(
            "SELECT * FROM positions WHERE t_unix BETWEEN ? AND ? ORDER BY t_unix",
            (t0 - 30, t1 + 30),
        ).fetchall()
        grouped = {}
        for r in rows:
            if np.linalg.norm(enu(r[2], r[3], center)) > radius_km * 1000 + 15000:
                continue
            d = grouped.setdefault(
                r[0],
                dict(hex=r[0], flight=r[7], type=r[8], t=[], xyz=[], gs=[], heading=[]),
            )
            d["t"].append(r[1])
            d["xyz"].append([*enu(r[2], r[3], center), r[4]])
            d["gs"].append(r[5])
            d["heading"].append(r[6])
        for d in grouped.values():
            for k in ("t", "xyz", "gs", "heading"):
                d[k] = np.asarray(d[k])
        return list(grouped.values())

    def coverage(self, regions):
        out = []
        for r in regions:
            bounds = self.db.execute(
                "SELECT min(t_unix),max(t_unix) FROM positions WHERE abs(lat-?) < ? AND abs(lon-?) < ?",
                (
                    r["lat"],
                    r["radius_km"] / 111,
                    r["lon"],
                    r["radius_km"] / (111 * math.cos(math.radians(r["lat"]))),
                ),
            ).fetchone()
            out.append(dict(**r, earliest=bounds[0], latest=bounds[1]))
        return out

    def count(self):
        return self.db.execute("SELECT count(*) FROM positions").fetchone()[0]


class Fetcher:
    def __init__(self, store):
        self.store = store
        self.last = {}
        self.locks = {}
        self.retry = {}
        self.status = "Waiting for first poll"

    async def fetch(self, lat, lon, radius_km):
        """Fetch a free aircraft feed with throttling and failure backoff."""
        key = (round(lat, 2), round(lon, 2), round(radius_km))
        async with self.locks.setdefault(key, asyncio.Lock()):
            if time.monotonic() - self.last.get(
                key, -1e9
            ) < 3 or time.monotonic() < self.retry.get(key, 0):
                return
            self.last[key] = time.monotonic()
            nm = min(250, radius_km / 1.852)
            urls = [
                f"https://api.adsb.lol/v2/point/{lat}/{lon}/{nm:.1f}",
                f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm:.1f}",
            ]
            async with httpx.AsyncClient(
                timeout=(
                    8
                    if os.getenv("VERCEL") is not None
                    or os.getenv("DOPPLER_STATELESS") == "1"
                    else 12
                ),
                headers={
                    "User-Agent": "DopplerLocator/1.0 (open aircraft acoustic research)"
                },
            ) as client:
                for url in urls:
                    try:
                        res = await client.get(url)
                        res.raise_for_status()
                        n = self.store.ingest(res.json(), url.split("/")[2])
                        self.status = f"Latest poll: {n} positions"
                        return
                    except (httpx.HTTPError, ValueError):
                        continue
            self.status = "Aircraft feeds unavailable; retrying with backoff"
            self.retry[key] = time.monotonic() + 24


def interpolate(track, t):
    """Interpolate fixes and extrapolate velocity for at most 30 seconds."""
    xyz = track["xyz"]
    ts = track["t"]
    out = np.column_stack([np.interp(t, ts, xyz[:, i]) for i in range(3)])
    for index, mask in [(0, t < ts[0]), (-1, t > ts[-1])]:
        h = np.radians(track["heading"][index])
        v = np.array([np.sin(h), np.cos(h), 0]) * track["gs"][index]
        out[mask] = xyz[index] + (t[mask] - ts[index])[:, None] * v
    valid = (t >= ts[0] - 30) & (t <= ts[-1] + 30)
    return out, valid
