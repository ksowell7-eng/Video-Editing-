"""HTTP with retries, for the three external services this pipeline calls."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from .errors import MissingDependency, StepFailed
from . import logs

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def api_key(name: str, service: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingDependency(
            f"{service} needs {name} in the environment",
            hint=f"export {name}=... before running. It is never read from the job file.",
        )
    return value


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: int = 120,
    attempts: int = 4,
    expect_json: bool = True,
) -> Any:
    """Exponential backoff on transient failures; honours Retry-After on 429."""
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, data=data, timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == attempts:
                break
            delay = min(30, 2 ** attempt)
            logs.warn(f"{method} {url.split('?')[0]} failed, retrying in {delay}s", attempt=attempt)
            time.sleep(delay)
            continue

        if response.status_code in _RETRY_STATUS and attempt < attempts:
            wait = response.headers.get("Retry-After")
            delay = float(wait) if wait and wait.isdigit() else min(30, 2 ** attempt)
            logs.warn(f"{url.split('?')[0]} returned {response.status_code}; retrying in {delay:.0f}s")
            time.sleep(delay)
            continue

        if response.status_code >= 400:
            raise StepFailed(
                f"{method} {url.split('?')[0]} → {response.status_code}: {response.text[:500]}"
            )
        if not expect_json:
            return response.content
        try:
            return response.json()
        except ValueError as exc:
            raise StepFailed(f"{url} returned non-JSON: {response.text[:300]}") from exc

    raise StepFailed(f"{method} {url.split('?')[0]} failed after {attempts} attempts: {last_error}")


def download(url: str, dst, *, timeout: int = 600, attempts: int = 3) -> None:
    """Stream a generated asset to disk."""
    from pathlib import Path  # noqa: PLC0415

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with dst.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
            if dst.stat().st_size > 0:
                return
        except requests.RequestException as exc:
            if attempt == attempts:
                raise StepFailed(f"Could not download {url.split('?')[0]}: {exc}") from exc
            time.sleep(2 ** attempt)
    raise StepFailed(f"Downloaded an empty file from {url.split('?')[0]}")
