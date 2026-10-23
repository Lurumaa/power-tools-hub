"""Preview a Power Tools Hub Canvas subtitle preset exactly as the hub runs it (same runtime, same sample text and timing).

  python preview.py <preset.json> <out_dir> [--fps 30] [--sheet-only] [--fmt 9:16|16:9] [--param name=value ...]
                    [--strip T0 T1 N] [--shots t1,t2,...] [--timeout 20]

Writes into out_dir:
  sheet.png    - 12 frames across the clip, each on dark | light | photo backdrops (look at this first)
  preview.mp4  - the whole clip, dark | light | photo side by side, tiles in the frame's shape (skipped with --sheet-only)
  strip.png    - with --strip: N frames from T0 to T1 on the dark backdrop (9:16: the caption band, 16:9: the whole frame)
  shot_<t>.png - with --shots: full-res frames (9:16: the caption band), dark over light
  errors.txt   - runtime errors thrown by the preset code, blocked network requests and a hang (empty = none)

The preset code is untrusted (anyone can write to the shared hub database), so it is fenced in:
  - like in the hub it runs inside <iframe sandbox="allow-scripts"> (opaque origin) on a host page; both carry a CSP that allows
    inline code and Google Fonts only (build_page.js);
  - the browser's network is cut: every request except the page itself and fonts.googleapis.com / fonts.gstatic.com is aborted
    (Playwright route), other host names do not resolve, WebRTC gets no UDP; Chromium's own OS sandbox is on;
  - the browser runs in a child process (this script with an internal flag) inside a Windows job object; a watchdog kills the
    whole tree (worker, Playwright driver, Chromium) when page load / the font wait or one frame takes longer than --timeout s
    and stops with "the preset hangs or is far too slow at t=..." (also written to errors.txt, exit code 2).
"""
import argparse, collections, json, os, queue, re, shutil, subprocess, sys, threading, time
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
WORKER_FLAG = "--_render-worker"          # internal: python preview.py --_render-worker <spec.json>
TAG = "@@pv "                             # prefix of the worker's progress lines on stdout
FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
CHROME_ARGS = [
    "--host-resolver-rules=MAP * ~NOTFOUND, " + ", ".join("EXCLUDE " + h for h in FONT_HOSTS),   # no DNS for anything else
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",                                # WebRTC: no direct UDP
]
# backdrops (CSS): flat dark, flat light like the hub, plus a busy warm photo-like gradient
BGS = {
    "dark": "#0b0c0e",
    "light": "#e9e4dd",
    "photo": "radial-gradient(circle at 30% 30%, #f3d9b1 0%, #c98d5a 30%, #5b4a3f 60%, #1d2733 100%)",
}
TIMING_FRAMES = 120


