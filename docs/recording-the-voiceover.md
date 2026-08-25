# Recording the voiceover

You record it, I place it against picture. This is the best outcome available —
a real voice is the one thing no model here can fake.

Read `gathering-2026-audio-spec.md` for the script, the direction for each line
and the timings. This page is only about capture.

---

## What to record with

A phone is genuinely fine. Voice Memos on an iPhone, or Recorder on Android.
A USB mic is better, but the room matters more than the microphone.

**The room.** A small carpeted room with soft furnishings. A walk-in closet
with clothes in it is the best-sounding room in most houses — the hanging
fabric kills reflections. Avoid kitchens, bathrooms, and anywhere with hard
parallel walls. Turn off HVAC, fridge, fans. Close the windows.

**Position.** Six to eight inches from the mic, speaking slightly across it
rather than straight into it — that stops plosives on "prayed" and "passed"
from thumping. If you hold a phone, brace your elbow so it doesn't drift.

**Levels.** Speak at the volume you'd use telling someone something serious
across a table. Not projected, not whispered. If your app shows a meter, aim
for it to sit around two-thirds, never touching the top.

---

## How to record it

**Leave three seconds of silence before you start.** Say nothing, don't move.
That is room tone, and it is the single most useful thing in the file — it lets
the ambience be matched across edits.

Then either:

**Option A — one continuous take** *(simplest)*
Read the lines in order, leaving **a clear two-second pause between each**. The
pauses are how the take gets split, so make them real silences — don't fill
them, don't rustle, don't breathe hard.

**Option B — one file per line** *(more control)*
Record each line separately and name the file after its line id:

```
01-never-end.wav      06-and-now.wav
02-someone.wav        07-our-turn.wav
03-prayed.wav         08-the-story.wav
04-taught.wav         09-doesnt-end.wav
05-went-before.wav    10-future.wav
```

Either way, **do several takes of the whole thing.** Three passes costs ten
minutes and the third is almost always the best — the first is stiff, the
second is careful, the third is when you stop performing.

---

## How to read it

The direction per line is in the audio spec. Three things matter more than the
rest:

**Talk to one person.** Not a congregation, not a camera. Picture one woman
sitting across from you who needs to hear this. That single adjustment does
more for the tone than any technical choice.

**Let the pauses be long.** They will feel far too long while you are reading
them and they will be right on picture. The gap before "…it's our turn" is
about two and a half seconds. Count it.

**Underplay it.** The film is doing the emotional work — the faces, the light,
the child looking up. Your job is to be the quiet voice beside it, not to add
feeling on top. If a take feels slightly too plain in the room, it is probably
the one.

Read past mistakes rather than stopping. An extra take of one line is easy to
use; a stop mid-sentence is not.

---

## Sending it

Anything over ~25 MB will not attach in chat, so put it in the repo:

```bash
cp ~/Desktop/voiceover.m4a uploads/
git add uploads/voiceover.m4a
git commit -m "Add recorded voiceover"
git push
```

Any common format works — wav, m4a, mp3, aiff, flac. **Send the highest quality
your recorder produces.** Don't compress it first; a two-minute wav is about
20 MB and clears the git limit easily.

---

## What happens next

```bash
# One continuous take, split on the pauses:
python -m pipeline narrate --script narration/gathering-2026.json \
  --duration 104.2 --provider recorded --recording uploads/voiceover.m4a \
  --out audio/vo.wav

# Or a folder of per-line files:
python -m pipeline narrate --script narration/gathering-2026.json \
  --duration 104.2 --provider recorded --recording recordings/ \
  --out audio/vo.wav
```

Each line gets a high-pass to lose room rumble, a gentle de-ess, mild
compression for an even read, and loudness normalisation — then it is placed at
its timecode against picture.

Deliberately **no noise reduction**. It costs more in artefacts than it buys on
a decent take, and a genuinely noisy recording is better re-recorded than
repaired. If the room is loud, that is worth another ten minutes.

Overlaps and overruns are reported rather than fixed. If a read runs into the
next line, the answer is a different timecode or another take — never a
time-stretched voice, which is instantly audible.

Then it attaches to the picture without re-rendering the grade:

```json
{ "op": "replace_audio", "file": "audio/vo.wav" }
```
