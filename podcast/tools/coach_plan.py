"""Edit-plan builder for coach_montage.py: everything is anchored to SOURCE time in a take, so cutting a pause or
moving an insert never shifts the rest by hand. A variant's build_edit.py is ~40 lines:

    from coach_plan import Plan
    P = Plan({"t0": "clips/t0s1_x.mp4", ...}, [("t0", 0.74, 2.36), ("t0", 2.57, 6.70), ...])
    i0 = P.insert("t0", 5.56, "t0", 7.61, kind="clip", src=A + "K1_spasm.mp4", **{"in": 2.5}, scale=0.80)
    P.zoom(P.seg(1), 1.12); P.zoom(i0[1], 1.0) ...
    P.plate(kind="phase", tag="START:", text="...", color="#FF7A1A", at=P.at("t3", 0.1), end=..., y=0.585)
    P.write(sys.argv[1], subs="words/subs.json", music=...)
Insert/plate/zoom times are timeline seconds returned by P.at(clip, src_t) / P.seg(k).
House layout : subs y 0.735 white caps on ONE yellow marker, headline 0.50, phase plates 0.585,
CTA 0.56, inserts scale 0.80 anchored to the top, bottom 20 % free."""
import json

FPS = 30
ANIM = "../task_976/anim/"
MUSIC = {"src": "../task_976/music/motivation-motivation-music.mp3", "offset": 1.25, "lufs": -24}


class Plan:
    def __init__(self, clips, segments):
        self.clips, self.SEG = clips, segments
        self.talk, self.starts, f = [], [], 0
        for c, i, o in segments:
            n = round((o - i) * FPS); self.starts.append(f / FPS)
            self.talk.append({"src": clips[c], "in": i, "out": round(i + n / FPS, 4)}); f += n
        self.T = f / FPS
        self.ins, self.zooms, self.plates, self.checks, self.targets = [], [[0, 1.0]], [], [], []

    def at(self, c, s):
        """timeline time of source time s in clip c (inside a kept segment; in a cut gap -> next segment start)"""
        for k, (cc, i, o) in enumerate(self.SEG):
            if cc == c and i - 1e-6 <= s <= self.talk[k]["out"] + 1e-6: return round(self.starts[k] + (s - i), 3)
        for k, (cc, i, o) in enumerate(self.SEG):
            if cc == c and s < i: return round(self.starts[k], 3)
        raise ValueError(f"{c} {s}: not in any segment")

    def seg(self, k): return round(self.starts[k], 3)

    def insert(self, c0, s0, c1, s1, **kw):
        a, b = self.at(c0, s0), self.at(c1, s1); self.ins.append({"at": a, "dur": round(b - a, 3), **kw}); return a, b

    def insert_tl(self, a, b, **kw):
        self.ins.append({"at": round(a, 3), "dur": round(b - a, 3), **kw}); return a, b

    def zoom(self, t, z): self.zooms.append([round(t, 3), z])
    def plate(self, **p): self.plates.append(p)
    def check(self, t, x=0.5, y=0.50, r=100): self.checks.append({"at": t, "x": x, "y": y, "r": r})
    def target(self, t, x=0.5, y=0.371, r=180): self.targets.append({"at": t, "x": x, "y": y, "r": r})

    def write(self, path, subs=None, music=MUSIC, anchor=(0.45, 0.30)):
        E = {"anchor": list(anchor), "drift": 0.025, "insert_bg": [34, 20, 8], "vo_lufs": -14, "sfx": True, "beat": 0.604,
             "talk": self.talk, "zooms": sorted(self.zooms), "inserts": self.ins, "targets": self.targets,
             "checks": self.checks, "plates": self.plates, "music": music}
        if subs:
            E["subs"] = {"file": subs, "y": 0.735, "size": 62, "maxw": 860, "pad": [24, 12],
                         "colors": [[0, "#FFD60A"]], "dark_under_marker": False}
        json.dump(E, open(path, "w"), indent=1)
        print(f"T={self.T:.2f}s  inserts " + " ".join(f"{x['at']:.2f}+{x['dur']:.2f}" for x in self.ins))
        return E
