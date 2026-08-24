"""Step 7 — identity and subject checks against a local vision model.

Two different questions, one mechanism (sampled frames + a local VLM on an
OpenAI-compatible endpoint, LM Studio by default):

  avatar  Does the generated coach still look like the reference person?
          Video models drift across a long generation — the face at 20s is
          often not the face at 2s — and that drift is exactly what makes an
          otherwise good take unusable.

  broll   Does this footage actually show what the search claimed?
          YouTube search returns reaction videos, highlight compilations with
          burned-in logos, and unrelated matches with the right words in the
          title.

The model is asked for strict JSON and its answer is parsed defensively: a VLM
that returns prose instead of JSON is a failed check, not a crash. Because it
runs locally it costs nothing, so it is safe to run on every take.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from ..errors import QualityGateFailed, StepFailed
from ..http import request
from ..media.edit import extract_frames
from ..media.probe import probe
from .base import Context, StepResult
from .. import logs

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

_AVATAR_PROMPT = (
    "You are checking whether two images show the same person. "
    "Answer with strict JSON only: "
    '{"same_person": true|false, "confidence": 0.0-1.0, "reason": "<8 words>"}. '
    "Judge identity only — face structure, hair, skin tone, age. Ignore lighting, "
    "camera angle, expression, motion blur and clothing."
)

_BROLL_PROMPT = (
    "You are checking whether a video frame is usable stock footage for a short "
    "documentary about: {subject}. Answer with strict JSON only: "
    '{"usable": true|false, "confidence": 0.0-1.0, "reason": "<8 words>"}. '
    "Mark it unusable if it is a talking-head reaction, a slide of text, a "
    "screen recording, a watermarked compilation, or unrelated to the subject."
)


def _encode(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _ask(endpoint: str, model: str, prompt: str, images: list[Path], timeout: int) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": _encode(image)}})
    response = request(
        "POST",
        f"{endpoint.rstrip('/')}/chat/completions",
        headers={"Content-Type": "application/json"},
        json_body={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 200,
        },
        timeout=timeout,
        attempts=2,
    )
    try:
        text = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StepFailed(f"Unexpected response from {endpoint}: {json.dumps(response)[:300]}") from exc

    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {"parsed": False, "raw": (text or "")[:200]}
    try:
        return {"parsed": True, **json.loads(match.group(0))}
    except json.JSONDecodeError:
        return {"parsed": False, "raw": match.group(0)[:200]}


def sample_times(duration: float, count: int) -> list[float]:
    """Evenly spaced samples, avoiding the first and last half second."""
    if duration <= 0 or count <= 0:
        return [0.0]
    lo, hi = min(0.5, duration / 4), max(0.5, duration - 0.5)
    if hi <= lo:
        return [duration / 2]
    step = (hi - lo) / max(1, count - 1) if count > 1 else 0
    return [round(lo + step * i, 3) for i in range(count)]


def check_clip(
    clip: Path,
    reference: Path | None,
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    kind: str,
    subject: str = "",
) -> dict[str, Any]:
    info = probe(clip)
    times = sample_times(info.duration_s, int(cfg["sample_frames"]))
    frames = extract_frames(clip, out_dir / f"{kind}_frames", times, max_px=int(cfg["max_image_px"]))

    verdicts: list[dict[str, Any]] = []
    for time_s, frame in zip(times, frames):
        if kind == "avatar":
            if reference is None:
                raise StepFailed("The avatar identity check needs input.coach_reference_image")
            answer = _ask(cfg["endpoint"], cfg["model"], _AVATAR_PROMPT, [reference, frame], int(cfg["timeout_s"]))
            passed = bool(answer.get("same_person")) and answer.get("parsed", False)
        else:
            answer = _ask(
                cfg["endpoint"], cfg["model"], _BROLL_PROMPT.format(subject=subject or "the article's topic"),
                [frame], int(cfg["timeout_s"]),
            )
            passed = bool(answer.get("usable")) and answer.get("parsed", False)
        verdicts.append({
            "t": time_s,
            "frame": str(frame),
            "pass": passed,
            "confidence": answer.get("confidence"),
            "reason": answer.get("reason") or answer.get("raw", ""),
        })

    passes = sum(1 for v in verdicts if v["pass"])
    ratio = passes / len(verdicts) if verdicts else 0.0
    return {
        "kind": kind,
        "clip": str(clip),
        "frames_checked": len(verdicts),
        "passes": passes,
        "pass_ratio": round(ratio, 3),
        "threshold": float(cfg["min_pass_ratio"]),
        "ok": ratio >= float(cfg["min_pass_ratio"]),
        "verdicts": verdicts,
    }


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["identity"]
    out_dir = ctx.dir_for("identity")
    if not cfg["enabled"]:
        logs.info("identity checks disabled")
        return StepResult(outputs=[], data={"checked": [], "skipped": True})

    reference = ctx.job.resolve(ctx.job["input"].get("coach_reference_image"))
    subject = ctx.data("article").get("title", "")

    targets: list[tuple[str, Path]] = []
    if "avatar" in cfg["targets"]:
        clip = ctx.data("avatar").get("clip")
        if clip:
            targets.append(("avatar", Path(clip)))
    if "broll" in cfg["targets"]:
        reel = ctx.data("reframe").get("broll") or ctx.data("broll").get("reel")
        if reel:
            targets.append(("broll", Path(reel)))

    if not targets:
        logs.info("nothing to check (no avatar or b-roll in this run)")
        return StepResult(outputs=[], data={"checked": []})

    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    for kind, clip in targets:
        try:
            report = check_clip(clip, reference, cfg, out_dir, kind=kind, subject=subject)
        except StepFailed as exc:
            # A local model that is not running should not sink a run that has
            # already paid for generation; it degrades to a loud warning.
            logs.warn(f"{kind} identity check unavailable: {str(exc)[:160]}")
            reports.append({"kind": kind, "clip": str(clip), "ok": None, "error": str(exc)[:300]})
            continue
        reports.append(report)
        level = logs.ok if report["ok"] else logs.warn
        level(f"{kind}: {report['passes']}/{report['frames_checked']} frames passed "
              f"({report['pass_ratio']:.0%}, need {report['threshold']:.0%})")
        if not report["ok"]:
            failures.append(kind)

    report_path = out_dir / "identity.json"
    report_path.write_text(json.dumps(reports, indent=2), encoding="utf-8")

    if "avatar" in failures:
        raise QualityGateFailed(
            "The generated coach does not match the reference across sampled frames",
            hint=(
                "Re-run the avatar step with a different avatar.seed (--force --only avatar), "
                "lower identity.min_pass_ratio, or supply a cleaner reference image. "
                f"Frame-by-frame verdicts: {report_path}"
            ),
        )
    if failures:
        logs.warn(f"identity check failed for: {', '.join(failures)} (not fatal)")

    return StepResult(
        outputs=[report_path],
        data={"checked": [r["kind"] for r in reports], "report": str(report_path),
              "failures": failures},
    )
