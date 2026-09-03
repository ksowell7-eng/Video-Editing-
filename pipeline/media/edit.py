"""ffmpeg operations: trim, concat, reframe, loudness, silence padding.

Every encode goes through `_encode_args` so quality settings stay in one place
and every intermediate in the run is the same codec — concat then never has to
re-decide, and the HyperFrames render gets uniformly seekable input.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from ..errors import StepFailed
from ..shell import ffmpeg, run
from .probe import probe

# Chrome (and therefore the HyperFrames renderer) seeks these reliably.
_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]


def _audio_codec_args(dst: Path) -> list[str]:
    """Match the codec to the container the caller asked for.

    Audio moves between wav (local TTS), mp3 (ElevenLabs) and m4a here, and an
    AAC stream in an .mp3 container is a hard ffmpeg error rather than a
    warning — so the extension picks the encoder.
    """
    suffix = dst.suffix.lower()
    if suffix == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100"]
    if suffix == ".wav":
        return ["-c:a", "pcm_s16le", "-ar", "48000"]
    return ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]


def _encode_args(*, with_audio: bool, fps: int | None = None, gop_seconds: float = 0.5) -> list[str]:
    args = list(_VIDEO_ARGS)
    if fps:
        # A short GOP costs a little size and buys frame-accurate seeking,
        # which is what the renderer does on every single frame.
        args += ["-r", str(fps), "-g", str(max(1, int(fps * gop_seconds)))]
    args += _AUDIO_ARGS if with_audio else ["-an"]
    return args


def trim(src: Path, dst: Path, start_s: float, duration_s: float, *, fps: int | None = None) -> Path:
    """Accurate trim: seek before input for speed, then re-encode for exactness."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    pre = max(0.0, start_s - 2.0)          # coarse seek to a keyframe before the cut
    fine = start_s - pre
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{pre:.3f}", "-i", str(src),
        "-ss", f"{fine:.3f}", "-t", f"{duration_s:.3f}",
        *_encode_args(with_audio=info.has_audio, fps=fps),
        str(dst),
    ])
    if not dst.exists() or dst.stat().st_size == 0:
        raise StepFailed(f"Trim produced no output: {dst}")
    return dst


def scale_pad(src: Path, dst: Path, width: int, height: int, *, fps: int | None = None) -> Path:
    """Fit into width x height with letterbox bars — the reframe fallback."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vf", vf, *_encode_args(with_audio=info.has_audio, fps=fps), str(dst),
    ])
    return dst


def concat(clips: Sequence[Path], dst: Path, *, fps: int | None = None, workdir: Path | None = None) -> Path:
    """Concatenate clips through the demuxer, re-encoding to a single profile.

    The clips come from different YouTube sources with different codecs, frame
    rates and pixel formats, so stream-copy concat is not an option — the
    filter path is the only one that survives mixed input.
    """
    if not clips:
        raise StepFailed("concat called with no clips")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        return trim(clips[0], dst, 0.0, probe(clips[0]).duration_s, fps=fps)

    workdir = workdir or dst.parent
    listing = workdir / f"{dst.stem}.concat.txt"
    listing.write_text(
        "".join(f"file {shlex.quote(str(Path(c).resolve()))}\n" for c in clips),
        encoding="utf-8",
    )
    any_audio = any(probe(c).has_audio for c in clips)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        *_encode_args(with_audio=any_audio, fps=fps), str(dst),
    ], timeout=3600)
    return dst


def reframe_with_path(
    src: Path,
    dst: Path,
    *,
    crop_w: int,
    crop_h: int,
    centers_x: np.ndarray,
    centers_y: np.ndarray,
    times: np.ndarray,
    out_w: int,
    out_h: int,
    fps: int,
    workdir: Path,
) -> Path:
    """Apply a per-frame crop path, then scale to the output size.

    The path is delivered to ffmpeg as a sendcmd script rather than baked into
    a crop expression: an expression long enough to describe a few hundred
    keyframes hits ffmpeg's parser limits, while sendcmd stays linear and
    readable when a render looks wrong and you need to see what the camera did.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)

    max_x = max(0, info.width - crop_w)
    max_y = max(0, info.height - crop_h)
    lines = []
    last: tuple[int, int] | None = None
    for t, cx, cy in zip(times, centers_x, centers_y):
        x = int(round(min(max(cx - crop_w / 2, 0), max_x)))
        y = int(round(min(max(cy - crop_h / 2, 0), max_y)))
        if last == (x, y):
            continue                     # only emit real changes
        last = (x, y)
        lines.append(f"{max(0.0, float(t)):.3f} crop x {x}, crop y {y};")

    script = workdir / f"{dst.stem}.sendcmd"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # sendcmd needs a POSIX-ish path with ':' and '\' escaped inside the filtergraph.
    escaped = str(script).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (
        f"sendcmd=f='{escaped}',"
        f"crop=w={crop_w}:h={crop_h}:x=0:y=0,"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    )
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vf", vf, *_encode_args(with_audio=info.has_audio, fps=fps), str(dst),
    ], timeout=3600)
    if not dst.exists() or dst.stat().st_size == 0:
        raise StepFailed(f"Reframe produced no output: {dst}")
    return dst


def normalize_loudness(src: Path, dst: Path, target_lufs: float = -16.0) -> Path:
    """Single-pass EBU R128 normalisation to a platform-sane loudness."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        *_audio_codec_args(dst), str(dst),
    ], timeout=900)
    return dst


def pad_audio(src: Path, dst: Path, target_s: float) -> Path:
    """Pad (or hard-cut) an audio file to an exact duration.

    Phase boundaries in the composition are fixed; VO that lands 200ms short
    would otherwise leave the next phase's first word overlapping the cut.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-af", f"apad=whole_dur={target_s:.3f},atrim=0:{target_s:.3f}",
        *_audio_codec_args(dst), str(dst),
    ])
    return dst


def extract_frames(src: Path, out_dir: Path, timestamps: Iterable[float], *, max_px: int = 768) -> list[Path]:
    """Pull single frames at given timestamps, for the vision identity check."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, ts in enumerate(timestamps):
        dst = out_dir / f"frame_{i:02d}.jpg"
        run([
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, ts):.3f}", "-i", str(src), "-frames:v", "1",
            "-vf", f"scale='min({max_px},iw)':-2:flags=lanczos",
            "-q:v", "3", str(dst),
        ], timeout=180)
        if dst.exists():
            written.append(dst)
    if not written:
        raise StepFailed(f"Could not extract any frames from {src}")
    return written


def still_to_clip(src: Path, dst: Path, duration_s: float, *, width: int, height: int, fps: int) -> Path:
    """Turn a reference still into a slow push-in clip.

    Used as the v2v driver when no driver footage is supplied: Seedance needs
    motion to work from, and a dead-still frame produces a dead-still avatar.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(round(duration_s * fps)))
    vf = (
        f"scale={width * 2}:-2:flags=lanczos,"
        f"zoompan=z='min(zoom+0.0004,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={width}x{height}:fps={fps},setsar=1"
    )
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-i", str(src), "-t", f"{duration_s:.3f}",
        "-vf", vf, *_encode_args(with_audio=False, fps=fps), str(dst),
    ], timeout=900)
    return dst
