"""Step 9 — assemble the HyperFrames composition.

Everything upstream lands here: reframed beds, the avatar, voiceover tracks,
word timings, and measured article phrases become one HTML project with a
paused GSAP timeline.

Assets are copied into the project directory rather than referenced in place.
The renderer resolves relative paths against the project, HyperFrames lints for
missing local assets, and a project you can zip and re-render on another
machine is worth the disk.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..compose.captions import build_cues, coverage
from ..compose.html import CompositionAssets, render_composition, write_project
from ..compose.markers import build_markers, select_phrases
from ..compose.timeline import (
    TRACK_BED, TRACK_MUSIC, TRACK_VOICE,
    Clip, MediaCursor, Timeline, drift_report, place_phases,
)
from ..errors import StepFailed
from ..media.probe import probe
from .base import Context, StepResult
from .. import logs

# Bed audio sits under the voiceover rather than competing with it.
_BED_AUDIO_VOLUME = 0.18
_MUSIC_VOLUME = 0.10


def _copy_asset(src: Path, project: Path, name: str) -> str:
    assets = project / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    dst = assets / name
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        shutil.copyfile(src, dst)
    return f"assets/{name}"


def run(ctx: Context) -> StepResult:
    out_dir = ctx.dir_for("compose")
    project = out_dir / "project"
    project.mkdir(parents=True, exist_ok=True)
    width, height = ctx.job.size
    fps = ctx.job.fps

    reframe = ctx.data("reframe")
    voice = ctx.data("voice")
    avatar = ctx.data("avatar")
    article = ctx.data("article")

    if not voice.get("tracks"):
        raise StepFailed("Nothing to compose: the voice step has not produced tracks")

    # --- phase placement, from measured voiceover ------------------------
    placed = place_phases(ctx.job.phases, voice.get("durations", {}))
    timeline = Timeline(width=width, height=height, fps=fps, phases=placed)
    drift = drift_report(placed, ctx.job.phases)

    # --- bed sources ------------------------------------------------------
    beds: dict[str, tuple[str, MediaCursor, bool]] = {}
    for name, key in (("highlight", "highlight"), ("broll", "broll")):
        path = reframe.get(key)
        if path and Path(path).exists():
            info = probe(Path(path))
            src = _copy_asset(Path(path), project, f"{name}.mp4")
            beds[name] = (src, MediaCursor(info.duration_s), info.has_audio)
    if avatar.get("clip") and Path(avatar["clip"]).exists():
        info = probe(Path(avatar["clip"]))
        src = _copy_asset(Path(avatar["clip"]), project, "avatar.mp4")
        beds["avatar"] = (src, MediaCursor(info.duration_s), info.has_audio)

    for phase in placed:
        if phase.bed == "black":
            continue
        bed = beds.get(phase.bed)
        if bed is None:
            logs.warn(f"{phase.id}: no '{phase.bed}' footage available; the phase will be black")
            continue
        src, cursor, has_audio = bed
        media_start = cursor.take(phase.duration)
        timeline.add(Clip(
            id=f"bed-{phase.id}", kind="video", start=phase.start, duration=phase.duration,
            track=TRACK_BED, src=src, media_start=media_start, classes=("bed",),
        ))
        if has_audio and phase.bed == "highlight":
            # Crowd noise from the highlight is worth keeping, well under the VO.
            timeline.add(Clip(
                id=f"bed-{phase.id}-audio", kind="audio", start=phase.start,
                duration=phase.duration, track=TRACK_BED, src=src,
                media_start=media_start, volume=_BED_AUDIO_VOLUME,
            ))

    # --- voiceover --------------------------------------------------------
    for track in voice["tracks"]:
        phase = timeline.phase(track["phase"])
        if phase is None:
            continue
        src = _copy_asset(Path(track["file"]), project, f"vo_{phase.id}.mp3")
        timeline.add(Clip(
            id=f"vo-{phase.id}", kind="audio", start=phase.start, duration=phase.duration,
            track=TRACK_VOICE, src=src, volume=1.0,
        ))

    music_path = ctx.job.resolve(ctx.job["input"].get("music_bed"))
    if music_path:
        src = _copy_asset(music_path, project, f"music{music_path.suffix}")
        timeline.add(Clip(
            id="music", kind="audio", start=0.0, duration=timeline.duration,
            track=TRACK_MUSIC, src=src, volume=_MUSIC_VOLUME,
        ))

    # --- captions ---------------------------------------------------------
    cues = []
    transcript: list[dict[str, Any]] = []
    if ctx.job["captions"]["enabled"]:
        tracks = []
        for entry in ctx.data("transcribe").get("tracks", []):
            phase = timeline.phase(entry["phase"])
            if phase is None:
                continue
            words = json.loads(Path(entry["file"]).read_text(encoding="utf-8"))
            tracks.append((phase.id, phase.start, words))
        cues = build_cues(
            tracks,
            words_per_cue=int(ctx.job["captions"]["words_per_cue"]),
            max_chars=int(ctx.job["captions"]["max_chars_per_cue"]),
        )
        for cue in cues:
            for i, word in enumerate(cue.words):
                transcript.append({
                    "id": f"{cue.phase}-{i}-{round(word.start * 1000)}",
                    "text": word.text,
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                })
        logs.info(f"{len(cues)} caption cues", coverage=f"{coverage(cues, timeline.duration):.0%}")

    # --- article markers --------------------------------------------------
    markers = []
    assets = CompositionAssets()
    phrases_json = article.get("phrases_json")
    screenshot = article.get("screenshot")
    if ctx.job["markers"]["enabled"] and phrases_json and screenshot and Path(screenshot).exists():
        phrases = json.loads(Path(phrases_json).read_text(encoding="utf-8"))
        # Unwrapped phrases mark cleanly; wrapped ones only get their first line.
        phrases.sort(key=lambda p: (p.get("wrapped", False), -float(p.get("score", 0))))
        lines_by_phase = {line["phase"]: line["text"] for line in ctx.data("script").get("lines", [])}
        selections = select_phrases(
            lines_by_phase, phrases, max_per_phase=int(ctx.job["markers"]["max_per_phase"]),
        )
        page = article.get("page") or {}
        markers = build_markers(
            placed, selections,
            frame_w=width, frame_h=height,
            page_w=float(page.get("width") or width),
            page_h=float(page.get("height") or height),
            hold_s=float(ctx.job["markers"]["hold_s"]),
            min_gap_s=float(ctx.job["markers"]["min_gap_s"]),
        )
        if markers:
            assets = CompositionAssets(
                article_screenshot=_copy_asset(Path(screenshot), project, "article.png"),
                article_page_width=float(page.get("width") or width),
            )
        logs.info(f"{len(markers)} article markers placed")

    # --- write it out -----------------------------------------------------
    html = render_composition(
        timeline, cues, markers, assets, {**ctx.job.raw, "id": ctx.job.id}, transcript,
    )
    index = write_project(
        project, html, name=ctx.job.id, fps=fps, width=width, height=height, transcript=transcript,
    )

    timeline_json = out_dir / "timeline.json"
    timeline_json.write_text(json.dumps({
        "duration_s": timeline.duration,
        "phases": [
            {"id": p.id, "start": p.start, "duration": p.duration, "bed": p.bed, "voice": p.voice}
            for p in placed
        ],
        "drift": drift,
        "clips": [
            {"id": c.id, "kind": c.kind, "track": c.track, "start": c.start,
             "duration": c.duration, "src": c.src, "media_start": c.media_start}
            for c in timeline.clips
        ],
        "cues": [c.to_dict() for c in cues],
        "markers": [m.to_dict() for m in markers],
    }, indent=2), encoding="utf-8")

    target = ctx.job.target_duration_s
    tolerance = float(ctx.job["output"]["duration_tolerance_s"])
    if abs(timeline.duration - target) > tolerance:
        logs.warn(
            f"composition is {timeline.duration:.1f}s against a {target:.0f}s target",
            hint="the voiceover ran long; check timeline.json drift",
        )

    logs.info(f"composition written: {index}", duration=f"{timeline.duration:.1f}s")
    return StepResult(
        outputs=[index, timeline_json],
        data={
            "project": str(project),
            "index": str(index),
            "timeline": str(timeline_json),
            "duration_s": timeline.duration,
            "cue_count": len(cues),
            "marker_count": len(markers),
            "drift": drift,
        },
    )
