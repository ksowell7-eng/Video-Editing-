"""Timecodes, in every shape a person actually writes them.

Edit requests arrive as prose — "cut the first three seconds", "the bit at
1:12", "0:00:04.5". All of those become seconds here, and seconds become
`M:SS.mmm` going back out, because a contact sheet labelled 74.5 is useless for
pointing at a moment.
"""

from __future__ import annotations

import re

from ..errors import ConfigError

_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")
_PLAIN = re.compile(r"^\d+(?:\.\d+)?$")
_SUFFIXED = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes|ms)$", re.I)


def parse(value: str | int | float | None, *, field: str = "time") -> float | None:
    """Accept 12, "12", "12.5", "1:12", "1:02:03.4", "500ms", "2min" → seconds.

    None and "" pass through as None, which callers read as "unbounded".
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ConfigError(f"{field} cannot be negative: {value}")
        return seconds

    text = str(value).strip().lower()
    if text in ("start", "begin", "beginning"):
        return 0.0
    if text in ("end", "eof"):
        return None

    if _PLAIN.match(text):
        return float(text)

    suffixed = _SUFFIXED.match(text)
    if suffixed:
        amount, unit = float(suffixed.group(1)), suffixed.group(2).lower()
        if unit == "ms":
            return amount / 1000
        if unit.startswith("m"):
            return amount * 60
        return amount

    clock = _CLOCK.match(text)
    if clock:
        hours = float(clock.group(1) or 0)
        minutes = float(clock.group(2))
        seconds = float(clock.group(3))
        if minutes >= 60 or seconds >= 60:
            raise ConfigError(f"{field} has an out-of-range component: {value}")
        return hours * 3600 + minutes * 60 + seconds

    raise ConfigError(
        f"{field} is not a time: {value!r}",
        hint='Use seconds (12.5), a clock ("1:12", "0:01:12.5"), or "500ms" / "2min".',
    )


def format_tc(seconds: float, *, millis: bool = False) -> str:
    """Seconds → M:SS or H:MM:SS, the way a person would read it back."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, whole = divmod(remainder, 60)
    fraction = seconds - int(seconds)
    base = f"{hours}:{minutes:02d}:{whole:02d}" if hours else f"{minutes}:{whole:02d}"
    return f"{base}.{int(round(fraction * 1000)):03d}" if millis else base


def resolve_span(
    spec: dict, duration: float, *, start_key: str = "from", end_key: str = "to",
) -> tuple[float, float]:
    """Read a from/to pair against a known duration, clamped and ordered."""
    start = parse(spec.get(start_key), field=start_key) or 0.0
    end = parse(spec.get(end_key), field=end_key)
    if end is None:
        end = duration
    start = min(max(0.0, start), duration)
    end = min(max(0.0, end), duration)
    if end < start:
        start, end = end, start
    return start, end
