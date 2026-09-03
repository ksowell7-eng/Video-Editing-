"""Build the Bizzy Bee Foot Detox promo — 9:16, ~26s."""
import subprocess, shlex
from pathlib import Path

ROOT = Path("/home/user/Video-Editing-")
F = ROOT / "assets/fonts"
ANTON, SCRIPT, PLAY = f"{F}/Anton-Regular.ttf", f"{F}/KaushanScript-Regular.ttf", f"{F}/PlayfairDisplay-400.ttf"
GOLD, IVORY = "#F2B417", "#FFF8EA"
W, H, FPS, XF = 1080, 1920, 30, 0.4
SEG = ROOT / "bizzybee/segments"; SEG.mkdir(parents=True, exist_ok=True)

def esc(t):
    return "'" + t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%") + "'"

def track(t, lvl):
    return {0: "", 1: " ", 2: " "}.get(lvl, "").join(t) if lvl else t

def alpha(a, b, f=0.45):
    """Fade in at a, hold, fade out ending at b — segment-relative."""
    return (f"if(lt(t,{a:.2f}),0,if(lt(t,{a+f:.2f}),(t-{a:.2f})/{f},"
            f"if(lt(t,{b-f:.2f}),1,if(lt(t,{b:.2f}),({b:.2f}-t)/{f},0))))")

def text(s, font, size, y, color=IVORY, lvl=0, a=0.5, b=3.1):
    return (f"drawtext=fontfile={font}:text={esc(track(s, lvl))}:x=(w-text_w)/2"
            f":y=h*{y}-text_h/2:fontsize={size}:fontcolor={color}:alpha='{alpha(a,b)}'")

SCRIMS = ROOT / "bizzybee/scrims"

# A single gentle warm grade over everything, so the phone footage and the
# generated footage read as one shoot rather than two sources.
GRADE = ("eq=contrast=1.06:saturation=1.05:gamma=1.02,"
         "colorbalance=rh=0.03:bh=-0.03:rm=0.012:bm=-0.012,"
         "noise=alls=2:allf=t+u")

def fit(src_is_small=False):
    """Cover the 9:16 frame. Small sources get sharpening to recover some bite."""
    base = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H},setsar=1")
    return base + (",unsharp=5:5:0.9:5:5:0.0" if src_is_small else "")

segments = [
    # (name, source, in, duration, speed, small?, scrim, [text layers])
    ("s1", "gen/a_water.mp4", 0.15, 3.6, 1.0, False, "vignette.png", [
        text("Bizzy Bee", SCRIPT, 168, 0.40, IVORY, 0, 0.5, 3.2),
        text("FOOT DETOX", ANTON, 150, 0.525, GOLD, 1, 0.75, 3.2),
    ]),
    ("s2", "gen/b_stilllife.mp4", 0.2, 3.6, 1.0, False, "bottom.png", [
        text("RELAX", ANTON, 118, 0.71, IVORY, 2, 0.4, 3.2),
        text("REFRESH  •  RECHARGE", ANTON, 70, 0.80, GOLD, 1, 0.6, 3.2),
    ]),
    ("s3", "real/clip2.mov", 0.1, 3.0, 0.82, True, "bottom.png", [
        text("30-MINUTE", ANTON, 126, 0.71, GOLD, 1, 0.35, 2.6),
        text("FOOT SOAK EXPERIENCE", ANTON, 62, 0.795, IVORY, 1, 0.5, 2.6),
    ]),
    ("s4", "gen/e_honeycomb.mp4", 0.1, 3.8, 1.0, False, "radial.png", [
        text("ONLY", ANTON, 92, 0.375, IVORY, 2, 0.3, 3.4),
        text("$40", ANTON, 380, 0.52, GOLD, 0, 0.45, 3.4),
        text("30-MINUTE SESSION", ANTON, 56, 0.655, IVORY, 2, 0.75, 3.4),
    ]),
    ("s5", "gen/c_hands.mp4", 0.15, 3.6, 1.0, False, "bottom.png", [
        text("TAKE 30 MINUTES", ANTON, 88, 0.70, IVORY, 1, 0.4, 3.2),
        text("for YOU", SCRIPT, 126, 0.805, GOLD, 0, 0.6, 3.2),
    ]),
    ("s6", "gen/d_client.mp4", 0.15, 3.6, 1.0, False, "bottom.png", [
        text("Sit Back.  Relax.", PLAY, 76, 0.735, IVORY, 0, 0.4, 3.2),
        text("Enjoy the Experience.", PLAY, 76, 0.815, GOLD, 0, 0.6, 3.2),
    ]),
]

