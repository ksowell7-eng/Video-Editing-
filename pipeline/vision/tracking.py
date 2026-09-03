"""Haar face tracking over a clip, and the crop geometry it feeds.

Detection runs on a downscaled copy every Nth frame — a 1280px frame costs
several times what a 480px one does and buys nothing, because the smoothing
stage throws away that precision anyway.

Track association is deliberately simple: among the faces in a frame, prefer
the one nearest the previous accepted center, weighted by size. Sports footage
is full of faces in the crowd; without the continuity term the crop hops
between them, and with it a single large background face can't steal the track
mid-clip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..errors import MissingDependency, StepFailed

_DETECT_WIDTH = 480


def _cv2():
    try:
        import cv2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingDependency(
            "opencv is required for face-tracked reframing",
            hint="pip install 'opencv-python-headless>=4.8,<5' (OpenCV 5 removed CascadeClassifier).",
        ) from exc
    if not hasattr(cv2, "CascadeClassifier"):
        raise MissingDependency(
            f"OpenCV {cv2.__version__} has no CascadeClassifier",
            hint="Haar cascades were removed in OpenCV 5. Pin 'opencv-python-headless<5'.",
        )
    return cv2


@dataclass
class Track:
    times: np.ndarray          # seconds, one entry per sampled frame
    cx: np.ndarray             # subject center x in source pixels, NaN when unseen
    cy: np.ndarray             # subject center y in source pixels, NaN when unseen
    width: int
    height: int
    fps: float
    frame_count: int
    detections: int

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.fps if self.fps else 0.0

    @property
    def detection_ratio(self) -> float:
        return self.detections / max(1, self.times.size)


@dataclass
class CropGeometry:
    """A fixed-size crop window that can slide inside the source frame."""

    crop_w: int
    crop_h: int
    src_w: int
    src_h: int

    @property
    def x_range(self) -> tuple[float, float]:
        half = self.crop_w / 2
        return half, max(half, self.src_w - half)

    @property
    def y_range(self) -> tuple[float, float]:
        half = self.crop_h / 2
        return half, max(half, self.src_h - half)

    @property
    def pans_x(self) -> bool:
        return self.src_w - self.crop_w > 1

    @property
    def pans_y(self) -> bool:
        return self.src_h - self.crop_h > 1


def crop_geometry(src_w: int, src_h: int, target_w: int, target_h: int) -> CropGeometry:
    """Largest window of the target aspect ratio that fits inside the source."""
    target_ar = target_w / target_h
    src_ar = src_w / src_h
    if src_ar > target_ar:
        crop_h = src_h
        crop_w = int(round(src_h * target_ar))
    else:
        crop_w = src_w
        crop_h = int(round(src_w / target_ar))
    # Even dimensions keep yuv420p encoders happy.
    crop_w = min(src_w, crop_w - (crop_w % 2))
    crop_h = min(src_h, crop_h - (crop_h % 2))
    return CropGeometry(crop_w=crop_w, crop_h=crop_h, src_w=src_w, src_h=src_h)


def _pick_face(faces, previous: tuple[float, float] | None, scale: float, min_px: int):
    """Score faces by size and continuity with the previous accepted center."""
    best = None
    best_score = -math.inf
    for (x, y, w, h) in faces:
        fw, fh = w / scale, h / scale
        if fw < min_px:
            continue
        cx, cy = (x + w / 2) / scale, (y + h / 2) / scale
        score = fw * fh
        if previous is not None:
            dist = math.hypot(cx - previous[0], cy - previous[1])
            # Continuity term: halves the score roughly every 300px of jump.
            score /= 1.0 + (dist / 300.0) ** 2
        if score > best_score:
            best_score, best = score, (cx, cy)
    return best


def track_faces(
    video: Path,
    *,
    detect_every_n: int = 3,
    min_face_px: int = 48,
    scale_factor: float = 1.15,
    min_neighbors: int = 5,
) -> Track:
    cv2 = _cv2()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise StepFailed(f"Could not open video for tracking: {video}")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise StepFailed(f"Video reports no dimensions: {video}")

        cascade_path = f"{cv2.data.haarcascades}haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            raise MissingDependency(f"Haar cascade failed to load from {cascade_path}")
        profile = cv2.CascadeClassifier(f"{cv2.data.haarcascades}haarcascade_profileface.xml")

        scale = min(1.0, _DETECT_WIDTH / width)
        det_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        min_det = max(12, int(min_face_px * scale))

        times: list[float] = []
        xs: list[float] = []
        ys: list[float] = []
        previous: tuple[float, float] | None = None
        detections = 0
        index = 0

        while True:
            grabbed = cap.grab()
            if not grabbed:
                break
            if index % max(1, detect_every_n) == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                small = cv2.resize(frame, det_size, interpolation=cv2.INTER_AREA)
                gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
                faces = cascade.detectMultiScale(
                    gray, scaleFactor=scale_factor, minNeighbors=min_neighbors,
                    minSize=(min_det, min_det),
                )
                if len(faces) == 0 and not profile.empty():
                    # Players turn away constantly; the profile cascade recovers
                    # a good share of the frames the frontal one drops.
                    faces = profile.detectMultiScale(
                        gray, scaleFactor=scale_factor, minNeighbors=min_neighbors,
                        minSize=(min_det, min_det),
                    )
                picked = _pick_face(faces, previous, scale, min_face_px)
                times.append(index / fps)
                if picked is None:
                    xs.append(math.nan)
                    ys.append(math.nan)
                else:
                    detections += 1
                    previous = picked
                    xs.append(picked[0])
                    ys.append(picked[1])
            index += 1
    finally:
        cap.release()

    if not times:
        raise StepFailed(f"No frames decoded from {video}")

    return Track(
        times=np.array(times),
        cx=np.array(xs),
        cy=np.array(ys),
        width=width,
        height=height,
        fps=float(fps),
        frame_count=index,
        detections=detections,
    )
