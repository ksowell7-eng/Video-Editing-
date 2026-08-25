"""Command line: `new` to make a job, `doctor` to check the machine, `run` to build.

    python -m pipeline new --clip input/highlight.mp4 --article https://... 
    python -m pipeline doctor --job jobs/my.job.json
    python -m pipeline run --job jobs/my.job.json
    python -m pipeline run --job jobs/my.job.json --from voice --set output.fps=60

`run` is resumable and idempotent: each step records the configuration it ran
under, and re-running only repeats the steps whose inputs actually changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import from_job
from .config import DEFAULTS, Job, parse_overrides
from .edits.apply import apply_edits, load_edit_list, next_version, prune_cache
from .edits.narrate import assemble, load_script, synthesize
from .edits.narrate import report as narration_report
from .edits.ops import OPS
from .edits.review import contact_sheet
from .errors import ConfigError, NeedsScript, PipelineError
from .preflight import report, run_checks
from .state import RunState
from .steps import STEP_NAMES, STEPS, Context, execute
from . import logs


def _run_dir(job: Job, override: str | None) -> Path:
    return Path(override).resolve() if override else (job.root / "runs" / job.id)


def cmd_new(args: argparse.Namespace) -> int:
    clip = Path(args.clip)
    if not clip.exists():
        logs.error(f"clip not found: {clip}")
        return 2

    destination = Path(args.out) if args.out else Path("jobs") / f"{args.id or clip.stem}.job.json"
    if destination.exists() and not args.force:
        logs.error(f"{destination} already exists (use --force to overwrite)")
        return 2

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        relative = str(clip.resolve().relative_to(destination.parent.resolve()))
    except ValueError:
        relative = str(clip.resolve())

    job = {
        "id": args.id or clip.stem.lower().replace(" ", "-"),
        "input": {
            "highlight_clip": relative,
            "article_url": args.article,
            "coach_reference_image": args.reference,
        },
        "output": {"target_duration_s": DEFAULTS["output"]["target_duration_s"]},
        "voice": {"narrator_voice_id": args.narrator or None, "coach_voice_id": args.coach or None},
    }
    if not args.reference:
        job["input"].pop("coach_reference_image")
        job["identity"] = {"targets": ["broll"]}

    destination.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    logs.ok(f"wrote {destination}")
    logs.info("next: fill in the voice ids, then `python -m pipeline doctor --job "
              f"{destination}`")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    job = Job.load(args.job) if args.job else None
    healthy = report(run_checks(job))
    if job:
        budget = from_job(job, run_id=job.id)
        summary = budget.summary()
        logs.info(
            f"budget: ${summary['run_usd']:.2f} spent on this job, "
            f"${summary['remaining_usd']:.2f} left before the cap"
        )
    return 0 if healthy else 3


def cmd_run(args: argparse.Namespace) -> int:
    overrides = parse_overrides(args.set or [])
    job = Job.load(args.job, overrides)
    run_dir = _run_dir(job, args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    logs.configure(verbose=args.verbose, logfile=run_dir / "pipeline.log")

    if not args.skip_doctor:
        if not report(run_checks(job)):
            logs.error("preflight failed; fix the above or pass --skip-doctor to try anyway")
            return 3

    state = RunState(run_dir, job.id)
    ctx = Context(
        job=job,
        state=state,
        budget=from_job(job, run_id=job.id),
        run_dir=run_dir,
        force=args.force,
        dry_run=args.dry_run,
    )

    selected = STEPS
    if args.only:
        selected = [s for s in STEPS if s.name in args.only]
        if not selected:
            logs.error(f"--only matched nothing; steps are: {', '.join(STEP_NAMES)}")
            return 2
    elif args.from_step:
        start = STEP_NAMES.index(args.from_step)
        selected = STEPS[start:]
    if args.until:
        stop = STEP_NAMES.index(args.until)
        selected = [s for s in selected if STEP_NAMES.index(s.name) <= stop]

    logs.info(f"run {job.id}: {len(selected)} step(s) in {run_dir}")
    ran = 0
    for step in selected:
        try:
            ran += int(execute(step, ctx))
        except NeedsScript as exc:
            logs.warn(str(exc))
            if exc.hint:
                logs.info(exc.hint)
            return exc.exit_code
        except PipelineError as exc:
            logs.error(str(exc))
            if exc.hint:
                logs.info(f"→ {exc.hint}")
            return exc.exit_code

    summary = ctx.budget.summary()
    output = state.data("render").get("output")
    if output:
        logs.ok(f"done: {output}", spent=f"${summary['run_usd']:.2f}")
    else:
        logs.ok(f"{ran} step(s) ran", spent=f"${summary['run_usd']:.2f}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Apply an edit list to a video. This is the change-request loop."""
    edits_path = Path(args.edits).resolve()
    blob = load_edit_list(edits_path)
    root = edits_path.parent

    source_value = args.source or blob.get("source")
    if not source_value:
        raise ConfigError(
            "No source video",
            hint="Pass --source, or set \"source\" in the edit list.",
        )
    source = Path(source_value).expanduser()
    if not source.is_absolute():
        source = (root / source).resolve()

    if args.out:
        output = Path(args.out).resolve()
    else:
        configured = blob.get("output")
        base = (root / configured).resolve() if configured else (
            root / "out" / f"{source.stem}.mp4"
        )
        output = base if args.overwrite else next_version(base)

    workdir = Path(args.workdir).resolve() if args.workdir else root / "editwork"
    reframe_cfg = {**DEFAULTS["reframe"], **blob.get("reframe", {})}

    logs.configure(verbose=args.verbose)
    if args.dry_run:
        from .edits.ops import describe  # noqa: PLC0415

        logs.info(f"{len(blob['ops'])} op(s) against {source.name} → {output.name}")
        for index, spec in enumerate(blob["ops"]):
            logs.info(f"  [{index + 1}] {describe(spec)}")
        return 0

    result = apply_edits(
        source, blob["ops"], output,
        workdir=workdir,
        fps=int(blob.get("fps", DEFAULTS["output"]["fps"])),
        job_root=root,
        reframe_cfg=reframe_cfg,
        force=args.force,
    )

    record = output.with_suffix(".edits.json")
    record.write_text(json.dumps({
        "source": str(source),
        "output": str(result.output),
        "duration_s": result.duration_s,
        "ops": blob["ops"],
        "steps": result.steps,
    }, indent=2, default=str), encoding="utf-8")

    if args.sheet:
        contact_sheet(result.output, result.output.with_suffix(".sheet.png"),
                      count=args.sheet_frames)
    pruned = prune_cache(workdir)
    if pruned:
        logs.debug(f"pruned {pruned} cached intermediate(s)")
    return 0


