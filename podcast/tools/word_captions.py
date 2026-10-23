"""One-word lilac captions in the reference style: one word at a time, centred at
y~925 of 1920, Inter Bold 115 (width of "designed" = 526 px like the reference), lilac #D6ADFB, thick black outline
+ soft shadow; each word shows from its start until the next word starts. Commas/periods dropped, ? and ! kept.

  python tools/word_captions.py --video clean.mp4 --audio vo_final.wav --out final/name [--bg-db -0] [--style p.json]
  -> <name>_subs.mp4 (captions) and <name>_clean.mp4 (no captions); both carry --audio (+ the video's own ambience).
Word times come from faster-whisper on --audio (checked against the verbatim layer by the caller).
--style: a Power Tools Hub subtitle preset with engine 'word' (font, weight, size, fill, outline, caps, place.py).
"""
import argparse, json, math, os, re, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FONT_DIRS = [Path(os.environ["WINDIR"]) / "Fonts", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"]
FONT = FONT_DIRS[0] / "Inter-Bold.ttf"
SIZE, CY, LILAC, STROKE = 115, 925, (214, 173, 251, 255), 9
STROKE_COL, CAPS = (0, 0, 0, 255), False
SHADOW = dict(on=True, color="#000000", op=59, angle=56, dist=7, blur=6)
WEIGHT_NAMES = {400: ["Regular"], 500: ["Medium"], 600: ["SemiBold", "Semibold"], 700: ["Bold"],
                800: ["ExtraBold", "Extrabold"], 900: ["Black", "Heavy"]}


def run(cmd): subprocess.run(cmd, check=True)


def _rgba(hex_, a=255):
    n = int(hex_.lstrip("#")[:6], 16)
    return (n >> 16 & 255, n >> 8 & 255, n & 255, a)


def apply_style(path):
    """take font / size / colours / position from a Power Tools Hub preset (engine 'word')"""
    global FONT, SIZE, CY, LILAC, STROKE, STROKE_COL, CAPS, SHADOW
    p = json.loads(Path(path).read_text(encoding="utf-8")); p = p.get("data", p)
    if p.get("engine") not in (None, "word"):
        raise SystemExit(f"preset '{p.get('name')}' is for {p['engine']}, not word_captions")
    st = p.get("st", p); fam = st.get("font", "Inter").replace(" ", "")
    for nm in WEIGHT_NAMES.get(st.get("weight", 700), ["Bold"]):
        hit = next((d / f"{fam}-{nm}{ext}" for d in FONT_DIRS for ext in (".ttf", ".otf") if (d / f"{fam}-{nm}{ext}").exists()), None)
        if hit: FONT = hit; break
    else:
        print(f"! font {st.get('font')} {st.get('weight')} not installed - using {FONT.name}")
    SIZE = st.get("size", SIZE); CAPS = bool(st.get("caps"))
    LILAC = _rgba(st["fill"]["color"]) if st.get("fill", {}).get("on", True) else (0, 0, 0, 0)
    sk = st.get("stroke") or {}
    STROKE = sk.get("width", 0) if sk.get("on") else 0; STROKE_COL = _rgba(sk.get("color", "#000000"))
    CY = (p.get("place") or {}).get("py", CY)
    if st.get("shadow"): SHADOW = {**SHADOW, **st["shadow"]}


def word_png(text, path):
    font = ImageFont.truetype(str(FONT), SIZE)
    if CAPS: text = text.upper()
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if SHADOW.get("on"):  # preset shadow; dist 0 = a soft shadow centred behind the word
        a = math.radians(SHADOW.get("angle", 0)); d = SHADOW.get("dist", 0)
        sc = _rgba(SHADOW.get("color", "#000000"), int(255 * SHADOW.get("op", 59) / 100))
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((W / 2 + d * math.cos(a), CY + d * math.sin(a)), text, font=font, fill=sc, anchor="mm",
                                stroke_width=STROKE + 2, stroke_fill=sc)
        im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(max(1, SHADOW.get("blur", 6) / 2))))
    ImageDraw.Draw(im).text((W / 2, CY), text, font=font, fill=LILAC, anchor="mm", stroke_width=STROKE,
                            stroke_fill=STROKE_COL)
    im.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True); ap.add_argument("--audio", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--bg", type=float, default=1.0, help="gain of the video's own audio under the voice (0 = drop)")
    ap.add_argument("--style", help="subtitle preset JSON from Power Tools Hub (section 'Subtitles', engine 'word')")
    ap.add_argument("--words", help="[[word,start,end],...] from tools/align_words.py (exact script words + times, "
                    "preferred over whisper)")
    a = ap.parse_args()
    if a.style: apply_style(a.style)
    if a.words:
        words = [(re.sub(r"[.,;:]+$", "", w), st, en) for w, st, en in json.loads(Path(a.words).read_text(encoding="utf-8"))]
    else:
        from faster_whisper import WhisperModel
        segs, _ = WhisperModel("medium", device="cpu", compute_type="int8").transcribe(a.audio, word_timestamps=True, language="en")
        words = [(re.sub(r"[.,;:]+$", "", w.word.strip()), w.start, w.end) for s in segs for w in s.words]
    vdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", a.video],
                                capture_output=True, text=True).stdout)
    tmp = Path(tempfile.mkdtemp(prefix="wcap_")); out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    ins, chain, last = [], [], "0:v"
    for i, (t, s, e) in enumerate(words):
        word_png(t, tmp / f"w{i}.png"); ins += ["-i", str(tmp / f"w{i}.png")]
        if i == 0: s = 0.0  # first word from the first frame, no empty lead-in
        end = words[i + 1][1] if i + 1 < len(words) else vdur
        chain.append(f"[{last}][{i + 2}:v]overlay=0:0:enable='gte(t,{s:.3f})*lt(t,{end:.3f})'[v{i}]"); last = f"v{i}"
        print(f"{s:5.2f}-{end:5.2f} {t}")
    mix = (f"[1:a]aresample=48000[vo];[0:a]volume={a.bg},aresample=48000[bg];[vo][bg]amix=inputs=2:duration=first:"
           f"normalize=0,apad=whole_dur={vdur:.3f}[a]") if a.bg > 0 else f"[1:a]aresample=48000,apad=whole_dur={vdur:.3f}[a]"
    enc = ["-c:v", "libx264", "-crf", "16", "-preset", "slow", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{vdur:.3f}"]
    run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-i", a.audio, *ins, "-filter_complex", mix + ";" + ";".join(chain),
         "-map", f"[{last}]", "-map", "[a]", *enc, str(out.with_name(out.name + "_subs.mp4"))])
    run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-i", a.audio, "-filter_complex", mix,
         "-map", "0:v", "-map", "[a]", *enc, str(out.with_name(out.name + "_clean.mp4"))])
    print(out.with_name(out.name + "_subs.mp4"), out.with_name(out.name + "_clean.mp4"))


if __name__ == "__main__":
    main()
