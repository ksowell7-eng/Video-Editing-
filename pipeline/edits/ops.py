"""The edit vocabulary.

Each entry turns one plain-language change — "trim the dead air at the top",
"speed up the middle", "make it vertical", "put the date on screen" — into one
deterministic ffmpeg pass.

Every op reads a source file and writes a new one; nothing is ever edited in
place. That is what makes the loop safe to iterate: the original is untouched
no matter how many rounds of changes come in, and removing an op from the list
genuinely undoes it rather than requiring an inverse edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..errors import ConfigError, StepFailed
from ..media.edit import _encode_args, concat, trim
from ..media.probe import MediaInfo, probe
from ..shell import ffmpeg, run
from .timecode import format_tc, parse, resolve_span

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
)

_POSITIONS = {
    "top": "(w-text_w)/2:h*0.08",
    "center": "(w-text_w)/2:(h-text_h)/2",
    "bottom": "(w-text_w)/2:h*0.82",
    "top-left": "w*0.06:h*0.08",
    "top-right": "w-text_w-w*0.06:h*0.08",
    "bottom-left": "w*0.06:h*0.82",
    "bottom-right": "w-text_w-w*0.06:h*0.82",
}


@dataclass
class OpContext:
    """Everything an op needs beyond its own spec."""

    workdir: Path
    fps: int
    job_root: Path
    reframe_cfg: dict[str, Any]

    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.job_root / p).resolve()


def _resolve_font(explicit: str | None, ctx: "OpContext", default_name: str) -> str:
    """Explicit path first, then the repo's bundled faces, then a system font.

    Bundled faces are found by walking up from the edit list, so an edit list in
    any subdirectory still finds assets/fonts at the project root.
    """
    if explicit:
        candidate = ctx.resolve(explicit)
        if candidate.exists():
            return str(candidate)
    for parent in [ctx.job_root, *ctx.job_root.parents]:
        candidate = parent / "assets" / "fonts" / default_name
        if candidate.exists():
            return str(candidate)
    return _font()


def _font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise StepFailed(
        "No font found for burned-in text",
        hint="Install DejaVu or Liberation fonts, or drop the 'text' op.",
    )


def _simple_filter(src: Path, dst: Path, vf: str | None, af: str | None,
                   info: MediaInfo, fps: int) -> Path:
    """One filtergraph pass, preserving whichever streams exist."""
    args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if vf:
        args += ["-vf", vf]
    if af and info.has_audio:
        args += ["-af", af]
    args += _encode_args(with_audio=info.has_audio, fps=fps)
    args += [str(dst)]
    run(args, timeout=3600)
    return dst


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def op_trim(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Keep a range, discard the rest."""
    info = probe(src)
    start, end = resolve_span(spec, info.duration_s)
    if end - start < 0.05:
        raise ConfigError(f"trim would leave {end - start:.2f}s of video")
    return trim(src, dst, start, end - start, fps=ctx.fps)


