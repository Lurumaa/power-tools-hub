"""Montage for a talking-head creo with full-screen inserts.

  python tools/coach_montage.py <edit.json> --out final/<name>.mp4 [--no-plates] [--disclaimer] [--preview]

edit.json (all times in seconds; talk segments are laid end to end on the timeline, every cut on the 30 fps grid):
  talk:    [{src, in, out}]                       speaker footage; its audio is the VO
  anchor:  [x, y]                                 punch-in centre (face), fractions of the frame
  zooms:   [[t, z], ...]                          punch-in level from t on (jump-cut look), slow drift added inside
  drift:   0.02                                   extra zoom per 5 s inside one zoom step (alive, never static)
  inserts: [{kind: clip|gauge, src, in, at, dur, scale, trans_in, trans_out, bg, label}]  full-screen cutaways
           (VO keeps running); trans: zoom (zoom-through + flash), whip (horizontal whip pan), cut
  checks:  [{at, x, y, r}]                        green check mark drawn on top (pop + stroke)
  plates:  [{kind: headline|phase|cta, at, end, y, tag, text, color}]
  music:   {src, offset, lufs}                    bed under the VO (VO lufs = vo_lufs)
  vo_lufs: -14,  sfx: true
Writes the mp4 (h264_nvenc ~6 Mbit/s, 30 fps, AAC). --no-plates = clean version (no text, no checks/gauge text).
"""
import argparse, json, math, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np, cv2
from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
W, H, FPS, SR = 1080, 1920, 30, 48000
FONTS = Path(os.environ["WINDIR"]) / "Fonts"
F_BLACK, F_XBOLD, F_REG = FONTS / "Montserrat-Black.ttf", FONTS / "Montserrat-ExtraBold.ttf", FONTS / "Inter-Regular.ttf"
DISC = [("AI generated content. Individual results may vary.", 1568), ("Not medical advice", 1601)]


# ------------------------------------------------------------------ easing
def clamp(x, a=0.0, b=1.0): return max(a, min(b, x))
def ease_out_cubic(x): x = clamp(x); return 1 - (1 - x) ** 3
def ease_in_out(x): x = clamp(x); return 3 * x * x - 2 * x * x * x
def ease_out_back(x, s=1.9):
    x = clamp(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


# ------------------------------------------------------------------ video readers
class Reader:
    """sequential frames of src from t_in, scaled/cropped to cover 1080x1920 (anchor-preserving)"""
    def __init__(self, src, t_in, n, fit="cover", speed=1.0):
        vf = (f"setpts=PTS/{speed}," if abs(speed - 1) > 1e-3 else "") + f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}"
        self.p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-ss", f"{t_in:.3f}", "-i", str(src), "-frames:v", str(n + 2),
                                   "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
        self.last = None

    def read(self):
        b = self.p.stdout.read(W * H * 3)
        if len(b) == W * H * 3: self.last = np.frombuffer(b, np.uint8).reshape(H, W, 3).copy()
        return self.last

    def close(self):
        try: self.p.stdout.close(); self.p.kill()
        except Exception: pass


def zoom_img(img, z, cx, cy, dx=0.0):
    if abs(z - 1) < 1e-4 and abs(dx) < 0.5: return img
    M = np.float32([[z, 0, (1 - z) * cx + dx], [0, z, (1 - z) * cy]])
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


_masks = {}
def fit_insert(img, scale, bgcol):
    """insert content scaled down and anchored to the top (room for the phase plate below), navy fill;
    uint8 blend with cached weights (a float32 full frame per frame ran the process out of memory)"""
    if abs(scale - 1) < 1e-3: return img
    small = cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    x0 = (W - small.shape[1]) // 2; y0 = int(H * 0.035)
    if scale not in _masks:  # soften the seam into the fill
        m = np.zeros((H, W), np.float32); m[y0 + 6:y0 + small.shape[0] - 6, x0 + 6:x0 + small.shape[1] - 6] = 1
        m = cv2.GaussianBlur(m, (0, 0), 25); fill = np.empty((H, W, 3), np.uint8); fill[:] = bgcol
        _masks[scale] = (m, 1.0 - m, fill)
    m, im_, fill = _masks[scale]
    out = fill.copy(); out[y0:y0 + small.shape[0], x0:x0 + small.shape[1]] = small
    return cv2.blendLinear(out, fill, m, im_)


# ------------------------------------------------------------------ plates (PIL -> BGRA numpy)
def text_box(text, font, fg, bg, padx=26, pady=14, radius=20):
    l, t, r, b = font.getbbox(text)
    w, h = r - l + 2 * padx, (font.size * 1.0) + 2 * pady
    im = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=bg)
    d.text((padx - l, pady + (font.size - (b + t)) / 2 - 1), text, font=font, fill=fg)
    return im


def wrap(text, font, maxw):
    lines, cur = [], ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if font.getlength(t) <= maxw or not cur: cur = t
        else: lines.append(cur); cur = wd
    if cur: lines.append(cur)
    if len(lines) > 1 and len(lines[-1].split()) == 1:  # never a one-word last line
        a = lines[-2].split(); lines[-2] = " ".join(a[:-1]); lines[-1] = a[-1] + " " + lines[-1]
    return lines


