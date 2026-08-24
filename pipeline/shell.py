"""Binary discovery and subprocess execution.

Discovery order for every tool is the same: an explicit environment override,
then PATH, then a small list of well-known locations. ffmpeg additionally has
to prove it can encode H.264/AAC — bundled stubs (Playwright ships one) resolve
on PATH-adjacent paths but carry no usable encoders, and finding that out at
render time instead of preflight costs a whole run.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import MissingDependency, StepFailed
from . import logs

_KNOWN_CHROME = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

REQUIRED_ENCODERS = ("libx264", "aac")


def _resolve(name: str, env_var: str, extra: Sequence[str] = ()) -> str | None:
    override = os.environ.get(env_var)
    if override:
        return override if Path(override).exists() else None
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra:
        if Path(candidate).exists():
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def ffmpeg_encoders(binary: str) -> frozenset[str]:
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        # Encoder rows look like: " V....D libx264   H.264 / AVC ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" and parts[1][:1].isalnum():
            names.add(parts[1])
    return frozenset(names)


@functools.lru_cache(maxsize=None)
def ffmpeg() -> str:
    binary = _resolve("ffmpeg", "FFMPEG_BIN")
    if not binary:
        raise MissingDependency(
            "ffmpeg not found",
            hint="Install it (apt install ffmpeg / brew install ffmpeg) or set FFMPEG_BIN.",
        )
    missing = [e for e in REQUIRED_ENCODERS if e not in ffmpeg_encoders(binary)]
    if missing:
        raise MissingDependency(
            f"ffmpeg at {binary} cannot encode: {', '.join(missing)}",
            hint=(
                "This is usually a stripped bundle (e.g. Playwright's ffmpeg-linux). "
                "Point FFMPEG_BIN at a full build."
            ),
        )
    return binary


@functools.lru_cache(maxsize=None)
def ffprobe() -> str:
    binary = _resolve("ffprobe", "FFPROBE_BIN")
    if not binary:
        raise MissingDependency("ffprobe not found", hint="It ships with ffmpeg; set FFPROBE_BIN to override.")
    return binary


@functools.lru_cache(maxsize=None)
def chrome() -> str:
    binary = _resolve("google-chrome", "CHROME_EXECUTABLE", _KNOWN_CHROME)
    if not binary:
        raise MissingDependency(
            "No Chrome/Chromium found for article scraping",
            hint="Run `playwright install chromium`, or set CHROME_EXECUTABLE to an existing binary.",
        )
    return binary


@functools.lru_cache(maxsize=None)
def npx() -> str:
    binary = _resolve("npx", "NPX_BIN")
    if not binary:
        raise MissingDependency("npx not found", hint="Install Node.js >= 22; HyperFrames is invoked through npx.")
    return binary


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 1800,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, logging it at debug level and failing loudly with real stderr."""
    printable = " ".join(str(a) for a in argv)
    logs.debug(f"exec {printable[:400]}")
    merged = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd) if cwd else None,
            capture_output=capture,
            text=True,
            timeout=timeout,
            env=merged,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailed(f"Timed out after {timeout}s: {printable[:200]}") from exc
    except FileNotFoundError as exc:
        raise MissingDependency(f"Executable not found: {argv[0]}") from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise StepFailed(
            f"Command failed ({proc.returncode}): {printable[:200]}\n" + "\n".join(tail)
        )
    return proc
