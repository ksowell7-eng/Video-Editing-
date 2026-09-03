"""`doctor` — check everything a run needs before it starts costing money.

Ordered so the cheap local checks report first and the network ones last, and
written so a failure names the fix rather than the symptom.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path

from .config import Job
from .errors import MissingDependency
from . import logs, shell


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True
    hint: str = ""


def _python_package(module: str, package: str, *, version_check=None) -> Check:
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return Check(package, False, "not installed", hint=f"pip install {package}")
    version = getattr(mod, "__version__", None)
    if version is None:
        try:
            from importlib.metadata import version as dist_version  # noqa: PLC0415

            version = dist_version(package.split(">")[0].split("<")[0].split("=")[0])
        except Exception:  # noqa: BLE001 - a missing version is cosmetic
            version = "installed"
    if version_check:
        problem = version_check(mod)
        if problem:
            return Check(package, False, f"{version} — {problem}", hint=f"pip install '{package}'")
    return Check(package, True, str(version))


def _opencv_problem(cv2) -> str | None:
    if not hasattr(cv2, "CascadeClassifier"):
        return "no CascadeClassifier (OpenCV 5 removed Haar cascades; pin <5)"
    if not Path(cv2.data.haarcascades, "haarcascade_frontalface_default.xml").exists():
        return "haarcascade data files missing"
    return None


def _binary(name: str, resolver) -> Check:
    try:
        path = resolver()
    except MissingDependency as exc:
        return Check(name, False, str(exc), hint=exc.hint or "")
    return Check(name, True, path)


def run_checks(job: Job | None = None) -> list[Check]:
    checks: list[Check] = [
        _binary("ffmpeg", shell.ffmpeg),
        _binary("ffprobe", shell.ffprobe),
        _binary("npx (Node >= 22)", shell.npx),
        _python_package("numpy", "numpy"),
        _python_package("cv2", "opencv-python-headless>=4.8,<5", version_check=_opencv_problem),
        _python_package("playwright", "playwright"),
        _python_package("yt_dlp", "yt-dlp"),
        _python_package("requests", "requests"),
    ]

    browser = os.environ.get("CHROME_EXECUTABLE")
    if browser and Path(browser).exists():
        checks.append(Check("chrome", True, f"{browser} (CHROME_EXECUTABLE)"))
    else:
        try:
            checks.append(Check("chrome", True, shell.chrome()))
        except MissingDependency as exc:
            checks.append(Check(
                "chrome", False, str(exc), fatal=False,
                hint="playwright downloads its own; this only matters if that copy is missing.",
            ))

    if job is None:
        return checks

    # Credentials are only required for the steps this job actually uses.
    uses_elevenlabs = job["voice"]["provider"] == "elevenlabs"
    needs_avatar = job["avatar"]["enabled"] and any(p.bed == "avatar" for p in job.phases)

    if not uses_elevenlabs:
        checks.append(Check(
            "voice provider", True,
            f"{job['voice']['provider']} (no API key needed)", fatal=False,
        ))
    if uses_elevenlabs:
        checks.append(Check(
            "ELEVENLABS_API_KEY", bool(os.environ.get("ELEVENLABS_API_KEY")),
            "set" if os.environ.get("ELEVENLABS_API_KEY") else "missing",
            hint="export ELEVENLABS_API_KEY=...",
        ))
        for role in ("narrator", "coach"):
            if any(p.voice == role for p in job.phases):
                configured = bool(job["voice"].get(f"{role}_voice_id"))
                checks.append(Check(
                    f"voice.{role}_voice_id", configured,
                    "set" if configured else "missing",
                    hint=f"Paste the ElevenLabs voice id into the job file as voice.{role}_voice_id.",
                ))
    if needs_avatar:
        checks.append(Check(
            "KIE_API_KEY", bool(os.environ.get("KIE_API_KEY")),
            "set" if os.environ.get("KIE_API_KEY") else "missing",
            hint="export KIE_API_KEY=...",
        ))

    if job["identity"]["enabled"]:
        endpoint = job["identity"]["endpoint"]
        reachable = False
        detail = f"{endpoint} unreachable"
        try:
            import requests  # noqa: PLC0415

            response = requests.get(f"{endpoint.rstrip('/')}/models", timeout=4)
            reachable = response.status_code < 500
            names = [m.get("id", "") for m in (response.json().get("data") or [])]
            detail = f"{endpoint} ({len(names)} models)"
            wanted = job["identity"]["model"]
            if names and not any(wanted in n for n in names):
                detail += f" — '{wanted}' not among them"
        except Exception:  # noqa: BLE001 - unreachable is the answer, not an error
            pass
        checks.append(Check(
            "LM Studio (identity)", reachable, detail, fatal=False,
            hint="Start LM Studio's local server, or set identity.enabled=false.",
        ))

    clip = job.resolve(job["input"]["highlight_clip"])
    checks.append(Check(
        "highlight clip", bool(clip and clip.exists()),
        str(clip) if clip else "unset",
    ))

    budget = job["budget"]
    checks.append(Check(
        "budget", True,
        f"${budget['max_usd_per_run']:.2f}/run, ${budget['max_usd_total']:.2f} lifetime "
        f"→ {job.resolve(budget['file'])}",
        fatal=False,
    ))
    return checks


def report(checks: list[Check]) -> bool:
    """Print the checks; return True when nothing fatal failed."""
    width = max(len(c.name) for c in checks) + 2
    healthy = True
    for check in checks:
        if check.ok:
            logs.ok(f"{check.name.ljust(width)} {check.detail}")
        elif check.fatal:
            healthy = False
            logs.error(f"{check.name.ljust(width)} {check.detail}")
            if check.hint:
                logs.info(f"{' ' * width}   → {check.hint}")
        else:
            logs.warn(f"{check.name.ljust(width)} {check.detail}")
            if check.hint:
                logs.info(f"{' ' * width}   → {check.hint}")
    return healthy