def cmd_narrate(args: argparse.Namespace) -> int:
    """Generate a narration track with lines placed at absolute timecodes."""
    script_path = Path(args.script).resolve()
    script = load_script(script_path)
    out = Path(args.out).resolve() if args.out else script_path.with_suffix(".wav")
    work = Path(args.workdir).resolve() if args.workdir else script_path.parent / "lines"

    logs.configure(verbose=args.verbose)
    logs.info(f"{len(script['lines'])} lines via {args.provider}")
    placed = synthesize(
        script, work, provider=args.provider,
        version=DEFAULTS["render"]["hyperframes_version"], force=args.force,
    )

    duration = float(args.duration) if args.duration else max(p.end for p in placed) + 1.0
    for problem in narration_report(placed, duration):
        logs.warn(problem)

    assemble(placed, out, duration, gain=float(args.gain))
    logs.ok(f"{out.name}", duration=f"{duration:.1f}s", lines=len(placed))
    logs.info("attach with: {\"op\": \"replace_audio\", \"file\": \"%s\"}" % out.name)
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    video = Path(args.video).resolve()
    out = Path(args.out).resolve() if args.out else video.with_suffix(".sheet.png")
    contact_sheet(video, out, count=args.frames, columns=args.columns)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """What is this file? First thing to run on anything newly arrived."""
    from .media.probe import probe  # noqa: PLC0415
    from .edits.timecode import format_tc  # noqa: PLC0415

    for value in args.video:
        path = Path(value).resolve()
        info = probe(path)
        logs.info(
            f"{path.name}",
            duration=format_tc(info.duration_s, millis=True),
            size=f"{info.width}x{info.height}",
            fps=f"{info.fps:.2f}",
            audio="yes" if info.has_audio else "no",
            codec=info.codec,
            mb=f"{path.stat().st_size / 1e6:.1f}",
        )
        if info.width and info.height:
            orientation = "vertical" if info.is_vertical else "landscape"
            logs.info(f"  {orientation}, aspect {info.aspect:.3f}")
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """List the edit vocabulary."""
    width = max(len(name) for name in OPS) + 2
    for name in sorted(OPS):
        spec = OPS[name]
        required = f"  (needs: {', '.join(spec.required)})" if spec.required else ""
        logs.info(f"  {name.ljust(width)} {spec.summary}{required}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    job = Job.load(args.job)
    state = RunState(_run_dir(job, args.run_dir), job.id)
    budget = from_job(job, run_id=job.id)
    logs.info(f"job {job.id}")
    for name in STEP_NAMES:
        record = state.steps.get(name)
        if record is None:
            logs.info(f"  {name.ljust(11)} —")
        elif record.status == "ok":
            logs.ok(f"  {name.ljust(11)} ok ({record.duration_s:.1f}s)")
        elif record.status == "failed":
            logs.error(f"  {name.ljust(11)} failed: {record.error.splitlines()[0][:90]}")
        else:
            logs.info(f"  {name.ljust(11)} {record.status}")
    summary = budget.summary()
    logs.info(f"budget: ${summary['run_usd']:.2f} / ${summary['run_cap_usd']:.2f} this run")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="Vertical shorts: an article and a highlight clip in, a 1080x1920 video out.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every command")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new = subparsers.add_parser("new", help="scaffold a job file from a clip and an article")
    new.add_argument("--clip", required=True, help="the highlight video")
    new.add_argument("--article", required=True, help="source article URL")
    new.add_argument("--reference", help="coach reference still (enables the avatar identity check)")
    new.add_argument("--narrator", help="ElevenLabs narrator voice id")
    new.add_argument("--coach", help="ElevenLabs coach voice id")
    new.add_argument("--id", help="job id (defaults to the clip's filename)")
    new.add_argument("--out", help="where to write the job file")
    new.add_argument("--force", action="store_true", help="overwrite an existing job file")
    new.set_defaults(func=cmd_new)

    doctor = subparsers.add_parser("doctor", help="check binaries, packages and credentials")
    doctor.add_argument("--job", help="also check this job's inputs and credentials")
    doctor.set_defaults(func=cmd_doctor)

    run_cmd = subparsers.add_parser("run", help="run the pipeline")
    run_cmd.add_argument("--job", required=True)
    run_cmd.add_argument("--from", dest="from_step", choices=STEP_NAMES, help="resume from this step")
    run_cmd.add_argument("--until", choices=STEP_NAMES, help="stop after this step")
    run_cmd.add_argument("--only", nargs="+", choices=STEP_NAMES, help="run just these steps")
    run_cmd.add_argument("--force", action="store_true", help="re-run even when outputs are fresh")
    run_cmd.add_argument("--dry-run", action="store_true", help="show what would run")
    run_cmd.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a parameter")
    run_cmd.add_argument("--run-dir", help="workspace directory (default: runs/<job id>)")
    run_cmd.add_argument("--skip-doctor", action="store_true", help="skip preflight")
    run_cmd.set_defaults(func=cmd_run)

    edit = subparsers.add_parser("edit", help="apply an edit list to a video")
    edit.add_argument("--edits", required=True, help="the edit list JSON")
    edit.add_argument("--source", help="source video (overrides the list's own 'source')")
    edit.add_argument("--out", help="output path (default: next version beside the last)")
    edit.add_argument("--overwrite", action="store_true", help="replace the output instead of versioning")
    edit.add_argument("--workdir", help="where intermediates and the cache live")
    edit.add_argument("--force", action="store_true", help="ignore the cache and re-render every op")
    edit.add_argument("--dry-run", action="store_true", help="list the ops without rendering")
    edit.add_argument("--sheet", action="store_true", help="also write a contact sheet")
    edit.add_argument("--sheet-frames", type=int, default=12)
    edit.set_defaults(func=cmd_edit)

    narrate = subparsers.add_parser("narrate", help="build a narration track from a timed script")
    narrate.add_argument("--script", required=True)
    narrate.add_argument("--out", help="output wav (default: beside the script)")
    narrate.add_argument("--duration", type=float, help="length of the finished cut, in seconds")
    narrate.add_argument("--provider", choices=["elevenlabs", "local"], default="elevenlabs",
                         help="'local' is a free offline scratch track for judging timing")
    narrate.add_argument("--gain", default=1.0, help="level applied to every line")
    narrate.add_argument("--workdir", help="where individual line files are cached")
    narrate.add_argument("--force", action="store_true", help="re-synthesise cached lines")
    narrate.set_defaults(func=cmd_narrate)

    sheet = subparsers.add_parser("sheet", help="write a stamped contact sheet for a video")
    sheet.add_argument("--video", required=True)
    sheet.add_argument("--out")
    sheet.add_argument("--frames", type=int, default=12)
    sheet.add_argument("--columns", type=int, default=4)
    sheet.set_defaults(func=cmd_sheet)

    inspect = subparsers.add_parser("inspect", help="duration, size, fps and audio for a file")
    inspect.add_argument("video", nargs="+")
    inspect.set_defaults(func=cmd_inspect)

    ops_cmd = subparsers.add_parser("ops", help="list the available edit operations")
    ops_cmd.set_defaults(func=cmd_ops)

    status = subparsers.add_parser("status", help="show step state and spend for a job")
    status.add_argument("--job", required=True)
    status.add_argument("--run-dir")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(verbose=getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except PipelineError as exc:
        logs.error(str(exc))
        if exc.hint:
            logs.info(f"→ {exc.hint}")
        return exc.exit_code
    except KeyboardInterrupt:
        logs.warn("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
