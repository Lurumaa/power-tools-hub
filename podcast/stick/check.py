#!/usr/bin/env python3
"""check.py - wait for one Omni job, download it and QA the new part. Run from podcast/.

    python stick/check.py <project> <job_id> <name> --text "<words of the new part>" [--from S] [--parent NAME] [--expect S]

--from    where the new part starts (an extend: the parent's length; a fresh clip: 0)
--parent  file name (without .mp4) of the parent clip in projects/<project>/clips - its part must come back untouched
--expect  total length the clip must have (parent length + added seconds)

Checks (all learned the hard way on example, 2026-10-06):
  words     pod.py qa-file: whisper + literal wav2vec2 (repeats, swallowed/cut words, mid-sentence pauses)
  lipsync   lipcheck.py: voice while the mouth is still (the base clip once acted a gesture with a closed mouth)
  parent    psnr of the parent part: an extend once re-rendered the previous 10 s as a zoomed close-up
  length    an extend once came back built on an OLDER node of the chain (28+4 s instead of 35+4 s)
  sheet     clips/<name>_sheet.jpg - READ IT: burned-in subtitles, zoom/reframe, style drift
Prints one verdict line: CHECK PASS / CHECK FAIL <reasons>.
It only sees the CDN, NOT a Kaleido moderation block: ~40 s after every launch call get_video_generation_job once;
failed/moderation -> no charge, kill this check, fix the prompt (the girl hook was blocked: child-looking character +
relationship words -> adult-character line in every prompt).
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CDN = "https://<kaleido-cdn>/video-studio/{}.mp4"


def run(cmd, **kw): return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def dur(f): return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)]).stdout.strip())


def psnr(a, b, t0, t1):
    out = run(["ffmpeg", "-ss", f"{t0}", "-to", f"{t1}", "-i", str(a), "-ss", f"{t0}", "-to", f"{t1}", "-i", str(b), "-lavfi", "psnr", "-f", "null", "-"]).stderr
    m = re.search(r"average:([0-9.]+|inf)", out)
    return float("inf") if m and m.group(1) == "inf" else (float(m.group(1)) if m else 0.0)


def sharp(f, times):
    """mean Laplacian variance of the centre crop (character outlines) at the given times"""
    import numpy as np
    vals = []
    for t in times:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t)}", "-i", str(f), "-frames:v", "1", "-vf", "scale=720:1280,crop=360:640:180:320,format=gray",
                              "-f", "rawvideo", "-"], capture_output=True).stdout
        if len(raw) < 360 * 640: continue
        a = np.frombuffer(raw[:360 * 640], np.uint8).reshape(640, 360).astype(np.float32)
        vals.append(float((a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]).var()))
    return sum(vals) / max(1, len(vals))


def main():
    p = argparse.ArgumentParser(); p.add_argument("project"); p.add_argument("job"); p.add_argument("name")
    p.add_argument("--text", required=True); p.add_argument("--from", dest="frm", type=float, default=0.0)
    p.add_argument("--parent"); p.add_argument("--expect", type=float); p.add_argument("--timeout", type=int, default=420)
    a = p.parse_args()
    clips = ROOT / "projects" / a.project / "clips"; clips.mkdir(parents=True, exist_ok=True)
    f = clips / f"{a.name}.mp4"; url = CDN.format(a.job); t0 = time.time()
    while True:
        try:
            req = urllib.request.Request(url, method="HEAD")
            if urllib.request.urlopen(req, timeout=20).status == 200: break
        except Exception: pass
        if time.time() - t0 > a.timeout: sys.exit(f"CHECK PENDING {a.job}: not on the CDN after {a.timeout}s - look at get_video_generation_job (moderation?)")
        time.sleep(6)
    time.sleep(2); urllib.request.urlretrieve(url, f)
    fails, d = [], dur(f)
    print(f"file {f.name}: {d:.2f}s")
    if a.expect and abs(d - a.expect) > 0.6:
        fails.append(f"length {d:.2f}s, expected {a.expect:.2f}s - the extend was built on another chain node; take only the new tail and cut on a sentence end")
    # pauses in the new part
    sil = re.findall(r"silence_(start|end): ([0-9.]+)", run(["ffmpeg", "-ss", f"{a.frm}", "-i", str(f), "-af", "silencedetect=n=-35dB:d=0.3", "-f", "null", "-"]).stderr)
    print("pauses (from new part start):", " ".join(f"{k[0]}{float(v):.2f}" for k, v in sil))
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a.frm}", "-i", str(f), "-vf", "fps=1.5,scale=200:-1,tile=6x2", "-frames:v", "1", str(clips / f"{a.name}_sheet.jpg")])
    # zoomed faces of the new part - READ IT: brows slanted down = angry (men on 'confident'), grins while talking, sad faces
    n_f = max(4, min(14, int((d - a.frm) * 2)))
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a.frm}", "-i", str(f), "-vf",
         f"fps={n_f / max(0.5, d - a.frm):.3f},scale=720:1280,crop=300:230:210:410,scale=150:-1,tile={n_f}x1", "-frames:v", "1", str(clips / f"{a.name}_faces.jpg")])
    if a.parent:
        par = clips / f"{a.parent}.mp4"; split = max(0.0, a.frm - 10)
        for w0, w1 in ([(0, split)] if split > 0.5 else []) + [(split, a.frm - 0.1)]:
            v = psnr(par, f, w0, w1); print(f"parent {a.parent} [{w0:.1f}-{w1:.1f}] psnr {v:.1f}")
            if v < 35: fails.append(f"parent part {w0:.1f}-{w1:.1f}s re-rendered (psnr {v:.1f}) - compare frames, likely a zoom/reframe")
    if a.frm > 2:  # every extend blurs a bit more (boy: 650 -> 413 -> 391 -> 330 -> 288); compare with the chain's fresh base
        fmt = json.loads((ROOT / "formats" / "stick_talking.json").read_text(encoding="utf-8"))
        base_s = sharp(f, [0.6, 1.2, 1.8]); new_s = sharp(f, [a.frm + 0.8, (a.frm + d) / 2, d - 0.5]); r = new_s / max(1, base_s)
        print(f"sharpness new/base {new_s:.0f}/{base_s:.0f} = {r:.2f} (fail < {fmt['sharpness_fail']}, warn < {fmt['sharpness_warn']})")
        if r < fmt["sharpness_fail"]: fails.append(f"blurred: sharpness {r:.2f} of the chain base - start a fresh chain from the character image at this piece")
    lip = run([sys.executable, str(ROOT / "stick" / "lipcheck.py"), str(f), f"{a.frm}"]).stdout.strip().splitlines()
    print("lipsync:", lip[-1] if lip else "?")
    if lip and "none" not in lip[-1]: fails.append("voice with a still mouth: " + lip[-1].split(":", 1)[1].strip())
    env = dict(os.environ); env.setdefault("POD_WHISPER_MODEL", "small")   # this PC has little free RAM for large-v3
    q = run([sys.executable, str(ROOT / "pod.py"), "qa-file", str(f), a.text, "--kind", "answer", "--from", f"{a.frm}"], cwd=ROOT, env=env).stdout
    try:
        j = json.loads(q[q.index("{"):])
        print("heard:", j.get("heard")); print("speech:", j.get("speech"), "warns:", j.get("warns"))
        first = re.findall(r"[a-z']+", a.text.lower())[:1]
        heard_ok = first and (j.get("verbatim") or "").startswith(first[0]) and (j.get("heard") or "").lower().startswith(first[0])
        for e in j.get("errors", []):
            if "starts mid-sound" in e and heard_ok and not a.frm:   # a hook/base that talks from frame 1 is fine when both ASRs hear word 1
                print("warn (first word heard by both ASRs):", e)
            else:
                fails.append(f"words: {e}")
        n = len(re.findall(r"[A-Za-z0-9']+", a.text)); s0 = a.frm + max(0.0, j.get("join_gap") or 0.0) if a.frm else j["speech"][0]
        if j.get("speech"): print(f"pace ~{n / max(0.5, j['speech'][1] - s0):.1f} wps (planned 3.5)")
    except Exception:  # low RAM (After Effects open): heavy QA segfaults -> light check, logged as such
        print("heavy QA crashed (low RAM) -> light check: whisper small.en words + level of the last 0.1 s")
        try:
            import numpy as np
            from faster_whisper import WhisperModel
            m = WhisperModel("small.en", device="cpu", compute_type="int8")
            part = clips / f"_{a.name}_part.wav"   # transcribe ONLY the new part (clip_timestamps made whisper invent words)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a.frm}", "-i", str(f), "-ac", "1", "-ar", "16000", str(part)])
            segs, _ = m.transcribe(str(part), language="en", word_timestamps=True)
            segs = list(segs); part.unlink(missing_ok=True)
            heard = " ".join(w.word.strip() for s in segs for w in s.words)
            norm = lambda t: re.findall(r"[a-z0-9']+", t.lower().replace("15", "fifteen"))
            want, got = norm(a.text.replace("15", "fifteen")), norm(heard)
            print("heard (small.en):", heard)
            if got != want: fails.append(f"words (light check) differ: want {len(want)} words, heard {len(got)}")
            pcm = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-sseof", "-0.1", "-i", str(f), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                                               capture_output=True).stdout, np.int16).astype(np.float32) / 32768
            tail_db = 20 * np.log10(np.sqrt((pcm ** 2).mean()) + 1e-9)
            print(f"last 0.1 s level {tail_db:.1f} dB")
            if tail_db > -35: fails.append(f"voice in the last 0.1 s ({tail_db:.0f} dB) - last word may be cut")
            print("NOTE: light check only (no verbatim repeat/swallow layer) - say so in RUNLOG")
        except Exception as e:
            fails.append(f"qa crashed, light check too: {e}")
    print("CHECK PASS - now Read the sheet" if not fails else "CHECK FAIL " + " | ".join(fails))


if __name__ == "__main__":
    main()
