"""Local asset vendoring for the composition.

The HyperFrames templates load GSAP from a CDN. That is fine interactively and
a liability in a pipeline: the renderer fetches the script inside headless
Chrome, and on any machine behind a proxy or offline the fetch 403s, `gsap` is
null, no timeline ever registers, and the render *still succeeds* — silently,
with every animated overlay stuck at its resting opacity of 0. The result is a
video with no captions and no markers and a zero exit code.

So GSAP is vendored into the project directory and referenced relatively. It is
fetched once into a user-level cache (npm first, since that is already
configured on any machine that can run `npx hyperframes`, then a plain HTTPS
download) and copied from there on every later run.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from ..errors import MissingDependency
from ..shell import run
from .. import logs

GSAP_VERSION = "3.14.2"
GSAP_FILENAME = "gsap.min.js"
_MIN_PLAUSIBLE_BYTES = 40_000
_CDN_URLS = (
    f"https://cdn.jsdelivr.net/npm/gsap@{GSAP_VERSION}/dist/gsap.min.js",
    f"https://unpkg.com/gsap@{GSAP_VERSION}/dist/gsap.min.js",
)


def cache_dir() -> Path:
    override = os.environ.get("VERTICAL_SHORTS_CACHE")
    base = Path(override) if override else Path.home() / ".cache" / "vertical-shorts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch_via_npm(dst: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            proc = run(
                ["npm", "pack", f"gsap@{GSAP_VERSION}", "--silent"],
                cwd=tmpdir, timeout=300, check=False,
            )
        except Exception as exc:  # noqa: BLE001 - npm absence is not fatal here
            logs.debug(f"npm pack for gsap unavailable: {exc}")
            return False
        if proc.returncode != 0:
            logs.debug(f"npm pack for gsap failed: {(proc.stderr or '').strip()[:200]}")
            return False
        tarballs = list(tmpdir.glob("*.tgz"))
        if not tarballs:
            return False
        with tarfile.open(tarballs[0]) as tar:
            member = next(
                (m for m in tar.getmembers() if m.name.endswith("dist/gsap.min.js")), None,
            )
            if member is None:
                return False
            extracted = tar.extractfile(member)
            if extracted is None:
                return False
            dst.write_bytes(extracted.read())
    return dst.exists() and dst.stat().st_size > _MIN_PLAUSIBLE_BYTES


def _fetch_via_https(dst: Path) -> bool:
    for url in _CDN_URLS:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed URLs
                payload = response.read()
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            logs.debug(f"gsap download failed from {url}: {exc}")
            continue
        if len(payload) > _MIN_PLAUSIBLE_BYTES:
            dst.write_bytes(payload)
            return True
    return False


def ensure_gsap(project_dir: Path) -> str:
    """Put gsap.min.js in the project and return the src to reference it by."""
    destination = project_dir / GSAP_FILENAME
    if destination.exists() and destination.stat().st_size > _MIN_PLAUSIBLE_BYTES:
        return GSAP_FILENAME

    cached = cache_dir() / f"gsap-{GSAP_VERSION}.min.js"
    if not (cached.exists() and cached.stat().st_size > _MIN_PLAUSIBLE_BYTES):
        logs.info(f"fetching GSAP {GSAP_VERSION} into {cached.parent}")
        if not _fetch_via_npm(cached) and not _fetch_via_https(cached):
            cached.unlink(missing_ok=True)
            raise MissingDependency(
                f"Could not obtain GSAP {GSAP_VERSION}",
                hint=(
                    f"Download {_CDN_URLS[0]} manually and save it as {cached}. "
                    "The renderer cannot animate captions or markers without it."
                ),
            )

    project_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, destination)
    return GSAP_FILENAME
