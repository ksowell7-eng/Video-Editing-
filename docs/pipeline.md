# Design notes

Reference for the decisions that are not obvious from the code, and the failure
modes that cost a run to discover.

## The HyperFrames contract

The composition generator (`pipeline/compose/html.py`) targets what the CLI
enforces, verified against `hyperframes@0.8.13`:

| Rule | Why it bites |
|------|--------------|
| Root carries `data-composition-id`, `data-width`, `data-height` | Without it the project is not a composition at all |
| Timed elements carry `data-start`, `data-duration`, `data-track-index` **and** `class="clip"` | The runtime uses `clip` for visibility; without it the element is always on |
| `<video>` is `muted`, with a sibling `<audio>` for sound | Declaring `data-has-audio` on a muted element is a lint error |
| Timelines are `paused` and registered on `window.__timelines[id]` | The renderer drives time; an unpaused timeline plays itself |
| Only `opacity, x, y, scale, scaleX, scaleY, rotation, width, height, visibility` animate | No colour tweens — karaoke cross-fades a bright duplicate over a dim base instead |
| No `Date.now`, `Math.random`, or network fetches | Frames render out of order and in parallel; anything non-deterministic differs between them |
| Full-frame overlays must rest at `opacity: 0` | A frame can be rendered before the reveal tween; the overlay covers everything |
| Every fade-out needs a matching `tl.set(..., {opacity: 0})` | Non-linear seeking can land after a fade without having played it, leaving the element visible |

Track indices are fixed so z-order is predictable:

```
0  beds        highlight / b-roll / avatar
1  overlays    article screenshot, markers
2  captions
3  voiceover   (audio)
4  music bed   (audio)
```

## Silent failures this pipeline guards against

These all produce a file and a zero exit code. They are the reason the render
step verifies rather than trusts.

1. **CDN GSAP blocked in the renderer.** No timeline registers, overlays stay at
   opacity 0, video renders with no captions and no markers. Guarded by
   vendoring GSAP locally *and* failing on `sub_timeline_script_failure` in the
   render log.
2. **A missing local asset.** The renderer skips it and carries on. Guarded by
   lint (`missing_local_asset`, `audio_src_not_found`) run before every render.
3. **A composition that does not match its output.** Guarded by probing the
   rendered file for size, duration and the presence of an audio track.
4. **Estimated caption timings mistaken for transcribed ones.** The transcribe
   step publishes `estimated: true`, and the run warns.

## Money

Two steps bill. `pipeline/budget.py` is the only thing that may authorise them.

- `reserve()` takes an exclusive `flock`, re-reads the ledger, checks the
  per-run and lifetime caps, and appends a **pending** entry *before* the
  request is made.
- `settle(actual)` rewrites that entry with the real cost.
- `release()` zeroes it when the provider never delivered.
- A process killed between reserve and settle leaves the pending charge in
  place. That is deliberate: the alternative is a crash loop that re-bills
  forever because the ledger looks empty.

The avatar step additionally caches on a hash of every input that changes the
generated video (model, prompt, seed, duration, reference image bytes). Re-runs
after a caption tweak re-use the take instead of buying another.

## The reframe

```
detections ─ fill_gaps ─ deadzone ─ median keyframes ─ PCHIP ─ rate limit ─ clamp
```

- **Gap fill** — a lost face must not snap the crop to centre and back.
- **Deadzone** — hold until the subject really moves; small motion reads as
  jitter, a held frame reads as intent.
- **Median keyframes** — one anchor per interval, median-filtered, which is what
  rejects a single false positive in the crowd.
- **PCHIP** — monotone cubic Hermite. Shape-preserving, so the path cannot
  overshoot an anchor. A natural cubic spline would swing past the subject at
  every direction change.
- **Rate limit** — forward and backward sweeps, averaged, so the limiter does
  not bias the path late.

Applied with ffmpeg `sendcmd` driving `crop x/y` per frame, rather than a crop
expression: a few hundred keyframes in one expression hits parser limits, and a
sendcmd script is readable when a render looks wrong.

Below ~12% detection coverage the clip falls back to letterbox. Forcing a crop
from a handful of false positives is worse than not cropping.

## The article scrape

Both entry points in `pipeline/scrape/article.js` walk the DOM with the same
filtered `TreeWalker` and build the same flat string. `extract()` returns that
string plus paragraph ranges; Python picks character spans; `measure()` turns
those spans back into `DOMRect`s through the identical walk.

Splitting it this way keeps phrase selection in ordinary testable code while the
measurement stays where the real layout is. `tests/test_phrases.py` asserts the
contract that makes it work: `text[span.start:span.end] == span.text`.

A phrase that wraps produces one rect per line. The marker takes the first line
— a union box would draw a rectangle around whitespace — and records `wrapped`
so selection can prefer phrases that do not.

## Extending it

- **A different subject.** Change nothing but the job file. No step knows the
  topic; queries come from article keywords and briefs come from the phases.
- **A different arc.** Edit `phases`. Word budgets, VO tracks, beds, markers and
  the timeline all follow.
- **A different presenter provider.** `pipeline/steps/avatar.py` is one
  submit/poll/download pair. Keep the reserve/settle calls around it.
- **A different marker style.** `markers.style` is `underline`, `box` or
  `circle`; the CSS for each is in `_css()` in `compose/html.py`.


## The edit loop

`pipeline/edits/` exists for a different shape of work than the pipeline: the
user sends a change, the video is rebuilt, repeat. Over many rounds, three
things matter more than throughput.

**Replay, don't mutate.** Every rebuild starts from the original source and
replays the whole op list. The consequence worth having is that *undo is
deletion* — remove the op and the change is gone. Applying an inverse edit
(re-adding three seconds you cut) does not restore the original frames and
compounds error with every round.

**Cache by prefix.** Each intermediate is stored under a hash of the source
fingerprint plus every op up to that point. Because change requests almost
always append to or tweak the tail of the list, the common case reuses the
whole prefix. Measured on a seven-op list: an unchanged replay is 0.7s and 0
encodes; changing only the last op is 4s and 1 encode; a cold build is ~40s.

**Version the output.** `clip.v1.mp4`, `clip.v2.mp4`, … Rolling back two rounds
is picking a file rather than reconstructing state.

### Op ordering

Ops are passes, applied in list order, so order is meaningful:

- framing (`reframe`, `crop`, `scale`, `rotate`) before graphics (`text`,
  `subtitles`) — otherwise text is burned in at the wrong size and then cropped;
- `speed` before anything with fixed timecodes, since retiming moves them;
- `loudness` last among audio ops, so it measures the final mix.

### Contact sheets

The review artifact. Frames sampled across the clip, each stamped with its
timecode, tiled into one image. It converts "the bit after the wide shot" into
"0:14.500", which is the difference between one round trip and three.

The stamp goes through the same drawtext escaping as the `text` op — an
unescaped colon terminates drawtext's option parsing and every label silently
truncates to `0`.
