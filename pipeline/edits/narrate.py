"""Build a narration track: lines placed at absolute timecodes.

Different from the shorts pipeline's `voice` step, which fills phase-shaped
slots. Here the picture is locked and each line has a moment it must land on,
so the track is assembled by laying every line onto a silent bed at its own
offset. Nothing is stretched or nudged to fit — if a read runs into the next
line, that is reported rather than hidden, because the fix is a shorter line or
a different timecode, not a time-warped voice.

Two providers, one interface:

  elevenlabs  the delivery voice. Needs ELEVENLABS_API_KEY in the environment
              and network access to api.elevenlabs.io.
  local       the Kokoro model bundled with the hyperframes CLI. Free, offline,
              and audibly synthetic — a scratch track for judging timing
              against picture before anyone pays for a read.
  recorded    a real human read. Either one file per line, named by line id, or
              a single continuous take split on the pauses between lines.

The recorded path is the one that matters. A human read is the only thing that
sounds like a person, and everything here exists to get one onto the timeline
without a DAW.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError, StepFailed
from ..media.probe import probe
from ..shell import ffmpeg, npx, run
from .timecode import format_tc, parse
from .. import logs

ELEVEN_API = "https://api.elevenlabs.io/v1"
# Published list rate, used only to report what a run will cost.
USD_PER_1K_CHARS = 0.18


@dataclass
class Placed:
    id: str
    text: str
    at: float
    duration: float
    file: Path

    @property
    def end(self) -> float:
        return self.at + self.duration


def load_script(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Narration script not found: {path}")
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    lines = blob.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ConfigError(f"{path.name} needs a non-empty 'lines' array")
    for i, line in enumerate(lines):
        if not isinstance(line, dict) or not str(line.get("text", "")).strip():
            raise ConfigError(f"lines[{i}] has no text")
        parse(line.get("at"), field=f"lines[{i}].at")
    return blob


def _synth_elevenlabs(text: str, dst: Path, cfg: dict[str, Any]) -> Path:
    from ..http import api_key, request  # noqa: PLC0415

    key = api_key("ELEVENLABS_API_KEY", "ElevenLabs")
    voice_id = cfg.get("voice_id")
    if not voice_id:
        raise ConfigError(
            "narration voice_id is not set",
            hint="Pick a voice in the ElevenLabs dashboard and put its id in the script's 'voice' block.",
        )
    payload = {
        "text": text,
        "model_id": cfg.get("model", "eleven_v3"),
        "voice_settings": {
            "stability": float(cfg.get("stability", 0.5)),
            "similarity_boost": float(cfg.get("similarity_boost", 0.75)),
            "style": float(cfg.get("style", 0.2)),
            "speed": float(cfg.get("speed", 0.95)),
            "use_speaker_boost": True,
        },
    }
    audio = request(
        "POST",
        f"{ELEVEN_API}/text-to-speech/{voice_id}?output_format={cfg.get('output_format', 'mp3_44100_128')}",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        json_body=payload,
        timeout=int(cfg.get("timeout_s", 180)),
        expect_json=False,
    )
    if not audio or len(audio) < 1024:
        raise StepFailed(f"ElevenLabs returned {len(audio or b'')} bytes for {dst.name}")
    dst.write_bytes(audio)
    return dst


def _synth_local(text: str, dst: Path, cfg: dict[str, Any], version: str) -> Path:
    run([
        npx(), "--yes", f"hyperframes@{version}", "tts", text,
        "-o", str(dst), "-v", cfg.get("local_voice", "af_heart"),
        "-s", str(float(cfg.get("speed", 0.95))), "--json",
    ], timeout=900)
    if not dst.exists() or dst.stat().st_size < 1024:
        raise StepFailed(f"local tts produced no audio for {dst.name}")
    return dst


def synthesize(
    script: dict[str, Any], work_dir: Path, *, provider: str, version: str, force: bool = False,
) -> list[Placed]:
    cfg = dict(script.get("voice", {}))
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if provider == "local" else ".mp3"

    total_chars = sum(len(str(l["text"])) for l in script["lines"])
    if provider == "elevenlabs":
        logs.cost(
            f"{total_chars} characters ≈ ${total_chars / 1000 * USD_PER_1K_CHARS:.2f}",
            lines=len(script["lines"]),
        )

    placed: list[Placed] = []
    for index, line in enumerate(script["lines"]):
        line_id = str(line.get("id") or f"{index:02d}")
        text = " ".join(str(line["text"]).split())
        dst = work_dir / f"{provider}_{line_id}{suffix}"
        if dst.exists() and not force:
            logs.info(f"  [{index + 1}/{len(script['lines'])}] {line_id}: cached")
        else:
            logs.info(f"  [{index + 1}/{len(script['lines'])}] {line_id}: {text[:52]!r}")
            if provider == "elevenlabs":
                _synth_elevenlabs(text, dst, cfg)
            else:
                _synth_local(text, dst, cfg, version)
        placed.append(Placed(
            id=line_id, text=text,
            at=float(parse(line.get("at"), field="at") or 0.0),
            duration=probe(dst).duration_s, file=dst,
        ))
    return placed




# --------------------------------------------------------------------------
# recorded narration
# --------------------------------------------------------------------------

# Cleanup applied to a human recording before it hits the timeline. Deliberately
# conservative: a high-pass to lose room rumble and mic handling, a gentle
# de-ess, mild compression for an even read, and loudness normalisation. No
# noise reduction — it costs more in artefacts than it buys on a decent take,
# and a genuinely noisy take should be re-recorded rather than repaired.
CLEANUP = (
    "aresample=48000,"
    # Rumble, handling noise and HVAC all live below this.
    "highpass=f=75,"
    # Phone mics up close add boxiness around 300Hz; a little chest at 150 puts
    # back the weight that removing it costs, without waking the room tone
    # (which sits loudest in the 150-400 band on a domestic recording).
    "equalizer=f=300:t=q:w=1.4:g=-1.8,"
    "equalizer=f=150:t=q:w=1.2:g=1.0,"
    "deesser=i=0.35,"
    "equalizer=f=4500:t=q:w=1.8:g=1.5,"
    # Two gentle stages in series rather than one hard one. The first catches
    # peaks, the second levels the read. That is what produces an even,
    # unhurried delivery without the pumping a single aggressive stage gives.
    "acompressor=threshold=0.10:ratio=2.2:attack=18:release=250:knee=6,"
    "acompressor=threshold=0.20:ratio=1.8:attack=40:release=400:knee=8,"
    "alimiter=limit=0.95:attack=5:release=60,"
    "loudnorm=I={lufs}:TP=-2:LRA=7"
)

# Deliberately no reverb. Narration for a documentary sits close and dry;
# added space is the single most common tell of an amateur voice track.


def clean(src: Path, dst: Path, *, lufs: float = -17.0) -> Path:
    """Normalise a recorded line so every read sits at the same level."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-af", CLEANUP.format(lufs=lufs), "-c:a", "pcm_s16le", str(dst),
    ], timeout=900)
    return dst


