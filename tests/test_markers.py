"""Marker placement: the phrase has to land where the browser measured it."""

import pytest

from pipeline.compose.markers import build_markers, frame_phrase, score_phrase, select_phrases
from pipeline.compose.timeline import PlacedPhase


def phrase(x=100, y=1500, w=320, h=22, text="keeper stayed rooted", score=0.9):
    return {"text": text, "score": score, "rect": {"x": x, "y": y, "w": w, "h": h}}


class TestScoring:
    def test_a_quoted_phrase_scores_high(self):
        assert score_phrase("The keeper stayed rooted as it curled in", "keeper stayed rooted") > 0.8

    def test_an_unrelated_line_scores_zero(self):
        assert score_phrase("Completely different subject", "keeper stayed rooted") == 0.0

    def test_stopwords_alone_do_not_match(self):
        assert score_phrase("and the of it", "the and of") == 0.0


class TestSelection:
    def test_a_phrase_is_never_used_by_two_phases(self):
        phrases = [phrase(text="keeper stayed rooted"), phrase(text="Marseille took the cup")]
        chosen = select_phrases(
            {"hook": "the keeper stayed rooted", "payoff": "the keeper stayed rooted"},
            phrases, max_per_phase=1,
        )
        picked = [p["text"] for phase_picks in chosen.values() for p in phase_picks]
        assert len(picked) == len(set(picked))

    def test_weak_matches_are_dropped(self):
        chosen = select_phrases({"hook": "nothing in common here"}, [phrase()], max_per_phase=2)
        assert chosen["hook"] == []


class TestFraming:
    def test_the_phrase_is_centred_horizontally(self):
        geo = frame_phrase(phrase(x=100, w=320, h=22), frame_w=1080, frame_h=1920,
                           page_w=1280, page_h=4000)
        centre = geo["rect_x"] + geo["rect_w"] / 2
        assert centre == pytest.approx(540, abs=1.0)

    def test_the_phrase_lands_in_the_upper_middle_band(self):
        geo = frame_phrase(phrase(), frame_w=1080, frame_h=1920, page_w=1280, page_h=4000)
        assert 700 < geo["rect_y"] < 1000

    def test_small_type_is_scaled_up_to_be_readable(self):
        geo = frame_phrase(phrase(h=14), frame_w=1080, frame_h=1920, page_w=1280, page_h=4000)
        assert geo["rect_h"] >= 40

    def test_a_long_phrase_still_fits_across_the_frame(self):
        geo = frame_phrase(phrase(x=0, w=1200, h=18), frame_w=1080, frame_h=1920,
                           page_w=1280, page_h=4000)
        assert geo["rect_x"] >= -1
        assert geo["rect_x"] + geo["rect_w"] <= 1081

    def test_the_page_always_covers_the_frame(self):
        geo = frame_phrase(phrase(), frame_w=1080, frame_h=1920, page_w=1280, page_h=4000)
        assert geo["image_x"] <= 0
        assert geo["image_x"] + 1280 * geo["image_scale"] >= 1080


class TestBuild:
    def phases(self):
        return [
            PlacedPhase("hook", 0.0, 5.0, "narrator", "highlight", True),
            PlacedPhase("quiet", 5.0, 5.0, "narrator", "broll", False),
        ]

    def test_markers_stay_inside_their_phase(self):
        markers = build_markers(
            self.phases(), {"hook": [phrase(), phrase(text="second one")]},
            frame_w=1080, frame_h=1920, page_w=1280, page_h=4000,
        )
        for marker in markers:
            assert marker.start >= 0.0
            assert marker.start + marker.duration <= 5.0 + 1e-6

    def test_phases_with_markers_disabled_get_none(self):
        markers = build_markers(
            self.phases(), {"quiet": [phrase()]},
            frame_w=1080, frame_h=1920, page_w=1280, page_h=4000,
        )
        assert markers == []

    def test_markers_keep_a_minimum_gap(self):
        markers = build_markers(
            self.phases(), {"hook": [phrase(), phrase(text="another phrase here")]},
            frame_w=1080, frame_h=1920, page_w=1280, page_h=4000, min_gap_s=2.0,
        )
        for a, b in zip(markers, markers[1:]):
            assert b.start - a.start >= 2.0 - 1e-6

    def test_a_phase_too_short_for_a_marker_gets_none(self):
        tiny = [PlacedPhase("blip", 0.0, 0.4, "narrator", "highlight", True)]
        assert build_markers(tiny, {"blip": [phrase()]}, frame_w=1080, frame_h=1920,
                             page_w=1280, page_h=4000) == []
