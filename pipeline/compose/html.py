"""Render the timeline as a HyperFrames HTML composition.

Everything here is written against the contract HyperFrames actually enforces
(`npx hyperframes docs data-attributes`, and its lint rules):

  * one root element carrying `data-composition-id`, `data-width`, `data-height`
  * every timed element carries `data-start`, `data-duration`, `data-track-index`
    and `class="clip"`
  * `<video>` is muted, with a sibling `<audio>` for its sound
  * GSAP timelines are created paused and registered on
    `window.__timelines[<composition id>]`
  * the composition is deterministic — no `Date.now`, no `Math.random`, no
    network fetches at render time

Two consequences shape the markup. First, GSAP's supported property set here is
opacity / x / y / scale / rotation / width / height / visibility — no colour
tweens — so karaoke highlighting is done by cross-fading a bright duplicate of
each word over a dim base rather than animating colour. Second, the transcript
is inlined as `const TRANSCRIPT = [...]`, the exact form
`hyperframes transcribe` looks for when it patches caption HTML, so re-running
transcribe against this project updates the words in place.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .assets import GSAP_FILENAME, ensure_gsap
from .captions import Cue
from .markers import Marker
from .timeline import Clip, Timeline

COMPOSITION_ID = "main"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _num(value: float) -> str:
    """Trim trailing zeros — the attributes read better and diff smaller."""
    return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"


def _attrs(pairs: dict[str, Any]) -> str:
    return " ".join(f'{k}="{_esc(v)}"' for k, v in pairs.items() if v is not None and v != "")


@dataclass
class CompositionAssets:
    """Paths are written into the HTML relative to the project directory."""

    article_screenshot: str | None = None
    # CSS width of the scraped page. The screenshot is captured at a device
    # scale factor above 1 for sharpness, so the <img> must be pinned back to
    # the page's CSS width or every measured rect lands at the wrong place.
    article_page_width: float | None = None
    music: str | None = None


def _clip_attrs(clip: Clip) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "id": clip.id,
        "class": " ".join(("clip",) + tuple(clip.classes)),
        "data-start": _num(clip.start),
        "data-duration": _num(clip.duration),
        "data-track-index": clip.track,
    }
    if clip.media_start:
        attrs["data-media-start"] = _num(clip.media_start)
    attrs.update(clip.attrs)
    return attrs


def _render_clip(clip: Clip) -> str:
    attrs = _clip_attrs(clip)
    style = f' style="{_esc(clip.style)}"' if clip.style else ""
    if clip.kind == "video":
        attrs["src"] = clip.src
        # Video is always muted here; its sound, if any, is carried by a sibling
        # <audio> clip. Declaring data-has-audio on a muted element is a lint error.
        attrs.pop("data-has-audio", None)
        return f"      <video {_attrs(attrs)} muted playsinline{style}></video>"
    if clip.kind == "audio":
        attrs["src"] = clip.src
        attrs["data-volume"] = _num(clip.volume)
        return f"      <audio {_attrs(attrs)}></audio>"
    if clip.kind == "image":
        attrs["src"] = clip.src
        return f"      <img {_attrs(attrs)}{style} />"
    return f"      <div {_attrs(attrs)}{style}>{clip.inner_html}</div>"


def _caption_html(cue: Cue, index: int, cfg: dict[str, Any]) -> Clip:
    words = []
    for w_index, word in enumerate(cue.words):
        text = _esc(word.text)
        words.append(
            f'<span class="w"><span class="w-base">{text}</span>'
            f'<span class="w-lit" id="c{index}w{w_index}">{text}</span></span>'
        )
    return Clip(
        id=f"cue-{index}",
        kind="box",
        start=cue.start,
        duration=max(0.12, cue.end - cue.start),
        track=2,
        classes=("cue",),
        inner_html='<div class="cue-inner">' + "".join(words) + "</div>",
        attrs={"data-phase": cue.phase},
    )


def _marker_html(
    marker: Marker, index: int, screenshot: str, cfg: dict[str, Any],
    page_width: float | None = None,
) -> Clip:
    width_rule = f"width:{_num(page_width)}px;" if page_width else ""
    style_page = (
        f"left:{_num(marker.image_x)}px;top:{_num(marker.image_y)}px;{width_rule}"
        f"transform:scale({_num(marker.image_scale)});transform-origin:0 0;"
    )
    style_hit = (
        f"left:{_num(marker.rect_x)}px;top:{_num(marker.rect_y)}px;"
        f"width:{_num(marker.rect_w)}px;height:{_num(marker.rect_h)}px;"
    )
    # Each marker shows the same screenshot at a different place. Without
    # distinct timing these are identical media nodes, which HyperFrames flags
    # as discoverable twice during compilation — and they are genuinely timed,
    # so the attributes belong here regardless.
    inner = (
        f'<img class="clip marker-page" id="mk{index}-page" src="{_esc(screenshot)}" '
        f'data-start="{_num(marker.start)}" data-duration="{_num(marker.duration)}" '
        f'data-track-index="1" '
        f'style="{style_page}" />'
        f'<div class="marker-scrim"></div>'
        f'<div class="marker-hit marker-{_esc(cfg.get("style", "underline"))}" '
        f'id="mk{index}-hit" style="{style_hit}"></div>'
    )
    return Clip(
        id=f"marker-{index}",
        kind="box",
        start=marker.start,
        duration=marker.duration,
        track=1,
        classes=("marker",),
        inner_html=inner,
        attrs={"data-phase": marker.phase},
    )


def build_timelines_js(
    cues: Sequence[Cue],
    markers: Sequence[Marker],
    duration: float,
    *,
    caption_style: str,
    marker_color: str,
) -> str:
    """Emit the paused GSAP timeline the runtime drives.

    Every tween is positioned absolutely on the master timeline, so the render
    is identical no matter which frame the renderer asks for first.
    """
    lines: list[str] = [
        "      window.__timelines = window.__timelines || {};",
        "      const tl = gsap.timeline({ paused: true });",
        "",
        "      // Progress hairline across the top edge.",
        f'      tl.fromTo("#progress", {{ scaleX: 0 }}, {{ scaleX: 1, duration: {_num(max(duration, 0.1))}, ease: "none" }}, 0);',
    ]

    if cues:
        lines += ["", "      // Captions: fade the cue in, then light each word on its own onset."]
    for i, cue in enumerate(cues):
        cue_len = max(0.12, cue.end - cue.start)
        fade = min(0.16, cue_len / 3)
        lines.append(
            f'      tl.fromTo("#cue-{i}", {{ opacity: 0, y: 18 }}, '
            f'{{ opacity: 1, y: 0, duration: {_num(fade)}, ease: "power2.out" }}, {_num(cue.start)});'
        )
        exit_at = max(cue.start + fade, cue.end - fade)
        lines.append(
            f'      tl.to("#cue-{i}", {{ opacity: 0, duration: {_num(fade)}, ease: "power1.in" }}, '
            f"{_num(exit_at)});"
        )
        # Hard kill on the exact boundary. The renderer seeks non-linearly, so
        # a frame can land after the fade without the fade having been played;
        # the set guarantees the element is gone at and after that instant.
        lines.append(f'      tl.set("#cue-{i}", {{ opacity: 0 }}, {_num(exit_at + fade)});')
        if caption_style == "karaoke":
            for w_index, word in enumerate(cue.words):
                lit = min(0.1, max(0.04, (word.end - word.start) / 3))
                lines.append(
                    f'      tl.fromTo("#c{i}w{w_index}", {{ opacity: 0 }}, '
                    f'{{ opacity: 1, duration: {_num(lit)}, ease: "none" }}, {_num(word.start)});'
                )
        else:
            lines.append(
                f'      tl.set("#cue-{i} .w-lit", {{ opacity: 1 }}, {_num(cue.start)});'
            )

    if markers:
        lines += ["", "      // Article markers: settle the page, then wipe the highlight across."]
    for i, marker in enumerate(markers):
        in_s = min(0.28, marker.duration / 4)
        out_s = min(0.24, marker.duration / 4)
        scale = marker.image_scale
        lines += [
            f'      tl.fromTo("#marker-{i}", {{ opacity: 0 }}, '
            f'{{ opacity: 1, duration: {_num(in_s)}, ease: "power2.out" }}, {_num(marker.start)});',
            f'      tl.fromTo("#mk{i}-page", {{ scale: {_num(scale)} }}, '
            f'{{ scale: {_num(scale * 1.035)}, duration: {_num(marker.duration)}, ease: "none" }}, '
            f"{_num(marker.start)});",
            f'      tl.fromTo("#mk{i}-hit", {{ scaleX: 0, opacity: 0.9 }}, '
            f'{{ scaleX: 1, opacity: 1, duration: {_num(min(0.45, marker.duration / 2))}, '
            f'ease: "power3.out" }}, {_num(marker.start + in_s)});',
            f'      tl.to("#marker-{i}", {{ opacity: 0, duration: {_num(out_s)}, ease: "power1.in" }}, '
            f"{_num(max(marker.start + in_s, marker.start + marker.duration - out_s))});",
            f'      tl.set("#marker-{i}", {{ opacity: 0 }}, '
            f"{_num(max(marker.start + in_s, marker.start + marker.duration - out_s) + out_s)});",
        ]

    lines += ["", f'      window.__timelines["{COMPOSITION_ID}"] = tl;']
    return "\n".join(lines)


def _css(width: int, height: int, cfg: dict[str, Any]) -> str:
    captions = cfg["captions"]
    markers = cfg["markers"]
    safe_bottom = int(height * float(captions["safe_bottom_pct"]) / 100)
    return f"""
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{
        width: {width}px; height: {height}px; overflow: hidden; background: #000;
        font-family: "Inter", "Helvetica Neue", Arial, sans-serif;
        -webkit-font-smoothing: antialiased;
      }}
      #root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; }}

      .bed {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}

      /* Keeps captions legible over bright footage without dimming the frame. */
      #scrim {{
        position: absolute; left: 0; right: 0; bottom: 0; height: {int(height * 0.42)}px;
        background: linear-gradient(to top, rgba(0,0,0,0.86) 0%, rgba(0,0,0,0.45) 45%, rgba(0,0,0,0) 100%);
        pointer-events: none;
      }}
      #progress {{
        position: absolute; top: 0; left: 0; width: {width}px; height: 6px;
        background: {markers["color"]}; transform-origin: 0 50%; opacity: 0.9;
      }}

      .cue {{
        position: absolute; left: 64px; right: 64px; bottom: {safe_bottom}px;
        display: flex; justify-content: center; text-align: center;
      }}
      .cue-inner {{
        display: flex; flex-wrap: wrap; gap: 0 16px; justify-content: center;
        max-width: {width - 128}px;
      }}
      .w {{ position: relative; display: inline-block; }}
      .w-base, .w-lit {{
        font-size: {captions["font_px"]}px; font-weight: 800; letter-spacing: -0.02em;
        line-height: 1.12; text-transform: uppercase;
        text-shadow: 0 4px 22px rgba(0,0,0,0.75), 0 1px 0 rgba(0,0,0,0.6);
      }}
      .w-base {{ color: rgba(255,255,255,0.62); }}
      .w-lit {{
        position: absolute; left: 0; top: 0; color: {captions["highlight_color"]};
        opacity: 0; white-space: nowrap;
      }}

      /* Overlays rest at opacity 0: the renderer can ask for any frame, and a
         full-frame element that defaults to visible covers everything before its
         reveal tween runs. Both are driven to 1 by the GSAP timeline. */
      .cue {{ opacity: 0; }}
      .marker {{ position: absolute; inset: 0; overflow: hidden; background: #0b0b0c; opacity: 0; }}
      .marker-page {{ position: absolute; image-rendering: auto; }}
      .marker-scrim {{
        position: absolute; inset: 0; pointer-events: none;
        background: radial-gradient(ellipse at 50% 44%, rgba(0,0,0,0) 30%, rgba(0,0,0,0.72) 78%);
      }}
      .marker-hit {{ position: absolute; transform-origin: 0 50%; }}
      .marker-underline {{
        border-bottom: 10px solid {markers["color"]};
        box-shadow: 0 10px 30px rgba(0,0,0,0.45);
      }}
      .marker-box {{
        background: {markers["color"]}; mix-blend-mode: multiply; border-radius: 4px;
      }}
      .marker-circle {{
        border: 8px solid {markers["color"]}; border-radius: 50%;
        transform-origin: 50% 50%;
      }}
"""


def render_composition(
    timeline: Timeline,
    cues: Sequence[Cue],
    markers: Sequence[Marker],
    assets: CompositionAssets,
    cfg: dict[str, Any],
    transcript: Sequence[dict[str, Any]],
    gsap_src: str = GSAP_FILENAME,
) -> str:
    width, height = timeline.width, timeline.height
    duration = timeline.duration

    clips = list(timeline.clips)
    for i, cue in enumerate(cues):
        clips.append(_caption_html(cue, i, cfg["captions"]))
    if assets.article_screenshot:
        for i, marker in enumerate(markers):
            clips.append(_marker_html(
                marker, i, assets.article_screenshot, cfg["markers"], assets.article_page_width,
            ))

    clips.sort(key=lambda c: (c.track, c.start))
    body = "\n".join(_render_clip(c) for c in clips)

    timelines_js = build_timelines_js(
        cues, markers if assets.article_screenshot else [], duration,
        caption_style=cfg["captions"]["style"],
        marker_color=cfg["markers"]["color"],
    )
    transcript_json = json.dumps(list(transcript), indent=2).replace("\n", "\n          ")

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <title>{_esc(cfg.get("id", "short"))}</title>
    <script src="{_esc(gsap_src)}"></script>
    <style>{_css(width, height, cfg)}    </style>
  </head>
  <body>
    <div
      id="root"
      data-composition-id="{COMPOSITION_ID}"
      data-start="0"
      data-duration="{_num(duration)}"
      data-width="{width}"
      data-height="{height}"
    >
{body}
      <div id="scrim"></div>
      <div id="progress"></div>
    </div>

    <script>
      // Word-level transcript, in the shape `hyperframes transcribe` patches.
      // Captions above are pre-rendered from exactly these timings.
      const TRANSCRIPT = {transcript_json};
      void TRANSCRIPT;
    </script>

    <script>
{timelines_js}
    </script>
  </body>
</html>
"""


def write_project(
    directory: Path,
    composition_html: str,
    *,
    name: str,
    fps: int,
    width: int,
    height: int,
    transcript: Sequence[dict[str, Any]],
) -> Path:
    """Write index.html plus the project sidecars HyperFrames expects.

    GSAP is vendored into the directory first, so the composition never depends
    on the renderer's network reaching a CDN.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ensure_gsap(directory)
    index = directory / "index.html"
    index.write_text(composition_html, encoding="utf-8")
    (directory / "meta.json").write_text(
        json.dumps({"id": name, "name": name, "fps": fps, "width": width, "height": height}, indent=2),
        encoding="utf-8",
    )
    (directory / "transcript.json").write_text(
        json.dumps(list(transcript), indent=2), encoding="utf-8",
    )
    return index