def stack(parts, gap=10, align="center"):
    w = max(p.width for p in parts); h = sum(p.height for p in parts) + gap * (len(parts) - 1)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0)); y = 0
    for p in parts:
        x = (w - p.width) // 2 if align == "center" else 0
        im.alpha_composite(p, (x, y)); y += p.height + gap
    return im


def shadowed(im, blur=14, off=8, alpha=110):
    pad = blur * 2
    out = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad + off), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    a = im.split()[3].point(lambda v: v * alpha // 255)
    sh.paste(Image.new("RGBA", im.size, (0, 0, 0, 255)), (pad, pad + off), a)
    out.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur))); out.alpha_composite(im, (pad, pad))
    return out


def hex2rgba(h, a=255):
    h = h.lstrip("#"); return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def build_plate(p):
    k = p["kind"]
    if k == "headline":
        ft, fm = ImageFont.truetype(str(F_BLACK), 62), ImageFont.truetype(str(F_BLACK), 64)
        parts = [text_box(p["tag"], ft, (10, 10, 10, 255), hex2rgba(p.get("color", "#FFD60A")), radius=16)] if p.get("tag", "WANTED:") else []
        for ln in ([x.strip() for x in p["text"].split("|")] if "|" in p["text"] else wrap(p["text"], fm, 900)): parts.append(text_box(ln, fm, (10, 10, 10, 255), (255, 255, 255, 255)))
        return [("all", shadowed(stack(parts, gap=8)))]
    if k == "phase":
        fl = ImageFont.truetype(str(F_BLACK), 54)
        fm = ImageFont.truetype(str(F_BLACK), 74)  # main text always ONE line (2 lines ran into the subs band, task_976_2)
        if fm.getlength(p["text"]) > 900: fm = ImageFont.truetype(str(F_BLACK), max(50, int(74 * 900 / fm.getlength(p["text"]))))
        lab = shadowed(text_box(p["tag"], fl, (11, 18, 32, 255), hex2rgba(p["color"]), radius=40, padx=30, pady=12))
        main = shadowed(stack([text_box(ln, fm, (10, 10, 10, 255), (255, 255, 255, 255)) for ln in [p["text"]]], gap=8))
        return [("label", lab), ("main", main)]
    if k == "cta":
        f = ImageFont.truetype(str(F_BLACK), p.get("size", 46))
        txt = p["text"] + "  →"
        if "|" in p["text"]: lines = [x.strip() for x in p["text"].split("|")]; lines[-1] += "  →"
        else: lines = wrap(txt, f, 820) if p.get("lines", 1) > 1 else [txt]
        lh = int(f.size * 1.18); padx, pady = 56, 30
        w = int(max(f.getlength(ln) for ln in lines)) + 2 * padx; h = lh * len(lines) + 2 * pady - (lh - f.size)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=min(h // 2, 52), fill=hex2rgba(p.get("color", "#1FC25A")))
        for li, ln in enumerate(lines):
            l, t, r, b = f.getbbox(ln); x = (w - (r - l)) / 2 - l
            d.text((x, pady + li * lh - t + (f.size - (b - t)) / 2), ln, font=f, fill=(255, 255, 255, 255))
        return [("btn", shadowed(im, blur=18, off=10, alpha=140))]
    raise ValueError(k)


def to_np(im):  # PIL RGBA -> premultiplied BGR float + alpha
    a = np.asarray(im, np.float32) / 255.0
    rgb, al = a[..., :3][..., ::-1], a[..., 3:4]
    return np.ascontiguousarray(rgb * al), al


def blend(frame, layer, x, y, scale=1.0, alpha=1.0):
    """layer=(premult_bgr, alpha) at centre (x, y) px, scaled about its centre"""
    pm, al = layer
    if alpha <= 0.003: return
    if abs(scale - 1) > 1e-3:
        h, w = al.shape[:2]; nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        pm = cv2.resize(pm, (nw, nh), interpolation=cv2.INTER_LINEAR); al = cv2.resize(al, (nw, nh), interpolation=cv2.INTER_LINEAR)[..., None]
    h, w = al.shape[:2]
    x0, y0 = int(round(x - w / 2)), int(round(y - h / 2))
    fx0, fy0, fx1, fy1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
    if fx1 <= fx0 or fy1 <= fy0: return
    sub_pm = pm[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0] * alpha
    sub_al = al[fy0 - y0:fy1 - y0, fx0 - x0:fx1 - x0] * alpha
    region = frame[fy0:fy1, fx0:fx1].astype(np.float32) / 255.0
    frame[fy0:fy1, fx0:fx1] = np.clip((region * (1 - sub_al) + sub_pm) * 255, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ gauge + check
_txt_cache = {}
def text_layer(s, size, font=F_BLACK, color=(255, 255, 255, 255)):
    k = (s, size, str(font), color)
    if k not in _txt_cache:
        f = ImageFont.truetype(str(font), size); l, t, r, b = f.getbbox(s)
        im = Image.new("RGBA", (r - l + 8, b - t + 8), (0, 0, 0, 0)); ImageDraw.Draw(im).text((4 - l, 4 - t), s, font=f, fill=color)
        _txt_cache[k] = to_np(shadowed(im, blur=10, off=4, alpha=120))
    return _txt_cache[k]


def lerp_col(stops, v):
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if v <= b:
            u = (v - a) / max(1e-6, b - a); return tuple(ca[i] + (cb[i] - ca[i]) * u for i in range(3))
    return stops[-1][1]


GAUGE_STOPS = [(0.0, (26, 122, 255)), (0.55, (255, 199, 34)), (1.0, (107, 212, 43))]  # BGR: orange -> cyan -> green


def gauge_value(tl, rise=(0.35, 2.3)):
    a, b = rise
    if tl < a: return 0.0
    if tl < b: return 1.03 * ease_out_cubic((tl - a) / (b - a))
    u = (tl - b) / 0.5
    return 1.0 + 0.03 * math.cos(min(1, u) * math.pi) * (1 - min(1, u)) if u < 1 else 1.0


def draw_gauge(frame, tl, label, plates_on):
    cx, cy, R, th = W // 2, int(H * 0.40), 360, 50
    v = gauge_value(tl); disp = min(100, int(round(min(v, 1.0) * 100)))
    over = frame.copy()
    cv2.ellipse(over, (cx, cy), (R, R), 0, 180, 360, (60, 52, 46), th, cv2.LINE_AA)
    for k in range(0, 11):  # ticks
        ang = math.radians(180 + k * 18)
        p0 = (int(cx + (R - th) * math.cos(ang)), int(cy + (R - th) * math.sin(ang)))
        p1 = (int(cx + (R - th - (26 if k % 5 == 0 else 14)) * math.cos(ang)), int(cy + (R - th - (26 if k % 5 == 0 else 14)) * math.sin(ang)))
        cv2.line(over, p0, p1, (200, 200, 200), 3, cv2.LINE_AA)
    col = lerp_col(GAUGE_STOPS, min(v, 1.0))
    if v > 0.002:
        glow = np.zeros_like(frame)
        cv2.ellipse(glow, (cx, cy), (R, R), 0, 180, 180 + 180 * min(v, 1.0), col, th + 30, cv2.LINE_AA)
        over = cv2.add(over, (cv2.GaussianBlur(glow, (0, 0), 22) * 0.55).astype(np.uint8))
        cv2.ellipse(over, (cx, cy), (R, R), 0, 180, 180 + 180 * min(v, 1.0), col, th, cv2.LINE_AA)
        ang = math.radians(180 + 180 * min(v, 1.0))
        cv2.circle(over, (int(cx + R * math.cos(ang)), int(cy + R * math.sin(ang))), th // 2 + 6, (255, 255, 255), -1, cv2.LINE_AA)
    a = ease_out_cubic(tl / 0.25)
    frame[:] = cv2.addWeighted(over, a, frame, 1 - a, 0)
    if plates_on:
        blend(frame, text_layer(label, 58, F_XBOLD), cx, cy - R - 70, alpha=a)
        blend(frame, text_layer(f"{disp}%", 170), cx, cy - 95, alpha=a)
        if tl > 2.5:
            blend(frame, text_layer("STABLE", 46, F_BLACK, (70, 225, 120, 255)), cx, cy + 40,
                  scale=ease_out_back((tl - 2.5) / 0.25), alpha=ease_out_cubic((tl - 2.5) / 0.15))


def draw_check(frame, tl, x, y, r):
    if tl < 0: return
    s = ease_out_back(tl / 0.28); cx, cy = int(x * W), int(y * H); rr = max(1, int(r * s))
    glow = np.zeros_like(frame)
    cv2.circle(glow, (cx, cy), rr + 30, (90, 210, 60), -1, cv2.LINE_AA)
    frame[:] = cv2.add(frame, (cv2.GaussianBlur(glow, (0, 0), 30) * (0.5 * ease_out_cubic(tl / 0.3))).astype(np.uint8))
    cv2.circle(frame, (cx, cy), rr, (75, 200, 34), -1, cv2.LINE_AA)
    cv2.circle(frame, (cx, cy), rr, (255, 255, 255), 8, cv2.LINE_AA)
    u = ease_in_out((tl - 0.15) / 0.3)
    if u > 0:
        pts = [(-0.45, 0.02), (-0.12, 0.33), (0.48, -0.30)]
        P = [(cx + int(px * r), cy + int(py * r)) for px, py in pts]
        L1 = math.dist(P[0], P[1]); L2 = math.dist(P[1], P[2]); d = u * (L1 + L2)
        if d <= L1:
            q = (int(P[0][0] + (P[1][0] - P[0][0]) * d / L1), int(P[0][1] + (P[1][1] - P[0][1]) * d / L1))
            cv2.line(frame, P[0], q, (255, 255, 255), 26, cv2.LINE_AA)
        else:
            e = (d - L1) / L2
            q = (int(P[1][0] + (P[2][0] - P[1][0]) * e), int(P[1][1] + (P[2][1] - P[1][1]) * e))
            cv2.line(frame, P[0], P[1], (255, 255, 255), 26, cv2.LINE_AA); cv2.line(frame, P[1], q, (255, 255, 255), 26, cv2.LINE_AA)


def draw_target(frame, tl, x, y, r, col=(40, 90, 255)):
    """reticle that zooms in and locks onto (x, y): 'we target the spasm'"""
    if tl < 0: return
    u = ease_out_cubic(tl / 0.45); rr = int(r * (1.8 - 0.8 * u)); cx, cy = int(x * W), int(y * H)
    a = min(1.0, tl / 0.15); over = frame.copy()
    rot = (1 - u) * 90
    cv2.circle(over, (cx, cy), rr, col, 7, cv2.LINE_AA); cv2.circle(over, (cx, cy), int(rr * 0.62), col, 4, cv2.LINE_AA)
    for k in range(4):
        ang = math.radians(rot + 90 * k)
        p0 = (int(cx + rr * 0.78 * math.cos(ang)), int(cy + rr * 0.78 * math.sin(ang)))
        p1 = (int(cx + rr * 1.22 * math.cos(ang)), int(cy + rr * 1.22 * math.sin(ang)))
        cv2.line(over, p0, p1, col, 8, cv2.LINE_AA)
    if u > 0.98:  # locked: soft pulse
        pr = int(rr * (1 + 0.08 * math.sin((tl - 0.45) * 2 * math.pi * 1.6)))
        cv2.circle(over, (cx, cy), pr, (255, 255, 255), 3, cv2.LINE_AA)
    frame[:] = cv2.addWeighted(over, a, frame, 1 - a, 0)



# ------------------------------------------------------------------ dynamic subs (1-2 lines, moving marker)
def _disp(w):
    w = "".join(ch for i, ch in enumerate(w) if not (ch == "." and not (0 < i < len(w) - 1 and w[i - 1].isdigit() and w[i + 1].isdigit())))
    return (w.rstrip(":!?;") or w).upper()


class Subs:
    """Dynamic subs: 1-2 lines per phrase, white caps; a rounded marker sits under the spoken word and recolours the
    letters it covers to dark (scanner effect). Same line: caterpillar move (leading edge jumps ahead with a small
    overshoot, trailing edge follows). Next line / next phrase: the marker hops in a small arc and shrinks mid-air.
    Marker colour follows the story (cfg colors). Phrases wider than maxw shrink their font (critic r4)."""
    def __init__(self, cfg, base):
        d = json.loads((base / cfg["file"]).read_text(encoding="utf-8"))
        self.words = d["words"]; self.size0 = cfg.get("size", 62); self.y = cfg.get("y", 0.735) * H
        self.maxw = cfg.get("maxw", 860); self.padx, self.pady = cfg.get("pad", [24, 12])
        self.cols = [(t, np.array(hex2rgba(c)[:3], np.float32)) for t, c in cfg.get("colors", [[0, "#FFD60A"]])]
        self.cache = {}; self.margin = 40; self.ph = []; fonts = {}
        self.recolor = cfg.get("dark_under_marker", False)  # review: text stays white over the yellow marker
        for p in d["phrases"]:
            i0, i1, brk = p["i0"], p["i1"], p.get("brk")
            lines = [(i0, brk), (brk, i1)] if brk else [(i0, i1)]
            f = ImageFont.truetype(str(F_BLACK), self.size0)
            widest = max(f.getlength(" ".join(_disp(self.words[i][0]) for i in range(a, b))) for a, b in lines)
            size = max(48, int(self.size0 * min(1.0, self.maxw / widest)))
            f = fonts.setdefault(size, ImageFont.truetype(str(F_BLACK), size)); sp = f.getlength(" "); lh = int(size * 1.42)
            boxes = {}
            for li, (a, b) in enumerate(lines):
                ws = [_disp(self.words[i][0]) for i in range(a, b)]
                tw = sum(f.getlength(w) for w in ws) + sp * (len(ws) - 1)
                x = W / 2 - tw / 2; yc = self.y + (li - (len(lines) - 1) / 2) * lh
                for i, w in zip(range(a, b), ws):
                    wl = f.getlength(w); boxes[i] = (x, x + wl, yc, w); x += wl + sp
            self.ph.append({"i0": i0, "i1": i1, "boxes": boxes, "f": f, "lh": lh, "size": size,
                            "s0": self.words[i0][1], "end": self.words[i1 - 1][2]})
        for k, p in enumerate(self.ph):  # on screen until the next phrase starts (max 0.6 s after its last word)
            p["t0"] = max(p["s0"] - 0.06, self.ph[k - 1]["t1"] if k else 0.0)
            p["t1"] = min(self.ph[k + 1]["s0"] - 0.06, p["end"] + 0.6) if k + 1 < len(self.ph) else p["end"] + 0.6

    def colour(self, t):
        c = self.cols[0][1]
        for k, (ta, ca) in enumerate(self.cols):
            if t >= ta:
                cp = self.cols[k - 1][1] if k else ca
                c = cp + (ca - cp) * ease_in_out((t - ta) / 0.3)
        return tuple(int(v) for v in c)

    def layers(self, pi):
        if pi not in self.cache:
            self.cache = {}  # one phrase at a time: RAM stays flat
            p = self.ph[pi]; m = self.margin; f = p["f"]
            xs = [b[0] for b in p["boxes"].values()] + [b[1] for b in p["boxes"].values()]
            ys = [b[2] for b in p["boxes"].values()]
            X0, X1, Y0, Y1 = min(xs) - m, max(xs) + m, min(ys) - p["lh"] / 2 - m, max(ys) + p["lh"] / 2 + m
            size = (int(X1 - X0), int(Y1 - Y0))
            white, dark, sh = (Image.new("RGBA", size, (0, 0, 0, 0)) for _ in range(3))
            dw, dd, ds = ImageDraw.Draw(white), ImageDraw.Draw(dark), ImageDraw.Draw(sh)
            for i, (xa, xb, yc, w) in p["boxes"].items():
                l, t, r, b = f.getbbox(w); pos = (xa - X0, yc - Y0 - (t + b) / 2)
                ds.text((pos[0], pos[1] + 5), w, font=f, fill=(0, 0, 0, 170))
                dw.text(pos, w, font=f, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 200))  # white on the yellow marker needs a dense outline (critic r6: 13.5:1)
                dd.text(pos, w, font=f, fill=(14, 14, 18, 255))
            sh = sh.filter(ImageFilter.GaussianBlur(7)); sh.alpha_composite(white)
            self.cache[pi] = (to_np(sh), to_np(dark), X0, Y0, size)
        return self.cache[pi]

    def box(self, p, i):
        xa, xb, yc, _ = p["boxes"][i]; hh = p["size"] * 0.98 + 2 * self.pady
        return xa - self.padx, xb + self.padx, yc, hh

    def marker(self, pi, t):
        """(L, R, Yc, h) of the marker at t, or None"""
        p = self.ph[pi]; idx = sorted(p["boxes"])
        started = [i for i in idx if (self.words[i][1] if i != idx[0] else p["t0"]) <= t]
        if not started: return None
        k = started[-1]; t_k = self.words[k][1] if k != idx[0] else p["t0"]
        L1, R1, Y1, h1 = self.box(p, k)
        src = self.box(p, k - 1) if k != idx[0] else None
        if src is None:  # first word of a phrase: appears at half size at once and springs to full (no blank frames,
            # no ride across the new phrase - critic r4 / my check: a ride-in read the words backwards)
            g = 0.5 + 0.5 * ease_out_back(clamp((t - t_k) / 0.14), 1.6); c = (L1 + R1) / 2
            return c - (R1 - L1) / 2 * g, c + (R1 - L1) / 2 * g, Y1, h1 * (0.7 + 0.3 * g), 1.0
        L0, R0, Y0, h0 = src
        if Y0 == Y1 and k != idx[0]:  # caterpillar along the line
            u = (t - t_k) / 0.19
            lead, trail = ease_out_back(clamp(u / 0.7), 1.3), ease_in_out(clamp((u - 0.18) / 0.82))
            if (L1 + R1) >= (L0 + R0): R, L = R0 + (R1 - R0) * lead, L0 + (L1 - L0) * trail
            else: L, R = L0 + (L1 - L0) * lead, R0 + (R1 - R0) * trail
            return L, R, Y1, h1 * (1 - 0.09 * math.sin(math.pi * clamp(u))), 1.0
        e = ease_in_out(clamp((t - t_k) / 0.2))   # hop to the next line / phrase in a small arc
        c = (L0 + R0) / 2 + ((L1 + R1) / 2 - (L0 + R0) / 2) * e
        w = ((R0 - L0) + ((R1 - L1) - (R0 - L0)) * e) * (1 - 0.4 * math.sin(math.pi * e))
        Y = Y0 + (Y1 - Y0) * e - 18 * math.sin(math.pi * e)
        return c - w / 2, c + w / 2, Y, h0 + (h1 - h0) * e, 1 - math.sin(math.pi * e)  # no recolour mid-air

    def draw(self, frame, t):
        pi = next((k for k, p in enumerate(self.ph) if p["t0"] <= t < p["t1"]), None)
        if pi is None: return
        p = self.ph[pi]; white, dark, X0, Y0, (lw, lh_) = self.layers(pi)
        pop = 0.92 + 0.08 * ease_out_back((t - p["t0"]) / 0.14)
        lcx, lcy = X0 + lw / 2, Y0 + lh_ / 2
        fx = lambda x: W / 2 + (x - W / 2) * pop; fy = lambda y: self.y + (y - self.y) * pop
        mk = self.marker(pi, t)
        if mk:
            L, R, Yc, hm, _ = mk; col = self.colour(t); w_ = max(8.0, R - L) * pop; h_ = hm * pop
            im = Image.new("RGBA", (int(w_) + 40, int(h_) + 46), (0, 0, 0, 0)); dm = ImageDraw.Draw(im)
            dm.rounded_rectangle([20, 26, 20 + w_, 26 + h_], radius=h_ * 0.3, fill=(0, 0, 0, 120))
            im = im.filter(ImageFilter.GaussianBlur(8)); dm = ImageDraw.Draw(im)
            dm.rounded_rectangle([20, 20, 20 + w_, 20 + h_], radius=h_ * 0.3, fill=col + (255,))
            blend(frame, to_np(im), fx((L + R) / 2), fy(Yc) + 3)
        blend(frame, white, fx(lcx), fy(lcy), scale=pop)
        if mk and self.recolor:  # optional: letters under the marker turn dark (mask in layer coords, before the pop)
            L, R, Yc, hm, da = mk
            m = Image.new("L", (lw, lh_), 0)
            ImageDraw.Draw(m).rounded_rectangle([L - X0, Yc - hm / 2 - Y0, R - X0, Yc + hm / 2 - Y0], radius=hm * 0.3, fill=255)
            a = np.asarray(m, np.float32)[..., None] * (da / 255.0)
            if a.max() > 0: blend(frame, (dark[0] * a, dark[1] * a), fx(lcx), fy(lcy), scale=pop)


# ------------------------------------------------------------------ transitions
def trans_zoom(a, b, u, cx, cy):
    """a -> b: a punches in with blur, b settles from 1.25x, white flash at the middle"""
    e = ease_in_out(u)
    za = 1 + 0.55 * e; zb = 1.25 - 0.25 * ease_out_cubic(u)
    A = zoom_img(a, za, cx, cy); Bf = zoom_img(b, zb, W / 2, H / 2)
    k = int(2 + 22 * math.sin(math.pi * u))
    if k > 2:
        A = cv2.addWeighted(A, 0.5, zoom_img(A, 1.04, cx, cy), 0.5, 0); Bf = cv2.addWeighted(Bf, 0.6, zoom_img(Bf, 1.03, W / 2, H / 2), 0.4, 0)
        A = cv2.blur(A, (k // 2 + 1, k // 2 + 1)); Bf = cv2.blur(Bf, (k // 3 + 1, k // 3 + 1))
    m = clamp((u - 0.35) / 0.3)
    out = cv2.addWeighted(A, 1 - m, Bf, m, 0)
    fl = 0.38 * math.sin(math.pi * u) ** 2
    return cv2.addWeighted(out, 1 - fl, np.full_like(out, 255), fl, 0)


def trans_whip(a, b, u, direction=-1):
    e = ease_in_out(u); off = e * W * direction
    A = zoom_img(a, 1.0, W / 2, H / 2, dx=off); Bf = zoom_img(b, 1.0, W / 2, H / 2, dx=off - W * direction)
    k = int(1 + 120 * math.sin(math.pi * u))
    if k > 1:
        A = cv2.blur(A, (k, 1)); Bf = cv2.blur(Bf, (k, 1))
    out = A.copy()
    xs = np.arange(W)
    mask = (xs >= W + off) if direction < 0 else (xs < off)
    out[:, mask] = Bf[:, mask]
    return out


# ------------------------------------------------------------------ audio
def load_audio(src, t_in, dur):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t_in:.4f}", "-i", str(src), "-t", f"{dur:.4f}", "-vn", "-ac", "2",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32).reshape(-1, 2).copy()
    n = int(round(dur * SR))
    if len(x) < n: x = np.vstack([x, np.zeros((n - len(x), 2), np.float32)])
    return x[:n]


def lufs(x):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f: p = f.name
    write_wav(p, x)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", p, "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True).stderr
    os.unlink(p)
    v = [l for l in r.splitlines() if l.strip().startswith("I:")]
    return float(v[-1].split()[1]) if v else -70.0


def write_wav(p, x):
    import wave
    y = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(p, "wb") as w: w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(y.tobytes())


def sfx_whoosh(d=0.42, up=True):
    n = int(d * SR); t = np.arange(n) / SR
    noise = np.random.default_rng(7).standard_normal(n).astype(np.float32)
    env = np.sin(np.pi * np.clip(t / d, 0, 1)) ** 1.6
    # sweeping band-pass via two one-pole filters with moving cutoff
    out = np.zeros(n, np.float32); lp = hp = 0.0
    for i in range(n):
        fc = (600 + 3200 * (t[i] / d)) if up else (3800 - 3200 * (t[i] / d))
        a = 1 - math.exp(-2 * math.pi * fc / SR); lp += a * (noise[i] - lp)
        b = 1 - math.exp(-2 * math.pi * 250 / SR); hp += b * (lp - hp); out[i] = lp - hp
    out = out / (np.abs(out).max() + 1e-9) * env
    return np.stack([out, out], 1) * 0.30


def sfx_pop():
    n = int(0.09 * SR); t = np.arange(n) / SR
    f = 900 - 500 * t / 0.09; y = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 45)
    return np.stack([y, y], 1).astype(np.float32) * 0.22


def sfx_ding():
    n = int(0.7 * SR); t = np.arange(n) / SR
    y = (np.sin(2 * np.pi * 1318.5 * t) * 0.6 + np.sin(2 * np.pi * 1975.5 * t) * 0.35) * np.exp(-t * 6)
    return np.stack([y, y], 1).astype(np.float32) * 0.20


def mix_at(bus, clip, t, gain=1.0):
    i = int(round(t * SR))
    if i < 0: clip = clip[-i:]; i = 0
    j = min(len(bus), i + len(clip))
    if j > i: bus[i:j] += clip[:j - i] * gain


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edit"); ap.add_argument("--out", required=True)
    ap.add_argument("--no-plates", action="store_true"); ap.add_argument("--disclaimer", action="store_true")
    ap.add_argument("--preview", action="store_true", help="first 25 s only, faster check")
    ap.add_argument("--vo-out", help="also write the VO-only stem (timeline) to this wav, then exit")
    a = ap.parse_args()
    E = json.loads(Path(a.edit).read_text(encoding="utf-8")); base = Path(a.edit).parent
    P = lambda s: (base / s) if not Path(s).is_absolute() else Path(s)
    plates_on = not a.no_plates

    # talk segments on the frame grid
    segs, cur = [], 0
    for s in E["talk"]:
        n = int(round((s["out"] - s["in"]) * FPS)); segs.append({**s, "f0": cur, "n": n}); cur += n
    total_f = cur if not a.preview else min(cur, 25 * FPS)
    T = total_f / FPS
    cx, cy = E.get("anchor", [0.5, 0.33]); cx *= W; cy *= H
    zooms = sorted(E.get("zooms", [[0, 1.0]])); drift = E.get("drift", 0.02)
    bgcol = tuple(E.get("insert_bg", [32, 18, 8]))

    # ---- audio: VO from the same segments, each brought to vo_lufs on its own (clips differ in level)
    vo = np.zeros((int(math.ceil(T * SR)) + SR, 2), np.float32)
    target = E.get("vo_lufs", -14.0)
    for s in segs:
        if s["f0"] >= total_f: break
        x = load_audio(P(s["src"]), s["in"], s["n"] / FPS)
        g = 10 ** ((target - lufs(x)) / 20) if np.abs(x).max() > 1e-4 else 1.0
        fd = int(0.012 * SR); x[:fd] *= np.linspace(0, 1, fd)[:, None]; x[-fd:] *= np.linspace(1, 0, fd)[:, None]
        mix_at(vo, x, s["f0"] / FPS, min(g, 8.0))
    if a.vo_out:
        write_wav(a.vo_out, vo[:int(T * SR)]); print(f"wrote {a.vo_out}"); return
    bus = vo.copy()
    if E.get("music"):
        m = E["music"]; md = load_audio(P(m["src"]), m.get("offset", 0.0), T + 0.1)
        if len(md): md = md * 10 ** ((m.get("lufs", -26.0) - lufs(md)) / 20)
        fo = int(1.0 * SR); md[-fo:] *= np.linspace(1, 0, fo)[:, None]
        mix_at(bus, md, 0.0)
    if E.get("sfx", True) and plates_on:
        wh = sfx_whoosh(); wd = sfx_whoosh(up=False); pop = sfx_pop(); ding = sfx_ding()
        for ins in E.get("inserts", []):
            if ins.get("trans_in", "zoom") != "cut": mix_at(bus, wh, ins["at"] - 0.2)
            if ins.get("trans_out", "whip") != "cut": mix_at(bus, wd, ins["at"] + ins["dur"] - 0.2, 0.8)
        for p in E.get("plates", []): mix_at(bus, pop, p["at"])
        for c in E.get("checks", []): mix_at(bus, ding, c["at"] + 0.05)
    bus = bus[:int(T * SR)]
    tmp = Path(tempfile.mkdtemp()); wav = tmp / "mix.wav"; write_wav(str(wav), bus * 0.98)

    # ---- plates
    PL = []
    if plates_on:
        for p in E.get("plates", []):
            PL.append({**p, "layers": [(nm, to_np(im)) for nm, im in build_plate(p)]})
    disc = None
    if a.disclaimer:
        # only the text band (a full-frame float layer per frame ran out of RAM)
        y0d = DISC[0][1] - 10; f = ImageFont.truetype(str(F_REG), 30)
        band = Image.new("RGBA", (W, DISC[-1][1] - y0d + 50), (0, 0, 0, 0)); d = ImageDraw.Draw(band)
        for txt, y in DISC:
            w_ = f.getlength(txt); d.text(((W - w_) / 2 + 1, y - y0d + 1), txt, font=f, fill=(0, 0, 0, 70)); d.text(((W - w_) / 2, y - y0d), txt, font=f, fill=(255, 255, 255, 128))
        disc = (to_np(band), y0d + band.height / 2)

    subs = Subs(E["subs"], base) if (plates_on and E.get("subs")) else None
    inserts = sorted(E.get("inserts", []), key=lambda i: i["at"])
    for ins in inserts:
        ins["f0"] = int(round(ins["at"] * FPS)); ins["n"] = int(round(ins["dur"] * FPS))
        ins["tin"] = int(round(ins.get("tin", 0.27) * FPS)) if ins.get("trans_in", "zoom") != "cut" else 0
        ins["tout"] = int(round(ins.get("tout", 0.27) * FPS)) if ins.get("trans_out", "whip") != "cut" else 0
        if ins.get("kind") == "gauge":
            bg = cv2.imread(str(P(ins["bg"]))); bg = cv2.resize(bg, (W, H), interpolation=cv2.INTER_AREA)
            ins["bgimg"] = (cv2.GaussianBlur(bg, (0, 0), 9) * 0.42).astype(np.uint8)

    enc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS),
                            "-i", "-", "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr",
                            "-b:v", "6M", "-maxrate", "9M", "-bufsize", "12M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                            "-af", "alimiter=limit=0.76:level=false", "-shortest", "-movflags", "+faststart", a.out], stdin=subprocess.PIPE)
    si, rd = 0, None; ird = {}
    for fi in range(total_f):
        t = fi / FPS
        while si < len(segs) - 1 and fi >= segs[si]["f0"] + segs[si]["n"]: si += 1; rd and rd.close(); rd = None
        s = segs[si]
        if rd is None: rd = Reader(P(s["src"]), s["in"], s["n"])
        talk = rd.read()
        # punch-in level + slow drift
        z0, zt = 1.0, 0.0
        for zt_, zz in zooms:  # on the frame grid: a float compare missed the cut frame by one ulp (critic r2)
            if fi >= round(zt_ * FPS): z0, zt = zz, zt_
        frame = zoom_img(talk, z0 + drift * (t - zt) / 5.0, cx, cy)
        # inserts
        for k, ins in enumerate(inserts):
            if not (ins["f0"] - 0 <= fi < ins["f0"] + ins["n"]): continue
            j = fi - ins["f0"]; tl = j / FPS
            if ins.get("kind") == "gauge":
                im = ins["bgimg"].copy(); draw_gauge(im, tl, ins.get("label", "PRESSURE HOLD"), plates_on)
            else:
                if k not in ird: ird[k] = Reader(P(ins["src"]), ins.get("in", 0.0), ins["n"], speed=ins.get("speed", 1.0))
                im = fit_insert(ird[k].read(), ins.get("scale", 1.0), bgcol)
                im = zoom_img(im, 1.0 + 0.04 * tl / max(0.1, ins["dur"]), W / 2, H * 0.4)
            for c in E.get("targets", []):
                if ins["at"] <= c["at"] < ins["at"] + ins["dur"] and plates_on: draw_target(im, t - c["at"], c.get("x", 0.5), c.get("y", 0.4), c.get("r", 170))
            for c in E.get("checks", []):
                if ins["at"] <= c["at"] < ins["at"] + ins["dur"] and plates_on: draw_check(im, t - c["at"], c.get("x", 0.5), c.get("y", 0.4), c.get("r", 150))
            if j < ins["tin"]:
                im = trans_zoom(frame, im, (j + 1) / (ins["tin"] + 1), cx, cy) if ins.get("trans_in", "zoom") == "zoom" else im
            elif j >= ins["n"] - ins["tout"] and ins.get("trans_out", "whip") == "whip":
                im = trans_whip(im, frame, (j - (ins["n"] - ins["tout"]) + 1) / (ins["tout"] + 1))
            frame = im
            if j == ins["n"] - 1 and k in ird: ird[k].close(); del ird[k]
        # plates
        for p in PL:
            if not (p["at"] - 0.01 <= t < p["end"]): continue
            tl, tr = t - p["at"], p["end"] - t
            y = p.get("y", 0.58) * H
            out_a = ease_out_cubic(tr / 0.15) if p.get("exit", True) else 1.0
            for li, (nm, layer) in enumerate(p["layers"]):
                if p["kind"] == "phase":
                    u = (tl - 0.08 * li) / 0.32; x = W / 2 - (1 - ease_out_back(u, 1.4)) * W * 0.75
                    lab_h = p["layers"][0][1][1].shape[0]; main_h = p["layers"][1][1][1].shape[0]
                    yy = y if nm == "label" else y + lab_h / 2 + main_h / 2 - 62
                    blend(frame, layer, x, yy, alpha=min(1, clamp(u * 3)) * out_a)
                elif p["kind"] == "cta":
                    pulse = 1 + 0.035 * max(0.0, math.sin(2 * math.pi * tl / E.get("beat", 0.604)))
                    blend(frame, layer, W / 2, y, scale=ease_out_back(tl / 0.3) * pulse, alpha=min(1, tl / 0.08) * out_a)
                else:
                    blend(frame, layer, W / 2, y, scale=0.55 + 0.45 * ease_out_back(tl / 0.24), alpha=min(1, (tl + 0.1) / 0.1) * out_a)
        if subs is not None: subs.draw(frame, t)
        if disc is not None: blend(frame, disc[0], W / 2, disc[1])
        enc.stdin.write(frame.tobytes())
        if fi % 150 == 0: print(f"  {t:5.1f}s / {T:.1f}s", flush=True)
    enc.stdin.close(); enc.wait(); rd and rd.close()
    import shutil; shutil.rmtree(tmp, ignore_errors=True)  # the mix wav (a full disk killed a render)
    print(f"wrote {a.out} ({T:.2f}s)")


if __name__ == "__main__":
    main()
