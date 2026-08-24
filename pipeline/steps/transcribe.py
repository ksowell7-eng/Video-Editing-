"""Step 8 — word-level timings via `npx hyperframes transcribe`.

Each phase's voiceover is transcribed on its own, in its own directory. That
isolation is not fussiness: `hyperframes transcribe` patches *every* HTML file
in the directory it is pointed at with the transcript it just produced, so
transcribing four tracks into one project would leave the composition holding
only the last one. Merging the four word lists onto the master timeline is done
here instead, where the phase offsets are known.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..compose.captions import estimate_word_timings
from ..errors import StepFailed
from ..media.probe import probe
from ..shell import npx, run as run_command
from .base import Context, StepResult
from .. import logs


def transcribe_file(
    audio: Path, work_dir: Path, *, engine: str, model: str, language: str, version: str,
) -> list[dict[str, Any]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    proc = run_command([
        npx(), "--yes", f"hyperframes@{version}", "transcribe", str(audio),
        "--dir", str(work_dir),
        "--engine", engine,
        "--model", model,
        "--language", language,
        "--json",
    ], timeout=1800, check=False)

    payload: dict[str, Any] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    if not payload:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise StepFailed(
            f"transcribe produced no JSON for {audio.name}\n" + "\n".join(tail),
            hint="Run `npx hyperframes doctor` — whisper.cpp may not be installed.",
        )
    if not payload.get("ok"):
        if payload.get("skipped"):
            logs.warn(f"{audio.name}: transcription skipped ({payload.get('reason', 'unavailable')})")
            return []
        raise StepFailed(f"transcribe failed for {audio.name}: {json.dumps(payload)[:300]}")

    transcript_path = Path(payload["transcriptPath"])
    if not transcript_path.exists():
        raise StepFailed(f"transcribe reported {transcript_path}, which does not exist")
    words = json.loads(transcript_path.read_text(encoding="utf-8"))
    if not isinstance(words, list):
        raise StepFailed(f"Unexpected transcript shape in {transcript_path}")
    return words


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["captions"]
    version = ctx.job["render"]["hyperframes_version"]
    out_dir = ctx.dir_for("transcribe")
    tracks = ctx.data("voice").get("tracks", [])
    if not tracks:
        raise StepFailed("No voiceover tracks to transcribe")

    if not cfg["enabled"]:
        logs.info("captions disabled; skipping transcription")
        return StepResult(outputs=[], data={"tracks": [], "word_count": 0})

    line_text = {line["phase"]: line["text"] for line in ctx.data("script").get("lines", [])}
    results: list[dict[str, Any]] = []
    outputs: list[Path] = []
    total_words = 0
    estimated = False

    for track in tracks:
        phase = track["phase"]
        audio = Path(track["file"])
        work_dir = out_dir / phase
        target = out_dir / f"{phase}.words.json"

        source = "cache"
        if target.exists():
            words = json.loads(target.read_text(encoding="utf-8"))
        else:
            try:
                words = transcribe_file(
                    audio, work_dir,
                    engine=cfg["engine"], model=cfg["model"], language=cfg["language"],
                    version=version,
                )
                source = "asr"
            except StepFailed as exc:
                if cfg["fallback"] != "estimate":
                    raise
                # Whisper is unavailable on this machine (no model, no network,
                # no whisper.cpp). The script text and the measured audio length
                # are both known, so approximate timings beat no captions.
                logs.warn(f"{phase}: transcription unavailable ({str(exc).splitlines()[0][:90]})")
                logs.info(f"{phase}: estimating word timings from the script instead")
                words = estimate_word_timings(line_text[phase], probe(audio).duration_s)
                source = "estimated"
            target.write_text(json.dumps(words, indent=2), encoding="utf-8")

        total_words += len(words)
        estimated = estimated or source == "estimated"
        results.append({"phase": phase, "file": str(target), "word_count": len(words),
                        "source": source})
        outputs.append(target)
        logs.info(f"{phase}: {len(words)} words ({source})")

    if total_words == 0:
        logs.warn("no words transcribed; the render will have no captions")

    if estimated:
        logs.warn(
            "some caption timings are estimated, not transcribed",
            hint="install whisper.cpp (npx hyperframes doctor) and re-run --only transcribe --force",
        )

    return StepResult(
        outputs=outputs,
        data={"tracks": results, "word_count": total_words, "estimated": estimated},
    )
