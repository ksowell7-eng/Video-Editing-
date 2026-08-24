"""Camera-path smoothing for the 16:9 → 9:16 reframe.

Raw Haar detections are noisy: the box jitters a few pixels every frame, drops
out entirely when the subject turns, and occasionally locks onto a false
positive in the crowd. Feeding those centers straight to a crop produces a
shaking frame. This module turns detections into a path a camera operator could
plausibly have shot.

The chain, in order, and why each link is there:

1. `fill_gaps`      — interpolate across dropouts so a lost face doesn't snap
                      the crop back to center and then back out again.
2. `apply_deadzone` — hold still until the subject has really moved. Small
                      motion reads as jitter; a held frame reads as intent.
3. `keyframes`      — decimate to one median-filtered anchor every N seconds.
                      The median is what rejects the false positive: a single
                      bad detection inside a window can't move the anchor.
4. `pchip`          — monotone cubic Hermite through those anchors. Plain
                      cubic splines overshoot at direction changes, which on a
                      camera path means swinging past the subject and coming
                      back. The Fritsch-Carlson slope limiter makes overshoot
                      impossible while staying C1-smooth.
5. `clamp_velocity` — cap pan rate so fast subject motion becomes a deliberate
                      follow rather than a whip.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "fill_gaps",
    "apply_deadzone",
    "keyframes",
    "pchip",
    "clamp_velocity",
    "smooth_path",
]


def fill_gaps(values: np.ndarray, fallback: float) -> np.ndarray:
    """Linearly interpolate NaN runs; extend the edges with the nearest value.

    An all-NaN input (nothing was ever detected) becomes a constant `fallback`.
    """
    v = np.asarray(values, dtype=float).copy()
    known = ~np.isnan(v)
    if not known.any():
        return np.full_like(v, float(fallback))
    idx = np.arange(v.size)
    v[~known] = np.interp(idx[~known], idx[known], v[known])
    return v


def apply_deadzone(values: np.ndarray, deadzone: float) -> np.ndarray:
    """Hold the last committed position until the target escapes the deadzone.

    Once it escapes, commit to the target minus the deadzone radius, so the
    move starts from the edge of the tolerance rather than jumping the full
    distance — the resulting step is the size of the real motion, not the
    accumulated slack.
    """
    v = np.asarray(values, dtype=float)
    if deadzone <= 0 or v.size == 0:
        return v.copy()
    out = np.empty_like(v)
    held = v[0]
    for i, target in enumerate(v):
        delta = target - held
        if abs(delta) > deadzone:
            held = target - np.sign(delta) * deadzone
        out[i] = held
    return out


def keyframes(times: np.ndarray, values: np.ndarray, interval: float) -> tuple[np.ndarray, np.ndarray]:
    """Median-decimate to one anchor per `interval` seconds.

    Returns (anchor_times, anchor_values), always including an anchor at the
    first and last sample so the path spans the full clip.
    """
    t = np.asarray(times, dtype=float)
    v = np.asarray(values, dtype=float)
    if t.size == 0:
        return t, v
    if t.size == 1 or interval <= 0:
        return t.copy(), v.copy()

    edges = np.arange(t[0], t[-1] + interval, interval)
    if edges.size < 2:
        edges = np.array([t[0], t[-1]])
    bins = np.clip(np.digitize(t, edges) - 1, 0, edges.size - 1)

    kt: list[float] = []
    kv: list[float] = []
    for b in range(edges.size):
        mask = bins == b
        if not mask.any():
            continue
        kt.append(float(np.median(t[mask])))
        kv.append(float(np.median(v[mask])))

    kt_arr, kv_arr = np.array(kt), np.array(kv)
    if kt_arr[0] > t[0]:
        kt_arr, kv_arr = np.r_[t[0], kt_arr], np.r_[v[0], kv_arr]
    if kt_arr[-1] < t[-1]:
        kt_arr, kv_arr = np.r_[kt_arr, t[-1]], np.r_[kv_arr, v[-1]]
    return kt_arr, kv_arr


def _pchip_slopes(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson tangents: shape-preserving, so the curve never overshoots."""
    n = x.size
    if n == 1:
        return np.zeros(1)
    h = np.diff(x)
    h[h == 0] = 1e-9
    delta = np.diff(y) / h
    m = np.zeros(n)
    if n == 2:
        return np.full(2, delta[0])

    # Interior tangents: weighted harmonic mean of neighbouring secants, and
    # exactly zero wherever the secants disagree in sign (a local extremum).
    for i in range(1, n - 1):
        if delta[i - 1] * delta[i] <= 0:
            m[i] = 0.0
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])

    # One-sided ends, clipped so they can't overshoot the first/last secant.
    def _end(d0: float, d1: float, h0: float, h1: float) -> float:
        m_end = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if m_end * d0 <= 0:
            return 0.0
        if abs(m_end) > 3 * abs(d0) and d0 * d1 <= 0:
            return 3 * d0
        return m_end

    m[0] = _end(delta[0], delta[1], h[0], h[1])
    m[-1] = _end(delta[-1], delta[-2], h[-1], h[-2])
    return m


