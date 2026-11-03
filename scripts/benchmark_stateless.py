"""Measure the hosted solver with three minutes of audio and 40 aircraft."""

import time
import numpy as np
from app.locate import locate, synthesize
from app.tracks import enu


def main():
    """Print elapsed time and accuracy for a reproducible crowded region."""
    t = np.arange(-100, 281, 4.0)
    flight = dict(
        hex="target",
        flight="BENCH",
        type="TEST",
        t=t,
        xyz=np.column_stack(
            [(t - 90) * 70, np.full_like(t, 1000), np.full_like(t, 600)]
        ),
        gs=np.full_like(t, 70),
        heading=np.full_like(t, 90),
    )
    rng = np.random.default_rng(12)
    tracks = [flight]
    for i in range(39):
        tracks.append(
            dict(
                flight,
                hex=f"{i:06x}",
                xyz=flight["xyz"]
                + np.array(
                    [
                        rng.uniform(-22000, 22000),
                        rng.uniform(-22000, 22000),
                        rng.uniform(1000, 9000),
                    ]
                ),
            )
        )
    audio = synthesize(flight, [0, 0], 0, 180)
    then = time.perf_counter()
    result = locate(audio, 0, tracks, (48.86, 2.35), 25, 4, stateless=True)
    elapsed = time.perf_counter() - then
    error = np.linalg.norm(enu(**result["best_guess"], center=(48.86, 2.35)))
    print(
        f"180 seconds / 40 aircraft: {elapsed:.2f}s; location error {error/1000:.3f} km",
        flush=True,
    )
    assert elapsed < 25


if __name__ == "__main__":
    main()
