"""The timeline model: phases and assets in, positioned clips out.

Phase durations come from the *rendered voiceover*, not from the target
lengths in the job file. Targets shape what Claude writes; once the audio
exists, its real duration is the truth, because a phase that is 400ms longer
than its slot clips its own last word.

Track indices are fixed so z-order is predictable across every run:

    0  beds        highlight / b-roll / avatar footage
    1  overlays    article card, markers, phase furniture
    2  captions
    3  voiceover   (audio)
    4  music bed   (audio, ducked)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

TRACK_BED = 0
TRACK_OVERLAY = 1
TRACK_CAPTION = 2
TRACK_VOICE = 3
TRACK_MUSIC = 4


@dataclass
class Clip:
    id: str
    kind: str                       # video | audio | image | box
    start: float
    duration: float
    track: int
    src: str = ""
    media_start: float = 0.0
    volume: float = 1.0
    style: str = ""
    inner_html: str = ""
    classes: tuple[str, ...] = ()
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class PlacedPhase:
    id: str
    start: float
    duration: float
    voice: str
    bed: str
    markers: bool

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Timeline:
    width: int
    height: int
    fps: int
    phases: list[PlacedPhase] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)

    @property
    def duration(self) -> float:
        ends = [c.end for c in self.clips] + [p.end for p in self.phases]
        return round(max(ends), 3) if ends else 0.0

    def add(self, clip: Clip) -> Clip:
        self.clips.append(clip)
        return clip

    def on_track(self, track: int) -> list[Clip]:
        return [c for c in self.clips if c.track == track]

    def phase(self, phase_id: str) -> PlacedPhase | None:
        return next((p for p in self.phases if p.id == phase_id), None)


class MediaCursor:
    """Hands out non-repeating segments of a shared source clip.

    Two phases both bedded on the highlight should not open on the same four
    seconds of footage. The cursor advances through the source and wraps once
    it runs out, so a short source degrades to a loop instead of an error.
    """

    def __init__(self, duration_s: float, *, start_at: float = 0.0):
        self.duration = max(0.0, float(duration_s))
        self.position = min(max(0.0, start_at), self.duration)

    def take(self, needed: float) -> float:
        if self.duration <= 0:
            return 0.0
        if needed >= self.duration:
            self.position = 0.0
            return 0.0
        if self.position + needed > self.duration:
            # Prefer the unused tail of the source over jumping straight back
            # to the top; only wrap once the tail is spent too.
            tail = self.duration - needed
            if tail > self.position + 1e-6:
                self.position = self.duration
                return round(tail, 3)
            self.position = 0.0
        start = self.position
        self.position += needed
        return round(start, 3)


def place_phases(phases: Iterable[Any], durations: dict[str, float]) -> list[PlacedPhase]:
    """Lay phases end to end, using measured VO duration where available."""
    placed: list[PlacedPhase] = []
    cursor = 0.0
    for phase in phases:
        duration = float(durations.get(phase.id, phase.target_s))
        if duration <= 0:
            duration = float(phase.target_s)
        placed.append(PlacedPhase(
            id=phase.id,
            start=round(cursor, 3),
            duration=round(duration, 3),
            voice=phase.voice,
            bed=phase.bed,
            markers=bool(phase.markers),
        ))
        cursor += duration
    return placed


def drift_report(placed: list[PlacedPhase], phases: Iterable[Any]) -> list[dict[str, Any]]:
    """Per-phase difference between the written target and what the VO measured."""
    targets = {p.id: float(p.target_s) for p in phases}
    report = []
    for p in placed:
        target = targets.get(p.id, p.duration)
        report.append({
            "phase": p.id,
            "target_s": round(target, 2),
            "actual_s": round(p.duration, 2),
            "drift_s": round(p.duration - target, 2),
        })
    return report
