---
name: vertical-shorts
description: Edit video from plain-language change requests, and build vertical 1080x1920 shorts from an article plus a clip. Applies trims, cuts, speed changes, reframing to 9:16, on-screen text, music beds and audio fixes as a non-destructive edit list, with contact sheets for review. Use when asked to edit, trim, cut, crop, reframe, caption, or fix the audio on a video the user supplies, when they send changes to a video already being worked on, or when asked to run, resume, or debug this repo's shorts pipeline.
---

# Video editing and vertical shorts

Two modes in one repo:

- **`edit`** — the user sends changes in plain language; you turn them into an
  edit list and rebuild the video. This is the everyday loop.
- **`run`** — the full pipeline: one article link plus one highlight clip into a
  finished vertical short.

Start with `edit` unless the request clearly names an article.

---

# Mode 1: editing a video the user sends

## The loop

1. **Find the file.** Two routes in, and the second is the common one:
   - Chat attachments land under `/mnt/user-data/` — but they cap around
     25–30 MB, which most phone video exceeds.
   - Anything larger comes in through `uploads/` in this repo. `git pull`, and
     it is there. Consumer file hosts (Drive, Dropbox, WeTransfer, iCloud) are
     blocked from the session network, and the GitHub REST API is not
     authorized here, so git is the channel that works. See `uploads/README.md`.

   Then run `python -m pipeline inspect <file>` — duration, size, fps, audio.
   Report those back; half of all change requests depend on knowing them.

2. **Give them something to point at.** `python -m pipeline sheet --video <file>`
   writes a stamped contact sheet. Send it. A user who can see `0:14.500` on a
   frame writes a precise request instead of "the bit after the wide shot".

3. **Turn their words into ops.** Keep one edit list per video, next to it:

   ```json
   {
     "source": "/mnt/user-data/clip.mp4",
     "output": "out/clip.mp4",
     "ops": [
       { "op": "cut", "from": "0:00", "to": "0:03.5", "note": "dead air at the top" },
       { "op": "reframe", "aspect": "9:16", "note": "make it vertical" }
     ]
   }
   ```

   Always write the user's own words into `note`. It is the record of *why*
   each op exists, and three rounds later it is the only thing that tells you
   which op to remove when they change their mind.

4. **Rebuild.** `python -m pipeline edit --edits <list>.json --sheet`
   Output is versioned (`clip.v1.mp4`, `clip.v2.mp4`, …) so nothing is lost.

5. **Send back the video and the sheet**, and say what changed: the new
   duration, the delta, and anything you had to interpret.

## Translating requests into ops

`python -m pipeline ops` lists all 19. The common ones:

| They say | You write |
|---|---|
| "cut the first three seconds" | `{"op": "cut", "from": 0, "to": 3}` |
| "just keep 0:10 to 0:25" | `{"op": "trim", "from": "0:10", "to": "0:25"}` |
| "speed up the boring middle" | `{"op": "speed", "from": …, "to": …, "factor": 1.5}` |
| "make it vertical / for TikTok" | `{"op": "reframe", "aspect": "9:16"}` |
| "put the date on screen" | `{"op": "text", "text": "…", "from": …, "to": …}` |
| "add music under it" | `{"op": "music", "file": "…", "gain": 0.18}` |
| "the audio is too quiet / uneven" | `{"op": "loudness", "lufs": -14}` |
| "hold on that frame" | `{"op": "freeze", "at": …, "seconds": 1.5}` |
| "fade it in and out" | `{"op": "fade", "in_s": 0.5, "out_s": 0.8}` |
| "grade it / make it cinematic" | `{"op": "grade", "warmth": 0.02, "vibrance": 0.05}` |
| "this shot doesn't match the others" | `{"op": "grade", "from": …, "to": …, "temperature": 5000}` |
| "put a title over it" | `{"op": "title", "text": "…", "from": …, "to": …}` |

## Narration on a locked cut

`python -m pipeline narrate --script <s>.json --duration <s> --provider local`
builds a track with each line at an absolute timecode. Use `--provider local`
first: it is free and offline, and it answers the question worth answering
early — do the timings work against picture — before anyone pays for a read.
Switch to `elevenlabs` for the delivery voice.

Never put an API key in a script or an edit list. `ELEVENLABS_API_KEY` in the
environment, nowhere else.

## Finishing work

`grade`, `title` and `endcard` are for finishing a locked cut rather than
changing it. Three things matter when using them:

- **Measure before grading.** Sample frames and compare mean R/G/B, black point
  and saturation across the film. Footage is rarely uniform — a shot in shade
  can sit 20+ points blue-of-red against everything else, and matching that one
  shot is worth more than any look applied to all of them. `grade` with
  `from`/`to` does the corrective pass on just that range.
- **Restraint is measurable.** A grade that moves saturation more than ~4 points
  or shifts R-B more than ~10 is a filter, not a grade. Check the numbers, then
  look at skin and whites specifically — numbers cannot tell you that skin has
  gone orange.
- **Justify stabilization and push-ins, or skip them.** Measure camera motion
  with phase correlation first. Under ~3 px/frame at 1280 wide is intentional
  operator movement, and `deshake` will fight it and warp edges. Digital
  push-ins on compressed footage cost real detail. Skipping both and saying why
  is usually the better answer.

## Rules for this loop

- **Never edit in place, and never overwrite the source.** Every rebuild replays
  the list from the original. That is what makes "actually, undo that" free.
- **To undo, delete the op.** Do not add an inverse op — removing the entry is
  the undo, and the cache makes the rebuild cheap.
- **Order matters.** `reframe` then `text` burns text at the final size; the
  reverse crops the text. Put framing ops before graphics ops.
- **Ask when a timecode is genuinely ambiguous**, but only then. "Trim the
  start" with a visible three seconds of black is not ambiguous — cut it, say
  what you did, and let them correct you.
- **Report the duration change every time.** It is how they notice you cut the
  wrong thing.
- The first rebuild of a long list re-encodes everything; later ones reuse
  every unchanged op. If a rebuild is slow, say so rather than going quiet.

---

# Mode 2: the full shorts pipeline

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

## Where things are (pipeline runs)

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
