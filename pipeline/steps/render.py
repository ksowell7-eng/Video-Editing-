"""Step 10 — lint, render, and verify the output.

The verification matters as much as the render. HyperFrames will happily
produce a video when a timeline script failed to load, and the result is a
clean-looking file with no captions and no markers on it — a silent failure
that costs a full re-run to notice by eye. So the render log is inspected for
correctness warnings, and the finished file is probed against what the
composition said it should be.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..errors import QualityGateFailed, StepFailed
from ..media.probe import probe
from ..shell import npx, run
from .base import Context, StepResult
from .. import logs

# Warnings that mean the picture is wrong, not merely suboptimal.
_FATAL_WARNINGS = {
    "sub_timeline_script_failure",
    "missing_local_asset",
    "audio_src_not_found",
}
_WARNING_CODES = re.compile(r'"warningCodes":\s*\[([^\]]*)\]')


def lint(project: Path, version: str) -> dict[str, Any]:
    proc = run([
        npx(), "--yes", f"hyperframes@{version}", "lint", str(project), "--json",
    ], timeout=900, check=False)
    blob = None
    for chunk in re.findall(r"\{.*\}", proc.stdout or "", re.DOTALL):
        try:
            blob = json.loads(chunk)
            break
        except json.JSONDecodeError:
            continue
    if blob is None:
        raise StepFailed(
            "hyperframes lint produced no JSON\n" + (proc.stderr or proc.stdout or "")[-600:]
        )
    return blob


def _render_warnings(output: str) -> set[str]:
    found: set[str] = set()
    for match in _WARNING_CODES.finditer(output or ""):
        for code in match.group(1).split(","):
            code = code.strip().strip('"')
            if code:
                found.add(code)
    for code in _FATAL_WARNINGS:
        if f"[{code}]" in (output or ""):
            found.add(code)
    return found


def run_step(ctx: Context) -> StepResult:
    cfg = ctx.job["render"]
    version = cfg["hyperframes_version"]
    out_dir = ctx.dir_for("render")
    compose = ctx.data("compose")
    project = Path(compose.get("project", ""))
    if not project.exists():
        raise StepFailed("No composition to render; run the compose step first")

    if cfg["lint"]:
        report = lint(project, version)
        errors = int(report.get("errorCount", 0))
        warnings = int(report.get("warningCount", 0))
        for finding in report.get("findings", []):
            level = logs.error if finding.get("severity") == "error" else logs.warn
            level(f"lint {finding.get('code')}: {finding.get('message', '')[:160]}")
        if errors:
            raise QualityGateFailed(
                f"hyperframes lint found {errors} error(s) in the composition",
                hint=f"Inspect {project / 'index.html'}; the findings above name the elements.",
            )
        logs.ok(f"lint clean ({warnings} warning(s))")

    rendered = out_dir / "render.mp4"
    log_path = out_dir / "render.log"
    proc = run([
        npx(), "--yes", f"hyperframes@{version}", "render", str(project),
        "-o", str(rendered),
        "-f", str(ctx.job.fps),
        "-q", ctx.job["output"]["quality"],
        "-w", str(int(cfg["workers"])),
        "--video-frame-format", cfg["video_frame_format"],
    ], timeout=int(cfg["timeout_s"]), check=False)

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    log_path.write_text(combined, encoding="utf-8")

    if proc.returncode != 0:
        raise StepFailed(
            f"hyperframes render exited {proc.returncode}; see {log_path}\n"
            + "\n".join(combined.strip().splitlines()[-12:])
        )
    if not rendered.exists() or rendered.stat().st_size == 0:
        raise StepFailed(f"render reported success but produced nothing at {rendered}")

    fatal = _render_warnings(combined) & _FATAL_WARNINGS
    if fatal:
        raise QualityGateFailed(
            f"The render completed but reported: {', '.join(sorted(fatal))}",
            hint=(
                "This is the silent-failure case — the file exists but is missing layers. "
                "'sub_timeline_script_failure' usually means gsap.min.js did not load; "
                f"check {log_path}."
            ),
        )

    info = probe(rendered)
    expected_w, expected_h = ctx.job.size
    expected_duration = float(compose.get("duration_s") or info.duration_s)
    problems = []
    if (info.width, info.height) != (expected_w, expected_h):
        problems.append(f"size {info.width}x{info.height}, expected {expected_w}x{expected_h}")
    if abs(info.duration_s - expected_duration) > 1.0:
        problems.append(f"duration {info.duration_s:.1f}s, composition said {expected_duration:.1f}s")
    if not info.has_audio:
        problems.append("no audio track")
    if problems:
        raise QualityGateFailed(
            "The rendered file does not match the composition: " + "; ".join(problems),
            hint=f"Keep {rendered} for inspection; the render log is at {log_path}.",
        )

    final = ctx.job.resolve(ctx.job["output"]["file"])
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rendered, final)

    logs.ok(
        f"rendered {info.duration_s:.1f}s at {info.width}x{info.height}",
        size=f"{rendered.stat().st_size / 1e6:.1f}MB", out=str(final),
    )
    return StepResult(
        outputs=[rendered, final],
        data={
            "render": str(rendered),
            "output": str(final),
            "duration_s": round(info.duration_s, 3),
            "size_bytes": rendered.stat().st_size,
            "log": str(log_path),
        },
    )
