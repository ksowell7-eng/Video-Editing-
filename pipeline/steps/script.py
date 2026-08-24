"""Step 4 — the voiceover script.

This is the one step a model writes rather than a program. The pipeline's job
is to make that handoff exact: it emits `script_request.json` containing the
article facts, each phase's brief, and a word budget derived from the phase
duration and the speaking rate — then stops. Claude writes `script.json`
alongside it, the run is repeated, and the step validates and passes it on.

Stopping is deliberate. Generating VO audio for lines that overrun their phase
means paying ElevenLabs for takes that get cut, so the word budget is checked
before a single character is synthesised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import NeedsScript, StepFailed
from .base import Context, StepResult
from .. import logs

SCHEMA_HINT = {
    "lines": [
        {"phase": "<phase id>", "voice": "narrator|coach", "text": "<the spoken line>"}
    ]
}


def word_budget(target_s: float, words_per_second: float) -> int:
    return max(3, int(round(target_s * words_per_second)))


def build_request(ctx: Context, article: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.job["script"]
    wps = float(cfg["words_per_second"])
    phases = []
    for phase in ctx.job.phases:
        budget = word_budget(phase.target_s, wps)
        phases.append({
            "phase": phase.id,
            "voice": phase.voice,
            "seconds": phase.target_s,
            "brief": phase.brief,
            "target_words": budget,
            "min_words": max(3, int(budget * (1 - float(cfg["tolerance"])))),
            "max_words": int(budget * (1 + float(cfg["tolerance"]))) + 1,
        })
    return {
        "instructions": (
            "Write one spoken line per phase, in order. Use only facts present in "
            "article.text — no invented names, scores, dates or quotes. Stay inside "
            "each phase's min/max word count; the count is what keeps the VO inside "
            "its slot. Write for the ear: short clauses, no headings, no stage "
            "directions, no emoji, and no markdown. Write the output to script.json "
            "in this directory using the schema in output_schema."
        ),
        "personas": {
            "narrator": ctx.job["script"]["narrator_persona"],
            "coach": ctx.job["script"]["coach_persona"],
        },
        "banned_phrases": ctx.job["script"]["banned_phrases"],
        "article": {
            "title": article.get("title", ""),
            "byline": article.get("byline", ""),
            "published": article.get("published", ""),
            "url": article.get("url", ""),
            "text": article.get("text", ""),
        },
        "phases": phases,
        "output_schema": SCHEMA_HINT,
        "output_path": "script.json",
    }


def validate(script: dict[str, Any], ctx: Context) -> list[dict[str, Any]]:
    cfg = ctx.job["script"]
    wps = float(cfg["words_per_second"])
    tolerance = float(cfg["tolerance"])
    banned = [b.lower() for b in cfg["banned_phrases"]]

    if not isinstance(script, dict) or not isinstance(script.get("lines"), list):
        raise StepFailed("script.json must be an object with a 'lines' array")

    by_phase = {}
    for entry in script["lines"]:
        if not isinstance(entry, dict) or "phase" not in entry or "text" not in entry:
            raise StepFailed(f"Malformed script line: {json.dumps(entry)[:160]}")
        by_phase[str(entry["phase"])] = entry

    problems: list[str] = []
    lines: list[dict[str, Any]] = []
    for phase in ctx.job.phases:
        entry = by_phase.get(phase.id)
        if entry is None:
            problems.append(f"missing line for phase '{phase.id}'")
            continue
        text = " ".join(str(entry["text"]).split())
        if not text:
            problems.append(f"phase '{phase.id}' has an empty line")
            continue
        count = len(text.split())
        budget = word_budget(phase.target_s, wps)
        low, high = max(3, int(budget * (1 - tolerance))), int(budget * (1 + tolerance)) + 1
        if not low <= count <= high:
            problems.append(
                f"phase '{phase.id}': {count} words, needs {low}-{high} for {phase.target_s:.0f}s"
            )
        for phrase in banned:
            if phrase and phrase in text.lower():
                problems.append(f"phase '{phase.id}' contains a banned phrase: {phrase!r}")
        voice = str(entry.get("voice") or phase.voice)
        if voice != phase.voice:
            logs.warn(f"phase '{phase.id}' declares voice '{voice}'; using '{phase.voice}' from the job")
        lines.append({
            "phase": phase.id,
            "voice": phase.voice,
            "text": text,
            "words": count,
            "estimated_s": round(count / wps, 2),
        })

    if problems:
        raise StepFailed(
            "script.json does not satisfy the phase budgets:\n  - " + "\n  - ".join(problems),
            hint="Edit script.json and re-run; nothing downstream has been billed yet.",
        )
    return lines


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["script"]
    out_dir = ctx.dir_for("script")
    article_json = ctx.data("article").get("article_json")
    if not article_json:
        raise StepFailed("The article step has not run yet")
    article = json.loads(Path(article_json).read_text(encoding="utf-8"))

    if cfg["strategy"] == "file":
        script_path = ctx.job.resolve(cfg["file"])
        if not script_path or not script_path.exists():
            raise StepFailed(f"script.file not found: {cfg['file']}")
    else:
        script_path = out_dir / "script.json"
        request_path = out_dir / "script_request.json"
        request_path.write_text(
            json.dumps(build_request(ctx, article), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if not script_path.exists():
            raise NeedsScript(
                f"Voiceover script not written yet: {script_path}",
                hint=(
                    f"Read {request_path} and write {script_path} to its output_schema, "
                    "then re-run the pipeline. It resumes from this step."
                ),
            )

    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StepFailed(f"{script_path} is not valid JSON: {exc}") from exc

    lines = validate(script, ctx)
    resolved = out_dir / "script.resolved.json"
    resolved.write_text(json.dumps({"lines": lines}, indent=2, ensure_ascii=False), encoding="utf-8")

    total_words = sum(line["words"] for line in lines)
    logs.info(
        f"script accepted: {len(lines)} lines, {total_words} words",
        estimated=f"{sum(l['estimated_s'] for l in lines):.1f}s",
    )
    return StepResult(
        outputs=[resolved],
        data={
            "script": str(resolved),
            "lines": lines,
            "total_words": total_words,
            "estimated_duration_s": round(sum(line["estimated_s"] for line in lines), 2),
        },
    )
