"""Apply an edit list to a video, non-destructively and incrementally.

The loop this exists for is: you say what to change, the change becomes an
entry in `ops`, the video is rebuilt. That happens many times, so two
properties matter more than raw speed.

**Nothing is destructive.** Every render starts from the original source and
replays the whole list. Removing an op genuinely undoes it; there is no inverse
edit to get wrong, and the source file is never touched.

**Unchanged work is not repeated.** Each intermediate is stored under a hash of
the source plus every op up to that point, so replaying a list whose first six
ops are unchanged reuses six files and re-encodes only what actually moved.
Appending a change to the end of a long list costs one pass, not the whole list.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, StepFailed
from ..media.probe import probe
from .ops import OPS, OpContext, describe, validate
from .timecode import format_tc
from .. import logs

CACHE_DIRNAME = ".editcache"


@dataclass
class EditResult:
    output: Path
    duration_s: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    reused: int = 0
    rendered: int = 0


def _file_fingerprint(path: Path) -> str:
    """Identify a source without hashing gigabytes of it.

    Path, size and mtime are enough: a re-encode or a replacement changes at
    least one of them, and the cache only has to notice that the source is not
    the same file it saw last time.
    """
    stat = path.stat()
    material = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def load_edit_list(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Edit list not found: {path}")
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise ConfigError(f"{path.name} must be an object with 'source' and 'ops'")
    blob.setdefault("ops", [])
    if not isinstance(blob["ops"], list):
        raise ConfigError("'ops' must be a list")
    for index, spec in enumerate(blob["ops"]):
        validate(spec, index)
    return blob


def next_version(output: Path) -> Path:
    """out/clip.mp4 → out/clip.v1.mp4, then v2, v3 …

    Every round of changes lands beside the last one instead of overwriting it,
    so going back to the version from two rounds ago is picking a file.
    """
    stem = output.stem
    if "." in stem and stem.rsplit(".", 1)[-1].startswith("v") and stem.rsplit(".", 1)[-1][1:].isdigit():
        stem = stem.rsplit(".", 1)[0]
    version = 1
    while (candidate := output.with_name(f"{stem}.v{version}{output.suffix}")).exists():
        version += 1
    return candidate


def apply_edits(
    source: Path,
    ops: list[dict[str, Any]],
    output: Path,
    *,
    workdir: Path,
    fps: int,
    job_root: Path,
    reframe_cfg: dict[str, Any],
    force: bool = False,
) -> EditResult:
    if not source.exists():
        raise ConfigError(f"Source video not found: {source}")

    cache = workdir / CACHE_DIRNAME
    cache.mkdir(parents=True, exist_ok=True)
    scratch = workdir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    ctx = OpContext(workdir=scratch, fps=fps, job_root=job_root, reframe_cfg=reframe_cfg)
    chain = _file_fingerprint(source)
    current = source
    original = probe(source)
    steps: list[dict[str, Any]] = []
    reused = rendered = 0

    logs.info(
        f"source: {source.name}",
        duration=format_tc(original.duration_s, millis=True),
        size=f"{original.width}x{original.height}",
        audio="yes" if original.has_audio else "no",
    )

    for index, spec in enumerate(ops):
        name = validate(spec, index)
        chain = hashlib.sha256(
            (chain + json.dumps(spec, sort_keys=True, default=str)).encode()
        ).hexdigest()[:16]
        cached = cache / f"{index:02d}_{name}_{chain}.mp4"
        summary = describe(spec)

        if cached.exists() and cached.stat().st_size > 0 and not force:
            logs.info(f"[{index + 1}/{len(ops)}] {summary}  (cached)")
            current = cached
            reused += 1
        else:
            started = time.time()
            logs.info(f"[{index + 1}/{len(ops)}] {summary}")
            try:
                produced = OPS[name].fn(current, cached, spec, ctx)
            except (ConfigError, StepFailed) as exc:
                raise type(exc)(
                    f"op {index + 1} ({summary}) failed: {exc}",
                    hint=getattr(exc, "hint", None),
                ) from exc
            if not produced.exists() or produced.stat().st_size == 0:
                raise StepFailed(f"op {index + 1} ({name}) produced no output")
            current = produced
            rendered += 1
            logs.debug(f"    took {time.time() - started:.1f}s")

        info = probe(current)
        steps.append({
            "index": index,
            "op": name,
            "summary": summary,
            "duration_s": round(info.duration_s, 3),
            "size": [info.width, info.height],
            "has_audio": info.has_audio,
            "file": str(current),
        })

    output.parent.mkdir(parents=True, exist_ok=True)
    if current == source:
        # An empty op list still produces a deliverable: a copy, so the caller
        # always has one path to hand back.
        shutil.copyfile(source, output)
    else:
        shutil.copyfile(current, output)

    final = probe(output)
    delta = final.duration_s - original.duration_s
    logs.ok(
        f"{output.name}",
        duration=format_tc(final.duration_s, millis=True),
        change=f"{delta:+.2f}s",
        size=f"{final.width}x{final.height}",
        passes=f"{rendered} rendered, {reused} cached",
    )
    return EditResult(
        output=output, duration_s=final.duration_s, steps=steps,
        reused=reused, rendered=rendered,
    )


def prune_cache(workdir: Path, keep_bytes: int = 4_000_000_000) -> int:
    """Drop the oldest intermediates once the cache passes a size budget."""
    cache = workdir / CACHE_DIRNAME
    if not cache.exists():
        return 0
    files = sorted(cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    removed = 0
    for path in files:
        total += path.stat().st_size
        if total > keep_bytes:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
