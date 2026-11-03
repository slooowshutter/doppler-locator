import numpy as np

from .tracks import interpolate, latlon
from .audio import spectrogram, png_image, SR

C = 340.0
MAX_RANGE = 12000.0


def doppler(track, point, arrival):
    """Map retarded emission time to received frequency and slant range."""
    te = np.arange(arrival.min() - 70, arrival.max() + 2, 0.5)
    a, valid = interpolate(track, te)
    r = np.linalg.norm(a - np.r_[point, 0], axis=1)
    rate = np.gradient(r, 0.5)
    ta = te + r / C
    g = np.interp(arrival, ta, C / (C + rate))
    ranges = np.interp(arrival, ta, r)
    ok = np.interp(arrival, ta, valid.astype(float)) > 0.99
    return g, ranges, ok


def synthesize(track, point, start, duration=120, seed=7):
    """Generate a noisy harmonic recording using the physical Doppler curve."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration * SR)) / SR
    grid = np.arange(0, duration + 0.02, 0.02)
    g, r, valid = doppler(track, point, start + grid)
    freq = np.interp(t, grid, g) * 183.7
    phase = np.cumsum(freq) * 2 * np.pi / SR
    amp = np.interp(t, grid, np.where(valid, 1 / np.maximum(r, 400), 0))
    amp /= max(amp.max(), 1e-9)
    noise = rng.normal(0, 1, len(t))
    noise = np.convolve(noise, np.ones(5) / 5, mode="same")
    y = (
        amp * (np.sin(phase) + 0.45 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase))
        + 0.12 * noise
        + 0.12 * np.sin(2 * np.pi * 50 * t)
        + 0.08 * np.sin(2 * np.pi * rng.uniform(240, 550) * t)
    )
    return (y / max(np.max(np.abs(y)), 1) * 0.85).astype("float32")


def locate(y, start, tracks, center, radius_km=25, clock_search_s=4, stateless=False):
    """Match acoustic ridges against aircraft over a bounded search grid."""
    from scipy.ndimage import gaussian_filter1d, maximum_filter1d

    if not tracks:
        raise ValueError("No aircraft in window")
    spec = spectrogram(y)
    stride = max(4, int(np.ceil(len(spec["t"]) / 120)))
    times = start + spec["t"][::stride]
    w = gaussian_filter1d(spec["w"][::stride], 0.65, axis=1)
    freq = spec["freq"]
    logstep = np.log(freq[1] / freq[0])
    ks = np.arange(0, len(freq), 2)
    # Harmonic support is computed once on the log axis, then shifted as one curve.
    bins = np.arange(len(freq))
    harmonics = w.copy()
    for harmonic, weight in [(2, 0.55), (3, 0.3)]:
        shift = np.log(harmonic) / logstep
        harmonics += weight * np.array(
            [np.interp(bins + shift, bins, row, right=0) for row in w]
        )
    w = harmonics
    clocks = (
        np.arange(-clock_search_s, clock_search_s + 0.1, 2)
        if clock_search_s
        else np.array([0.0])
    )
    if stateless and len(clocks) > 5:
        clocks = np.unique(np.r_[np.linspace(-clock_search_s, clock_search_s, 5), 0.0])
    # Keep all aircraft; discard those whose fixes cannot reach any search cell.
    tracks = [
        a
        for a in tracks
        if np.min(np.linalg.norm(a["xyz"][:, :2], axis=1))
        < radius_km * 1000 + MAX_RANGE + 4000
    ]
    details = {}
    top_two = {}
    aircraft_peaks = {}
    any_audible = False

    def evaluate(points, aircraft_ids=None, collect=False):
        nonlocal any_audible
        flat_prefix = np.vstack([np.zeros((1, len(ks))), np.cumsum(w[:, ks], axis=0)])
        scores = np.zeros(len(points))
        second = np.zeros(len(points))
        winners = [None] * len(points)
        for ai, a in enumerate(tracks):
            if aircraft_ids is not None and ai not in aircraft_ids:
                continue
            te = np.arange(
                times.min() - 70 - clock_search_s, times.max() + clock_search_s + 2, 0.5
            )
            positions, fixvalid = interpolate(a, te)
            for pi, p in enumerate(points):
                rr = np.linalg.norm(positions - np.r_[p, 0], axis=1)
                if not np.any((rr < MAX_RANGE) & fixvalid):
                    continue
                rate = np.gradient(rr, 0.5)
                arrival = te + rr / C
                gg = C / (C + rate)
                best = None
                for clock in clocks:
                    g = np.interp(times + clock, arrival, gg)
                    r = np.interp(times + clock, arrival, rr)
                    valid = (
                        np.interp(times + clock, arrival, fixvalid.astype(float)) > 0.99
                    )
                    mask = valid & (r < MAX_RANGE)
                    if mask.sum() * stride * 0.25 < 20:
                        continue
                    any_audible = True
                    off = np.log(g[mask]) / logstep
                    if np.ptp(off) < 0.9:
                        continue
                    indexes = ks[None, :] + off[:, None]
                    in_band = (indexes >= 0) & (indexes < len(freq) - 1)
                    indexes = np.clip(indexes, 0, len(freq) - 2)
                    indexes = np.rint(indexes).astype(int)
                    ids = np.flatnonzero(mask)
                    frames = ids[:, None]
                    s = np.mean(w[frames, indexes] * in_band, axis=0)
                    z = (s - s.mean()) / (s.std() + 1e-6)
                    k = int(np.argmax(z))
                    if ids[-1] - ids[0] + 1 == len(ids):
                        flat_best = float(
                            (
                                (flat_prefix[ids[-1] + 1] - flat_prefix[ids[0]])
                                / len(ids)
                            ).max()
                        )
                    else:
                        flat_best = float(np.mean(w[mask][:, ks], axis=0).max())
                    score = (
                        max(0, float(s[k]) - max(float(s.mean()), flat_best))
                        * min(1, mask.sum() * stride * 0.25 / 60) ** 0.5
                    )
                    # Reward coherent ridge strength; isolated noise peaks have low absolute support.
                    score *= min(1, float(z[k]) / 6.0)
                    if best is None or score > best[0]:
                        best = (score, int(ks[k]), float(clock), float(z[k]))
                if best:
                    if collect and (
                        ai not in aircraft_peaks or best[0] > aircraft_peaks[ai][0]
                    ):
                        aircraft_peaks[ai] = (best[0], p.copy())
                    if best[0] > scores[pi]:
                        second[pi] = scores[pi]
                        scores[pi] = best[0]
                        winners[pi] = (ai, *best)
                    else:
                        second[pi] = max(second[pi], best[0])
        for p, s, d, s2 in zip(points, scores, winners, second):
            details[tuple(p)] = (float(s), d)
            top_two[tuple(p)] = float(s + s2)
        return scores

    grid_step = max(1000, radius_km * 1000 / 25) if stateless else 1000
    axis = np.arange(-radius_km * 1000, radius_km * 1000 + 1, grid_step)
    points = np.array(
        [(x, y) for y in axis for x in axis if x * x + y * y <= (radius_km * 1000) ** 2]
    )
    # A broad first pass prevents narrow low-altitude ridges falling between 1 km cells.
    narrow_w = w
    full_clocks = clocks
    full_ks = ks
    full_times = times
    full_stride = stride
    w = maximum_filter1d(narrow_w[::2], size=11, axis=1)
    times = full_times[::2]
    stride = full_stride * 2
    clocks = np.unique(np.r_[clocks[:: max(1, int(np.ceil(len(clocks) / 3)))], 0])
    ks = ks[::2]
    broad = evaluate(points, collect=True)
    peaks = []
    for i in np.argsort(broad)[::-1]:
        if all(np.linalg.norm(points[i] - p) > 3000 for p in peaks):
            peaks.append(points[i])
        if len(peaks) == 3:
            break
    fine = np.unique(
        np.array(
            [
                (p[0] + x, p[1] + y)
                for p in peaks
                for x in (
                    np.arange(-2000, 2001, 400)
                    if stateless
                    else np.arange(-2000, 2001, 200)
                )
                for y in (
                    np.arange(-2000, 2001, 400)
                    if stateless
                    else np.arange(-2000, 2001, 200)
                )
                if np.linalg.norm(p + [x, y]) <= radius_km * 1000
            ]
        ),
        axis=0,
    )
    w = narrow_w[::2]
    clocks = full_clocks
    ks = full_ks[::2] if stateless else full_ks
    coarse = evaluate(points)
    ks = full_ks
    w = narrow_w
    times = full_times
    stride = full_stride
    if len(fine):
        evaluate(fine)
    # Preserve a competing aircraft whose narrow ridge was missed by the shared coarse grid.
    for ai, (support, p) in sorted(
        aircraft_peaks.items(), key=lambda item: item[1][0], reverse=True
    )[:8]:
        if support < 0.45 * max(broad):
            continue
        patch = np.array(
            [
                (p[0] + x, p[1] + yy)
                for x in np.arange(-2000, 2001, 400)
                for yy in np.arange(-2000, 2001, 400)
                if np.linalg.norm(p + [x, yy]) <= radius_km * 1000
            ]
        )
        prior = details.copy()
        prior_two = top_two.copy()
        if len(patch):
            patch_scores = evaluate(patch, {ai})
            local = patch[int(np.argmax(patch_scores))]
            small = np.array(
                [
                    (local[0] + x, local[1] + yy)
                    for x in np.arange(-400, 401, 200)
                    for yy in np.arange(-400, 401, 200)
                    if np.linalg.norm(local + [x, yy]) <= radius_km * 1000
                ]
            )
            if len(small):
                evaluate(small, {ai})
        for key, (s, d) in prior.items():
            if key in details and s > details[key][0]:
                details[key] = (s, d)
                top_two[key] = prior_two[key]
    ranked = sorted(
        details.items(),
        key=lambda v: (round(v[1][0], 6), -np.linalg.norm(v[0])),
        reverse=True,
    )
    if not any_audible:
        raise ValueError("No plane close enough")
    point, (score, win) = ranked[0]
    if win is None or score < 3 or win[-1] < 3:
        raise ValueError("No Doppler curve detected — no aircraft clearly heard")
    ai, _, ki, clock, zscore = win
    a = tracks[ai]
    f0 = float(freq[ki])
    g, r, valid = doppler(a, point, start + spec["t"] + clock)
    curve = [
        dict(time=float(t), freq=float(f0 * gg))
        for t, gg, ok, rr in zip(spec["t"], g, valid, r)
        if ok and rr < MAX_RANGE
    ]
    overlay = [
        (v["time"] / spec["t"][-1], np.log(v["freq"] / 40) / np.log(2000 / 40))
        for v in curve
    ]
    bestlat, bestlon = latlon(*point, center)
    top = []
    for p, (s, d) in ranked:
        lat, lon = latlon(*p, center)
        if all((lat - v["lat"]) ** 2 + (lon - v["lon"]) ** 2 > 0.015**2 for v in top):
            top.append(dict(lat=lat, lon=lon, score=float(s)))
        if len(top) == 3:
            break
    sharp = float(score / (np.percentile(coarse, 90) + 1e-6))
    area = int(sum(s >= 0.9 * score for s in coarse))
    confidence = (
        "high"
        if score > 6 and sharp > 1.4 and area < 30
        else "medium" if score > 4 else "low"
    )
    coarse_set = set(map(tuple, points))
    matched = dict(
        hex=a["hex"],
        flight=a["flight"],
        type=a["type"],
        f0=f0,
        z_score=zscore,
        ridge_score=score,
        clock_offset_s=clock,
        closest_approach_m=float(r.min()),
    )
    return dict(
        best_guess=dict(lat=bestlat, lon=bestlon),
        confidence=confidence,
        explanation=f"{confidence.capitalize()} confidence: ridge z-score {zscore:.1f}; peak / grid 90th percentile {sharp:.2f}. A single straight flight can leave mirror ambiguity.",
        peak_sharpness=sharp,
        near_peak_area_km2=area,
        top_peaks=top,
        winning_aircraft=matched,
        aircraft=[matched],
        predicted_curve=curve,
        comparison_curve=[dict(time=float(t), freq=f0) for t in spec["t"][::4]],
        heatmap=[
            dict(
                lat=latlon(*p, center)[0],
                lon=latlon(*p, center)[1],
                score=float(s),
                top_two_sum=top_two[tuple(p)],
                size_m=1000 if tuple(p) in coarse_set else 200,
            )
            for p, (s, d) in details.items()
        ],
        tracks=[
            dict(
                hex=b["hex"],
                flight=b["flight"],
                points=[list(latlon(v[0], v[1], center)) for v in b["xyz"]],
            )
            for b in tracks
        ],
        spectrogram=png_image(spec["w"], overlay),
        raw_spectrogram=png_image(spec["raw"][:, freq <= 600]),
        spectrogram_axes=dict(
            time_start=float(spec["t"][0]),
            time_end=float(spec["t"][-1]),
            frequency_min=40,
            frequency_max=2000,
        ),
    )
