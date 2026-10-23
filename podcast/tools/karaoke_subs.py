#!/usr/bin/env python3
"""karaoke_subs.py - soft-karaoke subtitles as ONE editable subtitle layer of the editor (podcast/stage projects).

    python tools/karaoke_subs.py <project id> [--retime]

1. word times: Groq whisper-large-v3-turbo (word timestamps) on the timeline mix qa/timeline_mix.wav (made by
   `pod.py audit`); the SPELLING comes from the script (state turns), Groq gives only the times. Cached in
   qa/groq_words.json (--retime asks Groq again). Key: ~/.config/groq_key.
2. the editor project gets `subs: [{preset: 'karaoke', words, punct, times, ...}]` - a subtitle track the user can
   restyle in the editor (font, size, colours, position). Periods are dropped from the words (kept as sentence-end
   markers in `punct`), commas stay.
3. The editor lays it out (index.html karaLayout: TextPlates.jsx block logic, wrap inside the 6 % side safe zones with
   the layer's real font, every block centred on the layer anchor) and plays it with CSS transitions. On Export the editor sends its
   blocks/lines; render.py calls render_band below, which draws exactly those blocks, frame by frame with the same
   transitions, into PNG states of the subtitle band that are composited over the video.
Run it AFTER `pod.py timeline` (timeline rewrites the project json).
"""
import argparse, difflib, json, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # podcast/
EDITOR = ROOT.parent / "editor"
TRANS = 0.066                                         # the editor's CSS transition (colour + letter scale), linear
SS = 2                                                # text masks drawn at 2x, then box-reduced (sub-pixel positions)


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def strip_periods(w):
    """drop periods, keep them between digits (3.5); commas stay"""
    return "".join(ch for i, ch in enumerate(w)
                   if not (ch == "." and not (0 < i < len(w) - 1 and w[i - 1].isdigit() and w[i + 1].isdigit())))


# ---------------------------------------------------------------- word times
def groq_words(wav, cache, retime):
    if cache.exists() and not retime:
        return json.loads(cache.read_text(encoding="utf-8"))
    import requests
    key = (Path.home() / ".config" / "groq_key").read_text().strip()
    with open(wav, "rb") as fh:
        r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": (Path(wav).name, fh, "audio/wav")},
                          data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                                "timestamp_granularities[]": "word", "language": "en", "temperature": "0"},
                          timeout=180)
    r.raise_for_status()
    words = [{"w": x["word"].strip(), "s": float(x["start"]), "e": float(x["end"])} for x in r.json().get("words", [])]
    cache.write_text(json.dumps(words, indent=0), encoding="utf-8")
    return words


def align(script_tokens, heard):
    """script spelling + Groq times; unmatched script words are spread between their matched neighbours"""
    a = [norm(t) for t in script_tokens]; b = [norm(h["w"]) for h in heard]
    times = [None] * len(a)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size): times[blk.a + k] = (heard[blk.b + k]["s"], heard[blk.b + k]["e"])
    i = 0
    while i < len(a):
        if times[i] is not None: i += 1; continue
        j = i
        while j < len(a) and times[j] is None: j += 1
        t0 = times[i - 1][1] if i > 0 else (heard[0]["s"] if heard else 0.0)
        t1 = times[j][0] if j < len(a) else t0 + 0.35 * (j - i)
        step = max(0.05, (t1 - t0) / (j - i))
        for k in range(i, j): times[k] = (t0 + (k - i) * step, t0 + (k - i + 1) * step)
        i = j
    return times


