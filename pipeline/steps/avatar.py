"""Step 6 — generate the coach avatar through KIE.ai (bytedance/seedance-2, v2v).

The only step that can spend real money in one call, so it is also the most
defensive one:

  * the segment length is known before the call, so the cost estimate is a
    multiplication rather than a guess, and the budget reserves it up front;
  * the reservation is released if the request never produced anything, and
    settled at the real duration if it did — a crash between the two leaves a
    pending charge on the ledger rather than free budget;
  * a finished generation is cached by a hash of everything that affects it,
    so re-running the pipeline after a caption tweak re-uses the video instead
    of buying another one.

v2v needs a driver clip. If the job does not supply one, a slow push-in is
built from the reference still — Seedance has nothing to animate from a frozen
frame, and the result reads as a photograph rather than a person.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..errors import StepFailed
from ..http import api_key, download, request
from ..media.edit import still_to_clip
from ..media.probe import probe
from .base import Context, StepResult
from .. import logs


def cache_key(cfg: dict[str, Any], seconds: float, reference: Path | None) -> str:
    """Everything that changes the generated video, and nothing that doesn't."""
    material = {
        "model": cfg["model"],
        "mode": cfg["mode"],
        "prompt": cfg["prompt"],
        "negative_prompt": cfg["negative_prompt"],
        "resolution": cfg["resolution"],
        "aspect_ratio": cfg["aspect_ratio"],
        "seed": cfg["seed"],
        "seconds": round(float(seconds), 2),
        "reference": hashlib.sha256(reference.read_bytes()).hexdigest()[:16] if reference else None,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _submit(cfg: dict[str, Any], key: str, payload: dict[str, Any]) -> str:
    response = request(
        "POST",
        f"{cfg['endpoint']}/createTask",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json_body={"model": cfg["model"], "input": payload},
        timeout=120,
    )
    data = response.get("data") or {}
    task_id = data.get("taskId") or data.get("task_id") or response.get("taskId")
    if not task_id:
        raise StepFailed(f"KIE.ai did not return a task id: {json.dumps(response)[:400]}")
    return str(task_id)


def _poll(cfg: dict[str, Any], key: str, task_id: str) -> str:
    """Block until the task resolves; return the result video URL."""
    deadline = time.time() + int(cfg["timeout_s"])
    interval = max(2, int(cfg["poll_interval_s"]))
    while time.time() < deadline:
        response = request(
            "GET",
            f"{cfg['endpoint']}/recordInfo?taskId={task_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
        )
        data = response.get("data") or {}
        state = str(data.get("state") or data.get("status") or "").lower()

        if state in ("success", "succeeded", "completed"):
            result = data.get("resultJson") or data.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = {}
            urls = result.get("resultUrls") or result.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                raise StepFailed(f"KIE.ai task {task_id} succeeded with no result URL")
            return str(urls[0])

        if state in ("fail", "failed", "error"):
            reason = data.get("failMsg") or data.get("message") or json.dumps(data)[:300]
            raise StepFailed(f"KIE.ai task {task_id} failed: {reason}")

        logs.debug(f"avatar task {task_id}: {state or 'pending'}")
        time.sleep(interval)

    raise StepFailed(
        f"KIE.ai task {task_id} did not finish within {cfg['timeout_s']}s",
        hint="Raise avatar.timeout_s, or check the task in the KIE.ai dashboard before re-running.",
    )


def generate(ctx: Context, cfg: dict[str, Any], seconds: float, driver: Path, out_dir: Path) -> Path:
    reference = ctx.job.resolve(ctx.job["input"].get("coach_reference_image"))
    key_hash = cache_key(cfg, seconds, reference)
    dst = out_dir / f"coach_{key_hash}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        logs.info(f"avatar cache hit ({key_hash}); no new generation billed")
        return dst

    estimate = seconds * float(cfg["cost_per_second_usd"])
    reservation = ctx.budget.reserve(
        f"kie:{cfg['model']}:{seconds:.1f}s",
        estimate,
        provider="kie.ai", model=cfg["model"], seconds=round(seconds, 2), cache_key=key_hash,
    )

    key = api_key("KIE_API_KEY", "KIE.ai")
    payload: dict[str, Any] = {
        "prompt": cfg["prompt"],
        "negative_prompt": cfg["negative_prompt"],
        "duration": round(seconds, 1),
        "resolution": cfg["resolution"],
        "aspect_ratio": cfg["aspect_ratio"],
        "seed": int(cfg["seed"]),
        "video_url": driver.resolve().as_uri(),
    }
    if reference:
        payload["image_url"] = reference.resolve().as_uri()

    try:
        task_id = _submit(cfg, key, payload)
        logs.info("avatar task submitted", task=task_id, seconds=f"{seconds:.1f}")
        url = _poll(cfg, key, task_id)
        download(url, dst, timeout=900)
    except Exception:
        reservation.release("generation failed")
        raise

    actual = probe(dst).duration_s
    reservation.settle(actual * float(cfg["cost_per_second_usd"]), delivered_s=round(actual, 2))
    return dst


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["avatar"]
    out_dir = ctx.dir_for("avatar")
    out_w, out_h = ctx.job.size
    fps = ctx.job.fps

    avatar_phases = [p for p in ctx.job.phases if p.bed == "avatar"]
    if not cfg["enabled"] or not avatar_phases:
        logs.info("no phase uses the avatar bed; skipping generation")
        return StepResult(outputs=[], data={"clip": None, "seconds": 0.0})

    durations = ctx.data("voice").get("durations", {})
    seconds = sum(float(durations.get(p.id, p.target_s)) for p in avatar_phases)
    # A little headroom: a generation that lands a few frames short leaves a
    # black gap at the end of the phase.
    seconds = round(seconds + 0.5, 1)

    driver_cfg = cfg.get("driver_clip")
    if driver_cfg:
        driver = ctx.job.resolve(driver_cfg)
        if not driver or not driver.exists():
            raise StepFailed(f"avatar.driver_clip not found: {driver_cfg}")
    else:
        reference = ctx.job.resolve(ctx.job["input"].get("coach_reference_image"))
        if not reference:
            raise StepFailed(
                "The avatar needs either avatar.driver_clip or input.coach_reference_image",
                hint="v2v has to be driven by motion; a reference still is turned into a push-in.",
            )
        driver = out_dir / "driver.mp4"
        if not driver.exists():
            still_to_clip(reference, driver, seconds, width=out_w, height=out_h, fps=fps)

    clip = generate(ctx, cfg, seconds, driver, out_dir)
    info = probe(clip)
    logs.info(
        f"avatar ready: {info.duration_s:.1f}s at {info.width}x{info.height}",
        spend=f"${ctx.budget.spent()[0]:.2f}",
    )

    manifest = out_dir / "avatar.json"
    manifest.write_text(json.dumps({
        "clip": str(clip),
        "driver": str(driver),
        "seconds_requested": seconds,
        "seconds_delivered": round(info.duration_s, 3),
        "phases": [p.id for p in avatar_phases],
        "budget": ctx.budget.summary(),
    }, indent=2), encoding="utf-8")

    return StepResult(
        outputs=[clip, manifest],
        data={
            "clip": str(clip),
            "seconds": round(info.duration_s, 3),
            "phases": [p.id for p in avatar_phases],
        },
    )
