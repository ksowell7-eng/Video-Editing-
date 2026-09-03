"""Word-level transcript → caption cues.

The transcript comes from `hyperframes transcribe`, whose word records are
`{id, text, start, end}` in seconds relative to the audio file it was given.
Each VO track is transcribed on its own, so every word carries the offset of
the phase it belongs to before the cues are grouped.

Grouping rules, in priority order:
  1. break on a pause longer than `gap_break_s` — a caption that spans silence
     reads as a mistake;
  2. break after sentence-final punctuation;
  3. break on the word or character limit.

The result is short, phrase-shaped cues rather than a fixed-width crawl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*$")
_MIN_CUE_S = 0.42
# An orphan word flashing for a few frames reads worse than a cue running a
# little long, so merging is allowed to exceed the character limit by this much.
_MERGE_CHAR_SLACK = 6


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass
class Cue:
    words: list[CaptionWord] = field(default_factory=list)
    phase: str = ""

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }


def load_words(records: Iterable[dict[str, Any]], *, offset: float = 0.0) -> list[CaptionWord]:
    """Normalise transcript records, shifting them onto the master timeline."""
    words: list[CaptionWord] = []
    for rec in records:
        text = str(rec.get("text", rec.get("word", ""))).strip()
        if not text:
            continue
        try:
            start = float(rec["start"]) + offset
            end = float(rec["end"]) + offset
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        words.append(CaptionWord(text=text, start=start, end=max(end, start + 0.04)))
    words.sort(key=lambda w: w.start)
    return words


def group_cues(
    words: list[CaptionWord],
    *,
    words_per_cue: int = 3,
    max_chars: int = 24,
    gap_break_s: float = 0.55,
    phase: str = "",
) -> list[Cue]:
    cues: list[Cue] = []
    current = Cue(phase=phase)

    def flush() -> None:
        nonlocal current
        if current.words:
            cues.append(current)
            current = Cue(phase=phase)

    for word in words:
        if current.words:
            gap = word.start - current.words[-1].end
            projected = len(current.text) + 1 + len(word.text)
            if (
                gap > gap_break_s
                or len(current.words) >= max(1, words_per_cue)
                or projected > max_chars
                or _SENTENCE_END.search(current.words[-1].text)
            ):
                flush()
        current.words.append(word)
    flush()

    # A cue shorter than a comfortable read gets absorbed by its neighbour
    # rather than flashing on screen for three frames.
    merged: list[Cue] = []
    for cue in cues:
        if (
            merged
            and cue.end - cue.start < _MIN_CUE_S
            and cue.start - merged[-1].end < gap_break_s
            and len(merged[-1].text) + len(cue.text) + 1 <= max_chars + _MERGE_CHAR_SLACK
            # Never merge across a sentence boundary: "done. next" on one card
            # reads worse than the orphan the merge exists to prevent.
            and not _SENTENCE_END.search(merged[-1].words[-1].text)
        ):
            merged[-1].words.extend(cue.words)
        else:
            merged.append(cue)
    return merged


def build_cues(
    tracks: list[tuple[str, float, list[dict[str, Any]]]],
    *,
    words_per_cue: int = 3,
    max_chars: int = 24,
) -> list[Cue]:
    """Build the master cue list from (phase_id, offset, transcript words)."""
    cues: list[Cue] = []
    for phase_id, offset, records in tracks:
        words = load_words(records, offset=offset)
        cues.extend(group_cues(
            words, words_per_cue=words_per_cue, max_chars=max_chars, phase=phase_id,
        ))
    cues.sort(key=lambda c: c.start)

    # Trim any overlap introduced by VO tracks whose measured length ran past
    # their slot, so two cues are never on screen at once.
    for a, b in zip(cues, cues[1:]):
        if a.words and b.words and a.end > b.start:
            a.words[-1].end = max(a.words[-1].start + 0.04, b.start - 0.02)
    return cues


def estimate_word_timings(text: str, duration: float) -> list[dict[str, Any]]:
    """Approximate word timings from the known script and the measured duration.

    Used only when ASR is unavailable. Words are given time in proportion to
    their length rather than uniformly — "the" and "hesitation" do not take the
    same time to say — and sentence-final punctuation reserves a short pause,
    which is where a uniform split drifts worst by the end of a line.

    The result is close enough for readable captions and never as good as a
    real transcript; anything relying on exact word onsets should re-run once
    whisper is available.
    """
    words = text.split()
    if not words or duration <= 0:
        return []

    pause_after = [0.22 if _SENTENCE_END.search(w) else (0.08 if w.endswith(",") else 0.0) for w in words]
    pause_after[-1] = 0.0
    weights = [max(1.0, len(w.strip(".,!?;:'\"" ))) for w in words]

    speech_time = max(0.1, duration - sum(pause_after))
    total_weight = sum(weights)
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for i, word in enumerate(words):
        span = speech_time * weights[i] / total_weight
        out.append({
            "id": f"e{i}",
            "text": word,
            "start": round(cursor, 3),
            "end": round(cursor + span, 3),
        })
        cursor += span + pause_after[i]
    return out


def coverage(cues: list[Cue], duration: float) -> float:
    """Fraction of the video that has a caption on screen — a sanity metric."""
    if duration <= 0:
        return 0.0
    covered = sum(max(0.0, c.end - c.start) for c in cues)
    return min(1.0, covered / duration)
