"""Contact sheets: how a change request gets precise.

Reviewing an edit in a chat window is the awkward part of this loop. Nobody
wants to scrub a file to find the moment they mean, and describing it as "the
bit after the wide shot" costs a round trip.

So every edit can emit a grid of stamped frames. You point at 0:14, and 0:14 is
unambiguous to both of us.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import StepFailed
from ..media.probe import probe
from ..shell import ffmpeg, run
from .ops import _escape_drawtext, _font
from .timecode import format_tc
from .. import logs


def sheet_times(duration: float, count: int) -> list[float]:
    """Evenly spaced samples that avoid the very first and last frame."""
    if duration <= 0 or count <= 0:
        return []
    if count == 1:
        return [duration / 2]
    inset = min(0.25, duration / (count * 4))
    span = max(0.0, duration - 2 * inset)
    return [round(inset + span * i / (count - 1), 3) for i in range(count)]


def contact_sheet(
    video: Path,
    out: Path,
    *,
    count: int = 12,
    columns: int = 4,
    tile_width: int = 360,
) -> Path:
    """Render a labelled grid of frames sampled across the clip."""
    info = probe(video)
    if info.duration_s <= 0:
        raise StepFailed(f"{video.name} reports no duration")

    times = sheet_times(info.duration_s, count)
    frames_dir = out.parent / f".{out.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    tile_height = max(2, int(round(tile_width * info.height / max(1, info.width))))
    tile_height -= tile_height % 2

    tiles: list[Path] = []
    for index, at in enumerate(times):
        tile = frames_dir / f"t{index:02d}.png"
        label = format_tc(at, millis=True)
        # The label contains colons; unescaped they terminate drawtext's own
        # option parsing and the stamp silently truncates to "0".
        drawtext = (
            f"drawtext=fontfile={_font()}:text={_escape_drawtext(label)}:x=10:y=h-th-10:"
            f"fontsize={max(14, tile_width // 18)}:fontcolor=white:"
            f"box=1:boxcolor=black@0.65:boxborderw=6"
        )
        run([
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", f"scale={tile_width}:{tile_height}:flags=lanczos,{drawtext}",
            str(tile),
        ], timeout=180)
        if tile.exists():
            tiles.append(tile)

    if not tiles:
        raise StepFailed(f"Could not sample any frames from {video}")

    rows = (len(tiles) + columns - 1) // columns
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for tile in tiles:
        args += ["-i", str(tile)]
    # tile= needs a full grid; pad the last row with the layout's own colour.
    args += [
        "-filter_complex",
        f"{''.join(f'[{i}:v]' for i in range(len(tiles)))}"
        f"xstack=inputs={len(tiles)}:layout={_layout(len(tiles), columns)}:fill=black[out]"
        if len(tiles) > 1 else "[0:v]null[out]",
        "-map", "[out]", "-frames:v", "1", str(out),
    ]
    run(args, timeout=600)

    for tile in tiles:
        tile.unlink(missing_ok=True)
    frames_dir.rmdir()

    logs.ok(f"contact sheet: {out.name}", frames=len(tiles), grid=f"{columns}x{rows}")
    return out


def _layout(count: int, columns: int) -> str:
    """xstack layout string: w0_0|w0_h0|… for a simple grid."""
    cells = []
    for index in range(count):
        row, column = divmod(index, columns)
        x = "0" if column == 0 else "+".join(f"w{c}" for c in range(column))
        y = "0" if row == 0 else "+".join(f"h{r * columns}" for r in range(row))
        cells.append(f"{x}_{y}")
    return "|".join(cells)