def op_cut(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Remove a range and close the gap."""
    info = probe(src)
    start, end = resolve_span(spec, info.duration_s)
    if end - start < 0.01:
        raise ConfigError("cut spans no time")

    pieces: list[Path] = []
    if start > 0.05:
        head = ctx.workdir / f"{dst.stem}_head.mp4"
        pieces.append(trim(src, head, 0.0, start, fps=ctx.fps))
    if info.duration_s - end > 0.05:
        tail = ctx.workdir / f"{dst.stem}_tail.mp4"
        pieces.append(trim(src, tail, end, info.duration_s - end, fps=ctx.fps))
    if not pieces:
        raise ConfigError("cut would remove the entire video")
    if len(pieces) == 1:
        pieces[0].replace(dst)
        return dst
    return concat(pieces, dst, fps=ctx.fps, workdir=ctx.workdir)


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5–100 per instance, so extremes get chained."""
    steps: list[float] = []
    remaining = factor
    while remaining < 0.5:
        steps.append(0.5)
        remaining /= 0.5
    while remaining > 100.0:
        steps.append(100.0)
        remaining /= 100.0
    steps.append(remaining)
    return ",".join(f"atempo={s:.6f}" for s in steps)


def op_speed(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Change speed, over the whole clip or one range."""
    info = probe(src)
    factor = float(spec.get("factor", 1.0))
    if factor <= 0:
        raise ConfigError(f"speed factor must be positive, got {factor}")
    if abs(factor - 1.0) < 1e-6:
        return trim(src, dst, 0, info.duration_s, fps=ctx.fps)

    has_range = spec.get("from") is not None or spec.get("to") is not None
    vf = f"setpts=PTS/{factor:.6f}"
    af = _atempo_chain(factor)
    if not has_range:
        return _simple_filter(src, dst, vf, af, info, ctx.fps)

    # Ranged: split, retime the middle, stitch. Doing it with one enable=
    # expression is not possible — setpts has no per-range form.
    start, end = resolve_span(spec, info.duration_s)
    pieces: list[Path] = []
    if start > 0.05:
        pieces.append(trim(src, ctx.workdir / f"{dst.stem}_a.mp4", 0.0, start, fps=ctx.fps))
    middle_src = trim(src, ctx.workdir / f"{dst.stem}_b_src.mp4", start, end - start, fps=ctx.fps)
    middle = _simple_filter(
        middle_src, ctx.workdir / f"{dst.stem}_b.mp4", vf, af, probe(middle_src), ctx.fps,
    )
    pieces.append(middle)
    if info.duration_s - end > 0.05:
        pieces.append(trim(src, ctx.workdir / f"{dst.stem}_c.mp4", end,
                           info.duration_s - end, fps=ctx.fps))
    return concat(pieces, dst, fps=ctx.fps, workdir=ctx.workdir)


def op_freeze(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Hold a single frame for a while, then carry on."""
    from ..media.edit import still_to_clip  # noqa: PLC0415

    info = probe(src)
    at = parse(spec.get("at"), field="at") or 0.0
    at = min(max(0.0, at), max(0.0, info.duration_s - 0.05))
    hold = float(spec.get("seconds", 1.0))
    if hold <= 0:
        raise ConfigError("freeze seconds must be positive")

    frame = ctx.workdir / f"{dst.stem}_frame.png"
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}",
         "-i", str(src), "-frames:v", "1", str(frame)], timeout=300)
    still = still_to_clip(frame, ctx.workdir / f"{dst.stem}_still.mp4", hold,
                          width=info.width, height=info.height, fps=ctx.fps)

    pieces = []
    if at > 0.05:
        pieces.append(trim(src, ctx.workdir / f"{dst.stem}_a.mp4", 0.0, at, fps=ctx.fps))
    pieces.append(still)
    if info.duration_s - at > 0.05:
        pieces.append(trim(src, ctx.workdir / f"{dst.stem}_c.mp4", at,
                           info.duration_s - at, fps=ctx.fps))
    return concat(pieces, dst, fps=ctx.fps, workdir=ctx.workdir)