# ============================== worker: the browser part, run in a child process ==============================
def worker(spec_path):
    import io
    from PIL import Image
    from playwright.sync_api import sync_playwright
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    page_uri = spec["page_uri"]
    blocked, csp = [], []

    def say(**m):
        sys.stdout.write(TAG + json.dumps(m) + "\n"); sys.stdout.flush()

    def on_route(route):
        url = route.request.url
        u = urlsplit(url)
        if unquote(url).lower() == unquote(page_uri).lower() or (u.scheme == "https" and u.hostname in FONT_HOSTS):
            route.continue_()
        else:
            blocked.append(url[:300]); route.abort("blockedbyclient")

    def on_console(msg):                # CSP refusals: one short line per URL (Chromium logs two long ones per fetch)
        txt = msg.text
        if "Content Security Policy" in txt:
            m = re.search(r"'([a-z][a-z0-9+.-]*:[^']*)'", txt) or re.search(r"cannot load (\S+?)\.?\s", txt + " ")
            url = (m.group(1) if m else txt)[:300]
            if url not in csp:
                csp.append(url)

    def flush(pg):
        errs = pg.evaluate("window.__errs.splice(0)")
        if errs or blocked or csp:
            say(errs=errs, blocked=blocked[:], csp=csp[:]); blocked.clear(); csp.clear()

    def poll(target, expr):             # no own timeout: the parent's watchdog decides when a step is too slow
        while not target.evaluate(expr):
            time.sleep(0.05)

    say(at="launch")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(args=CHROME_ARGS, chromium_sandbox=True)
        except Exception as e:          # a machine where Chromium's OS sandbox cannot start: run without it, say so
            say(note="Chromium sandbox unavailable (%s); running without it" % str(e).splitlines()[0][:120])
            b = p.chromium.launch(args=CHROME_ARGS)
        ctx = b.new_context(viewport={"width": spec["vw"], "height": spec["vh"]}, device_scale_factor=spec["dpr"],
                            service_workers="block", accept_downloads=False)
        ctx.route("**/*", on_route)
        pg = ctx.new_page()             # canvas = native frame size (viewport x dpr)
        ctx.on("page", lambda pg2: pg2.close())   # no popups (the iframe sandbox forbids them anyway)
        pg.on("console", on_console)

        say(at="load")
        pg.goto(page_uri, timeout=0)
        poll(pg, "window.__ready === true")
        cv = pg.query_selector("#cv").content_frame()
        poll(cv, "!!(document.fonts && document.fonts.status === 'loaded')")
        pg.wait_for_timeout(600)
        pg.evaluate("window.__init()")
        say(at="frame", job="first draw", i=0, n=1, t=0.0)
        pg.evaluate("window.__render(0)"); pg.wait_for_timeout(300)
        flush(pg)

        for job in spec["jobs"]:
            d = Path(job["dir"]); d.mkdir(parents=True, exist_ok=True)
            n = len(job["times"])
            for i, t in enumerate(job["times"]):
                say(at="frame", job=job["name"], i=i, n=n, t=t)
                pg.evaluate("t => window.__render(t)", t)
                tiles = []
                for name in job["bgs"]:
                    pg.evaluate("bg => { document.documentElement.style.background = bg; document.body.style.background = bg; }", BGS[name])
                    im = Image.open(io.BytesIO(pg.screenshot())).convert("RGB")
                    if job.get("crop"):
                        x0, y0, x1, y1 = job["crop"]
                        im = im.crop((round(im.width * x0), round(im.height * y0), round(im.width * x1), round(im.height * y1)))
                    if job.get("size"):
                        im = im.resize(tuple(job["size"]), Image.LANCZOS)
                    tiles.append((name, im))
                if job.get("row"):              # video frame: the backdrops side by side
                    row = Image.new("RGB", (sum(im.width for _, im in tiles), tiles[0][1].height))
                    x = 0
                    for _, im in tiles:
                        row.paste(im, (x, 0)); x += im.width
                    row.save(d / f"f{i:04d}.png")
                else:
                    for name, im in tiles:
                        im.save(d / f"{name}_{i:04d}.png")
                flush(pg)
            say(job_done=job["name"])
            if job.get("timing"):           # after the sheet is saved: a far-too-slow preset still leaves its sheet
                total, done = spec["total"], -1
                pg.evaluate("([total, n]) => { window.__timing(total, n); }", [total, TIMING_FRAMES])
                while True:                 # one progress line per frame drawn; nothing new = the watchdog's clock runs
                    st = pg.evaluate("(() => { const s = window.__tstat; return [s.i, s.t, s.done]; })()")
                    if st[0] != done:
                        done = st[0]
                        if not st[2]:
                            say(at="frame", job="timing pass", i=done, n=TIMING_FRAMES, t=st[1])
                    if st[2]:
                        break
                    time.sleep(0.02)
                st = pg.evaluate("window.__tstat")
                say(timing=sum(st["draw"]) / len(st["draw"]), worst=max(st["draw"]), cycle=st["cycle"])
                flush(pg)
        b.close()
    say(done=True)


# ============================== parent: watchdog around the worker ==============================
class Stop(Exception):
    pass


