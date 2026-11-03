import math
import sys
import time
import base64
import json
import httpx


def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    return (
        6371
        * 2
        * math.asin(
            min(
                1,
                math.sqrt(
                    math.sin((lat2 - lat1) / 2) ** 2
                    + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
                ),
            )
        )
    )


def main(base):
    """Run a live end-to-end smoke check against a server."""
    with httpx.Client(base_url=base, timeout=240) as c:
        r = c.get("/healthz")
        r.raise_for_status()
        print("Health:", r.json())
        r = c.post("/api/demo/synthetic", json={})
        r.raise_for_status()
        demo = r.json()
        print("Demo source:", demo.get("source_aircraft", "unknown"))
        if demo.get("wav_b64"):
            wav = base64.b64decode(demo["wav_b64"])
        else:
            r = c.get(demo["wav_url"])
            r.raise_for_status()
            wav = r.content
        then = time.monotonic()
        r = c.post(
            "/api/analyze",
            files={"file": ("demo.wav", wav, "audio/wav")},
            data=dict(
                start_utc=str(demo["start_utc"]),
                **demo["region"],
                clock_search_s=4,
                tracks_json=json.dumps(demo.get("tracks_json", [])),
            ),
        )
        if r.is_error:
            print(r.text)
        r.raise_for_status()
        result = r.json()
        assert result["heatmap"]
        error = distance(
            (demo["true_lat"], demo["true_lon"]),
            (result["best_guess"]["lat"], result["best_guess"]["lon"]),
        )
        print(
            f'Error distance: {error:.3f} km; analysis {time.monotonic()-then:.1f}s; {result["confidence"]}; flight {result["winning_aircraft"]["flight"]}'
        )
        if error >= 2:
            print(
                "Demo truth:",
                (demo["true_lat"], demo["true_lon"]),
                "Winning fit:",
                result["winning_aircraft"],
            )
        assert error < 2, f"Synthetic demo error {error:.2f} km exceeds 2 km"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787")
