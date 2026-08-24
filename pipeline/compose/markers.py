"""On-screen article markers driven by measured phrase positions.

The scrape records, for every candidate phrase, the DOMRect it occupied in the
rendered article page — the same page the full-height screenshot came from.
That pairing is what makes the marker exact: the composition shows a slice of
the real article and animates a highlight over the real phrase, at the real
coordinates the browser laid it out at. No text re-rendering, no guessing.

This module does two things:

  `select_phrases`  picks which phrase each phase should mark, by scoring
                    overlap between the VO line and the phrase text;
  `frame_phrase`    computes the transform that puts that phrase in the middle
                    of a 1080x1920 frame at a readable size, plus the highlight
                    rectangle in final-frame coordinates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset("""
a an the and or but of to in on at by for with from as is are was were be been
that this it its their his her they them he she we you i not no so if then than
""".split())


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS and len(t) > 2}


@dataclass
class Marker:
    phase: str
    phrase: str
    start: float
    duration: float
    # Screenshot placement, in final-frame pixels.
    image_x: float
    image_y: float
    image_scale: float
    # Highlight rectangle, in final-frame pixels.
    rect_x: float
    rect_y: float
    rect_w: float
    rect_h: float
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phrase": self.phrase,
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "image": {"x": round(self.image_x, 1), "y": round(self.image_y, 1),
                      "scale": round(self.image_scale, 4)},
            "rect": {"x": round(self.rect_x, 1), "y": round(self.rect_y, 1),
                     "w": round(self.rect_w, 1), "h": round(self.rect_h, 1)},
            "score": round(self.score, 3),
        }


def score_phrase(line: str, phrase: str) -> float:
    """Jaccard-ish overlap, biased toward phrases the line actually quotes."""
    line_tokens, phrase_tokens = _tokens(line), _tokens(phrase)
    if not line_tokens or not phrase_tokens:
        return 0.0
    shared = line_tokens & phrase_tokens
    if not shared:
        return 0.0
    # Recall against the phrase matters more than precision against the line:
    # a long VO line quoting a short phrase in full is an excellent match.
    recall = len(shared) / len(phrase_tokens)
    precision = len(shared) / len(line_tokens)
    return round(0.75 * recall + 0.25 * precision, 4)


def select_phrases(
    lines_by_phase: dict[str, str],
    phrases: Sequence[dict[str, Any]],
    *,
    max_per_phase: int = 2,
    min_score: float = 0.25,
) -> dict[str, list[dict[str, Any]]]:
    """Assign article phrases to phases, never reusing a phrase twice."""
    used: set[int] = set()
    chosen: dict[str, list[dict[str, Any]]] = {}
    for phase_id, line in lines_by_phase.items():
        scored = []
        for i, phrase in enumerate(phrases):
            if i in used:
                continue
            score = score_phrase(line, str(phrase.get("text", "")))
            if score >= min_score:
                scored.append((score, i, phrase))
        scored.sort(key=lambda s: (-s[0], s[1]))
        picked = []
        for score, i, phrase in scored[:max_per_phase]:
            used.add(i)
            picked.append({**phrase, "score": score})
        chosen[phase_id] = picked
    return chosen


def frame_phrase(
    phrase: dict[str, Any],
    *,
    frame_w: int,
    frame_h: int,
    page_w: float,
    page_h: float,
    target_text_px: float = 46.0,
    band_h_ratio: float = 0.34,
    side_margin_px: float = 60.0,
) -> dict[str, float]:
    """Place the screenshot so this phrase sits centred in a readable band.

    The screenshot is scaled so the phrase's own line height lands near
    `target_text_px` on the final frame — measured type, not a guessed zoom —
    then translated so the phrase centre sits at the middle of the band.
    """
    rect = phrase.get("rect", phrase)
    rx, ry = float(rect.get("x", 0)), float(rect.get("y", 0))
    rw, rh = float(rect.get("w", 0)), float(rect.get("h", 0))

    scale = target_text_px / rh if rh > 0 else 1.0
    # Cap so the marked phrase itself always fits across the frame with a
    # margin. A long phrase on a wide page would otherwise be zoomed until its
    # own underline ran off both edges.
    if rw > 0:
        scale = min(scale, max(0.05, (frame_w - 2 * side_margin_px) / rw))
    # Floor so the page still covers the frame; bare background at the edges
    # looks like a broken asset. If the two fight, coverage wins.
    if page_w > 0:
        scale = max(scale, frame_w / page_w)
    scale = max(0.05, min(scale, 6.0))

    band_center_y = frame_h * (0.5 - band_h_ratio * 0.12)
    image_x = frame_w / 2 - (rx + rw / 2) * scale
    image_y = band_center_y - (ry + rh / 2) * scale

    # Keep the page over the frame: no bare background showing at the edges.
    scaled_w, scaled_h = page_w * scale, page_h * scale
    if scaled_w >= frame_w:
        image_x = min(0.0, max(image_x, frame_w - scaled_w))
    else:
        image_x = (frame_w - scaled_w) / 2
    if scaled_h >= frame_h:
        image_y = min(0.0, max(image_y, frame_h - scaled_h))
    else:
        image_y = (frame_h - scaled_h) / 2

    return {
        "image_x": image_x,
        "image_y": image_y,
        "image_scale": scale,
        "rect_x": image_x + rx * scale,
        "rect_y": image_y + ry * scale,
        "rect_w": rw * scale,
        "rect_h": rh * scale,
    }


def build_markers(
    placed_phases: Iterable[Any],
    selections: dict[str, list[dict[str, Any]]],
    *,
    frame_w: int,
    frame_h: int,
    page_w: float,
    page_h: float,
    hold_s: float = 1.4,
    min_gap_s: float = 1.6,
    lead_in_s: float = 0.35,
) -> list[Marker]:
    """Turn phase/phrase selections into timed, positioned marker specs."""
    markers: list[Marker] = []
    for phase in placed_phases:
        picks = selections.get(phase.id, [])
        if not picks or not getattr(phase, "markers", False):
            continue
        # Spread the phase's markers across its middle, leaving the first and
        # last beat clean so a marker never collides with a phase transition.
        usable_start = phase.start + lead_in_s
        usable_end = phase.end - 0.3
        span = max(0.0, usable_end - usable_start)
        if span < 0.6:
            continue
        slots = len(picks)
        for i, phrase in enumerate(picks):
            slot_start = usable_start + span * (i / slots)
            duration = min(hold_s, span / slots - 0.1)
            if duration < 0.5:
                continue
            if markers and slot_start - markers[-1].start < min_gap_s:
                slot_start = markers[-1].start + min_gap_s
                if slot_start + duration > usable_end:
                    continue
            geo = frame_phrase(
                phrase, frame_w=frame_w, frame_h=frame_h, page_w=page_w, page_h=page_h,
            )
            markers.append(Marker(
                phase=phase.id,
                phrase=str(phrase.get("text", ""))[:200],
                start=round(slot_start, 3),
                duration=round(duration, 3),
                score=float(phrase.get("score", 0.0)),
                **geo,
            ))
    return markers
