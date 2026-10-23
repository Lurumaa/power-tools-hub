#!/usr/bin/env python3
"""timeline.py - assemble hook + body pieces into an editor project and audit the mixed audio. Run from podcast/.

    python stick/timeline.py <project> <name> <segments.json> <script.txt>

segments.json: [{"file": "hook_boy.mp4", "from": 0, "to": 5}, {"file": "body_boy_e5.mp4", "from": 0, "to": 39}, ...]
  file      a clip in projects/<project>/clips
  from/to   the part of that clip to use (a whole chain, or one piece of it when a piece had to be replaced)
  "raw": true  cut exactly at from/to (no speech-edge trim): for the pieces of ONE chain taken each from the file
            where it was generated first - every extend re-encodes the whole clip (psnr 46 -> 42 dB by e4), so piece k
            comes from body_<char>_<step k>.mp4 [parent length .. own length]; the seam lies in the tail silence.
  Put "raw" on every piece of a chain except the chain's first (its start) and last (its end) edges: use
  {"file": ..., "from": 0, "to": 9.0, "trim_out": false} etc. - see segments_girl.json.
Cut points are found here, never by hand: in = first speech onset after `from` - 0.12 s, out = last speech end
before `to` + 0.12 s (a hand cut at 9.2 s swallowed the "I" that started at 9.11 s). Writes
editor/projects/<name>.json (videos copied to editor/videos/<name>/), mixes the audio exactly like the editor
(40 ms fades) and runs pod.py qa-file on the WHOLE script. Hand over only on TIMELINE PASS.
"""
import json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAD = 0.12


def run(cmd, **kw): return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def speech_edges(f, t0, t1):
    log = run(["ffmpeg", "-ss", f"{t0}", "-to", f"{t1}", "-i", str(f), "-af", "silencedetect=n=-40dB:d=0.08", "-f", "null", "-"]).stderr
    ev = [(k, t0 + float(v)) for k, v in re.findall(r"silence_(start|end): ([0-9.]+)", log)]
    on = t0
    if ev and ev[0][0] == "end" or (ev and ev[0][0] == "start" and abs(ev[0][1] - t0) < 0.02):  # leading silence
        on = next((t for k, t in ev if k == "end"), t0)
    off = t1
    if ev and ev[-1][0] == "end" and t1 - ev[-1][1] < 0.06: ev = ev[:-1]   # newer ffmpeg closes a silence at EOF
    if ev and ev[-1][0] == "start": off = ev[-1][1]           # trailing silence
    return max(t0, on - PAD), min(t1, off + PAD)


def main():
    if len(sys.argv) < 5: sys.exit(__doc__)
    proj, name, segf, scriptf = sys.argv[1:5]
    clips = ROOT / "projects" / proj / "clips"; vdir = ROOT.parent / "editor" / "videos" / name; vdir.mkdir(parents=True, exist_ok=True)
    segs = json.loads(Path(segf).read_text(encoding="utf-8"))
    lines = {l.split(":", 1)[0].strip().upper(): l.split(":", 1)[1].strip() for l in Path(scriptf).read_text(encoding="utf-8").splitlines() if ":" in l}
    script = (lines["HOOK"] + " " + lines["BODY"]).replace(" | ", " ").replace("|", " ")
    media, items, t, cut = [], [], 0.0, []
    for k, s in enumerate(segs):
        src = clips / s["file"]; shutil.copy2(src, vdir / s["file"])
        i, o = speech_edges(src, s["from"], s["to"])
        if s.get("raw") or s.get("trim_in") is False: i = s["from"]
        if s.get("raw") or s.get("trim_out") is False: o = s["to"]
        cut.append((s["file"], i, o))
        mid = f"m{k}"
        if not any(m["src"].endswith(s["file"]) for m in media): media.append({"id": mid, "src": f"videos/{name}/{s['file']}", "name": s["file"]})
        mid = next(m["id"] for m in media if m["src"].endswith(s["file"]))
        items.append({"kind": "video", "track": 0, "media": mid, "start": round(t, 3), "in": round(i, 3), "out": round(o, 3), "link": f"L{k}"})
        items.append({"kind": "audio", "track": k % 2, "media": mid, "start": round(t, 3), "in": round(i, 3), "out": round(o, 3), "fade": 0.04, "link": f"L{k}"})
        print(f"{t:6.2f}s  {s['file']:<24} in {i:6.2f} out {o:6.2f}")
        t += o - i
    (ROOT.parent / "editor" / "projects" / f"{name}.json").write_text(json.dumps({"name": name, "media": media, "tracks": {"v": 1, "a": 2}, "items": items}, indent=1), encoding="utf-8")
    print(f"total {t:.2f}s -> editor/projects/{name}.json")
    inp, fc = [], []
    for k, (fname, i, o) in enumerate(cut):
        inp += ["-ss", f"{i}", "-t", f"{o - i:.3f}", "-i", str(clips / fname)]
        fc.append(f"[{k}:a]aresample=48000,afade=t=in:d=0.04,afade=t=out:st={o - i - 0.04:.3f}:d=0.04[a{k}]")
    fc.append("".join(f"[a{k}]" for k in range(len(cut))) + f"concat=n={len(cut)}:v=0:a=1[a]")
    wav = clips / f"{name}_timeline.wav"
    run(["ffmpeg", "-v", "error", "-y", *inp, "-filter_complex", ";".join(fc), "-map", "[a]", str(wav)])
    env = dict(os.environ); env.setdefault("POD_WHISPER_MODEL", "small")
    q = run([sys.executable, str(ROOT / "pod.py"), "qa-file", str(wav), script, "--kind", "answer"], cwd=ROOT, env=env).stdout
    try:
        j = json.loads(q[q.index("{"):]); errs = j.get("errors", [])
        w1 = re.findall(r"[a-z']+", script.lower())[:1]   # a hook that talks from frame 1 is fine when both ASRs hear word 1
        if w1 and (j.get("verbatim") or "").startswith(w1[0]) and (j.get("heard") or "").lower().startswith(w1[0]):
            errs = [e for e in errs if "starts mid-sound" not in e]
        print("warns:", j.get("warns"))
        print("TIMELINE PASS" if not errs else "TIMELINE FAIL " + " | ".join(errs))
    except Exception:
        print(q[-800:]); print("TIMELINE FAIL qa crashed")
    print(f"open: http://localhost:8765/?project=projects/{name}.json")


if __name__ == "__main__":
    main()