def detect_speech(
    take: Path, *, noise_db: float = -34.0, min_silence_s: float = 0.45,
    pad_s: float = 0.12,
) -> list[tuple[float, float]]:
    """Find the spoken stretches in a continuous take.

    Works off ffmpeg's silencedetect: the gaps are reported, and the speech is
    what lies between them. A little padding is added at each end so the split
    never clips a soft consonant at the start or the tail of a word.
    """
    proc = run([
        ffmpeg(), "-hide_banner", "-nostats", "-i", str(take),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
        "-f", "null", "-",
    ], timeout=900, check=False)
    output = (proc.stderr or "") + (proc.stdout or "")

    total = probe(take).duration_s
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in output.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
            except (IndexError, ValueError):
                continue
            silences.append((start, end))
            start = None
    if start is not None:
        silences.append((start, total))

    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for sil_start, sil_end in silences:
        if sil_start - cursor > 0.12:
            speech.append((max(0.0, cursor - pad_s), min(total, sil_start + pad_s)))
        cursor = sil_end
    if total - cursor > 0.12:
        speech.append((max(0.0, cursor - pad_s), total))
    return speech


def split_take(
    take: Path, script: dict[str, Any], out_dir: Path, **kwargs: Any,
) -> tuple[list[Path], list[str]]:
    """Cut a continuous take into one file per line, in script order.

    Returns the files and any problems. A count mismatch is reported rather
    than guessed at — silently pairing eleven detected segments with ten lines
    would put every line after the extra one on the wrong picture.
    """
    lines = script["lines"]
    segments = detect_speech(take, **kwargs)
    problems: list[str] = []
    if len(segments) != len(lines):
        problems.append(
            f"found {len(segments)} spoken segments but the script has {len(lines)} lines"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for index, (start, end) in enumerate(segments):
        line_id = str(lines[index].get("id") or f"{index:02d}") if index < len(lines) else f"extra_{index:02d}"
        dst = out_dir / f"recorded_{line_id}.wav"
        run([
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(take), "-t", f"{end - start:.3f}",
            "-c:a", "pcm_s16le", str(dst),
        ], timeout=600)
        files.append(dst)
    return files, problems


def collect_recorded(
    script: dict[str, Any], source: Path, work_dir: Path, *, lufs: float = -17.0,
) -> tuple[list[Placed], list[str]]:
    """Gather a human read, from either a directory of lines or one take."""
    work_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    if source.is_dir():
        raw: list[Path] = []
        for index, line in enumerate(script["lines"]):
            line_id = str(line.get("id") or f"{index:02d}")
            found = sorted(
                p for p in source.iterdir()
                if p.is_file() and line_id in p.stem
                and p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aiff", ".aif", ".flac")
            )
            if not found:
                raise ConfigError(
                    f"No recording found for line '{line_id}' in {source}",
                    hint="Name each file after its line id, e.g. 03-prayed.wav.",
                )
            if len(found) > 1:
                problems.append(f"{line_id}: {len(found)} files match; using {found[0].name}")
            raw.append(found[0])
    else:
        raw, problems = split_take(take=source, script=script, out_dir=work_dir / "split")

    placed: list[Placed] = []
    for index, line in enumerate(script["lines"]):
        if index >= len(raw):
            break
        line_id = str(line.get("id") or f"{index:02d}")
        cleaned = clean(raw[index], work_dir / f"clean_{line_id}.wav", lufs=lufs)
        placed.append(Placed(
            id=line_id, text=" ".join(str(line["text"]).split()),
            at=float(parse(line.get("at"), field="at") or 0.0),
            duration=probe(cleaned).duration_s, file=cleaned,
        ))
    return placed, problems


def assemble(placed: list[Placed], out: Path, duration: float, *, gain: float = 1.0) -> Path:
    """Lay every line onto a silent bed at its own offset.

    adelay positions each read; amix sums them. Normalisation is off so a
    moment where two lines overlap stays audibly wrong instead of being
    quietly attenuated into looking fine.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={duration:.3f}"]
    for entry in placed:
        args += ["-i", str(entry.file)]

    parts = ["[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[bed]"]
    labels = ["[bed]"]
    for i, entry in enumerate(placed, start=1):
        delay_ms = int(round(entry.at * 1000))
        parts.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},volume={gain:.3f}[v{i}]"
        )
        labels.append(f"[v{i}]")
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[out]"
    )

    args += ["-filter_complex", ";".join(parts), "-map", "[out]",
             "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(out)]
    run(args, timeout=1800)
    return out


def report(placed: list[Placed], duration: float) -> list[str]:
    """Overlaps and overruns — the two ways a timed VO track goes wrong."""
    problems: list[str] = []
    for a, b in zip(placed, placed[1:]):
        if a.end > b.at + 0.02:
            problems.append(
                f"{a.id} runs to {format_tc(a.end, millis=True)} but {b.id} starts at "
                f"{format_tc(b.at, millis=True)} — {a.end - b.at:.2f}s overlap"
            )
    for entry in placed:
        if entry.end > duration + 0.02:
            problems.append(
                f"{entry.id} ends at {format_tc(entry.end, millis=True)}, past the "
                f"{format_tc(duration, millis=True)} cut"
            )
    return problems