for name, src, ss, dur, speed, small, scrim_png, layers in segments:
    base = [fit(small)]
    if speed != 1.0:
        base.append(f"setpts={1/speed:.4f}*PTS")
    base.append(GRADE)
    fi, fo = 0.35, 0.45
    graph = (
        f"[0:v]{','.join(base)}[base];"
        f"[1:v]scale={W}:{H},format=rgba,"
        f"fade=t=in:st=0.15:d={fi}:alpha=1,"
        f"fade=t=out:st={dur-fo-0.1:.2f}:d={fo}:alpha=1[sc];"
        f"[base][sc]overlay=0:0:shortest=1[bg];"
        f"[bg]{','.join(layers)}[v]"
    )
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{ss}", "-i", str(ROOT / "bizzybee" / src),
        "-loop", "1", "-i", str(SCRIMS / scrim_png),
        "-filter_complex", graph, "-map", "[v]",
        "-t", f"{dur}", "-an",
        "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", str(SEG / f"{name}.mp4"),
    ], check=True)
    print("segment", name, f"{dur}s")

# End card — the honeycomb, dimmed and slowed, under the contact block.
END = 7.0
end_layers = [
    "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.74:t=fill",
    text("Bizzy Bee", SCRIPT, 150, 0.235, IVORY, 0, 0.3, END),
    text("FOOT DETOX", ANTON, 132, 0.345, GOLD, 1, 0.5, END),
    (f"drawbox=x=(iw-380)/2:y=ih*0.405:w=380:h=4:color={GOLD}@0.9:t=fill"
     f":enable='between(t,0.8,{END})'"),
    text("30 MINUTES  •  $40", ANTON, 80, 0.475, IVORY, 1, 0.9, END),
    text("APPOINTMENTS REQUIRED", ANTON, 44, 0.545, GOLD, 2, 1.1, END),
    text("Betty Borders", SCRIPT, 94, 0.645, IVORY, 0, 1.3, END),
    text("573-429-4680", ANTON, 116, 0.735, GOLD, 1, 1.5, END),
    text("Relax.  Refresh.  Revitalize.", PLAY, 50, 0.855, IVORY, 0, 2.0, END),
]
subprocess.run([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
    "-stream_loop", "2", "-i", str(ROOT / "bizzybee/gen/e_honeycomb.mp4"),
    "-t", f"{END}", "-vf", ",".join([fit(), "setpts=1.35*PTS", GRADE] + end_layers),
    "-an", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
    "-pix_fmt", "yuv420p", str(SEG / "s7.mp4"),
], check=True)
print("segment s7 endcard", END, "s")

# Chain the crossfades.
names = ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]
durs = [3.6, 3.6, 3.0, 3.8, 3.6, 3.6, END]
args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
for n in names:
    args += ["-i", str(SEG / f"{n}.mp4")]
parts, prev, length = [], "[0:v]", durs[0]
for i in range(1, len(names)):
    off = length - XF
    out = f"[x{i}]" if i < len(names) - 1 else "[v]"
    parts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}{out}")
    prev = out
    length = length + durs[i] - XF
args += ["-filter_complex", ";".join(parts), "-map", "[v]",
         "-r", str(FPS), "-c:v", "libx264", "-preset", "slow", "-crf", "19",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(ROOT / "bizzybee/bizzy-bee-promo-v1.mp4")]
subprocess.run(args, check=True)
print(f"\nfinal: {length:.2f}s")
