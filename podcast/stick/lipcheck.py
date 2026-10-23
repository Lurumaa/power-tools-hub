# lipcheck.py <clip> [from_s] - lipsync check for the stick-figure clips: finds the face (skin blob), measures how much
# the lower half of the face changes frame to frame, and flags stretches where there is voice but the mouth is still.
import sys, subprocess, numpy as np
f = sys.argv[1]; fr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
W, H, FPS = 360, 640, 24
raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(fr), "-i", f, "-vf", f"scale={W}:{H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True).stdout
v = np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3).astype(np.int16)
aud = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(fr), "-i", f, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
a = np.frombuffer(aud, np.int16).astype(np.float32) / 32768
R, G, B = v[..., 0], v[..., 1], v[..., 2]
skin = (R > 200) & (G > 160) & (G < 225) & (B > 120) & (B < 195) & (R - B > 45)
mouths = []
for k in range(len(v)):
    ys, xs = np.nonzero(skin[k])
    if len(ys) < 200: mouths.append(None); continue
    keep = ys < np.percentile(ys, 60) + 40                # face only (hands sit lower / to the side)
    ys, xs = ys[keep], xs[keep]
    y0, y1, x0, x1 = np.percentile(ys, 3), np.percentile(ys, 97), np.percentile(xs, 5), np.percentile(xs, 95)
    cy, cx = int(y0 + (y1 - y0) * 0.78), int((x0 + x1) / 2); hh, hw = int((y1 - y0) * 0.16), int((x1 - x0) * 0.22)
    m = v[k, max(0, cy - hh):cy + hh, max(0, cx - hw):cx + hw].mean(axis=2)
    mouths.append(np.array(np.resize(m, (24, 32)), np.float32) if m.size else None)
d = [0.0] + [float(np.abs(mouths[i] - mouths[i - 1]).mean()) if mouths[i] is not None and mouths[i - 1] is not None else 0.0 for i in range(1, len(mouths))]
d = np.array(d); win = int(FPS * 0.5); bad = []
for i in range(0, len(d) - win, win // 2):
    t = fr + i / FPS; seg = a[int(i / FPS * 16000):int((i + win) / FPS * 16000)]
    db = 20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9)
    if db > -32 and d[i:i + win].mean() < 1.2: bad.append((round(t, 2), round(db, 1), round(float(d[i:i + win].mean()), 2)))
print("mouth motion median:", round(float(np.median(d)), 2))
print("VOICE-BUT-STILL-MOUTH (t, dB, motion):", bad if bad else "none")