class ProcTree:
    """Kills the worker and everything it started (Playwright's node driver, Chromium and its renderers).
    Windows: a job object with KILL_ON_JOB_CLOSE - every descendant joins it, so one call kills them all, and they also die if
    this script itself is killed. Elsewhere: a new session + killpg. psutil (if installed) mops up anything left."""

    def __init__(self):
        self.job = None; self.proc = None
        if os.name == "nt":
            try:
                self._k32, self.job = self._win_job()
            except Exception as e:
                print("warning: no job object (%s); falling back to a process-tree kill" % e, file=sys.stderr)

    @staticmethod
    def _win_job():
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE; k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
        k32.SetInformationJobObject.restype = wt.BOOL
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
        k32.AssignProcessToJobObject.restype = wt.BOOL; k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.TerminateJobObject.restype = wt.BOOL; k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
        k32.CloseHandle.restype = wt.BOOL; k32.CloseHandle.argtypes = [wt.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rt", "wt", "ot")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObject failed, error %d" % ctypes.get_last_error())
        info = Extended(); info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (no breakaway)
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # JobObjectExtendedLimitInformation
            k32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed, error %d" % ctypes.get_last_error())
        return k32, job

    def popen(self, cmd, **kw):
        if os.name != "nt":
            kw["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **kw)
        if self.job is not None:
            if not self._k32.AssignProcessToJobObject(self.job, int(self.proc._handle)):
                print("warning: could not put the worker into the job object; falling back to a process-tree kill", file=sys.stderr)
                self._k32.CloseHandle(self.job); self.job = None
        return self.proc

    def kill(self):
        """Kill the whole tree; returns the processes still alive afterwards (should be none)."""
        if self.proc is None:
            return []
        try:
            import psutil
        except ImportError:
            psutil = None
        kids = []
        if psutil is not None:
            try:
                kids = psutil.Process(self.proc.pid).children(recursive=True)
            except Exception:
                kids = []
        if self.job is not None:
            self._k32.TerminateJobObject(self.job, 1)
        elif os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            pass
        if psutil is None:
            return []
        for k in kids:
            try:
                k.kill()
            except Exception:
                pass
        _, alive = psutil.wait_procs(kids, timeout=10)     # a terminated process can take a few s to go on a busy machine
        return [k for k in alive if k.is_running()]

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.kill()
        if self.job is not None:
            self._k32.CloseHandle(self.job); self.job = None     # KILL_ON_JOB_CLOSE: nothing of the tree survives this


def where_of(m):
    if m["at"] == "launch":
        return "browser start"
    if m["at"] == "load":
        return "page load / font wait (the runtime already draws t=0 there, when its fonts arrive)"
    return "t=%.2f s (%s, frame %d of %d)" % (m["t"], m["job"], m["i"] + 1, m["n"])


def run_worker(spec, work, timeout, on_event):
    """Run the browser part in a child process and pass its events on. A step (browser start, page load + fonts, one frame)
    that makes no progress in `timeout` s kills the whole process tree and raises Stop."""
    work.mkdir(parents=True, exist_ok=True)
    spec_path = work / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    tree = ProcTree()
    proc = tree.popen([sys.executable, str(Path(__file__).resolve()), WORKER_FLAG, str(spec_path)],
                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, encoding="utf-8", errors="replace", env=env)
    lines, tail = queue.Queue(), collections.deque(maxlen=40)

    def pump(stream, to_queue):
        for line in stream:
            if to_queue:
                lines.put(line)
            else:
                tail.append(line.rstrip())
        if to_queue:
            lines.put(None)

    for stream, q_ in ((proc.stdout, True), (proc.stderr, False)):
        threading.Thread(target=pump, args=(stream, q_), daemon=True).start()
    where, done = "browser start", False
    try:
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                alive = tree.kill()
                left = "" if not alive else " (%d browser processes could not be killed: %s)" % (
                    len(alive), ", ".join("%s %d" % (p.name(), p.pid) for p in alive if p.is_running()))
                if where == "browser start":
                    raise Stop("the browser did not start in %g s; it was killed%s" % (timeout, left))
                raise Stop("the preset hangs or is far too slow at %s: no progress in %g s, the browser was killed%s"
                           % (where, timeout, left))
            if line is None:
                break
            if not line.startswith(TAG):
                tail.append(line.rstrip()); continue
            m = json.loads(line[len(TAG):])
            if "at" in m:
                where = where_of(m)
            if m.get("done"):
                done = True
            on_event(m)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if not done:
            last = [x for x in tail if x.strip()][-6:]
            raise Stop("the render failed at %s (exit code %s):\n  %s" % (where, proc.poll(), "\n  ".join(last) or "no output"))
    finally:
        tree.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("preset"); ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=30); ap.add_argument("--sheet-only", action="store_true")
    ap.add_argument("--strip", nargs=3, type=float, metavar=("T0", "T1", "N"),
                    help="also write strip.png: N frames from T0 to T1 on the dark backdrop (check motion smoothness)")
    ap.add_argument("--fmt", default="9:16", choices=["9:16", "16:9"])
    ap.add_argument("--param", action="append", default=[], help="slider override name=value (number or #colour)")
    ap.add_argument("--shots", help="comma-separated times: full-res caption-band crops shot_<t>.png (dark over light)")
    ap.add_argument("--timeout", type=float, default=20,
                    help="seconds one step (page load + font wait, or one frame) may take before the browser is killed (default 20)")
    a = ap.parse_args()
    from PIL import Image, ImageDraw

    out = Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    page_html = out / "page.html"
    FW, FH = {"9:16": (1080, 1920), "16:9": (1920, 1080)}[a.fmt]
    tall = FH > FW
    ov = {}
    for kv in a.param:
        k_, v_ = kv.split("=", 1)
        ov[k_] = v_ if v_.startswith("#") else float(v_)
    VW, VH = (360, 640) if tall else (640, 360)
    bp = subprocess.run(["node", str(HERE / "build_page.js"), a.preset, str(page_html), str(FW), str(FH), json.dumps(ov)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if bp.returncode:
        sys.exit("build_page.js failed:\n" + (bp.stderr or bp.stdout).strip()[-2000:])
    info = json.loads(bp.stdout)
    total = info["total"]
    names = list(BGS)
    work = out / "_render"
    shutil.rmtree(work, ignore_errors=True)

    # ---- what to render (the worker renders every frame once and screenshots it on each backdrop) ----
    jobs = []
    # sheet: 12 columns x 3 rows (dark / light / photo); tiles 480 px high in the frame's shape (270x480 or 853x480)
    sheet_t = [total * (i + 0.5) / 12 for i in range(12)]
    ch = 480; cw = round(ch * FW / FH)
    jobs.append({"name": "sheet", "times": sheet_t, "bgs": names, "size": [cw, ch], "dir": str(work / "sheet"), "timing": True})
    if a.strip:
        t0, t1, k = a.strip[0], a.strip[1], max(2, int(a.strip[2]))
        strip_t = [t0 + (t1 - t0) * i / (k - 1) for i in range(k)]
        # 9:16: the middle band where captions live (y 700..1700 of 1920) at 1/3 scale; 16:9: the whole frame at 1/3 scale
        crop, size = ([0, 700 / 1920, 1, 1700 / 1920], [360, 333]) if tall else (None, [640, 360])
        jobs.append({"name": "strip", "times": strip_t, "bgs": ["dark"], "crop": crop, "size": size, "dir": str(work / "strip")})
    if a.shots:
        shot_t = [float(x) for x in a.shots.split(",")]
        crop = [0, 900 / 1920, 1, 1650 / 1920] if tall else None        # full res: 9:16 the caption band, 16:9 the frame
        jobs.append({"name": "shots", "times": shot_t, "bgs": ["dark", "light"], "crop": crop, "dir": str(work / "shots")})
    if not a.sheet_only:
        n = int(total * a.fps)
        fr = out / "frames"                         # (the worker creates it)
        for old in fr.glob("f????.png"):            # stale frames of a longer earlier run would end up in the video
            old.unlink()
        tile = [540, 960] if tall else [960, 540]   # tiles in the frame's shape: row 1620x960 (9:16) or 2880x540 (16:9)
        jobs.append({"name": "video", "times": [i / a.fps for i in range(n)], "bgs": names, "size": tile, "row": True, "dir": str(fr)})

    errs, blocked = [], []

    def write_errors(extra=None):
        lines_ = list(errs) + ["blocked network request: " + u for u in blocked] + ([extra] if extra else [])
        (out / "errors.txt").write_text("\n".join(lines_), encoding="utf-8")

    def done_sheet():
        sheet = Image.new("RGB", (cw * 12, ch * 3 + 30), "#222")
        d = ImageDraw.Draw(sheet)
        for r, nm in enumerate(names):
            for c in range(12):
                with Image.open(work / "sheet" / f"{nm}_{c:04d}.png") as im:
                    sheet.paste(im, (c * cw, 30 + r * ch))
        for c, t in enumerate(sheet_t):
            d.text((c * cw + 6, 8), f"t={t:.2f}s", fill="#fff")
        sheet.save(out / "sheet.png")
        write_errors()
        print("sheet", out / "sheet.png", "errors", len(errs))
        print("words", json.dumps([[w, round(s, 2)] for w, s in zip(info.get("w", []), info.get("s", []))]))

    def done_strip():
        job = next(j for j in jobs if j["name"] == "strip")
        tw_, th_ = job["size"]; k = len(job["times"])
        strip = Image.new("RGB", (tw_ * k, th_ + 24), "#222"); ds = ImageDraw.Draw(strip)
        for i, t in enumerate(job["times"]):
            with Image.open(work / "strip" / f"dark_{i:04d}.png") as im:
                strip.paste(im, (i * tw_, 24))
            ds.text((i * tw_ + 6, 6), f"t={t:.3f}", fill="#fff")
        strip.save(out / "strip.png"); print("strip", out / "strip.png")

    def done_shots():
        job = next(j for j in jobs if j["name"] == "shots")
        for i, tt in enumerate(job["times"]):
            with Image.open(work / "shots" / f"dark_{i:04d}.png") as d0, Image.open(work / "shots" / f"light_{i:04d}.png") as l0:
                both = Image.new("RGB", (d0.width, d0.height * 2)); both.paste(d0, (0, 0)); both.paste(l0, (0, d0.height))
            both.save(out / f"shot_{tt:.2f}.png")
        print("shots", len(job["times"]))

    def done_video():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(a.fps), "-i", str(out / "frames" / "f%04d.png"),
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(out / "preview.mp4")], check=True)
        print("video", out / "preview.mp4")

    finish = {"sheet": done_sheet, "strip": done_strip, "shots": done_shots, "video": done_video}

    def on_event(m):
        if m.get("note"):
            print("note:", m["note"])
        for e in m.get("errs", []):
            if e not in errs:
                errs.append(e)
        for u in ["%s (refused by the CSP)" % x for x in m.get("csp", [])] + ["%s (aborted by the network cut)" % x for x in m.get("blocked", [])]:
            if u not in blocked:
                blocked.append(u); print("blocked network request:", u)
        if "timing" in m:
            print(f"avg draw {m['timing']:.1f} ms/frame JS (runtime clear + preset code, slowest {m['worst']:.1f} ms), "
                  f"{m['cycle']:.1f} ms/frame incl. the browser painting it - on the native {FW}x{FH} canvas, "
                  f"{TIMING_FRAMES} frames across the clip back to back, no screenshots. The hub's publish check times the JS "
                  f"part at 640 px long side (>50 ms blocks, >12 ms warns)")
        if m.get("job_done"):
            finish[m["job_done"]]()

    spec = {"page_uri": page_html.as_uri(), "vw": VW, "vh": VH, "dpr": FW / VW, "total": total, "jobs": jobs}
    try:
        run_worker(spec, work, a.timeout, on_event)
    except Stop as e:
        msg = "ERROR: " + str(e)
        write_errors(msg)
        print(msg, file=sys.stderr)
        print("errors", out / "errors.txt", file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(2)
    write_errors()
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == WORKER_FLAG:
        worker(sys.argv[2])
    else:
        main()
