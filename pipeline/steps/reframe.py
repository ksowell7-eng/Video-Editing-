"""Step 3 — auto-reframe 16:9 sources to the vertical output.

Runs over the highlight clip and the b-roll reel. For each: track faces, smooth
the detections into a camera path, then apply that path as a moving crop.

When a clip has almost no detections — a wide stadium shot, a graphic, an
overhead replay — face tracking has nothing to say, and forcing a crop on a
handful of false positives is worse than not cropping at all. Those clips fall
back to a centred crop, or to letterbox if the job asks for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import StepFailed
from ..media.edit import reframe_with_path, scale_pad
from ..media.probe import probe
from ..vision.smoothing import smooth_path
from ..vision.tracking import crop_geometry, track_faces
from .base import Context, StepResult
from .. import logs

_MIN_DETECTION_RATIO = 0.12


def reframe_clip(
    src: Path,
    dst: Path,
    cfg: dict[str, Any],
    *,
    out_w: int,
    out_h: int,
    fps: int,
    workdir: Path,
) -> dict[str, Any]:
    info = probe(src)
    if not info.has_video:
        raise StepFailed(f"{src.name} has no video stream to reframe")

    geometry = crop_geometry(info.width, info.height, out_w, out_h)
    report: dict[str, Any] = {
        "source": str(src),
        "output": str(dst),
        "source_size": [info.width, info.height],
        "crop_size": [geometry.crop_w, geometry.crop_h],
        "duration_s": round(info.duration_s, 3),
    }

    if not geometry.pans_x and not geometry.pans_y:
        # Already the target shape: a straight scale is exact and cheap.
        logs.info(f"{src.name}: already {info.width}x{info.height}; scaling only")
        scale_pad(src, dst, out_w, out_h, fps=fps)
        report.update(strategy="scale", detection_ratio=None)
        return report

    strategy = cfg["strategy"]
    track = None
    if strategy == "face_track":
        track = track_faces(
            src,
            detect_every_n=int(cfg["detect_every_n_frames"]),
            min_face_px=int(cfg["min_face_px"]),
        )
        report["detection_ratio"] = round(track.detection_ratio, 3)
        logs.info(
            f"{src.name}: faces in {track.detection_ratio:.0%} of sampled frames",
            samples=track.times.size,
        )
        if track.detection_ratio < _MIN_DETECTION_RATIO:
            logs.warn(f"{src.name}: too few detections to track; falling back")
            strategy = "letterbox" if cfg["letterbox_fallback"] else "center"

    if strategy in ("center", "letterbox"):
        if strategy == "letterbox":
            scale_pad(src, dst, out_w, out_h, fps=fps)
            report.update(strategy="letterbox")
            return report
        times = np.array([0.0, max(0.1, info.duration_s)])
        cx = np.full(2, info.width / 2)
        cy = np.full(2, info.height / 2)
    else:
        assert track is not None
        times = track.times
        bias = float(cfg["subject_bias_y"]) * geometry.crop_h
        x_lo, x_hi = geometry.x_range
        y_lo, y_hi = geometry.y_range
        cx = smooth_path(
            times, track.cx,
            fallback=info.width / 2, lower=x_lo, upper=x_hi,
            deadzone=float(cfg["deadzone_px"]),
            keyframe_interval_s=float(cfg["keyframe_interval_s"]),
            max_rate=float(cfg["max_pan_px_per_s"]),
        )
        cy = smooth_path(
            times, track.cy + bias,
            fallback=info.height / 2, lower=y_lo, upper=y_hi,
            deadzone=float(cfg["deadzone_px"]),
            keyframe_interval_s=float(cfg["keyframe_interval_s"]),
            max_rate=float(cfg["max_pan_px_per_s"]),
        )
        report["strategy"] = "face_track"
        report["pan_px"] = round(float(np.ptp(cx)), 1)

    reframe_with_path(
        src, dst,
        crop_w=geometry.crop_w, crop_h=geometry.crop_h,
        centers_x=cx, centers_y=cy, times=times,
        out_w=out_w, out_h=out_h, fps=fps, workdir=workdir,
    )
    report.setdefault("strategy", strategy)
    return report


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["reframe"]
    out_dir = ctx.dir_for("reframe")
    out_w, out_h = ctx.job.size
    fps = ctx.job.fps

    jobs: list[tuple[str, Path]] = [
        ("highlight", ctx.job.resolve(ctx.job["input"]["highlight_clip"]))
    ]
    broll = ctx.data("broll").get("reel")
    if broll:
        jobs.append(("broll", Path(broll)))

    reports: list[dict[str, Any]] = []
    outputs: list[Path] = []
    produced: dict[str, str] = {}

    for name, src in jobs:
        dst = out_dir / f"{name}_9x16.mp4"
        if not cfg["enabled"]:
            logs.info(f"{name}: reframe disabled; scaling with letterbox")
            scale_pad(src, dst, out_w, out_h, fps=fps)
            reports.append({"source": str(src), "output": str(dst), "strategy": "disabled"})
        else:
            reports.append(reframe_clip(
                src, dst, cfg, out_w=out_w, out_h=out_h, fps=fps, workdir=out_dir / "work",
            ))
        produced[name] = str(dst)
        outputs.append(dst)

    report_path = out_dir / "reframe.json"
    report_path.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    outputs.append(report_path)

    return StepResult(
        outputs=outputs,
        data={
            **produced,
            "report": str(report_path),
            "durations": {name: round(probe(Path(path)).duration_s, 3) for name, path in produced.items()},
        },
    )
