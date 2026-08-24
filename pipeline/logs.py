"""Step-scoped logging that reads well in a terminal and greps well in CI."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


_LEVELS = {
    "debug": ("2", "  "),
    "info": ("0", "  "),
    "step": ("1;36", "▸ "),
    "ok": ("32", "✓ "),
    "warn": ("33", "! "),
    "error": ("31", "✗ "),
    "cost": ("35", "$ "),
}

_verbose = os.environ.get("PIPELINE_VERBOSE") == "1"
_logfile: Path | None = None


def configure(*, verbose: bool = False, logfile: Path | None = None) -> None:
    global _verbose, _logfile
    _verbose = verbose or os.environ.get("PIPELINE_VERBOSE") == "1"
    _logfile = logfile
    if _logfile:
        _logfile.parent.mkdir(parents=True, exist_ok=True)


def log(level: str, message: str, **fields: object) -> None:
    if level == "debug" and not _verbose:
        return
    color, prefix = _LEVELS.get(level, ("0", "  "))
    tail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    line = f"{prefix}{message}" + (f"  {_c('2', tail)}" if tail else "")
    print(_c(color, line) if level in ("step", "ok", "warn", "error", "cost") else line, file=sys.stderr)
    if _logfile:
        record = {"ts": round(time.time(), 3), "level": level, "msg": message, **fields}
        with _logfile.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def debug(msg: str, **f: object) -> None:
    log("debug", msg, **f)


def info(msg: str, **f: object) -> None:
    log("info", msg, **f)


def ok(msg: str, **f: object) -> None:
    log("ok", msg, **f)


def warn(msg: str, **f: object) -> None:
    log("warn", msg, **f)


def error(msg: str, **f: object) -> None:
    log("error", msg, **f)


def cost(msg: str, **f: object) -> None:
    log("cost", msg, **f)


@contextmanager
def step(name: str, description: str = ""):
    log("step", f"{name}" + (f" — {description}" if description else ""))
    started = time.time()
    try:
        yield
    except Exception:
        log("error", f"{name} failed", after=f"{time.time() - started:.1f}s")
        raise
    log("ok", f"{name}", took=f"{time.time() - started:.1f}s")
