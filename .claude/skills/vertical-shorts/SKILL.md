---
name: vertical-shorts
description: Build a vertical 1080x1920 short from a source article and a highlight clip — scrapes the article with headless Chrome, pulls b-roll from YouTube, auto-reframes to 9:16, generates an AI presenter, voices the script, and renders a HyperFrames composition. Use when asked to turn an article plus a clip into a short, reel, or TikTok/Shorts video, or when asked to run, resume, or debug this repo's video pipeline.
---

# Vertical shorts pipeline

Turns **one article link plus one highlight clip** into a finished vertical
video. The pipeline does the deterministic work; you do exactly one creative
step — writing the voiceover — and supervise the rest.

## The one thing you write

Everything except the script is code. Step `script` stops the run and hands you
a brief. That is the handoff, and it is the only place your judgement enters the
render.

## Run it

```bash
python -m pipeline doctor --job jobs/<name>.job.json     # check tools and keys first
python -m pipeline run    --job jobs/<name>.job.json     # resumable; safe to re-run
python -m pipeline status --job jobs/<name>.job.json     # step state and spend
```

Scaffold a job from a clip and an article:

```bash
python -m pipeline new --clip input/highlight.mp4 \
  --article https://example.com/report --reference input/coach.png
```

## Working the pipeline

1. **Always run `doctor` first.** It checks binaries, Python packages, API keys
   and voice ids. Two steps spend real money; a preflight failure is cheaper
   than a half-finished run.

2. **Run it.** Steps are `article → broll → reframe → script → voice → avatar →
   identity → transcribe → compose → render`. Each records what it ran under, so
   re-running skips work whose inputs have not changed.

3. **Exit code 20 means the script is your turn.** The run stops at `script` and
   writes `runs/<job>/script/script_request.json`. Read it — it carries the
   article text, each phase's brief, and a per-phase word budget — then write
   `runs/<job>/script/script.json`:

   ```json
   { "lines": [ { "phase": "hook", "voice": "narrator", "text": "..." } ] }
   ```

   Then re-run the same command. It resumes at `script`.

   Rules the validator enforces, so get them right the first time:
   - one line per phase, using the phase ids from the request;
   - word count inside each phase's `min_words`/`max_words` — that is what keeps
     the VO inside its slot, and it is checked *before* anything is billed;
   - only facts present in `article.text`. No invented names, scores, dates or
     quotes;
   - no banned phrases, no markdown, no stage directions, no emoji.

   Write for the ear: short clauses, one idea per sentence. The narrator states
   what happened; the coach explains why it worked.

4. **Read the warnings.** They are the interesting output:
   - `VO runs Ns past its slot` — the line was too long and got cut. Shorten it
     or raise that phase's `target_s`.
   - `faces in N% of sampled frames` under ~12% — the reframe fell back to
     letterbox. Expected on wide stadium shots and graphics.
   - `caption timings are estimated` — whisper was unavailable, so timings were
     derived from the script. Usable, not exact.
   - identity `N/M frames passed` — the generated presenter drifted off the
     reference.

5. **Report what you built**: the output path, its duration, the spend, and any
   warning above. Don't claim captions are transcribed when they were estimated.

## When something fails

| Exit | Meaning | What to do |
|-----|---------|-----------|
| 2 | Bad job file | The message names the parameter. Fix and re-run. |
| 3 | Missing tool or credential | Run `doctor`; install what it names. |
| 4 | A step failed | Read the error; it carries the real stderr. |
| 5 | Budget cap | Raise `budget.max_usd_per_run`, or shorten the generated segment. Never raise a cap without saying so. |
| 6 | Quality gate | Lint error, identity drift, or the render did not match the composition. Fix the cause; do not bypass. |
| 20 | Script needed | Write `script.json` (above) and re-run. |

Useful flags: `--from <step>` resume, `--only <step>` run one, `--force` re-run
a fresh step, `--dry-run` show the plan, `--set key=value` override any
parameter (e.g. `--set output.fps=60`).

## Iterating without spending

Set `voice.provider` to `local` to speak lines with the model bundled in the
HyperFrames CLI instead of ElevenLabs. Free and offline — the right way to
iterate on timing, captions and layout. Switch back to `elevenlabs` for the
take you ship.

`--only compose render` re-cuts and re-renders from assets already on disk, so
layout changes cost nothing.

## Rules that are not negotiable

- **Never edit `runs/<job>/compose/project/index.html` by hand.** It is
  generated; the next compose overwrites it. Change the generator or the job
  parameters instead.
- **Never raise a budget cap to get past exit code 5 without telling the user.**
- **Never bypass a quality gate** by disabling lint or lowering
  `identity.min_pass_ratio` just to make a run finish. Fix the cause, or report
  it.
- **Never put an API key in a job file.** Keys are read from the environment:
  `ELEVENLABS_API_KEY`, `KIE_API_KEY`.
- **Check `estimated` on captions before claiming accurate timings.**

## Where things are

```
runs/<job-id>/
  state.json              per-step status, fingerprints, outputs
  article/                article.json, phrases.json, article.png
  reframe/                *_9x16.mp4 + reframe.json (strategy, detection rate)
  script/                 script_request.json  ← read;  script.json  ← you write
  voice/                  one mp3 per phase
  avatar/                 the generated presenter + its cost record
  identity/               per-frame verdicts
  compose/project/        index.html, assets/, transcript.json
  render/                 render.mp4 and the full render log
budget.json               the spend ledger, enforced before every paid call
```

Design notes, parameter reference and the failure modes worth knowing are in
`README.md` and `docs/pipeline.md`.
