"""Job configuration: the whole parameter surface of a run.

A job file is the only thing a caller writes. It names the highlight clip and
the article, and overrides any default it cares about; everything unset falls
back to DEFAULTS below. Unknown keys are rejected rather than ignored — a typo
in a parameter file is otherwise invisible until the render looks wrong.

Secrets never live here. API keys are read from the environment at call time.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from . import logs

DEFAULTS: dict[str, Any] = {
    "id": None,
    "input": {
        "highlight_clip": None,          # required: the vintage highlight (any aspect)
        "article_url": None,             # required unless article_html is set
        "article_html": None,            # local .html, for offline runs and tests
        "coach_reference_image": None,   # identity anchor for the avatar + identity check
        "music_bed": None,               # optional loopable bed, ducked under VO
    },
    "output": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "target_duration_s": 45,
        "duration_tolerance_s": 3.0,
        "quality": "high",               # hyperframes render: draft | standard | high
        "file": "out/short.mp4",
    },
    # The four-phase shorts arc. Phases are data, not code: add, drop, or
    # reorder them and every downstream step follows.
    "phases": [
        {"id": "hook", "voice": "narrator", "target_s": 4, "bed": "highlight", "markers": True,
         "brief": "Cold open on the decisive moment. One sentence, present tense, no setup."},
        {"id": "context", "voice": "narrator", "target_s": 10, "bed": "broll", "markers": True,
         "brief": "The situation from the article: who, when, what was at stake. Facts only."},
        {"id": "analysis", "voice": "coach", "target_s": 22, "bed": "avatar", "markers": False,
         "brief": "The coach's tactical read: what the players actually did and why it worked."},
        {"id": "payoff", "voice": "narrator", "target_s": 9, "bed": "highlight", "markers": True,
         "brief": "Land the lesson in one line, then the outcome. No call to action."},
    ],
    "article": {
        "selectors": ["article", "main", "[itemprop=articleBody]", ".article-body", "#content"],
        "min_chars": 400,
        "max_phrases": 60,
        "phrase_min_words": 2,
        "phrase_max_words": 7,
        "viewport": {"width": 1280, "height": 2400},
        "timeout_s": 45,
        "wait_until": "networkidle",
        "screenshot": True,
    },
    "broll": {
        "enabled": True,
        "queries": [],                   # empty: derived from article keywords
        "results_per_query": 6,
        "keep_clips": 6,
        "clip_len_s": [2.0, 4.5],
        "max_source_duration_s": 900,
        "min_height": 720,
        "match_filter": "!is_live & duration < 900",
        "only_creative_commons": False,   # restrict to CC-BY sources

        "cookies_file": None,
        "sponsorblock": True,            # skip intros/sponsor segments when marked
        "download_concurrency": 2,
    },
    "reframe": {
        "enabled": True,
        "strategy": "face_track",        # face_track | center | saliency_fallback
        "detect_every_n_frames": 3,
        "min_face_px": 48,
        "smoothing": "cubic",
        "smoothing_window_s": 1.2,
        "keyframe_interval_s": 0.5,
        "max_pan_px_per_s": 240,
        "deadzone_px": 36,
        "subject_bias_y": -0.06,         # nudge the crop up; heads sit high in 9:16
        "letterbox_fallback": True,
    },
    "identity": {
        "enabled": True,
        "targets": ["avatar"],           # avatar | broll
        "endpoint": "http://localhost:1234/v1",
        "model": "qwen3.6",
        "sample_frames": 6,
        "min_pass_ratio": 0.67,
        "max_regenerations": 1,
        "timeout_s": 120,
        "max_image_px": 768,
    },
    "script": {
        "strategy": "claude_handoff",    # claude_handoff | file
        "file": None,
        "words_per_second": 2.6,         # eleven_v3 at default pace
        "tolerance": 0.18,               # allowed drift on per-phase word budget
        "narrator_persona": "A dry, economical documentary narrator. No hype, no questions.",
        "coach_persona": "A veteran coach doing post-game analysis at a whiteboard. Direct, technical, warm.",
        "banned_phrases": ["let's dive in", "game changer", "in today's video", "buckle up"],
    },
    "voice": {
        # "elevenlabs" is the delivery voice; "local" uses the Kokoro model
        # bundled with hyperframes, which costs nothing and is the right choice
        # while iterating on timing and layout.
        "provider": "elevenlabs",
        "model": "eleven_v3",
        "narrator_voice_id": None,
        "coach_voice_id": None,
        "stability": 0.45,
        "similarity_boost": 0.75,
        "style": 0.35,
        "speed": 1.0,
        "output_format": "mp3_44100_128",
        "loudness_lufs": -16.0,
        "timeout_s": 180,
    },
    "avatar": {
        "enabled": True,
        "provider": "kie",
        "model": "bytedance/seedance-2",
        "endpoint": "https://api.kie.ai/api/v1/jobs",
        "mode": "v2v",
        "driver_clip": None,             # v2v source; defaults to a still-derived loop
        "prompt": "Talking-head coach in a dim tactics room, shallow depth of field, natural head motion.",
        "negative_prompt": "text overlay, watermark, extra limbs, warped face",
        "resolution": "1080p",
        "aspect_ratio": "9:16",
        "seed": 7,
        "poll_interval_s": 10,
        "timeout_s": 900,
        "cost_per_second_usd": 0.12,
    },
    "budget": {
        "file": "budget.json",
        "max_usd_per_run": 4.00,
        "max_usd_total": 40.00,
        "fail_closed": True,             # unknown price => refuse, never guess low
    },
    "captions": {
        "enabled": True,
        "engine": "whisper",             # passed to `hyperframes transcribe --engine`
        "model": "small.en",
        "language": "en",
        "style": "karaoke",              # karaoke | block
        # What to do when whisper is unavailable: derive approximate timings
        # from the script text, or ship without captions.
        "fallback": "estimate",          # estimate | none
        "words_per_cue": 3,
        "max_chars_per_cue": 24,
        "safe_bottom_pct": 18,
        "font_px": 74,
        "highlight_color": "#ffd75e",
    },
    "markers": {
        "enabled": True,
        "max_per_phase": 2,
        "min_gap_s": 1.6,
        "hold_s": 1.4,
        "style": "underline",            # underline | box | circle
        "color": "#ffcc00",
    },
    "render": {
        "lint": True,
        "workers": 4,
        "video_frame_format": "auto",
        "timeout_s": 3600,
        "hyperframes_version": "0.8.13",
    },
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VALID_BEDS = {"highlight", "broll", "avatar", "black"}


def _strip_comments(value: Any) -> Any:
    """Drop "//" keys so a job file can carry inline notes.

    JSON has no comments and a parameter file is read by people, so keys
    starting with // are treated as prose and removed before validation.
    """
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not str(k).startswith("//")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in out:
            raise ConfigError(
                f"Unknown parameter '{where}'",
                hint=f"Valid keys at '{path or 'top level'}': {', '.join(sorted(out))}",
            )
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value, where)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Phase:
    id: str
    voice: str
    target_s: float
    bed: str
    markers: bool = False
    brief: str = ""

    @property
    def word_budget_hint(self) -> str:
        return f"{self.id}: about {self.target_s:.0f}s"


@dataclass
class Job:
    raw: dict[str, Any]
    path: Path
    root: Path

    # ---- construction -------------------------------------------------

    @classmethod
    def load(cls, path: str | Path, overrides: dict[str, Any] | None = None) -> "Job":
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise ConfigError(f"Job file not found: {p}")
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p.name} is not valid JSON: {exc}") from exc
        user.pop("$schema", None)
        user = _strip_comments(user)
        # Overrides are layered on top of the merged defaults, not onto the user
        # dict: --set output.fps=60 has to work whether or not the job file
        # happens to mention output, and it should be validated against the full
        # schema either way.
        merged = _deep_merge(DEFAULTS, user)
        if overrides:
            merged = _deep_merge(merged, overrides)
        job = cls(raw=merged, path=p, root=p.parent)
        job.validate()
        return job

    # ---- typed accessors ----------------------------------------------

    def section(self, name: str) -> dict[str, Any]:
        return self.raw[name]

    def __getitem__(self, name: str) -> Any:
        return self.raw[name]

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def phases(self) -> list[Phase]:
        return [Phase(**p) for p in self.raw["phases"]]

    @property
    def fps(self) -> int:
        return int(self.raw["output"]["fps"])

    @property
    def size(self) -> tuple[int, int]:
        out = self.raw["output"]
        return int(out["width"]), int(out["height"])

    @property
    def target_duration_s(self) -> float:
        return float(self.raw["output"]["target_duration_s"])

    def resolve(self, value: str | None) -> Path | None:
        """Resolve a job-relative path. Absolute paths pass through."""
        if not value:
            return None
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else (self.root / p).resolve()

    def fingerprint(self, *sections: str) -> str:
        """Stable hash of the given config sections, for step-skip decisions."""
        payload = json.dumps({s: self.raw[s] for s in sections}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    # ---- validation ----------------------------------------------------

    def validate(self) -> None:
        raw = self.raw
        if not raw.get("id"):
            raw["id"] = self.path.stem.replace(".job", "") or "job"
        if not _ID_RE.match(str(raw["id"])):
            raise ConfigError(
                f"id '{raw['id']}' must be lowercase alphanumeric with . _ - (max 64 chars)"
            )

        inp = raw["input"]
        if not inp.get("highlight_clip"):
            raise ConfigError(
                "input.highlight_clip is required",
                hint="Point it at the highlight video, e.g. \"input/highlight.mp4\".",
            )
        clip = self.resolve(inp["highlight_clip"])
        if clip is None or not clip.exists():
            raise ConfigError(
                f"highlight clip not found: {inp['highlight_clip']}",
                hint=f"Resolved against the job file's directory: {self.root}",
            )
        if not inp.get("article_url") and not inp.get("article_html"):
            raise ConfigError("Set input.article_url (or input.article_html for an offline run)")
        if inp.get("article_url") and not str(inp["article_url"]).startswith(("http://", "https://")):
            raise ConfigError(f"input.article_url must be http(s): {inp['article_url']}")
        for key in ("article_html", "coach_reference_image", "music_bed"):
            if inp.get(key):
                p = self.resolve(inp[key])
                if p is None or not p.exists():
                    raise ConfigError(f"input.{key} not found: {inp[key]}")

        out = raw["output"]
        if out["fps"] not in (24, 30, 60):
            raise ConfigError(f"output.fps must be 24, 30 or 60 (hyperframes render); got {out['fps']}")
        if out["quality"] not in ("draft", "standard", "high"):
            raise ConfigError(f"output.quality must be draft|standard|high; got {out['quality']}")
        if int(out["width"]) <= 0 or int(out["height"]) <= 0:
            raise ConfigError("output.width and output.height must be positive")
        if int(out["width"]) > int(out["height"]):
            raise ConfigError(
                f"output is {out['width']}x{out['height']} — landscape",
                hint="This pipeline builds vertical shorts; the default is 1080x1920.",
            )

        phases = raw["phases"]
        if not phases:
            raise ConfigError("phases must not be empty")
        seen: set[str] = set()
        for i, p in enumerate(phases):
            missing = {"id", "voice", "target_s", "bed"} - set(p)
            if missing:
                raise ConfigError(f"phases[{i}] is missing: {', '.join(sorted(missing))}")
            extra = set(p) - {"id", "voice", "target_s", "bed", "markers", "brief"}
            if extra:
                raise ConfigError(f"phases[{i}] has unknown keys: {', '.join(sorted(extra))}")
            if p["id"] in seen:
                raise ConfigError(f"duplicate phase id '{p['id']}'")
            seen.add(p["id"])
            if float(p["target_s"]) <= 0:
                raise ConfigError(f"phases[{i}].target_s must be positive")
            if p["bed"] not in _VALID_BEDS:
                raise ConfigError(
                    f"phases[{i}].bed '{p['bed']}' is not one of: {', '.join(sorted(_VALID_BEDS))}"
                )

        total = sum(float(p["target_s"]) for p in phases)
        tolerance = float(out["duration_tolerance_s"])
        if abs(total - float(out["target_duration_s"])) > tolerance:
            raise ConfigError(
                f"phase durations sum to {total:.1f}s but output.target_duration_s "
                f"is {out['target_duration_s']}s (tolerance ±{tolerance}s)",
                hint="Adjust phases[].target_s or output.target_duration_s so they agree.",
            )

        voices = {p["voice"] for p in phases}
        unknown = voices - {"narrator", "coach"}
        if unknown:
            raise ConfigError(f"phases reference unknown voices: {', '.join(sorted(unknown))}")

        if any(p["bed"] == "avatar" for p in phases) and not raw["avatar"]["enabled"]:
            raise ConfigError(
                "a phase uses bed 'avatar' but avatar.enabled is false",
                hint="Enable the avatar, or switch that phase's bed to broll/highlight.",
            )
        if any(p["bed"] == "broll" for p in phases) and not raw["broll"]["enabled"]:
            raise ConfigError("a phase uses bed 'broll' but broll.enabled is false")

        lo, hi = raw["broll"]["clip_len_s"]
        if float(lo) <= 0 or float(hi) < float(lo):
            raise ConfigError(f"broll.clip_len_s must be [min, max] with 0 < min <= max; got {[lo, hi]}")

        ident = raw["identity"]
        bad_targets = set(ident["targets"]) - {"avatar", "broll"}
        if bad_targets:
            raise ConfigError(f"identity.targets may only contain avatar/broll; got {sorted(bad_targets)}")
        if not 0 < float(ident["min_pass_ratio"]) <= 1:
            raise ConfigError("identity.min_pass_ratio must be in (0, 1]")
        if ident["enabled"] and "avatar" in ident["targets"] and not inp.get("coach_reference_image"):
            # The check compares generated frames against a reference face. With
            # no reference there is nothing to compare to, so it is dropped
            # rather than failing the job — but never silently.
            ident["targets"] = [t for t in ident["targets"] if t != "avatar"]
            logs.warn(
                "identity: no input.coach_reference_image, so the avatar identity check is off",
                hint="add the reference still to enable it",
            )

        budget = raw["budget"]
        if float(budget["max_usd_per_run"]) <= 0:
            raise ConfigError("budget.max_usd_per_run must be positive")
        if float(budget["max_usd_total"]) < float(budget["max_usd_per_run"]):
            raise ConfigError("budget.max_usd_total must be >= budget.max_usd_per_run")

        script = raw["script"]
        if raw["voice"]["provider"] not in ("elevenlabs", "local"):
            raise ConfigError("voice.provider must be elevenlabs or local")

        if script["strategy"] not in ("claude_handoff", "file"):
            raise ConfigError("script.strategy must be claude_handoff or file")
        if script["strategy"] == "file" and not script.get("file"):
            raise ConfigError("script.strategy 'file' requires script.file")
        if float(script["words_per_second"]) <= 0:
            raise ConfigError("script.words_per_second must be positive")

        if raw["captions"]["style"] not in ("karaoke", "block"):
            raise ConfigError("captions.style must be karaoke or block")
        if raw["captions"]["fallback"] not in ("estimate", "none"):
            raise ConfigError("captions.fallback must be estimate or none")
        if raw["markers"]["style"] not in ("underline", "box", "circle"):
            raise ConfigError("markers.style must be underline, box or circle")


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """Turn --set output.fps=60 --set broll.enabled=false into a nested dict."""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--set expects key=value, got '{pair}'")
        key, _, value = pair.partition("=")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        cursor = out
        parts = key.strip().split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = parsed
    return out
