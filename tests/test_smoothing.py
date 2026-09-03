"""The camera path is the part a viewer notices when it's wrong."""

import numpy as np
import pytest

from pipeline.vision.smoothing import (
    apply_deadzone, clamp_velocity, fill_gaps, keyframes, pchip, smooth_path,
)


def test_fill_gaps_interpolates_dropouts():
    values = np.array([10.0, np.nan, np.nan, 40.0])
    assert fill_gaps(values, 0.0).tolist() == [10.0, 20.0, 30.0, 40.0]


def test_fill_gaps_extends_edges_from_nearest_known_value():
    values = np.array([np.nan, 5.0, np.nan])
    assert fill_gaps(values, 99.0).tolist() == [5.0, 5.0, 5.0]


def test_fill_gaps_uses_fallback_when_nothing_was_detected():
    values = np.full(4, np.nan)
    assert fill_gaps(values, 640.0).tolist() == [640.0] * 4


def test_deadzone_holds_until_the_subject_really_moves():
    values = np.array([100.0, 110.0, 105.0, 300.0])
    held = apply_deadzone(values, deadzone=50.0)
    assert held[0] == held[1] == held[2] == 100.0   # jitter absorbed
    assert held[3] == pytest.approx(250.0)          # committed, minus the radius


def test_deadzone_of_zero_is_a_passthrough():
    values = np.array([1.0, 2.0, 3.0])
    assert apply_deadzone(values, 0.0).tolist() == values.tolist()


def test_keyframes_take_the_median_and_reject_outliers():
    times = np.arange(0, 1.0, 0.1)
    values = np.full(times.size, 100.0)
    values[3] = 9000.0                              # a single false detection
    kt, kv = keyframes(times, values, interval=0.5)
    assert kv.max() < 200.0
    assert kt[0] == pytest.approx(times[0])
    assert kt[-1] == pytest.approx(times[-1])


def test_pchip_passes_through_its_anchors():
    kt = np.array([0.0, 1.0, 2.0])
    kv = np.array([0.0, 10.0, 5.0])
    assert pchip(kt, kv, kt).tolist() == pytest.approx(kv.tolist())


def test_pchip_never_overshoots_a_local_extremum():
    # The whole reason for monotone cubic: a plain spline would swing past 10
    # here, which on a camera path means panning off the subject and back.
    kt = np.array([0.0, 1.0, 2.0, 3.0])
    kv = np.array([0.0, 10.0, 10.0, 0.0])
    dense = pchip(kt, kv, np.linspace(0, 3, 200))
    assert dense.max() <= 10.0 + 1e-9
    assert dense.min() >= 0.0 - 1e-9


def test_pchip_with_a_single_anchor_is_constant():
    assert pchip(np.array([1.0]), np.array([7.0]), np.array([0.0, 5.0])).tolist() == [7.0, 7.0]


def test_clamp_velocity_enforces_the_rate_limit():
    times = np.arange(0, 2, 0.1)
    values = np.where(times < 1.0, 0.0, 1000.0)     # an instantaneous jump
    limited = clamp_velocity(times, values, max_rate=100.0)
    rates = np.abs(np.diff(limited)) / np.diff(times)
    assert rates.max() <= 100.0 + 1e-6


def test_smooth_path_stays_inside_the_crop_bounds():
    times = np.arange(0, 4, 1 / 30)
    observations = 960 + 2000 * np.sin(times)      # far outside any real frame
    path = smooth_path(
        times, observations, fallback=960, lower=304, upper=1616,
        deadzone=30, keyframe_interval_s=0.5, max_rate=240,
    )
    assert path.min() >= 304 - 1e-6
    assert path.max() <= 1616 + 1e-6
    assert path.size == times.size


def test_smooth_path_respects_the_pan_rate():
    times = np.arange(0, 3, 1 / 30)
    rng = np.random.default_rng(0)
    observations = 500 + 300 * np.sin(times * 3) + rng.normal(0, 20, times.size)
    path = smooth_path(
        times, observations, fallback=500, lower=0, upper=1920,
        deadzone=20, keyframe_interval_s=0.5, max_rate=200,
    )
    rates = np.abs(np.diff(path)) / np.diff(times)
    assert rates.max() <= 200 + 1e-6


def test_smooth_path_handles_a_crop_with_no_room_to_pan():
    times = np.arange(0, 1, 0.1)
    path = smooth_path(
        times, np.full(times.size, np.nan), fallback=540,
        lower=540, upper=539,                      # inverted: crop wider than source
        deadzone=10, keyframe_interval_s=0.5, max_rate=100,
    )
    assert np.allclose(path, path[0])


def test_smooth_path_on_empty_input():
    assert smooth_path([], [], fallback=0, lower=0, upper=1).size == 0