def pchip(kt: np.ndarray, kv: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Evaluate a monotone cubic Hermite spline through (kt, kv) at times t."""
    kt = np.asarray(kt, dtype=float)
    kv = np.asarray(kv, dtype=float)
    t = np.asarray(t, dtype=float)
    if kt.size == 0:
        return np.zeros_like(t)
    if kt.size == 1:
        return np.full_like(t, kv[0])

    m = _pchip_slopes(kt, kv)
    idx = np.clip(np.searchsorted(kt, t, side="right") - 1, 0, kt.size - 2)
    x0, x1 = kt[idx], kt[idx + 1]
    y0, y1 = kv[idx], kv[idx + 1]
    m0, m1 = m[idx], m[idx + 1]
    h = x1 - x0
    h = np.where(h == 0, 1e-9, h)
    s = np.clip((t - x0) / h, 0.0, 1.0)
    s2, s3 = s * s, s * s * s

    # Hermite basis
    h00 = 2 * s3 - 3 * s2 + 1
    h10 = s3 - 2 * s2 + s
    h01 = -2 * s3 + 3 * s2
    h11 = s3 - s2
    return h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1


def clamp_velocity(times: np.ndarray, values: np.ndarray, max_rate: float) -> np.ndarray:
    """Limit |d(value)/dt| to max_rate, forward then backward.

    The forward pass alone biases the path late (it can only ever lag). Running
    it again in reverse and averaging the two keeps the result centred on the
    original motion while still respecting the rate limit.
    """
    t = np.asarray(times, dtype=float)
    v = np.asarray(values, dtype=float)
    if max_rate <= 0 or v.size < 2:
        return v.copy()

    def sweep(order: np.ndarray) -> np.ndarray:
        out = v.copy()
        prev = out[order[0]]
        prev_t = t[order[0]]
        for i in order[1:]:
            dt = abs(t[i] - prev_t)
            limit = max_rate * dt
            delta = np.clip(v[i] - prev, -limit, limit)
            prev = prev + delta
            prev_t = t[i]
            out[i] = prev
        return out

    forward = sweep(np.arange(v.size))
    backward = sweep(np.arange(v.size)[::-1])
    return 0.5 * (forward + backward)


def smooth_path(
    times,
    observations,
    *,
    fallback: float,
    lower: float,
    upper: float,
    deadzone: float = 0.0,
    keyframe_interval_s: float = 0.5,
    max_rate: float = 240.0,
) -> np.ndarray:
    """Full chain: noisy per-sample observations → a bounded, smooth camera path.

    `observations` may contain NaN for frames with no detection. `lower`/`upper`
    bound the returned centers so the crop window always stays inside the frame.
    """
    t = np.asarray(times, dtype=float)
    raw = np.asarray(observations, dtype=float)
    if t.size == 0:
        return np.zeros(0)
    if lower > upper:  # crop is wider than the source; there is nowhere to pan
        lower = upper = 0.5 * (lower + upper)

    filled = fill_gaps(raw, fallback)
    held = apply_deadzone(filled, deadzone)
    kt, kv = keyframes(t, held, keyframe_interval_s)
    curve = pchip(kt, kv, t)
    limited = clamp_velocity(t, curve, max_rate)
    return np.clip(limited, lower, upper)
