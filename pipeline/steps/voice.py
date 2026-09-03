"""Step 5 — voice the script with ElevenLabs.

One request per phase, so a re-write of a single line re-bills a single line.
Each track is loudness-normalised and then padded or trimmed to the phase slot
it has to fill, which is what keeps the composition's phase boundaries honest.

ElevenLabs is charged per character rather than per second, and the character
count is known before the call, so the budget reservation here is exact rather
than estimated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import StepFailed
from ..http import api_key, request
from ..media.edit import normalize_loudness, pad_audio
from ..media.probe import probe
from ..shell import npx, run as run_command
from .base import Context, StepResult
from .. import logs

API_ROOT = "https://api.elevenlabs.io/v1"
# ElevenLabs bills credits, not dollars; this is the published list rate for
# the standard tiers and is only used for budget bookkeeping.
USD_PER_1K_CHARS = 0.18


# Kokoro voices used when voice.provider is "local" and no id is configured.
_LOCAL_DEFAULTS = {"narrator": "bm_george", "coach": "am_michael"}


def _voice_id(cfg: dict[str, Any], voice: str) -> str:
    key = f"{voice}_voice_id"
    value = cfg.get(key)
    if not value and cfg["provider"] == "local":
        return _LOCAL_DEFAULTS.get(voice, "af_heart")
    if not value:
        raise StepFailed(
            f"voice.{key} is not set",
            hint="Pick a voice in the ElevenLabs dashboard and paste its id into the job file.",
        )
    return str(value)


def synthesize(text: str, voice_id: str, cfg: dict[str, Any], dst: Path) -> Path:
    key = api_key("ELEVENLABS_API_KEY", "ElevenLabs")
    payload = {
        "text": text,
        "model_id": cfg["model"],
        "voice_settings": {
            "stability": float(cfg["stability"]),
            "similarity_boost": float(cfg["similarity_boost"]),
            "style": float(cfg["style"]),
            "speed": float(cfg["speed"]),
            "use_speaker_boost": True,
        },
    }
    audio = request(
        "POST",
        f"{API_ROOT}/text-to-speech/{voice_id}?output_format={cfg['output_format']}",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        json_body=payload,
        timeout=int(cfg["timeout_s"]),
        expect_json=False,
    )
    if not audio or len(audio) < 1024:
        raise StepFailed(f"ElevenLabs returned {len(audio or b'')} bytes for {dst.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(audio)
    return dst


def synthesize_local(text: str, voice_id: str, cfg: dict[str, Any], dst: Path, version: str) -> Path:
    """Speak the line with the Kokoro model bundled in the hyperframes CLI.

    Free and offline, so a run can be iterated on without spending credits.
    It writes a wav; the caller normalises and pads it exactly as it would an
    ElevenLabs mp3.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        npx(), "--yes", f"hyperframes@{version}", "tts", text,
        "-o", str(dst), "-v", voice_id, "-s", str(float(cfg["speed"])), "--json",
    ], timeout=900)
    if not dst.exists() or dst.stat().st_size < 1024:
        raise StepFailed(f"local tts produced no audio for {dst.name}")
    return dst


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["voice"]
    out_dir = ctx.dir_for("voice")
    lines = ctx.require("script", "lines")
    phases = {p.id: p for p in ctx.job.phases}

    tracks: list[dict[str, Any]] = []
    outputs: list[Path] = []

    for line in lines:
        phase = phases[line["phase"]]
        # Kokoro emits wav, ElevenLabs mp3; both are normalised to the same
        # padded mp3 the composition references.
        raw = out_dir / (f"{phase.id}.raw.wav" if cfg["provider"] == "local" else f"{phase.id}.raw.mp3")
        final = out_dir / f"{phase.id}.mp3"

        if raw.exists() and final.exists():
            logs.info(f"{phase.id}: voiceover already rendered")
        elif cfg["provider"] == "local":
            synthesize_local(
                line["text"], _voice_id(cfg, phase.voice), cfg, raw,
                ctx.job["render"]["hyperframes_version"],
            )
            normalized = out_dir / f"{phase.id}.norm.mp3"
            normalize_loudness(raw, normalized, float(cfg["loudness_lufs"]))
            pad_audio(normalized, final, phase.target_s)
            normalized.unlink(missing_ok=True)
        else:
            characters = len(line["text"])
            reservation = ctx.budget.reserve(
                f"elevenlabs:{phase.id}",
                characters / 1000 * USD_PER_1K_CHARS,
                provider="elevenlabs", model=cfg["model"], characters=characters,
            )
            try:
                synthesize(line["text"], _voice_id(cfg, phase.voice), cfg, raw)
            except Exception:
                reservation.release("synthesis failed")
                raise
            reservation.settle()

            normalized = out_dir / f"{phase.id}.norm.mp3"
            normalize_loudness(raw, normalized, float(cfg["loudness_lufs"]))
            # Lock each track to its slot: the composition's phase boundaries
            # are fixed, so a track that runs long would bleed into the next.
            pad_audio(normalized, final, phase.target_s)
            normalized.unlink(missing_ok=True)

        spoken = probe(raw).duration_s if raw.exists() else phase.target_s
        tracks.append({
            "phase": phase.id,
            "voice": phase.voice,
            "file": str(final),
            "raw_file": str(raw),
            "spoken_s": round(spoken, 3),
            "slot_s": round(phase.target_s, 3),
            "overrun_s": round(spoken - phase.target_s, 3),
        })
        outputs.append(final)

    manifest = out_dir / "voice.json"
    manifest.write_text(json.dumps(tracks, indent=2), encoding="utf-8")
    outputs.append(manifest)

    for track in tracks:
        if track["overrun_s"] > 0.35:
            logs.warn(
                f"{track['phase']}: VO runs {track['overrun_s']:.2f}s past its slot and was cut",
                hint="shorten the line or raise the phase's target_s",
            )

    logs.info(f"voiced {len(tracks)} phases", spend=f"${ctx.budget.spent()[0]:.2f}")
    return StepResult(
        outputs=outputs,
        data={
            "tracks": tracks,
            "manifest": str(manifest),
            "durations": {t["phase"]: t["slot_s"] for t in tracks},
            "spoken": {t["phase"]: t["spoken_s"] for t in tracks},
        },
    )