def snap_onsets(wav, times):
    """Groq/whisper gives a word right after a pause the start of the pause (it begins where the previous word ended),
    so the karaoke lit the next word 0.4-0.8 s before it was said (example "but", "pushing", "to", 2026-10-09).
    Move each word's start to where the sound really starts inside its own span (10 ms RMS, threshold from the mix's
    own speech and noise levels, 20 ms sustained; a short tail of the previous word before a pause is skipped),
    keeping a 30 ms lead; words that start at once are left alone."""
    import numpy as np
    sr = 16000
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    hop, win = sr // 100, sr // 50
    n = max(0, (len(x) - win) // hop)
    if not n: return times
    fr = np.lib.stride_tricks.sliding_window_view(x, win)[::hop][:n]
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    # speech level - 24 dB: breaths, room tone and decays in a pause sit around -38..-45 dB and must count as silence
    db = np.median(np.stack([db, np.roll(db, 1), np.roll(db, -1)]), axis=0)      # 3-frame median against blips
    thr = max(np.percentile(db, 90) - 24, np.percentile(db, 10) + 8)
    loud = db > thr
    out = []
    for s, e in times:
        a, b = int(s * 100), min(n - 1, int(min(e, s + 1.2) * 100))
        k = a
        # the span often opens with the previous word's tail (whisper ends words early): a short sound (<= 0.35 s)
        # followed by >= 0.15 s of silence is skipped, the word starts after that silence
        while k < b and loud[k]: k += 1
        if k - a > 35: k = a                              # a long sound at the start = the word itself
        q = k
        while q < b and not loud[q]: q += 1
        if k > a and q - k < 15: k = a                    # no real pause after the tail
        on = next((j for j in range(k, b) if loud[j] and loud[j + 1]), None)
        ns = (on / 100 - 0.03) if on is not None else s
        out.append((ns, e) if ns - s > 0.08 and ns < e - 0.05 else (s, e))
    return out


# ---------------------------------------------------------------- render (called by render.py on Export)
def _rgba(hex_, op=100):
    n = int(hex_.lstrip("#")[:6], 16)
    return (n >> 16 & 255, n >> 8 & 255, n & 255, round(255 * op / 100))


def render_band(sub, W, H, FPS, total, font_file, cache_root):
    """sub = the editor's export entry {st, tf, box, lineH, words, times, blocks:[{from,to,a,b,lines,shift}], spans}.
    Draws every frame state of the track as a PNG of only the horizontal band it ever uses, cached by content, and
    returns (concat list for ffmpeg, band top y). Same look as the editor's DOM, frame by frame:
    - the active word animates like the editor's CSS (colour + letter scale, 66 ms linear) - the old version
      cross-faded two still images and the letters showed double (review: '...');
    - text is drawn as coverage masks at SS x resolution (sub-pixel letter positions, smooth edges, no jitter while a
      word grows) and coloured afterwards, so edges keep their colour - drawing a colour onto a transparent image
      darkened every edge pixel, which showed as dark/red halos once the band went through YUV;
    - plates (Background) on their own layer under the text, one-layer text shadow from the whole text."""
    import hashlib, math, os, threading
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    key_src = json.dumps({"v": 5, "sub": {k: v for k, v in sub.items() if k != "fontFile"}, "W": W, "H": H, "FPS": FPS,
                          "total": round(total, 3), "font": str(font_file), "mt": os.path.getmtime(font_file)}, sort_keys=True)
    cdir = Path(cache_root) / hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:16]
    if (cdir / "list.txt").exists() and (cdir / "band.json").exists():
        return cdir / "list.txt", json.loads((cdir / "band.json").read_text())["y0"]
    st = sub["st"]; size = st["size"]; kar = st.get("kar") or {}
    kar_on = bool(kar.get("on"))
    word_mode = kar_on and kar.get("mode") == "word"
    fill_mode = kar_on and kar.get("mode") != "word"      # 'Karaoke fill': every spoken word stays coloured
    hl = (kar.get("scale") or 100) / 100 if word_mode else 1
    f = ImageFont.truetype(font_file, size)
    asc, desc = f.getmetrics(); lh = sub["lineH"]
    tl = threading.local()                    # FreeType faces are not shared between threads
    def font(px):
        d = getattr(tl, "d", None)
        if d is None: d = tl.d = {}
        if px not in d: d[px] = ImageFont.truetype(font_file, px)
        return d[px]
    words = [w.upper() if st.get("caps") else w for w in sub["words"]]
    fill = _rgba(st["fill"]["color"]) if st["fill"].get("on", True) else (0, 0, 0, 0)
    hlc = _rgba(kar.get("color", "#ffdd00")) if kar_on else fill
    sh = st.get("shadow") or {}; stroke = st.get("stroke") or {}
    dp = st.get("depth") or {}
    dn = max(0, int(round(dp.get("size", 0)))) if dp.get("on") else 0
    sw = stroke.get("width", 0) if stroke.get("on") else 0
    sc = _rgba(stroke.get("color", "#000000")) if sw else None
    tf, box = sub["tf"], sub.get("box") or {"w": W, "h": H}
    box_l = tf["px"] - tf.get("ax", box["w"] / 2); box_t = tf["py"] - tf.get("ay", box["h"] / 2)
    align, valign = st.get("align", "center"), st.get("valign", "middle")
    bg = st.get("bg") or {}
    plate = None if not bg.get("on") else "word" if bg.get("target") == "word" else "block" if bg.get("mode") == "block" else "lines"
    padX = bg.get("pad", 0) if bg.get("linkPad") else bg.get("padX", 0)
    padY = bg.get("pad", 0) if bg.get("linkPad") else bg.get("padY", 0)
    pcol = _rgba(bg.get("color", "#ffffff"), bg.get("op", 100)); prad = bg.get("radius", 0)
    space = f.getlength(" ")
    grow = kar.get("grow", "letters")

    blocks, T, spans = sub["blocks"], sub["times"], sub.get("spans") or [[0, total]]
    reach = (sh.get("dist", 0) + 1.5 * sh.get("blur", 0) if sh.get("on") else 0) + size * (hl - 1) + size * 0.5 + sw + dn
    los, his = [], []
    for bl in blocks:
        nl = len(bl["lines"])
        tp = box_t + {"top": 0, "bottom": box["h"] - nl * lh}.get(valign, (box["h"] - nl * lh) / 2) - bl["shift"]
        los.append(tp - padY - reach); his.append(tp + nl * lh + padY + reach)
    Y0 = max(0, int(min(los)) // 2 * 2) if blocks else 0
    BH = max(2, (min(H, int(math.ceil(max(his)))) - Y0 + 1) // 2 * 2) if blocks else 2

    def coloured(mask, col):
        """a solid colour layer whose alpha is the coverage mask (x the colour's own alpha)"""
        lay = Image.new("RGBA", mask.size, col[:3] + (0,))
        lay.putalpha(mask if col[3] >= 255 else mask.point(lambda v: v * col[3] // 255))
        return lay

    def draw(k, cur, look):
        """block k, cur = the spoken word (word plate), look = {word: (rgba, scale)} for words not plain fill/1.0.
        The editor's layout: lines stacked by lineH inside the layer box (valign), each line aligned in the box."""
        bl = blocks[k]
        n = len(bl["lines"])
        top = box_t + {"top": 0, "bottom": box["h"] - n * lh}.get(valign, (box["h"] - n * lh) / 2) - bl["shift"] - Y0
        groups = {}                               # (rgba, scale) -> [(text, x, y, px, anchor)] in 1x band coords
        rects, block_x = [], []
        for j, (a, b) in enumerate(bl["lines"]):
            ltop = top + j * lh
            base = ltop + (lh - (asc + desc)) / 2 + asc
            oy = base - asc + 0.78 * (asc + desc)          # transform-origin 50% 78% of the letter box
            wid = []
            for i in range(a, b):
                kk = look.get(i, (fill, 1.0))[1]
                wid.append(f.getlength(words[i]) * (kk if grow == "spread" else 1))
            lw = sum(wid) + space * (b - a - 1)
            x = {"left": box_l, "right": box_l + box["w"] - lw}.get(align, box_l + (box["w"] - lw) / 2)
            if plate == "lines": rects.append((x - padX, ltop - padY, x + lw + padX, ltop + lh + padY))
            block_x.append((x, x + lw))
            for i in range(a, b):
                w = words[i]; col, kk = look.get(i, (fill, 1.0))
                ops = groups.setdefault((col, kk), [])
                if plate == "word" and i == cur:
                    rects.append((x - padX, base - asc - padY, x + f.getlength(w) + padX, base + desc + padY))
                if abs(kk - 1) < 1e-4:
                    ops.append((w, x, base, size, "ls"))
                else:
                    by = oy + (base - oy) * kk
                    if grow == "word":
                        ops.append((w, x + wid[i - a] / 2, by, size * kk, "ms"))
                    else:
                        s2 = kk if grow == "spread" else 1
                        for ci, ch in enumerate(w):
                            ops.append((ch, x + s2 * (f.getlength(w[:ci]) + f.getlength(ch) / 2), by, size * kk, "ms"))
                x += wid[i - a] + space
        if plate == "block" and block_x:
            rects.append((min(a for a, _ in block_x) - padX, top - padY, max(b for _, b in block_x) + padX, top + n * lh + padY))

        out = Image.new("RGBA", (W, BH), (0, 0, 0, 0))
        for r in rects:                           # each plate its own element in the editor: overlaps add up
            m = Image.new("L", (W * SS, BH * SS), 0)
            rad = max(0, min(prad, (r[2] - r[0]) / 2, (r[3] - r[1]) / 2))
            ImageDraw.Draw(m).rounded_rectangle([round(v * SS) for v in r], radius=round(rad * SS), fill=255)
            out.alpha_composite(coloured(m.reduce(SS), pcol))

        def mask_of(ops, stroke_w=0):
            m = Image.new("L", (W * SS, BH * SS), 0); d = ImageDraw.Draw(m)
            for t, x, y, px, anc in ops:
                d.text((x * SS, y * SS), t, font=font(max(1, round(px * SS))), fill=255, anchor=anc,
                       stroke_width=round(stroke_w * SS), stroke_fill=255)
            return m.reduce(SS)
        order = sorted(groups, key=lambda g: (g[1], g != (fill, 1.0)))   # plain words first, growing words on top
        txt = Image.new("RGBA", (W, BH), (0, 0, 0, 0))
        if dn:                                    # 3D extrusion: the outlined letters repeated 1 px further along
            # `angle` per layer, `color` at the face -> `color2` at the back (the editor's .kdp layer draws the same)
            from PIL import ImageChops
            glyph = None
            for g in order:
                m = mask_of(groups[g], sw); glyph = m if glyph is None else ImageChops.lighter(glyph, m)
            ang = math.radians(dp.get("angle", 90))
            c0, c1 = _rgba(dp.get("color", "#333333")), _rgba(dp.get("color2", dp.get("color", "#000000")))
            for i in range(dn, 0, -1):
                txt.alpha_composite(coloured(glyph, lerp(c0, c1, (i - 1) / max(1, dn - 1))),
                                    (round(math.cos(ang) * i), round(math.sin(ang) * i)))
        if sw:
            for g in order: txt.alpha_composite(coloured(mask_of(groups[g], sw), sc))
        for g in order: txt.alpha_composite(coloured(mask_of(groups[g]), g[0]))
        if sh.get("on"):                          # CSS text-shadow: offset by angle/dist, Gaussian sigma = blur / 2
            # one layer: blur the alpha of the WHOLE text, x op/100 (up to 200 % = denser), clipped - same as the
            # editor's SVG filter (feGaussianBlur sigma = blur/2 -> feFuncA slope = op/100)
            col = _rgba(sh.get("color", "#000000")); op = sh.get("op", 60)
            a = txt.getchannel("A").filter(ImageFilter.GaussianBlur(sh.get("blur", 10) / 2)).point(lambda v: min(255, v * op // 100))
            shadow = Image.new("RGBA", (W, BH), col[:3] + (0,)); shadow.putalpha(a)
            ang = math.radians(sh.get("angle", 90)); dist = sh.get("dist", 0)
            out.alpha_composite(shadow, (round(math.cos(ang) * dist), round(math.sin(ang) * dist)))
        out.alpha_composite(txt)
        return out

    def lerp(c0, c1, p): return tuple(round(u + (v - u) * p) for u, v in zip(c0, c1))

    def state(t):
        """(block, spoken word, look) at time t, like index.html renderKara + its CSS transitions"""
        if not any(s <= t < e for s, e in spans): return None
        for k, bl in enumerate(blocks):
            if bl["from"] <= t < bl["to"]:
                act = bl["a"]
                for i in range(bl["a"], bl["b"]):
                    if T[i][0] <= t + 1e-3: act = i
                look = {}
                if kar_on:
                    # a block appears already in its state (new DOM, no transition); inside a block the change from
                    # the previous word animates over TRANS seconds from that word's start
                    p = 1.0 if act == bl["a"] else min(1.0, max(0.0, (t - T[act][0]) / TRANS))
                    p = round(p * 8) / 8
                    if fill_mode:
                        for i in range(bl["a"], act): look[i] = (hlc, 1.0)
                        look[act] = (lerp(fill, hlc, p), 1.0)
                    else:
                        look[act] = (lerp(fill, hlc, p), round(1 + (hl - 1) * p, 4))
                        if act > bl["a"] and p < 1: look[act - 1] = (lerp(hlc, fill, p), round(hl - (hl - 1) * p, 4))
                    look = {i: v for i, v in look.items() if v != (fill, 1.0)}
                return ("s", k, act, tuple(sorted(look.items())))
        return None

    frames = [state(fr / FPS) for fr in range(int(round(total * FPS)))]
    shutil.rmtree(cdir, ignore_errors=True); cdir.mkdir(parents=True)
    names = {}
    for key in frames:
        if key not in names: names[key] = cdir / f"s{len(names):04d}.png"
    blank = Image.new("RGBA", (W, BH), (0, 0, 0, 0))
    def save(key):
        im = blank if key is None else draw(key[1], key[2], dict(key[3]))
        im.save(names[key], compress_level=1)
    with ThreadPoolExecutor(max_workers=min(12, os.cpu_count() or 4)) as ex:
        list(ex.map(save, list(names)))
    lst = []
    for key in frames:
        if lst and lst[-1][0] == names[key]: lst[-1][1] += 1
        else: lst.append([names[key], 1])
    with open(cdir / "list.txt", "w", encoding="utf-8") as fh_:
        for pth, n in lst: fh_.write(f"file '{pth.as_posix()}'\nduration {n / FPS:.6f}\n")
        fh_.write(f"file '{lst[-1][0].as_posix()}'\n")
    (cdir / "band.json").write_text(json.dumps({"y0": Y0, "h": BH}))
    # keep the 6 newest caches
    olds = sorted([d for d in Path(cache_root).iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime, reverse=True)[6:]
    for d in olds: shutil.rmtree(d, ignore_errors=True)
    return cdir / "list.txt", Y0


# ---------------------------------------------------------------- project -> editor subtitle layer
def load_style(path):
    """a subtitle preset from Power Tools Hub (section '...', engine 'editor'): {name, title, st, place:{py, boxW, boxH}}.
    Also accepts the editor's own preset shape {name, st} or a bare st."""
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    p = p.get("data", p)                                  # an ArtifactData read keeps the document under "data"
    if "st" not in p: p = {"st": p}
    if p.get("engine", "editor") != "editor":
        sys.exit(f"preset '{p.get('name')}' is for {p['engine']}, not the editor - use that tool (see its 'how to use')")
    return p


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("id"); ap.add_argument("--retime", action="store_true")
    ap.add_argument("--style", help="subtitle preset JSON from Power Tools Hub (section 'Subtitles'); default = Soft karaoke")
    a = ap.parse_args()
    pdir = ROOT / "projects" / a.id
    st = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
    projf = EDITOR / "projects" / f"{a.id}.json"
    proj = json.loads(projf.read_text(encoding="utf-8"))
    mix = pdir / "qa" / "timeline_mix.wav"
    if not mix.exists():
        subprocess.run([sys.executable, str(ROOT / "pod.py"), "audit", a.id], check=True, cwd=ROOT)
    heard = groq_words(mix, pdir / "qa" / "groq_words.json", a.retime)
    tokens = " ".join(t["text"] for t in st["turns"]).split()
    times = snap_onsets(mix, align(tokens, heard))
    words = [strip_periods(t) for t in tokens]
    punct = ["." if re.search(r"[.!?][\"'”’)\]]*$", t) else "" for t in tokens]
    total = max(i["start"] + (i["out"] - i["in"]) for i in proj["items"] if not i.get("overlay"))
    # the old PNG/webm overlay (first version) is replaced by the editable layer
    proj["media"] = [m for m in proj["media"] if m["id"] != "msubs"]
    proj["items"] = [i for i in proj["items"] if i.get("media") != "msubs"]
    proj["tracks"]["v"] = max(2, proj["tracks"].get("v", 1))
    entry = {"name": "Subtitles", "preset": "karaoke", "track": 1, "start": 0, "dur": round(total, 3),
             "words": words, "punct": punct, "times": [[round(s, 3), round(e, 3)] for s, e in times],
             "box": {"w": 950, "h": 360}, "tf": {"px": 540, "py": 1152, "ax": 475, "ay": 180}}
    if a.style:                                           # the editor merges st over its 'karaoke' base, so a full st wins
        pre = load_style(a.style); place = pre.get("place") or {}
        bw, bh = place.get("boxW", 950), place.get("boxH", 360)
        entry.update(name=pre.get("title") or pre.get("name") or "Subtitles", st=pre["st"], box={"w": bw, "h": bh},
                     tf={"px": 540, "py": place.get("py", 1152), "ax": bw / 2, "ay": bh / 2})
    proj["subs"] = [entry]
    projf.write_text(json.dumps(proj, indent=1), encoding="utf-8")
    for old in ("subs_karaoke.webm", "subs_karaoke.mov"):
        (EDITOR / "videos" / a.id / old).unlink(missing_ok=True)
    shutil.rmtree(EDITOR / "videos" / a.id / "subs_frames", ignore_errors=True)
    print(f"{len(words)} words -> editor subtitle layer '{entry['name']}' in projects/{a.id}.json")


if __name__ == "__main__":
    main()
