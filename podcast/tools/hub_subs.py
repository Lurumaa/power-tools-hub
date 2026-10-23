"""Power Tools Hub subtitle styles of the engines "motion" and "canvas" burned onto a video with the hub's OWN drawing code, so the
video matches the card the user picked (no re-implementation per style).

  python tools/hub_subs.py --style projects/<id>/subs_style.json --words <words.json> --video <in.mp4> --out <out.mp4>
      [--offset SECONDS] [--preview SECONDS] [--param name=value ...] [--timeout 30]

--style   the preset as saved from the hub ("use subs X": ArtifactData get sublib/X -> projects/<id>/subs_style.json)
--words   word times of the voice in THIS video: [[word, start, end], ...] (tools/align_words.py) or [{text, start, end}]
          (spelling as in the script; --offset shifts them, e.g. when the voice starts later in the video)
--param   canvas styles: slider values (size=1.2, color=#00a8ff); defaults = the preset's own
--preview render only the first N seconds (quick look)

How it draws:
  motion -> skillhub/index.html served on localhost in headless Chromium, the hub's attachView + drawSubFrame on a transparent
            1080x1920 canvas, frame by frame at the video's fps (9:16 videos only - Motion styles are vertical).
  canvas -> skillhub/tools/build_page.js: the hub's canvas runtime with the preset code inside a sandboxed iframe, network
            cut except Google Fonts, frame size 1080x1920 or 1920x1080 by the video's shape.
Frames (RGBA) go straight into ffmpeg and are laid over the video (scaled to its size); audio is copied untouched;
H.264 NVENC ~5 Mbit/s (libx264 if NVENC is missing). The browser runs in a child process: if one frame makes no progress for
--timeout seconds (a preset that hangs) the whole process tree is killed and the run fails with a clear message.
editor / word / coach styles: use tools/karaoke_subs.py, tools/word_captions.py, coach_montage (see CLAUDE.md).
"""
import argparse, base64, functools, http.server, json, os, queue, socketserver, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                 # podcast/
HUB = ROOT.parent / "skillhub"                             # skillhub/index.html + tools/build_page.js
WORK_FLAG = "--__worker"


# ------------------------------------------------------------------ helpers
def probe(video):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,r_frame_rate,nb_frames,duration:format=duration", "-of", "json", str(video)],
                                  capture_output=True, text=True, check=True).stdout)
    st = j["streams"][0]; num, den = map(int, st["r_frame_rate"].split("/"))
    fps = num / den
    dur = float(st.get("duration") or j["format"]["duration"])
    n = int(st["nb_frames"]) if st.get("nb_frames") else int(round(dur * fps))
    return int(st["width"]), int(st["height"]), fps, n


def load_words(path, offset):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows.get("words", [])
    out = []
    for r in rows:
        if isinstance(r, (list, tuple)): w, s, e = r[0], r[1], r[2]
        else: w, s, e = r.get("text", r.get("word")), r.get("start"), r.get("end")
        out.append({"text": str(w), "start": round(float(s) + offset, 3), "end": round(float(e) + offset, 3)})
    if not out: raise SystemExit("no words in " + str(path))
    return out


