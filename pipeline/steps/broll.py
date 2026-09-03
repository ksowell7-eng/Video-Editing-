"""Step 2 — find and cut b-roll from YouTube with yt-dlp.

Search terms come from the article's own keywords unless the job names its
own. Each result is screened before download (duration, resolution, live
status, optionally licence), because downloading a 40-minute upload to keep
three seconds of it is the slowest possible way to fail.

Segments are taken from the interior of each source. The first and last
several seconds of an upload are overwhelmingly titles, intros and end cards,
and none of that is footage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import MissingDependency, StepFailed
from ..media.edit import concat, trim
from ..media.probe import probe
from .base import Context, StepResult
from .. import logs

_INTRO_SKIP_S = 8.0
_OUTRO_SKIP_S = 6.0


def _ytdl():
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as exc:
        raise MissingDependency(
            "yt-dlp is required for b-roll",
            hint="pip install yt-dlp, or set broll.enabled=false to skip it.",
        ) from exc
    return yt_dlp


def build_queries(cfg: dict[str, Any], keywords: list[str], title: str) -> list[str]:
    """Explicit queries win; otherwise pair the headline with its keywords."""
    if cfg["queries"]:
        return list(cfg["queries"])
    head = " ".join(title.split()[:6])
    queries = [q for q in ([head] if head else []) if q]
    for i in range(0, min(6, len(keywords)), 2):
        pair = " ".join(keywords[i:i + 2])
        if pair:
            queries.append(f"{pair} archive footage")
    return queries[:4] or ["archive footage"]


def _acceptable(info: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Return a rejection reason, or None if the candidate passes."""
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return "live"
    duration = info.get("duration") or 0
    if not duration:
        return "unknown duration"
    if duration > float(cfg["max_source_duration_s"]):
        return f"too long ({duration:.0f}s)"
    if duration < 12:
        return f"too short ({duration:.0f}s)"
    height = info.get("height") or 0
    if height and height < int(cfg["min_height"]):
        return f"only {height}p"
    if cfg.get("only_creative_commons") and info.get("license") != "Creative Commons Attribution license (reuse allowed)":
        return "not creative-commons"
    return None


def search(queries: list[str], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    yt_dlp = _ytdl()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if cfg.get("cookies_file"):
        options["cookiefile"] = str(cfg["cookies_file"])

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    with yt_dlp.YoutubeDL(options) as ydl:
        for query in queries:
            search_url = f"ytsearch{int(cfg['results_per_query'])}:{query}"
            try:
                result = ydl.extract_info(search_url, download=False)
            except Exception as exc:  # noqa: BLE001 - one bad query must not sink the step
                logs.warn(f"search failed for {query!r}: {str(exc)[:120]}")
                continue
            for entry in (result or {}).get("entries", []) or []:
                if not entry:
                    continue
                video_id = entry.get("id")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                reason = _acceptable(entry, cfg)
                if reason:
                    logs.debug(f"skip {video_id}: {reason}")
                    continue
                candidates.append({
                    "id": video_id,
                    "title": entry.get("title", ""),
                    "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "duration": float(entry.get("duration") or 0),
                    "height": entry.get("height"),
                    "uploader": entry.get("uploader", ""),
                    "license": entry.get("license", ""),
                    "query": query,
                })
    return candidates


def download(candidate: dict[str, Any], dst_dir: Path, cfg: dict[str, Any]) -> Path | None:
    yt_dlp = _ytdl()
    target = dst_dir / f"src_{candidate['id']}.%(ext)s"
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(target),
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": int(cfg["download_concurrency"]),
    }
    if cfg.get("cookies_file"):
        options["cookiefile"] = str(cfg["cookies_file"])
    if cfg.get("sponsorblock"):
        options["postprocessors"] = [{
            "key": "SponsorBlock",
            "categories": ["sponsor", "intro", "outro", "selfpromo"],
            "when": "after_filter",
        }]
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([candidate["url"]])
    except Exception as exc:  # noqa: BLE001 - skip this source, try the next
        logs.warn(f"download failed for {candidate['id']}: {str(exc)[:140]}")
        return None
    matches = sorted(dst_dir.glob(f"src_{candidate['id']}.*"))
    return matches[0] if matches else None


def _segment_window(duration: float, want: float, index: int) -> float:
    """Where to cut from inside a source, avoiding intro and end cards."""
    usable_start = min(_INTRO_SKIP_S, max(0.0, duration * 0.1))
    usable_end = max(usable_start + want, duration - _OUTRO_SKIP_S)
    span = max(0.0, usable_end - usable_start - want)
    if span <= 0:
        return usable_start
    # Deterministic spread: successive clips from one source come from
    # different thirds rather than all from the same spot.
    return usable_start + span * ((index * 0.37) % 1.0)


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["broll"]
    if not cfg["enabled"]:
        logs.info("b-roll disabled for this job")
        return StepResult(outputs=[], data={"reel": None, "clip_count": 0, "sources": []})

    out_dir = ctx.dir_for("broll")
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    article = ctx.data("article")
    queries = build_queries(cfg, article.get("keywords", []), article.get("title", ""))
    logs.info(f"searching {len(queries)} queries", first=queries[0][:60])

    candidates = search(queries, cfg)
    if not candidates:
        raise StepFailed(
            "No usable b-roll candidates found",
            hint="Widen broll.queries, raise broll.max_source_duration_s, or lower broll.min_height.",
        )
    logs.info(f"{len(candidates)} candidates passed screening")

    want_lo, want_hi = (float(x) for x in cfg["clip_len_s"])
    keep = int(cfg["keep_clips"])
    cuts: list[Path] = []
    manifest: list[dict[str, Any]] = []

    for index, candidate in enumerate(candidates):
        if len(cuts) >= keep:
            break
        source = download(candidate, clips_dir, cfg)
        if not source:
            continue
        try:
            info = probe(source)
        except StepFailed:
            continue
        want = min(want_hi, max(want_lo, info.duration_s / 4))
        if info.duration_s < want + 2:
            logs.debug(f"{candidate['id']}: shorter than a usable cut after download")
            continue
        start = _segment_window(info.duration_s, want, index)
        cut = clips_dir / f"cut_{len(cuts):02d}_{candidate['id']}.mp4"
        try:
            trim(source, cut, start, want, fps=ctx.job.fps)
        except StepFailed as exc:
            logs.warn(f"trim failed for {candidate['id']}: {exc}")
            continue
        cuts.append(cut)
        manifest.append({
            **candidate,
            "source_file": str(source),
            "cut_file": str(cut),
            "cut_start_s": round(start, 3),
            "cut_duration_s": round(want, 3),
        })
        source.unlink(missing_ok=True)   # the full download is dead weight now

    if not cuts:
        raise StepFailed("Every b-roll candidate failed to download or trim")

    reel = out_dir / "broll.mp4"
    concat(cuts, reel, fps=ctx.job.fps, workdir=out_dir)
    manifest_path = out_dir / "broll.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    duration = probe(reel).duration_s
    logs.info(f"assembled {len(cuts)} clips into {duration:.1f}s of b-roll")

    return StepResult(
        outputs=[reel, manifest_path],
        data={
            "reel": str(reel),
            "manifest": str(manifest_path),
            "clip_count": len(cuts),
            "duration_s": round(duration, 3),
            "sources": [c["url"] for c in manifest],
        },
    )
