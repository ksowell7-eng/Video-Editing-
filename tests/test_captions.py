"""Cue grouping decides whether captions read as speech or as a crawl."""

import pytest

from pipeline.compose.captions import (
    build_cues, coverage, estimate_word_timings, group_cues, load_words,
)


def words(*pairs):
    return [{"text": t, "start": s, "end": e} for t, s, e in pairs]


def test_load_words_applies_the_phase_offset():
    loaded = load_words(words(("hello", 0.0, 0.4)), offset=10.0)
    assert (loaded[0].start, loaded[0].end) == (10.0, 10.4)


def test_load_words_accepts_the_word_key_too():
    loaded = load_words([{"word": "hi", "start": 0, "end": 0.2}])
    assert loaded[0].text == "hi"


def test_load_words_skips_records_without_usable_timing():
    loaded = load_words([{"text": "ok", "start": 0, "end": 0.2}, {"text": "bad"}])
    assert [w.text for w in loaded] == ["ok"]


def test_load_words_repairs_reversed_timings():
    loaded = load_words([{"text": "x", "start": 1.0, "end": 0.5}])
    assert loaded[0].start < loaded[0].end


def test_cues_break_on_a_long_pause():
    loaded = load_words(words(("one", 0, 0.3), ("two", 2.0, 2.3)))
    cues = group_cues(loaded, words_per_cue=5, max_chars=40, gap_break_s=0.5)
    assert [c.text for c in cues] == ["one", "two"]


def test_cues_break_after_a_sentence_ends():
    loaded = load_words(words(("done.", 0, 0.3), ("next", 0.35, 0.6)))
    cues = group_cues(loaded, words_per_cue=5, max_chars=40)
    assert [c.text for c in cues] == ["done.", "next"]


def test_cues_respect_the_word_limit():
    loaded = load_words(words(*[(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(6)]))
    cues = group_cues(loaded, words_per_cue=2, max_chars=99)
    assert all(len(c.words) <= 2 for c in cues)


def test_overlapping_phase_tracks_are_trimmed_apart():
    # A VO track that ran long would otherwise leave two cues on screen at once.
    track_a = ("hook", 0.0, words(("late", 0.0, 5.0)))
    track_b = ("context", 4.0, words(("next", 0.0, 1.0)))
    cues = build_cues([track_a, track_b])
    assert cues[0].end <= cues[1].start


def test_coverage_is_bounded():
    cues = build_cues([("p", 0.0, words(("a", 0, 1.0)))])
    assert 0.0 <= coverage(cues, 2.0) <= 1.0
    assert coverage(cues, 0) == 0.0


class TestEstimatedTimings:
    """The fallback used when whisper is unavailable."""

    def test_it_spans_exactly_the_measured_duration(self):
        out = estimate_word_timings("one two three four", 4.0)
        assert out[0]["start"] == 0.0
        assert out[-1]["end"] == pytest.approx(4.0, abs=0.01)

    def test_longer_words_get_more_time(self):
        out = estimate_word_timings("a hesitation", 2.0)
        short = out[0]["end"] - out[0]["start"]
        long = out[1]["end"] - out[1]["start"]
        assert long > short

    def test_timings_never_go_backwards(self):
        out = estimate_word_timings("First one. Then another, and more.", 6.0)
        for a, b in zip(out, out[1:]):
            assert a["end"] <= b["start"]

    def test_empty_input_produces_nothing(self):
        assert estimate_word_timings("", 5.0) == []
        assert estimate_word_timings("word", 0) == []
