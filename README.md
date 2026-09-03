# Video editing pipeline

Two ways in.

## 1. Edit a video from change requests

Put the video in `uploads/` and push it (see `uploads/README.md` — chat
attachments cap out around 25–30 MB, below most phone video). Then say what you
want changed. Each change becomes an entry in an
edit list; the video is rebuilt from the original every time.

```bash
python -m pipeline inspect clip.mp4                    # what is this file
python -m pipeline sheet --video clip.mp4              # stamped frames to point at
python -m pipeline edit --edits clip.edits.json --sheet
```

```json
{
  "source": "clip.mp4",
  "output": "out/clip.mp4",
  "ops": [
    { "op": "cut",     "from": "0:00", "to": "0:03.5", "note": "dead air at the top" },
    { "op": "reframe", "aspect": "9:16",               "note": "make it vertical" },
    { "op": "text",    "text": "88th minute", "from": "0:01", "to": "0:04" },
    { "op": "music",   "file": "bed.mp3", "gain": 0.18 },
    { "op": "loudness", "lufs": -14 }
  ]
}
```

Twenty-two operations — `python -m pipeline ops` lists them:
`trim, cut, speed, freeze, append, reframe, crop, scale, rotate, color, grade,
stabilize, fade, text, title, subtitles, endcard, volume, mute, replace_audio,
music, loudness`.

Three of those are finishing rather than editing:

- **`grade`** — a restrained golden-hour film grade: gentle S-curve on a lifted
  toe, greens and cyans desaturated while skin is left alone, warmth in the
  highlights only so shadows never go teal, highlight-only bloom, temporal
  grain. Give it a `from`/`to` and the corrective white balance applies to just
  that range, which is how one shot lit differently from the rest gets matched
  to it.
- **`title`** — editorial serif type with wide tracking and a slow symmetric
  fade. Not `text`: no box, no label, meant to sit over picture as a beat.
- **`endcard`** — an event reveal built on a frame lifted from the film itself,
  blurred past recognition and dropped in level, with staggered line entrances.

Three properties make this survivable over many rounds:

- **Non-destructive.** Every rebuild replays the list against the untouched
  original. Undo is *deleting the op*, not applying an inverse.
- **Versioned.** Output lands as `clip.v1.mp4`, `clip.v2.mp4`, … so going back
  two rounds is picking a file.
- **Incremental.** Each intermediate is cached under a hash of the source plus
  every op up to it. Changing the last op in a seven-op list re-encodes one
  pass, not seven — 4s instead of 40s, measured.

`--sheet` writes a grid of timecode-stamped frames, which is how a review turns
into a precise request instead of a round trip.

## 2. Build a short from an article

Give it an article link and a highlight clip. It returns a finished
**1080×1920** short: the clip reframed to vertical with a tracked crop, b-roll
cut in from YouTube, an AI presenter delivering analysis, voiced narration,
word-level captions, and animated markers drawn over the source article at the
exact pixels the browser laid the text out at.

```bash
python -m pipeline new --clip input/highlight.mp4 --article https://example.com/report
python -m pipeline doctor --job jobs/highlight.job.json
python -m pipeline run    --job jobs/highlight.job.json
```

The theme is an example. Nothing in the pipeline knows about sport — the
article supplies the facts, and the job file supplies the shape.

---

## What runs, in order

| Step | Does | Costs |
|------|------|-------|
| `article` | Loads the page in headless Chrome, extracts the body, and measures a `DOMRect` for every candidate marker phrase. Captures a full-page screenshot. | — |
| `broll` | Searches YouTube with yt-dlp, screens candidates *before* downloading, cuts a segment from the interior of each, concatenates. | — |
| `reframe` | Haar face-track → smoothed camera path → moving crop, 16:9 to 9:16. | — |
| `script` | Emits a brief and stops. Claude writes the lines. Validated against per-phase word budgets. | — |
| `voice` | One ElevenLabs request per phase, loudness-normalised and padded to its slot. | **$** |
| `avatar` | KIE.ai `bytedance/seedance-2` v2v, capped and cached. | **$$** |
| `identity` | Samples frames and asks a local VLM whether the presenter still matches the reference, and whether b-roll shows what it claimed. | — |
| `transcribe` | `hyperframes transcribe`, Whisper `small.en`, word-level timings. | — |
| `compose` | Assembles the HyperFrames HTML: beds, VO, captions, markers, GSAP timeline. | — |
| `render` | Lints, renders, and **verifies the result against the composition**. | — |

Every step writes into `runs/<job-id>/<step>/` and records the configuration it
ran under. Re-running skips whatever has not changed — which matters, because
two of these steps bill.

---

## The job file is the whole interface

One JSON file names the video and the parameters; everything unset falls back to
a documented default. A minimal one:

```json
{
  "id": "keeper-hesitation",
  "input": {
    "highlight_clip": "../input/highlight.mp4",
    "article_url": "https://example.com/report",
    "coach_reference_image": "../input/coach.png"
  },
  "voice": {
    "narrator_voice_id": "...",
    "coach_voice_id": "..."
  }
}
```

See `jobs/example.job.json` for the annotated version and
`pipeline/config.py` (`DEFAULTS`) for every parameter with its default.

- Paths resolve against the job file's own directory.
- `"//"` keys are treated as comments and ignored.
- **Unknown keys are errors.** A typo in a parameter file is otherwise invisible
  until the render looks wrong.
- API keys are never read from here — only from `ELEVENLABS_API_KEY` and
  `KIE_API_KEY` in the environment.

Override anything from the command line: `--set output.fps=60`,
`--set broll.enabled=false`.

### Phases

The four-phase arc is data, not code. Add, drop or reorder phases and every
downstream step follows.

```json
{ "id": "analysis", "voice": "coach", "target_s": 22, "bed": "avatar",
  "markers": false, "brief": "The coach's tactical read." }
```

`bed` is what fills the frame (`highlight`, `broll`, `avatar`, `black`),
`voice` picks the speaker, `target_s` sets the word budget the script must hit,
and `brief` is the instruction Claude writes to.

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium          # headless Chrome for the scrape
```

Also needed: **ffmpeg with libx264 and aac**, and **Node ≥ 22** (HyperFrames runs
through `npx`). `python -m pipeline doctor` verifies all of it, and it verifies
that ffmpeg can *actually encode* rather than merely existing — stripped bundles
resolve on `PATH` and fail at render time.

```bash
export ELEVENLABS_API_KEY=...
export KIE_API_KEY=...
```

### Iterating without spending

Set `voice.provider` to `local` and lines are spoken by the Kokoro model bundled
with the HyperFrames CLI (`pip install kokoro-onnx soundfile`). Free, offline,
and good enough to judge timing and layout. Switch back to `elevenlabs` for the
take you ship.

---

## Things worth knowing

**The composition vendors GSAP locally, on purpose.** The HyperFrames templates
load it from a CDN. Inside the renderer, a blocked CDN means `gsap` is null, no
timeline registers, every animated overlay stays at its resting opacity of 0 —
and the render *still exits 0*, producing a clean-looking video with no captions
and no markers on it. So GSAP is fetched once into `~/.cache/vertical-shorts`,
copied into the project, and the render step fails the run if the log reports
`sub_timeline_script_failure` anyway.

**The budget is enforced before the call, not after the invoice.** `budget.json`
is an append-only ledger with a per-run and a lifetime cap. Spend is *reserved*
under an exclusive lock before the request goes out and settled at the real cost
after, so a process that dies mid-generation leaves a pending charge rather than
freeing budget for an infinite retry loop.

**The camera path is monotone cubic, not a plain spline.** Detections are
gap-filled, deadzoned, median-decimated to keyframes, interpolated with a
Fritsch–Carlson Hermite spline, then rate-limited. The shape-preserving
interpolation is the point: a plain cubic overshoots at direction changes, which
on a camera path means swinging past the subject and coming back.

**Phrase selection is Python; measurement is the browser.** The page hands back
one flat string plus paragraph ranges; phrase choice happens in ordinary
testable code; the chosen character offsets go back into the *same* DOM walk to
become `DOMRect`s. That is what lets a marker sit exactly on the phrase instead
of near it.

**Each VO track is transcribed in its own directory.** `hyperframes transcribe`
patches every HTML file in the directory it is given with the transcript it just
produced — four tracks in one project would leave only the last. Merging onto
the master timeline happens in the pipeline, where phase offsets are known.

**Captions degrade rather than disappear.** If Whisper is unavailable, timings
are estimated from the known script and the measured audio length, weighted by
word length with pauses at sentence ends. The run reports `estimated` so nobody
mistakes it for a transcript.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 2 | Bad job file — the message names the parameter |
| 3 | Missing binary, package or credential |
| 4 | A step failed |
| 5 | A paid call would exceed the budget cap |
| 6 | Quality gate: lint error, identity drift, or output/composition mismatch |
| 20 | The voiceover script needs writing — see `runs/<job>/script/script_request.json` |

---

## Tests

```bash
python -m pytest -q
```

167 tests over the logic worth trusting: the camera path, cue grouping, marker
geometry, phrase offsets, budget accounting, config validation, timecode
parsing, edit-list replay and caching, and the HyperFrames HTML contract (every
timed element is a clip, videos are muted with sibling audio, fades have hard
kills, the composition is deterministic).

---

## Rights

`broll` downloads from YouTube. Downloading may conflict with the source's terms
of service, and the footage stays under its own copyright — clearing rights for
whatever you publish is on you. `broll.only_creative_commons` restricts the
search to CC-BY uploads; `broll.enabled: false` turns the step off entirely.