def has_nvenc():
    """a real 0.2 s test encode: ffmpeg builds list h264_nvenc even on PCs without an NVIDIA GPU"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                        "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


def kill_tree(pid):
    if os.name == "nt": subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try: os.killpg(os.getpgid(pid), 9)
        except Exception: pass


# ------------------------------------------------------------------ worker (runs in a child process)
def worker(spec):
    from playwright.sync_api import sync_playwright
    say = lambda **m: print(json.dumps(m), flush=True)
    eng, FW, FH, fps, n, t0 = spec["engine"], spec["FW"], spec["FH"], spec["fps"], spec["frames"], spec["start"]
    ff = subprocess.Popen(spec["ffmpeg"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    srv = None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": FW, "height": FH}, device_scale_factor=1)
            if eng == "canvas":
                def route(rt):
                    u = rt.request.url
                    if u.startswith("file:") or u.startswith("data:") or "fonts.googleapis.com" in u or "fonts.gstatic.com" in u: rt.continue_()
                    else: rt.abort("blockedbyclient")
                ctx.route("**/*", route)
                pg = ctx.new_page()
                pg.goto(Path(spec["page"]).as_uri())
                pg.wait_for_function("window.__ready === true", timeout=0)
                pg.evaluate("() => document.fonts && document.fonts.ready")
                pg.evaluate("window.__init()"); pg.evaluate("window.__render(0)"); pg.wait_for_timeout(400)
                say(stage="ready")
                for i in range(n):
                    t = t0 + i / fps
                    pg.evaluate("t => window.__render(t)", t)
                    ff.stdin.write(pg.screenshot(omit_background=True, type="png"))
                    say(frame=i + 1, t=round(t, 3))
                errs = pg.evaluate("window.__errs")
                if errs: say(errors=errs[:5])
            else:
                class Handler(http.server.SimpleHTTPRequestHandler):   # the hub file has no <meta charset>: say UTF-8 in the header
                    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
                    def log_message(self, *a, **k): pass
                handler = functools.partial(Handler, directory=str(HUB))
                srv = socketserver.TCPServer(("127.0.0.1", 0), handler); port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                pg = ctx.new_page()
                pg.goto(f"http://localhost:{port}/index.html")
                pg.wait_for_function("!!window.__hub", timeout=60000)
                pg.evaluate("""async ({ style, words, total }) => {
                  const H = window.__hub; await document.fonts.ready;
                  const box = document.createElement('div'); box.className = 'stage';
                  box.style.cssText = 'position:fixed;left:0;top:0;width:1080px;height:1920px;z-index:999999;background:transparent;border-radius:0;aspect-ratio:auto';
                  const cv = document.createElement('canvas'); box.append(cv); document.body.append(box);
                  const p = H.normalizePreset(style);
                  const v = H.attachView(cv, p, { bg: 'none' }); H.subViews.delete(v);
                  try { await H.ensureSubFont(p); } catch (e) {}
                  await document.fonts.ready;
                  v.tw = { words: words.map(w => w.text), times: words.map(w => [w.start, w.end]), total };
                  v.L = H.subLayout(v.p, v.tw); v.playing = false;
                  window.__v = v; window.__H = H;
                  return [cv.width, cv.height];
                }""", {"style": spec["style"], "words": spec["words"], "total": spec["total"]})
                say(stage="ready")
                for i in range(n):
                    t = t0 + i / fps
                    url = pg.evaluate("t => { window.__H.drawSubFrame(window.__v, t); return window.__v.canvas.toDataURL('image/png'); }", t)
                    ff.stdin.write(base64.b64decode(url.split(",", 1)[1]))
                    say(frame=i + 1, t=round(t, 3))
            b.close()
        ff.stdin.close(); rc = ff.wait()
        if rc: say(error="ffmpeg failed: " + ff.stderr.read().decode("utf-8", "replace")[-600:])
        else: say(done=True)
    finally:
        if srv: srv.shutdown()


# ------------------------------------------------------------------ main
def main():
    if WORK_FLAG in sys.argv:
        return worker(json.loads(Path(sys.argv[sys.argv.index(WORK_FLAG) + 1]).read_text(encoding="utf-8")))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", required=True); ap.add_argument("--words", required=True)
    ap.add_argument("--video", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--offset", type=float, default=0.0); ap.add_argument("--preview", type=float)
    ap.add_argument("--param", action="append", default=[]); ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args()
    raw = json.loads(Path(a.style).read_text(encoding="utf-8"))
    style = raw["data"] if isinstance(raw.get("data"), dict) and "engine" not in raw else raw
    eng = style.get("engine", "editor")
    if eng not in ("motion", "canvas"):
        sys.exit(f'engine "{eng}": use tools/karaoke_subs.py (editor), tools/word_captions.py (word) or coach_montage (coach)')
    W, H, fps, n = probe(a.video)
    if a.preview: n = min(n, int(round(a.preview * fps)))
    FW, FH = (1080, 1920) if H >= W else (1920, 1080)
    if eng == "motion" and W > H: sys.exit("Motion styles are vertical (9:16); this video is horizontal - use a Canvas style")
    words = load_words(a.words, a.offset)
    total = max(w["end"] for w in words) + 1.0
    work = Path(a.out).resolve().parent / (Path(a.out).stem + "_hubsubs"); work.mkdir(parents=True, exist_ok=True)
    spec = {"engine": eng, "FW": FW, "FH": FH, "fps": fps, "frames": n, "start": 0.0, "words": words, "total": total, "style": style}
    if eng == "canvas":
        over = {}
        for kv in a.param:
            k, v = kv.split("=", 1); over[k] = v if v.startswith("#") else float(v)
        (work / "words.json").write_text(json.dumps(words), encoding="utf-8")
        (work / "preset.json").write_text(json.dumps(style), encoding="utf-8")
        page = work / "page.html"
        subprocess.run(["node", str(HUB / "tools" / "build_page.js"), str(work / "preset.json"), str(page), str(FW), str(FH),
                        json.dumps(over), str(work / "words.json")], check=True, capture_output=True, text=True)
        spec["page"] = str(page)
    venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "5M", "-maxrate", "7M", "-bufsize", "10M"] if has_nvenc() else ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    spec["ffmpeg"] = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(a.video), "-f", "image2pipe", "-c:v", "png", "-framerate", f"{fps:.6f}", "-i", "-",
                      "-filter_complex", f"[1:v]scale={W}:{H}:flags=lanczos,format=rgba[s];[0:v][s]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[v]",
                      "-map", "[v]", "-map", "0:a?", "-c:a", "copy", *venc, "-r", f"{fps:.6f}"] + (["-t", f"{n / fps:.3f}"] if a.preview else []) + ["-movflags", "+faststart", str(a.out)]
    sp = work / "spec.json"; sp.write_text(json.dumps(spec), encoding="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, __file__, WORK_FLAG, str(sp)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    q = queue.Queue()
    def pump():
        for l in child.stdout: q.put(l)
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()
    last, frame, t_start, ok, msgs = time.time(), 0, time.time(), False, []
    while True:
        try: line = q.get(timeout=1.0)
        except queue.Empty: line = ""
        if line is None: break
        if line:
            last = time.time()
            try: m = json.loads(line)
            except ValueError: msgs.append(line.rstrip()); continue
            if "frame" in m:
                frame = m["frame"]
                if frame % 60 == 0 or frame == n: print(f"  {frame}/{n} frames ({frame / max(1e-6, time.time() - t_start):.1f} fps)", flush=True)
            if m.get("errors"): print("preset errors:", m["errors"])
            if m.get("error"): msgs.append(m["error"])
            if m.get("done"): ok = True
        limit = a.timeout * (4 if frame == 0 else 1)          # page + fonts may take longer than one frame
        if time.time() - last > limit:
            kill_tree(child.pid)
            sys.exit(f"stopped: no progress for {limit:.0f} s at frame {frame}/{n} - the style hangs or is far too slow")
    child.wait()
    if not ok: sys.exit("render failed:\n" + "\n".join(msgs[-15:]))
    print(f"{eng} style '{style.get('name')}' -> {a.out} ({n} frames at {fps:.3f} fps, {W}x{H}, drawn at {FW}x{FH})")


if __name__ == "__main__":
    main()
