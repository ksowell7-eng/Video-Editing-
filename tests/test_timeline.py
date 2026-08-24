"""Phase placement and the media cursor."""

import pytest

from pipeline.compose.timeline import (
    Clip, MediaCursor, Timeline, drift_report, place_phases,
)
from pipeline.config import DEFAULTS, Phase


def phases():
    return [Phase(**p) for p in DEFAULTS["phases"]]


def test_phases_are_laid_end_to_end_with_no_gaps():
    placed = place_phases(phases(), {})
    for a, b in zip(placed, placed[1:]):
        assert a.end == pytest.approx(b.start)
    assert placed[0].start == 0.0


def test_measured_voiceover_duration_wins_over_the_target():
    placed = place_phases(phases(), {"hook": 6.5})
    assert placed[0].duration == 6.5
    assert placed[1].start == 6.5


def test_a_zero_duration_measurement_falls_back_to_the_target():
    placed = place_phases(phases(), {"hook": 0})
    assert placed[0].duration == DEFAULTS["phases"][0]["target_s"]


def test_drift_report_shows_the_difference():
    report = drift_report(place_phases(phases(), {"hook": 6.0}), phases())
    hook = next(r for r in report if r["phase"] == "hook")
    assert hook["drift_s"] == pytest.approx(2.0)


class TestMediaCursor:
    def test_successive_takes_do_not_repeat_the_same_footage(self):
        cursor = MediaCursor(20.0)
        assert cursor.take(5) != cursor.take(5)

    def test_it_wraps_instead_of_running_off_the_end(self):
        cursor = MediaCursor(10.0)
        for _ in range(5):
            start = cursor.take(4)
            assert 0 <= start <= 6

    def test_a_source_shorter_than_the_request_starts_at_zero(self):
        assert MediaCursor(2.0).take(5) == 0.0

    def test_an_empty_source_is_safe(self):
        assert MediaCursor(0).take(3) == 0.0


class TestTimeline:
    def test_duration_covers_every_clip(self):
        timeline = Timeline(width=1080, height=1920, fps=30)
        timeline.add(Clip(id="a", kind="video", start=0, duration=4, track=0))
        timeline.add(Clip(id="b", kind="audio", start=3, duration=5, track=3))
        assert timeline.duration == 8.0

    def test_tracks_are_queryable(self):
        timeline = Timeline(width=1080, height=1920, fps=30)
        timeline.add(Clip(id="a", kind="video", start=0, duration=1, track=0))
        timeline.add(Clip(id="b", kind="audio", start=0, duration=1, track=3))
        assert [c.id for c in timeline.on_track(0)] == ["a"]

    def test_an_empty_timeline_has_no_duration(self):
        assert Timeline(width=1080, height=1920, fps=30).duration == 0.0
