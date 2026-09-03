"""The generated composition has to satisfy the HyperFrames contract.

These assertions mirror rules the CLI's own linter enforces. They are cheap to
run here and expensive to discover at render time, where a violated rule can
still produce a video — just one with no captions on it.
"""

import re

import pytest

from pipeline.compose.captions import build_cues
from pipeline.compose.html import CompositionAssets, render_composition
from pipeline.compose.markers import Marker
from pipeline.compose.timeline import Clip, PlacedPhase, Timeline
from pipeline.config import DEFAULTS


@pytest.fixture
def composition():
    timeline = Timeline(width=1080, height=1920, fps=30)
    timeline.phases = [PlacedPhase("hook", 0, 4, "narrator", "highlight", True)]
    timeline.add(Clip(id="bed-hook", kind="video", start=0, duration=4, track=0,
                      src="assets/hook.mp4", classes=("bed",),
                      attrs={"data-has-audio": "true"}))
    timeline.add(Clip(id="bed-hook-audio", kind="audio", start=0, duration=4, track=0,
                      src="assets/hook.mp4", volume=0.2))
    timeline.add(Clip(id="vo-hook", kind="audio", start=0, duration=4, track=3,
                      src="assets/vo.mp3"))
    words = [{"text": t, "start": i * 0.4, "end": i * 0.4 + 0.35}
             for i, t in enumerate("The keeper stayed rooted here now".split())]
    cues = build_cues([("hook", 0.0, words)])
    markers = [Marker(phase="hook", phrase="keeper stayed", start=0.5, duration=1.4,
                      image_x=-10, image_y=-200, image_scale=2.0,
                      rect_x=200, rect_y=800, rect_w=600, rect_h=46)]
    assets = CompositionAssets(article_screenshot="assets/article.png", article_page_width=1280)
    return render_composition(timeline, cues, markers, assets,
                              {**DEFAULTS, "id": "t"}, transcript=words)


def test_the_root_declares_the_composition(composition):
    assert 'data-composition-id="main"' in composition
    assert 'data-width="1080"' in composition
    assert 'data-height="1920"' in composition


def test_every_timed_element_is_a_clip(composition):
    # HyperFrames uses class="clip" for visibility control; a timed element
    # without it is a lint error and renders wrong.
    for match in re.finditer(r"<(video|audio|img|div)\s[^>]*data-start=[^>]*>", composition):
        tag = match.group(0)
        if "data-composition-id" in tag:
            continue      # the composition root is a wrapper, not a clip
        assert 'class="clip' in tag, tag[:120]


def test_every_timed_element_declares_duration_and_track(composition):
    for match in re.finditer(r'<\w+\s[^>]*class="clip[^>]*>', composition):
        tag = match.group(0)
        assert "data-start=" in tag, tag[:120]
        assert "data-duration=" in tag, tag[:120]
        assert "data-track-index=" in tag, tag[:120]


def test_videos_are_muted_and_never_declare_their_own_audio(composition):
    # The contract is muted video plus a sibling <audio>; declaring
    # data-has-audio on a muted element is a lint error.
    for tag in re.findall(r"<video[^>]*>", composition):
        assert " muted" in tag
        assert "data-has-audio" not in tag


def test_the_timeline_is_paused_and_registered(composition):
    assert "gsap.timeline({ paused: true })" in composition
    assert 'window.__timelines["main"]' in composition


def test_gsap_is_referenced_locally_not_from_a_cdn(composition):
    # A CDN fetch that fails inside the renderer produces a video with no
    # animation and a zero exit code.
    assert '<script src="gsap.min.js">' in composition
    assert "cdn.jsdelivr.net" not in composition


def test_overlays_rest_hidden(composition):
    # Frames are rendered out of order; anything that fades in must start at 0.
    assert re.search(r"\.cue \{\s*opacity: 0", composition)
    assert re.search(r"\.marker \{[^}]*opacity: 0", composition)


def test_every_fade_out_has_a_hard_kill(composition):
    # Non-linear seeking can land after a fade without playing it.
    faded = set(re.findall(r'tl\.to\("(#[\w-]+)", \{ opacity: 0', composition))
    killed = set(re.findall(r'tl\.set\("(#[\w-]+)", \{ opacity: 0 \}', composition))
    assert faded <= killed, f"missing hard kill for {faded - killed}"


def test_the_composition_is_deterministic(composition):
    for forbidden in ("Date.now", "Math.random", "fetch(", "XMLHttpRequest"):
        assert forbidden not in composition


def test_the_transcript_is_inlined_in_the_patchable_form(composition):
    # `hyperframes transcribe` rewrites exactly this declaration.
    assert re.search(r"const TRANSCRIPT = \[", composition)


def test_caption_words_carry_a_dim_base_and_a_lit_overlay(composition):
    assert 'class="w-base"' in composition
    assert 'class="w-lit"' in composition


def test_markers_pin_the_screenshot_to_the_page_width(composition):
    # The screenshot is captured at 2x; without this the rects miss.
    assert "width:1280px" in composition


def test_two_renders_of_the_same_input_are_identical(composition):
    timeline = Timeline(width=1080, height=1920, fps=30)
    timeline.add(Clip(id="a", kind="video", start=0, duration=2, track=0, src="a.mp4"))
    args = (timeline, [], [], CompositionAssets(), {**DEFAULTS, "id": "t"}, [])
    assert render_composition(*args) == render_composition(*args)
