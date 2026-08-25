# The Gathering 2026 — audio spec

Everything the picture cut needs and cannot produce here: narration, music and
sound design. Timecodes are against the **finished cut** (film 0:00–1:35.700,
event card 1:35.700–1:44.200).

Hand this to a VO artist and a composer, or to an editor conforming a licensed
track. Nothing in it requires re-cutting the picture.

---

## 1. Narration

**Voice.** Adult female, warm and grounded, lower register, natural American,
40–55 in vocal character. Sincere and understated — one woman telling another
something that matters. Not commercial, not breathy, not theatrical, not
preached, not performed as poetry.

**Direction.** Under-play every line. The silences are written into the timings
below and are load-bearing: they are where the picture does the work. If a take
feels slightly too plain in isolation, it is probably right against picture.

Total script: **54 words across 1:44**. That sparseness is deliberate.

| In | Out | Line | Direction |
|---|---|---|---|
| 0:02.5 | 0:06.5 | "The faith we carry was never meant to end with us." | Quiet open. Music and breeze have had 2s alone. Not an announcement. |
| — | — | *silence 0:06.5 – 0:09.5* | The Bible comes into frame. Let it. |
| 0:09.5 | 0:13.5 | "Someone showed us what it looked like to follow Jesus." | Past tense, personal. Slight warmth on "someone". |
| — | — | *silence 0:13.5 – 0:24.5* | Bible detail and field breathe. No VO for 11 seconds. |
| 0:24.5 | 0:25.7 | "Someone prayed." | Reflective. Full stop, not a list item. |
| 0:27.0 | 0:28.2 | "Someone taught." | Same weight. Do not accelerate. |
| 0:29.5 | 0:31.3 | "Someone went before us." | Slightly softer. This one settles. |
| — | — | *silence 0:31.3 – 0:48.5* | Second generation enters. Music carries it. |
| 0:48.5 | 0:49.6 | "And now…" | The turn. Almost thrown away. |
| — | — | *long pause ≈ 2.4s* | **The most important silence in the film.** |
| 0:52.0 | 0:53.4 | "…it's our turn." | Barely above the previous line. No lift, no swell in the read. |
| — | — | *silence 0:53.4 – 1:10.5* | Grandmother, child, Bible. 17 seconds, untouched. |
| 1:10.5 | 1:13.0 | "The story of what God has done…" | Opening out. Still conversational. |
| 1:14.5 | 1:16.5 | "…doesn't end with us." | Land it. Do not push. |
| — | — | *instrumental 1:16.5 – 1:19.0* | |
| 1:19.0 | 1:23.0 | "Future generations will hear about the wonders of the Lord." | Warm and certain. Not a benediction, not a trailer line. Psalm 22:30 — say it like she believes it, not like she is quoting it. |
| — | — | *silence 1:23.0 – end* | Walk-away and card play out with music only. |

**Recording notes.** Record dry, close, no reverb baked in. Leave 1s of room
tone at head and tail. A single alternate read of "…it's our turn." and the
final line is worth having — those two carry the film.

---

## 2. Music

One continuous cue, 0:00 – 1:44.2. It should never announce a section change;
every entrance is a layer added under what is already playing.

| Section | Time | Arrangement |
|---|---|---|
| Entrance | 0:00 – 0:02 | Ambience alone for the first ~1.5s, felt piano enters under it |
| Establish | 0:02 – 0:24 | Felt piano, sparse. Soft atmospheric pad. Nothing else. |
| Older generation | 0:24 – 0:31 | Unchanged. The three "Someone…" lines need space, not support. |
| Second generation | 0:31 – 0:48 | Warm strings enter *underneath*, low and sustained. No new rhythm. |
| The turn | 0:48 – 0:54 | Begin the build at "And now…". Gradual. Nothing lands on the cut. |
| Grandmother + child | 0:54 – 1:09 | Strings fill out. Restrained organic percussion only if it stays felt rather than heard. |
| All generations | 1:09 – 1:23 | **Emotional peak.** Fullest arrangement. Still no drums that read as drums. |
| Resolve | 1:23 – 1:35.7 | Settle back. Strings thin out, piano returns alone under the walk-away. |
| Card | 1:35.7 – 1:44.2 | Sustained pad, resolving to silence by 1:43.5. |

**Not this:** vocals, pop drums, ukulele, corporate-motivational, generic church
promo, trailer drums, over-sentimental piano, worship crescendo.

The test: the music should be removable without the film becoming confusing.
If it is telling the viewer how to feel, it is too much.

**Licensing.** Musicbed, Artlist, Soundstripe or a commissioned cue. Once the
track exists it drops in as one op — see below.

---

## 3. Sound design

Low throughout. This is a field on a summer evening, not a soundscape.

| Time | Cue | Notes |
|---|---|---|
| 0:00 – 1:35.7 | Quiet outdoor ambience bed | Continuous, very low. Distant birds sparingly. |
| 0:00 – 0:24 | Breeze through sunflower leaves | Slightly up during the sunflower close-ups; leaves, not wind. |
| **0:07.6 – 0:09.0** | **Bible opening** | Verified in footage: the cover is lifted at 7.5–8.5 and the pages settle by 9.5. Soft leather, no creak clichés. |
| **0:12.3 – 0:13.2** | **Single page turn** | Verified: a page lifts and is backlit at 12.5. One page, thin paper, close but not loud. |
| 0:31 – 1:23 | Ambience continues | Do not add footsteps, dresses, or child vocalisations — the film has no sync sound and added human sound will read as foley. |
| 1:26 – 1:35.7 | Breeze returns slightly | Under the walk-away, resolving with the music. |
| 1:35.7 – 1:44.2 | Ambience fades out by 1:37 | The card is quiet. |

**Do not** add a whoosh, riser, sub-drop or impact at 0:48. The turn is a cut, a
child's face and a pause. Anything else cheapens it.

---

## 4. Conforming it

Once narration and music exist, they attach to the picture cut without
re-rendering the grade:

```json
{ "op": "replace_audio", "file": "audio/mix.wav", "note": "final mix" }
```

Or, to build the mix here from stems:

```json
{ "op": "replace_audio", "file": "audio/vo.wav" },
{ "op": "music", "file": "audio/music.wav", "gain": 0.18, "duck": true },
{ "op": "loudness", "lufs": -14 }
```

`duck: true` pulls the bed under the narration automatically via sidechain
compression, which is the right default here — the VO must always win.

**Delivery loudness:** −14 LUFS integrated, −1.5 dBTP. That is the streaming
standard and keeps the quiet opening genuinely quiet.
