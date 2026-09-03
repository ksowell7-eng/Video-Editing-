"""ffprobe wrappers. Everything downstream sizes itself from these numbers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..errors import StepFailed
from ..shell import ffprobe, run


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_s: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool
    codec: str

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


def probe(path: str | Path) -> MediaInfo:
    p = Path(path)
    if not p.exists():
        raise StepFailed(f"Cannot probe missing file: {p}")
    proc = run([
        ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ], timeout=120)
    try:
        blob = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StepFailed(f"ffprobe returned unparseable JSON for {p}") from exc

    streams = blob.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (blob.get("format", {}).get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    if video:
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        try:
            num, _, den = rate.partition("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    return MediaInfo(
        path=p,
        duration_s=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=fps or 30.0,
        has_video=video is not None,
        has_audio=audio is not None,
        codec=(video or audio or {}).get("codec_name", ""),
    )


def audio_duration(path: str | Path) -> float:
    return probe(path).duration_s