def op_append(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Join another file on the end (or the front, with position: before)."""
    other = ctx.resolve(str(spec["file"]))
    if not other.exists():
        raise ConfigError(f"append: file not found: {spec['file']}")
    order = [src, other] if spec.get("position", "after") == "after" else [other, src]
    return concat(order, dst, fps=ctx.fps, workdir=ctx.workdir)


# --------------------------------------------------------------------------
# frame
# --------------------------------------------------------------------------

def _aspect(value: str) -> tuple[int, int]:
    presets = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080),
               "4:5": (1080, 1350), "4:3": (1440, 1080)}
    text = str(value).strip()
    if text in presets:
        return presets[text]
    if "x" in text.lower():
        w, _, h = text.lower().partition("x")
        return int(w), int(h)
    raise ConfigError(
        f"aspect {value!r} not recognised",
        hint=f"Use one of {', '.join(presets)} or an explicit size like 1080x1920.",
    )


def op_reframe(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Re-aim the frame to a new aspect using the tracked crop path."""
    from ..steps.reframe import reframe_clip  # noqa: PLC0415

    width, height = _aspect(spec.get("aspect", "9:16"))
    cfg = {**ctx.reframe_cfg}
    if "strategy" in spec:
        cfg["strategy"] = spec["strategy"]
    reframe_clip(src, dst, cfg, out_w=width, out_h=height, fps=ctx.fps,
                 workdir=ctx.workdir / "reframe")
    return dst


def op_crop(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    width = int(spec.get("width", info.width))
    height = int(spec.get("height", info.height))
    x = int(spec.get("x", (info.width - width) // 2))
    y = int(spec.get("y", (info.height - height) // 2))
    return _simple_filter(src, dst, f"crop={width}:{height}:{x}:{y},setsar=1", None, info, ctx.fps)


def op_scale(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    if "aspect" in spec:
        width, height = _aspect(spec["aspect"])
    else:
        width = int(spec.get("width", -2))
        height = int(spec.get("height", -2))
    if width == -2 and height == -2:
        raise ConfigError("scale needs width, height, or aspect")
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
          if spec.get("pad", True) and width > 0 and height > 0
          else f"scale={width}:{height}:flags=lanczos,setsar=1")
    return _simple_filter(src, dst, vf, None, info, ctx.fps)


def op_rotate(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    degrees = int(spec.get("degrees", 90)) % 360
    mapping = {0: "null", 90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}
    if degrees not in mapping:
        raise ConfigError("rotate degrees must be 0, 90, 180 or 270")
    return _simple_filter(src, dst, mapping[degrees], None, info, ctx.fps)


def op_color(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Brightness / contrast / saturation, plus optional sharpening."""
    info = probe(src)
    parts = [
        f"eq=brightness={float(spec.get('brightness', 0.0)):.3f}"
        f":contrast={float(spec.get('contrast', 1.0)):.3f}"
        f":saturation={float(spec.get('saturation', 1.0)):.3f}"
        f":gamma={float(spec.get('gamma', 1.0)):.3f}"
    ]
    if spec.get("sharpen"):
        parts.append("unsharp=5:5:0.8:5:5:0.0")
    return _simple_filter(src, dst, ",".join(parts), None, info, ctx.fps)


def op_stabilize(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    strength = int(spec.get("strength", 4))
    return _simple_filter(src, dst, f"deshake=rx={strength}:ry={strength}", None, info, ctx.fps)


def op_fade(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Fade the head and tail, picture and sound together."""
    info = probe(src)
    fade_in = float(spec.get("in_s", spec.get("in", 0.0)) or 0.0)
    fade_out = float(spec.get("out_s", spec.get("out", 0.0)) or 0.0)
    if fade_in + fade_out > info.duration_s:
        raise ConfigError(
            f"fades ({fade_in + fade_out:.1f}s) exceed the clip ({info.duration_s:.1f}s)"
        )
    colour = spec.get("color", "black")
    vf, af = [], []
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in:.3f}:color={colour}")
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        start = max(0.0, info.duration_s - fade_out)
        vf.append(f"fade=t=out:st={start:.3f}:d={fade_out:.3f}:color={colour}")
        af.append(f"afade=t=out:st={start:.3f}:d={fade_out:.3f}")
    if not vf:
        raise ConfigError("fade needs in_s and/or out_s")
    return _simple_filter(src, dst, ",".join(vf), ",".join(af) or None, info, ctx.fps)


# --------------------------------------------------------------------------
# graphics
# --------------------------------------------------------------------------

def op_text(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Burn a line of text on screen, optionally only for a range."""
    info = probe(src)
    text = str(spec.get("text", "")).strip()
    if not text:
        raise ConfigError("text op needs a 'text' value")

    position = spec.get("position", "bottom")
    if position not in _POSITIONS:
        raise ConfigError(
            f"position {position!r} not recognised",
            hint=f"Use one of: {', '.join(_POSITIONS)}",
        )
    x, _, y = _POSITIONS[position].partition(":")
    size = int(spec.get("size", max(28, round(info.height * 0.045))))

    options = [
        f"fontfile={_font()}",
        f"text={_escape_drawtext(text)}",
        f"x={x}", f"y={y}",
        f"fontsize={size}",
        f"fontcolor={spec.get('color', 'white')}",
        "borderw=3", "bordercolor=black@0.85",
        "line_spacing=8",
    ]
    if spec.get("box", True):
        options += ["box=1", f"boxcolor={spec.get('box_color', 'black@0.45')}", "boxborderw=18"]
    if spec.get("from") is not None or spec.get("to") is not None:
        start, end = resolve_span(spec, info.duration_s)
        options.append(f"enable='between(t,{start:.3f},{end:.3f})'")

    return _simple_filter(src, dst, "drawtext=" + ":".join(options), None, info, ctx.fps)


def _escape_drawtext(text: str) -> str:
    """drawtext eats :, \\, ', % and newlines unless they are escaped."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")     # a typographic apostrophe dodges quoting entirely
        .replace("%", "\\%")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


def op_subtitles(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Burn an existing .srt/.vtt into the picture."""
    info = probe(src)
    subs = ctx.resolve(str(spec["file"]))
    if not subs.exists():
        raise ConfigError(f"subtitles: file not found: {spec['file']}")
    style = spec.get(
        "style",
        f"FontName=DejaVu Sans,Fontsize={int(spec.get('size', 22))},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,BorderStyle=3,Outline=2,Shadow=0",
    )
    escaped = str(subs).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return _simple_filter(
        src, dst, f"subtitles='{escaped}':force_style='{style}'", None, info, ctx.fps,
    )


# --------------------------------------------------------------------------
# sound
# --------------------------------------------------------------------------

def op_volume(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    if not info.has_audio:
        raise ConfigError("volume: this clip has no audio track")
    factor = float(spec.get("factor", 1.0))
    if spec.get("from") is not None or spec.get("to") is not None:
        start, end = resolve_span(spec, info.duration_s)
        af = f"volume=enable='between(t,{start:.3f},{end:.3f})':volume={factor:.4f}"
    else:
        af = f"volume={factor:.4f}"
    return _simple_filter(src, dst, None, af, info, ctx.fps)


def op_mute(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         *_encode_args(with_audio=False, fps=ctx.fps), str(dst)], timeout=3600)
    return dst


def op_replace_audio(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    audio = ctx.resolve(str(spec["file"]))
    if not audio.exists():
        raise ConfigError(f"replace_audio: file not found: {spec['file']}")
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        *_encode_args(with_audio=True, fps=ctx.fps), str(dst),
    ], timeout=3600)
    return dst


def op_music(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Lay a music bed under the existing audio, ducked beneath it."""
    info = probe(src)
    music = ctx.resolve(str(spec["file"]))
    if not music.exists():
        raise ConfigError(f"music: file not found: {spec['file']}")
    gain = float(spec.get("gain", 0.18))

    if not info.has_audio:
        # Nothing to duck against; the bed simply becomes the audio track.
        run([
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", f"[1:a]volume={gain:.3f}[bed]",
            "-map", "0:v:0", "-map", "[bed]", "-shortest",
            *_encode_args(with_audio=True, fps=ctx.fps), str(dst),
        ], timeout=3600)
        return dst

    duck = spec.get("duck", True)
    if duck:
        # sidechaincompress pulls the bed down whenever the main audio speaks,
        # which is what keeps narration intelligible without manual keyframes.
        graph = (
            f"[1:a]volume={gain:.3f},aloop=loop=-1:size=2e9[bed];"
            f"[0:a]asplit=2[main][key];"
            f"[bed][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked];"
            f"[main][ducked]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )
    else:
        graph = (
            f"[1:a]volume={gain:.3f},aloop=loop=-1:size=2e9[bed];"
            f"[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-i", str(music),
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[out]",
        *_encode_args(with_audio=True, fps=ctx.fps), str(dst),
    ], timeout=3600)
    return dst


def op_loudness(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    info = probe(src)
    if not info.has_audio:
        raise ConfigError("loudness: this clip has no audio track")
    target = float(spec.get("lufs", -14.0))
    return _simple_filter(
        src, dst, None, f"loudnorm=I={target}:TP=-1.5:LRA=11", info, ctx.fps,
    )


# --------------------------------------------------------------------------
# grading and titling
# --------------------------------------------------------------------------

# A restrained golden-hour grade. The numbers are deliberately small: the goal
# is footage that looks graded, not footage that looks filtered. Every stage is
# separable so a single characteristic can be dialled without rebuilding.
_GRADE_BASE = (
    # Set a consistent toe and shoulder without clipping either end. omin lifts
    # the black point a little so shadow detail survives; omax rolls the
    # highlights off short of 255 so nothing takes on an HDR sheen.
    "colorlevels=romin={black}:gomin={black}:bomin={black}"
    ":romax={white}:gomax={white}:bomax={white},"
    # Gentle S-curve. Blue is pulled fractionally below red and green through
    # the midtones for warmth — shadows stay neutral, so blacks never go teal.
    "curves=r='0/0.015 0.25/0.248 0.5/{mid_r} 0.75/0.775 1/0.985'"
    ":g='0/0.015 0.25/0.243 0.5/{mid_g} 0.75/0.762 1/0.978'"
    ":b='0/0.02 0.25/0.242 0.5/{mid_b} 0.75/0.748 1/0.968',"
    # Desaturate greens and cyans only. Foliage calms down; skin is untouched
    # because skin lives in the reds and yellows.
    "selectivecolor=greens=0 0 {greens} -0.04:cyans=0 0 {cyans} -0.03"
    ":yellows=0 0 {yellows} -0.02,"
    # Warmth in the highlights, a trace in the midtones, nothing in the shadows.
    "colorbalance=rh={warm}:gh={warm_g}:bh=-{warm}:rm={warm_m}:bm=-{warm_m},"
    # Vibrance rather than saturation, with green held back.
    "vibrance=intensity={vibrance}:gbal=0.96"
)

# Highlight-only bloom, screened back. Preserves the natural haze of shooting
# into the sun instead of manufacturing a glow.
_GRADE_BLOOM = (
    "split[bl_a][bl_b];[bl_b]curves=all='0/0 0.75/0 0.9/0.5 1/1',"
    "gblur=sigma={sigma}[bl_c];[bl_a][bl_c]blend=all_mode=screen:all_opacity={opacity}"
)


def op_grade(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Apply the film grade.

    With `from`/`to`, the corrective half runs only over that range — which is
    how a shot lit differently from the rest gets matched to it rather than
    every shot being bent to accommodate one.
    """
    info = probe(src)
    warmth = float(spec.get("warmth", 0.020))
    parts = []

    # Corrective white balance, optionally limited to one range.
    temperature = spec.get("temperature")
    if temperature:
        window = ""
        if spec.get("from") is not None or spec.get("to") is not None:
            start, end = resolve_span(spec, info.duration_s)
            window = f":enable='between(t,{start:.3f},{end:.3f})'"
        mix = float(spec.get("temperature_mix", 0.55))
        shift = float(spec.get("temperature_shift", 0.035))
        parts.append(f"colortemperature=temperature={int(temperature)}:mix={mix}{window}")
        parts.append(
            f"colorbalance=rm={shift}:bm=-{shift}:rh={shift * 0.57:.4f}"
            f":bh=-{shift * 0.57:.4f}{window}"
        )

    if spec.get("base", True):
        parts.append(_GRADE_BASE.format(
            black=float(spec.get("black_lift", 0.05)),
            white=float(spec.get("white_rolloff", 0.972)),
            mid_r=float(spec.get("mid_r", 0.518)),
            mid_g=float(spec.get("mid_g", 0.508)),
            mid_b=float(spec.get("mid_b", 0.500)),
            greens=float(spec.get("greens", -0.22)),
            cyans=float(spec.get("cyans", -0.12)),
            yellows=float(spec.get("yellows", -0.05)),
            warm=f"{warmth:.4f}",
            warm_g=f"{warmth * 0.25:.4f}",
            warm_m=f"{warmth * 0.45:.4f}",
            vibrance=float(spec.get("vibrance", 0.05)),
        ))

    chain = ",".join(parts)
    if spec.get("bloom", True):
        chain += "," + _GRADE_BLOOM.format(
            sigma=float(spec.get("bloom_sigma", 20)),
            opacity=float(spec.get("bloom", 0.10)) if not isinstance(spec.get("bloom"), bool) else 0.10,
        )
    grain = float(spec.get("grain", 3))
    if grain > 0:
        # Temporal so it reads as film stock rather than fixed sensor dirt.
        chain += f",noise=alls={grain:.0f}:allf=t+u"

    return _simple_filter(src, dst, chain, None, info, ctx.fps)


# drawtext cannot set letter-spacing — the filter exposes line_spacing only.
# Tracking is therefore interleaved into the string itself, which is what makes
# set type read as editorial rather than as a caption.
_TRACKING_SPACERS = {0: "", 1: "\u200a", 2: "\u2009", 3: "\u2009\u200a"}


def _track(text: str, level: int) -> str:
    spacer = _TRACKING_SPACERS.get(max(0, min(3, int(level))), "")
    return spacer.join(text) if spacer else text


def _fade_alpha(start: float, end: float, fade: float) -> str:
    """drawtext alpha that ramps in, holds, and ramps out."""
    return (
        f"if(lt(t,{start:.3f}),0,"
        f"if(lt(t,{start + fade:.3f}),(t-{start:.3f})/{fade:.3f},"
        f"if(lt(t,{end - fade:.3f}),1,"
        f"if(lt(t,{end:.3f}),({end:.3f}-t)/{fade:.3f},0))))"
    )


def op_title(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """A single line of editorial type that fades up and away.

    Distinct from `text`: no box, a serif face, wide tracking, and a slow
    symmetric fade. Meant to sit over picture as a thematic beat rather than
    to label anything.
    """
    info = probe(src)
    text = str(spec.get("text", "")).strip()
    if not text:
        raise ConfigError("title op needs a 'text' value")

    start, end = resolve_span(spec, info.duration_s)
    fade = float(spec.get("fade", 1.2))
    if end - start < fade * 2:
        raise ConfigError(
            f"title '{text}' is on screen {end - start:.1f}s but its fades need "
            f"{fade * 2:.1f}s"
        )

    font = _resolve_font(spec.get("font"), ctx, "PlayfairDisplay-400.ttf")
    size = int(spec.get("size", max(30, round(info.height * 0.062))))
    tracked = _track(text, spec.get("tracking", 2))

    y = {
        "top": f"h*{spec.get('inset', 0.14)}",
        "center": "(h-text_h)/2",
        "lower": f"h*{spec.get('inset', 0.72)}",
        "bottom": f"h*{spec.get('inset', 0.82)}",
    }.get(spec.get("position", "lower"), f"h*{spec.get('inset', 0.72)}")

    options = [
        f"fontfile={font}",
        f"text={_escape_drawtext(tracked)}",
        "x=(w-text_w)/2", f"y={y}",
        f"fontsize={size}",
        f"fontcolor={spec.get('color', '#F2EADC')}",
        f"alpha='{_fade_alpha(start, end, fade)}'",
        "expansion=none",
    ]

    chain = "drawtext=" + ":".join(options)
    if spec.get("scrim"):
        # Ivory type over a bright sky needs help. A soft gradient under the
        # line is an editorial device, not a drop shadow, and it only exists
        # while the type is on screen.
        strength = float(spec.get("scrim", 0.28))
        chain = (
            f"drawbox=x=0:y=ih*0.55:w=iw:h=ih*0.45:color=black@{strength}:t=fill"
            f":enable='between(t,{start:.3f},{end:.3f})',"
        ) + chain
    return _simple_filter(src, dst, chain, None, info, ctx.fps)


def op_endcard(src: Path, dst: Path, spec: dict, ctx: OpContext) -> Path:
    """Append the event reveal.

    The background is a frame lifted from the film itself, blurred well past
    recognition and dropped in level — organically connected to what came
    before, and quiet enough that set type stays the only thing to read.
    """
    info = probe(src)
    lines = spec.get("lines") or []
    if not lines:
        raise ConfigError("endcard needs a 'lines' list")
    hold = float(spec.get("seconds", 8.0))

    font_regular = _resolve_font(spec.get("font"), ctx, "PlayfairDisplay-400.ttf")
    font_medium = _resolve_font(spec.get("font_medium"), ctx, "PlayfairDisplay-500.ttf")

    # Background plate, pulled from the film.
    at = parse(spec.get("plate_at"), field="plate_at")
    plate = ctx.workdir / f"{dst.stem}_plate.png"
    plate.parent.mkdir(parents=True, exist_ok=True)
    if at is not None:
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", str(plate)], timeout=300)
    if not plate.exists():
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"color=c={spec.get('background', '#141210')}:s={info.width}x{info.height}",
             "-frames:v", "1", str(plate)], timeout=120)

    blur = float(spec.get("blur", 46))
    dim = float(spec.get("dim", 0.62))
    plate_chain = (
        f"scale={info.width}:{info.height},gblur=sigma={blur},"
        f"colorlevels=romax={1 - dim:.3f}:gomax={1 - dim:.3f}:bomax={1 - dim:.3f},"
        f"noise=alls=3:allf=t+u"
    )

    fade = float(spec.get("fade", 1.4))
    draws = []
    for entry in lines:
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        size = int(entry.get("size", round(info.height * 0.05)))
        options = [
            f"fontfile={font_medium if entry.get('weight') == 'medium' else font_regular}",
            f"text={_escape_drawtext(_track(text, entry.get('tracking', 0)))}",
            "x=(w-text_w)/2",
            f"y=h*{float(entry.get('y', 0.5)):.4f}-text_h/2",
            f"fontsize={size}",
            f"fontcolor={entry.get('color', '#F2EADC')}",
            "expansion=none",
        ]
        delay = float(entry.get("delay", 0.0))
        options.append(
            f"alpha='if(lt(t,{delay:.3f}),0,"
            f"if(lt(t,{delay + fade:.3f}),(t-{delay:.3f})/{fade:.3f},"
            f"if(lt(t,{hold - 0.8:.3f}),1,max(0,({hold:.3f}-t)/0.8))))'"
        )
        draws.append("drawtext=" + ":".join(options))

    card = ctx.workdir / f"{dst.stem}_card.mp4"
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-i", str(plate), "-t", f"{hold:.3f}",
        "-vf", plate_chain + "," + ",".join(draws) + ",setsar=1",
        *_encode_args(with_audio=False, fps=ctx.fps),
        str(card),
    ], timeout=1800)

    return concat([src, card], dst, fps=ctx.fps, workdir=ctx.workdir)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OpSpec:
    fn: Callable[[Path, Path, dict, OpContext], Path]
    required: tuple[str, ...]
    summary: str


OPS: dict[str, OpSpec] = {
    "trim":          OpSpec(op_trim, (), "keep a time range, discard the rest"),
    "cut":           OpSpec(op_cut, (), "remove a time range and close the gap"),
    "speed":         OpSpec(op_speed, ("factor",), "retime the whole clip or one range"),
    "freeze":        OpSpec(op_freeze, ("seconds",), "hold a frame, then continue"),
    "append":        OpSpec(op_append, ("file",), "join another clip before or after"),
    "reframe":       OpSpec(op_reframe, (), "re-aim to a new aspect with a tracked crop"),
    "crop":          OpSpec(op_crop, (), "crop to an explicit rectangle"),
    "scale":         OpSpec(op_scale, (), "resize, letterboxing by default"),
    "rotate":        OpSpec(op_rotate, (), "rotate by 90/180/270"),
    "color":         OpSpec(op_color, (), "brightness, contrast, saturation, gamma"),
    "stabilize":     OpSpec(op_stabilize, (), "smooth out camera shake"),
    "fade":          OpSpec(op_fade, (), "fade picture and sound at the head and tail"),
    "text":          OpSpec(op_text, ("text",), "burn a line of text on screen"),
    "subtitles":     OpSpec(op_subtitles, ("file",), "burn in an .srt or .vtt"),
    "volume":        OpSpec(op_volume, ("factor",), "change level, whole clip or a range"),
    "mute":          OpSpec(op_mute, (), "drop the audio track"),
    "replace_audio": OpSpec(op_replace_audio, ("file",), "swap in a new audio track"),
    "music":         OpSpec(op_music, ("file",), "lay a music bed under, ducked"),
    "loudness":      OpSpec(op_loudness, (), "normalise to a target LUFS"),
    "grade":         OpSpec(op_grade, (), "golden-hour film grade, optionally shot-matched"),
    "title":         OpSpec(op_title, ("text",), "editorial serif type that fades up and away"),
    "endcard":       OpSpec(op_endcard, ("lines",), "append the event reveal card"),
}


def validate(spec: dict, index: int) -> str:
    """Check one op entry, returning its name."""
    if not isinstance(spec, dict):
        raise ConfigError(f"ops[{index}] must be an object, got {type(spec).__name__}")
    name = spec.get("op")
    if not name:
        raise ConfigError(f"ops[{index}] has no 'op' key")
    if name not in OPS:
        raise ConfigError(
            f"ops[{index}]: unknown op {name!r}",
            hint=f"Available: {', '.join(sorted(OPS))}",
        )
    missing = [k for k in OPS[name].required if spec.get(k) is None]
    if missing:
        raise ConfigError(f"ops[{index}] ({name}) is missing: {', '.join(missing)}")
    for key in ("from", "to", "at"):
        if key in spec:
            parse(spec[key], field=f"ops[{index}].{key}")
    return name


def describe(spec: dict) -> str:
    """A one-line human summary, for the run log and the change record."""
    name = spec.get("op", "?")
    bits = []
    if spec.get("from") is not None or spec.get("to") is not None:
        start = parse(spec.get("from"), field="from") or 0.0
        end = parse(spec.get("to"), field="to")
        precise = start % 1 or (end is not None and end % 1)
        shown_end = format_tc(end, millis=bool(precise)) if end is not None else "end"
        bits.append(f"{format_tc(start, millis=bool(precise))}–{shown_end}")
    for key in ("factor", "aspect", "seconds", "degrees", "text", "file", "lufs", "position"):
        if key in spec:
            value = str(spec[key])
            bits.append(f"{key}={value[:40]}")
    note = spec.get("note")
    line = f"{name}({', '.join(bits)})" if bits else name
    return f"{line}  — {note}" if note else line
