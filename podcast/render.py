#!/usr/bin/env python3
"""render.py - render an editor timeline to MP4 on this machine (ffmpeg; NVENC on the GPU when available).

    python render.py <job.json>

The editor's Export button posts its current state to serve.js, which writes the job and starts this script.
Job: {name, w, h, fps, out, status, media:[{id, path, w, h}], tracks:[{id, type, on}],
      items:[{kind: video|audio, track, media, start, in, out, speed, volume, on, tf}], disclaimer:{on, text, pos}}
What it renders is what the editor plays: at every moment the top visible video layer (highest V track wins), all
audio items mixed, a hidden/muted track or a switched-off item is left out. Overlay items (item.overlay) are composited full-frame on top,
then the editor's text/shape layers (job.layers: PNGs drawn by the editor) and karaoke subtitle tracks (job.subs). Progress goes to job['status'] as JSON {state, progress, encoder, out, error}.
"""
import json, math, subprocess, sys, textwrap, time
from pathlib import Path

FONT = "C:/Windows/Fonts/arial.ttf"


def write_status(job, **kw):
    p = Path(job["status"]); s = {}
    if p.exists():
        try: s = json.loads(p.read_text(encoding="utf-8"))
        except Exception: s = {}
    s.update(kw); p.write_text(json.dumps(s), encoding="utf-8")


def has_nvenc():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                        "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def ilen(it): return (it["out"] - it["in"]) / (it.get("speed") or 1)


def atempo(speed):
    """atempo takes 0.5..2 per stage"""
    out, s = [], float(speed)
    while s > 2.0: out.append("atempo=2.0"); s /= 2.0
    while s < 0.5: out.append("atempo=0.5"); s /= 0.5
    if abs(s - 1) > 1e-4: out.append(f"atempo={s:.5f}")
    return out


def esc(p):  # a path inside a filtergraph option
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def placement(it, m, W, H, FPS, d, n, chains):
    """filters that put one video layer on a W x H frame exactly like the editor's transform (cover + tf)"""
    tf = it.get("tf") or {}
    mw, mh = m.get("w") or W, m.get("h") or H
    f = max(W / mw, H / mh); cw, ch = mw * f, mh * f          # the editor's cover size (lsz)
    sx, sy = tf.get("sx", 100) / 100, tf.get("sy", 100) / 100
    px, py = tf.get("px", W / 2), tf.get("py", H / 2)
    ax, ay = tf.get("ax", cw / 2), tf.get("ay", ch / 2)
    rot, op = tf.get("rot", 0) or 0, tf.get("op", 100)
    default = abs(sx - 1) < 1e-4 and abs(sy - 1) < 1e-4 and abs(px - W / 2) < 0.5 and abs(py - H / 2) < 0.5 \
        and abs(ax - cw / 2) < 0.5 and abs(ay - ch / 2) < 0.5 and not rot and op >= 100
    if default:
        return f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}"
    sw, sh = max(2, round(cw * sx / 2) * 2), max(2, round(ch * sy / 2) * 2)
    x, y = px - ax * sx, py - ay * sy
    fg = f"scale={sw}:{sh}:flags=lanczos,format=rgba"
    if op < 100: fg += f",colorchannelmixer=aa={op / 100:.3f}"
    if rot:
        r = math.radians(rot)
        fg += f",rotate={r:.5f}:c=none:ow=rotw({r:.5f}):oh=roth({r:.5f})"
        nw = abs(sw * math.cos(r)) + abs(sh * math.sin(r)); nh = abs(sw * math.sin(r)) + abs(sh * math.cos(r))
        x -= (nw - sw) / 2; y -= (nh - sh) / 2
    chains.append(f"color=c=black:s={W}x{H}:r={FPS}:d={d:.4f}[bg{n}]")
    return ("PLACE", fg, round(x), round(y))


def media_fps(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True)
    try:
        a, b = (r.stdout.strip() or "0/1").split("/"); return round(int(a) / int(b), 3)
    except ValueError:
        return 0


def set_fps(job):
    """Probe every clip's frame rate (build uses it to map output frames to source frames exactly). The output
    keeps the editor's fps (30): 30 for smoother subtitles - 24 fps clips then repeat
    every 4th frame, as in any 30p edit of 24p footage. The drift that looked like desync came from per-piece
    duration trims, not from the rate - pieces are cut on the output frame grid now (build)."""
    for m in job["media"]:
        if any(i["media"] == m["id"] and i["kind"] == "video" for i in job["items"]): m["fps"] = media_fps(m["path"])
    return job


def build(job):
    W, H, FPS = int(job["w"]), int(job["h"]), job.get("fps") or 30
    media = {m["id"]: m for m in job["media"]}
    tracks = job["tracks"]
    on = {t["id"]: t.get("on", True) for t in tracks}
    vorder = {t["id"]: k for k, t in enumerate([t for t in tracks if t["type"] == "v"])}
    vall = [i for i in job["items"] if i["kind"] == "video" and i.get("on", True) and on.get(i["track"], True) and i["media"] in media]
    vit = [i for i in vall if not i.get("overlay")]
    oit = [i for i in vall if i.get("overlay")]   # full-frame transparent layers (karaoke subs) composited on top
    ait = [i for i in job["items"] if i["kind"] == "audio" and i.get("on", True) and on.get(i["track"], True) and i["media"] in media]
    total = max([i["start"] + ilen(i) for i in vit + ait] + [0.5])
    warns = []
    if any((i.get("tf") or {}).get("keys") for i in vit): warns.append("keyframed transforms are rendered static")

    # video: cut the timeline where any layer starts/ends; each piece shows its top layer (or black)
    cuts = sorted({0.0, round(total, 4)} | {round(i["start"], 4) for i in vit} | {round(i["start"] + ilen(i), 4) for i in vit})
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 1e-3 or a >= total: continue
        mid = (a + b) / 2
        cover = [i for i in vit if i["start"] - 1e-6 <= mid < i["start"] + ilen(i)]
        top = max(cover, key=lambda i: vorder.get(i["track"], -1)) if cover else None
        if pieces and pieces[-1][2] is top and abs(pieces[-1][1] - a) < 1e-6: pieces[-1][1] = b
        else: pieces.append([a, b, top])

    # Every piece is cut on the output's frame grid: piece = output frames [round(a*FPS), round(b*FPS)), trimmed by frame
    # COUNT. Trimming by duration rounded every piece to whole frames on its own and the error added up cut after cut -
    # the picture ran 1-2 frames ahead of the voice by the end. The
    # audio is placed by absolute time, so frame-exact video = in sync everywhere.
    inputs, chains, vlabels = [], [], []  # inputs: 6 args per file (-ss x -t y -i path)
    fps_f = float(FPS)
    for n, (a, b, it) in enumerate(pieces):
        f0, f1 = round(a * fps_f), round(b * fps_f); nf = f1 - f0
        if nf <= 0: continue
        d = nf / fps_f
        if it is None:
            chains.append(f"color=c=black:s={W}x{H}:r={FPS}:d={d + 1:.4f},trim=end_frame={nf},format=yuv420p,setsar=1[v{n}]")
            vlabels.append(f"[v{n}]"); continue
        sp = it.get("speed") or 1
        m = media[it["media"]]
        src_a = it["in"] + (f0 / fps_f - it["start"]) * sp      # source time of the piece's first output frame
        mf = m.get("fps") or 0
        k = len(inputs) // 6
        if mf:
            # exact: source frame kf+N gets its own timestamp relative to the piece start (the first one clamped to 0),
            # then fps with round=up shows, at every output frame time, the latest source frame at or before it -
            # what the editor shows at that moment (24 fps clips in a 30 fps edit repeat every 4th frame)
            kf = max(0, int(math.floor(src_a * mf + 1e-4)))
            inputs += ["-ss", f"{max(0.0, (kf - 0.5) / mf):.4f}", "-t", f"{d * sp + 1:.4f}", "-i", m["path"]]
            head = f"[{k}:v]setpts=max(((({kf}+N)/{mf})-({src_a:.6f}))/{sp}\\,0)/TB,fps={FPS}:start_time=0:round=up"
        else:
            inputs += ["-ss", f"{src_a:.4f}", "-t", f"{d * sp + 1:.4f}", "-i", m["path"]]
            head = f"[{k}:v]setpts=PTS/{sp},fps={FPS}:start_time=0"
        tail = f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={nf},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v{n}]"
        pl = placement(it, m, W, H, FPS, d, n, chains)
        if isinstance(pl, str):
            chains.append(f"{head},{pl},{tail}")
        else:
            _, fg, x, y = pl
            chains.append(f"{head},{fg}[fg{n}]")
            chains.append(f"[bg{n}][fg{n}]overlay=x={x}:y={y}:format=auto,{tail}")
        vlabels.append(f"[v{n}]")
    chains.append(f"{''.join(vlabels)}concat=n={len(vlabels)}:v=1:a=0[vcat]")

    # overlays (item.overlay): the editor plays a VP9-alpha .webm; ffmpeg's native VP9 decoder drops alpha, so the
    # render takes the qtrle .mov written next to it (karaoke_subs.py writes both)
    last = "vcat"
    for n, it in enumerate(oit):
        path = Path(media[it["media"]]["path"])
        if path.suffix.lower() == ".webm" and path.with_suffix(".mov").exists(): path = path.with_suffix(".mov")
        k = len(inputs) // 6; inputs += ["-ss", f"{it['in']:.4f}", "-t", f"{ilen(it):.4f}", "-i", str(path)]
        chains.append(f"[{k}:v]setpts=PTS-STARTPTS+{it['start']:.4f}/TB,fps={FPS},scale={W}:{H},format=yuva420p[ov{n}]")
        chains.append(f"[{last}][ov{n}]overlay=0:0:eof_action=pass:format=auto[ovo{n}]"); last = f"ovo{n}"

    # audio: every item trimmed, re-timed, levelled, placed; all summed without normalisation (like the editor)
    alabels = []
    for n, it in enumerate(ait):
        path = media[it["media"]]["path"]
        if not has_audio(path): continue
        k = len(inputs) // 6; inputs += ["-ss", f"{it['in']:.4f}", "-t", f"{it['out'] - it['in']:.4f}", "-i", path]
        f = ["asetpts=PTS-STARTPTS", "aresample=48000"] + atempo(it.get("speed") or 1)
        f.append(f"volume={max(0.0, min(1.0, float(it.get('volume', 1)))):.3f}")
        fd = min(float(it.get("fade") or 0), (it["out"] - it["in"]) / (it.get("speed") or 1) / 2)
        if fd > 0:  # declick ramps at the item edges, same as the editor preview
            f += [f"afade=t=in:st=0:d={fd:.3f}", f"afade=t=out:st={(it['out'] - it['in']) / (it.get('speed') or 1) - fd:.4f}:d={fd:.3f}"]
        ms = int(round(it["start"] * 1000)); f.append(f"adelay={ms}:all=1")
        chains.append(f"[{k}:a]{','.join(f)}[a{n}]"); alabels.append(f"[a{n}]")
    if alabels:
        chains.append(f"{''.join(alabels)}amix=inputs={len(alabels)}:normalize=0:dropout_transition=0,apad,atrim=0:{total:.4f}[aout]")
    else:
        chains.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.4f}[aout]")
    return inputs, chains, total, warns, last


def add_disclaimer(job, W, H, last, chains):
    """disclaimer, as the editor shows it: centred, 3 % from the top or bottom, white 78 %, soft shadow"""
    dsc = job.get("disclaimer") or {}
    if not (dsc.get("on") and (dsc.get("text") or "").strip()): return last
    size = round(W * 0.023)
    lines = textwrap.wrap(dsc["text"].strip(), width=max(10, int(W * 0.88 / (size * 0.52))))
    lh = round(size * 1.25)
    y0 = round(H * 0.03) if dsc.get("pos") == "top" else round(H - H * 0.03 - lh * len(lines))
    txtdir = Path(job["status"]).parent
    for j, line in enumerate(lines):
        tf = txtdir / f"{Path(job['status']).stem}_dsc{j}.txt"; tf.write_text(line, encoding="utf-8")
        chains.append(f"[{last}]drawtext=fontfile='{esc(FONT)}':textfile='{esc(tf)}':fontsize={size}:fontcolor=white@0.78:"
                      f"shadowcolor=black@0.8:shadowx=0:shadowy=1:x=(w-text_w)/2:y={y0 + j * lh}[dsc{j}]")
        last = f"dsc{j}"
    return last


class Stalled(RuntimeError):
    pass


def run_ffmpeg(job, inputs, chains, outs, total, p0, p1, tag, stdin=None, stall=None):
    """one ffmpeg run with one or more outputs [(maps + codec args, path)], progress mapped into [p0, p1] of the job;
    stderr drained in a thread (never blocks ffmpeg); stall = seconds without progress -> killed, Stalled raised"""
    import threading
    gfile = Path(job["status"]).with_suffix(f".{tag}.filter.txt"); gfile.write_text(";\n".join(chains), encoding="utf-8")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostats", "-progress", "pipe:1", *inputs, "-filter_complex_script", str(gfile)]
    for args, out in outs:
        cmd += [*args, "-pix_fmt", "yuv420p", "-r", str(job.get("fps") or 30), "-t", f"{total:.4f}", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if stdin is not None: stdin.close()       # the parent's copy: pass 1 must see a broken pipe if pass 2 dies
    errlog = []
    threading.Thread(target=lambda: errlog.extend(p.stderr), daemon=True).start()
    seen = {"t": time.time(), "us": -1, "stalled": False}
    if stall:
        def watch():
            while p.poll() is None:
                time.sleep(0.25)
                if time.time() - seen["t"] > stall:
                    seen["stalled"] = True; p.kill(); return
        threading.Thread(target=watch, daemon=True).start()
    last = 0
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try: us = int(line.split("=")[1])
            except ValueError: continue
            if us > seen["us"]: seen["us"] = us; seen["t"] = time.time()
            prog = p0 + (p1 - p0) * min(1.0, us / 1e6 / total)
            if time.time() - last > 0.4: write_status(job, progress=round(min(0.99, prog), 3)); last = time.time()
    p.wait()
    if seen["stalled"]: raise Stalled(f"no progress for {stall} s")
    if p.returncode != 0:
        raise RuntimeError(("".join(errlog[-12:]) or f"ffmpeg exit {p.returncode}")[-1500:])


def clean_job(job):
    """the clean file of a bundle: video + audio only (no overlay items, layers, subtitles, disclaimer)"""
    return {**job, "out": job["clean_out"], "clean_out": None, "layers": [], "subs": [], "disclaimer": {"on": False},
            "items": [i for i in job["items"] if not i.get("overlay")]}


def render(job, p0=0.0, p1=1.0):
    """Renders job['out'] and, when job['clean_out'] is set (Export bundle), the clean file in the SAME ffmpeg run:
    the pieces are decoded and scaled once, split, and encoded twice (two NVENC sessions)."""
    W, H, FPS = int(job["w"]), int(job["h"]), job.get("fps") or 30
    inputs, chains, total, warns, vlast = build(job)
    nv = has_nvenc()
    # ~5 Mbit/s, comparable to typical NLE exports: NVENC slowest preset + lookahead + B-frames + AQ.
    # cq 17 + unsharp gave 15 Mbit/s (125 MB a minute) of sharpened noise that players stuttered on;
    # measured against a lossless render: SSIM 0.992, PSNR 48 dB.
    venc = (["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "24", "-b:v", "5M", "-maxrate", "7M",
             "-bufsize", "14M", "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32", "-bf", "3", "-b_ref_mode", "middle",
             "-profile:v", "high"]
            if nv else ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "7M", "-bufsize", "14M", "-tune", "film",
                        "-profile:v", "high"])
    aac = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    write_status(job, encoder="GPU (NVENC)" if nv else "CPU (x264)", warnings=warns, duration=round(total, 2))
    layers, subs = job.get("layers") or [], job.get("subs") or []
    # legacy overlay items sit inside the base graph, so their clean file needs its own run
    both = bool(job.get("clean_out")) and not any(i.get("overlay") for i in job["items"])
    if both:
        chains.append(f"[{vlast}]split=2[vfull][vclean]"); chains.append("[aout]asplit=2[afull][aclean]")
        vlast, alab = "vfull", "afull"
    else:
        alab = "aout"
    pe = p0 + (p1 - p0) * 0.5 if job.get("clean_out") and not both else p1    # progress end of this run
    outs = [(["-map", "[vout]", "-map", f"[{alab}]", *venc, *aac], job["out"])]
    if both: outs.append((["-map", "[vclean]", "-map", "[aclean]", *venc, *aac], job["clean_out"]))

    if not layers and not subs:          # nothing drawn on top: one plain pass
        vlast = add_disclaimer(job, W, H, vlast, chains); chains.append(f"[{vlast}]null[vout]")
        run_ffmpeg(job, inputs, chains, outs, total, p0, pe, "main")
    else:
        # Subtitles are a small band of PNG states (karaoke_subs.render_band, cached), text/shape layers PNGs
        # cropped to what they cover, composited in 4:4:4 (a yuva420p band on a 4:2:0 frame gave coloured halos).
        # Normally ONE graph does it all. The first version overlaid a full-frame qtrle .mov and ffmpeg's scheduler
        # stalled (stuck at 6 %) - so a watchdog kills a graph that stops moving
        # for 8 s and the render is redone as two ffmpeg processes running at the same time (base piped into the
        # overlay pass).
        sys.path.insert(0, str(Path(__file__).parent / "tools"))
        from karaoke_subs import render_band
        from PIL import Image
        bands = {n: render_band(sb, W, H, FPS, total, sb["fontFile"], Path(job["status"]).parent / ".subcache")
                 for n, sb in enumerate(subs)}
        lays = []
        for n, l in enumerate(layers):
            im = Image.open(l["path"]).convert("RGBA"); bb = im.getchannel("A").getbbox()
            if not bb: continue
            x0, y0 = bb[0] // 2 * 2, bb[1] // 2 * 2
            x1, y1 = min(im.width, (bb[2] + 1) // 2 * 2), min(im.height, (bb[3] + 1) // 2 * 2)
            cp = Path(job["status"]).with_suffix(f".layer{n}.png"); im.crop((x0, y0, x1, y1)).save(cp, compress_level=1)
            lays.append((l, str(cp), x0, y0))

        def add_top(ins, ch, last):
            ch.append(f"[{last}]format=yuv444p[m444]"); last = "m444"
            seq = [(l.get("z", 0), 0, n, "layer", (l, cp, x, y)) for n, (l, cp, x, y) in enumerate(lays)] + \
                  [(sb.get("z", 0), 1, n, "sub", sb) for n, sb in enumerate(subs)]
            for z, _, n, kind, d in sorted(seq, key=lambda x: (x[0], x[1], x[2])):
                k = ins.count("-i")
                if kind == "layer":
                    l, cp, x, y = d
                    ins += ["-loop", "1", "-t", f"{total:.4f}", "-i", cp]
                    ch.append(f"[{k}:v]format=rgba[ly{n}]")
                    ch.append(f"[{last}][ly{n}]overlay={x}:{y}:enable='between(t,{l['start']:.4f},{l['end']:.4f})':format=yuv444[lyo{n}]")
                    last = f"lyo{n}"
                else:
                    lst, by = bands[n]
                    ins += ["-f", "concat", "-safe", "0", "-i", str(lst)]
                    ch.append(f"[{k}:v]fps={FPS},format=yuva444p[sb{n}]")
                    ch.append(f"[{last}][sb{n}]overlay=0:{by}:eof_action=pass:format=yuv444[sbo{n}]"); last = f"sbo{n}"
            last = add_disclaimer(job, W, H, last, ch); ch.append(f"[{last}]format=yuv420p[vout]")

        try:
            add_top(inputs, chains, vlast)
            run_ffmpeg(job, inputs, chains, outs, total, p0, pe, "main", stall=8)
        except Stalled:
            warns = warns + ["one-graph render stalled - redone as two piped passes"]
            write_status(job, progress=p0, warnings=warns)
            import threading
            inputs, chains, total, warns0, vlast = build(job)
            chains.append(f"[{vlast}]null[vout]")
            gfile = Path(job["status"]).with_suffix(".base.filter.txt"); gfile.write_text(";\n".join(chains), encoding="utf-8")
            vfast = (["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq", "-rc", "vbr", "-cq", "10", "-b:v", "0"]
                     if nv else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "10"])
            cmd1 = ["ffmpeg", "-hide_banner", "-y", "-nostats", "-loglevel", "error", *inputs, "-filter_complex_script", str(gfile),
                    "-map", "[vout]", "-map", "[aout]", *vfast, "-pix_fmt", "yuv420p", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-t", f"{total:.4f}", "-f", "nut", "pipe:1"]
            p1_ = subprocess.Popen(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            err1 = []
            threading.Thread(target=lambda: err1.extend(p1_.stderr), daemon=True).start()
            try:
                ins2, ch2 = (["-hwaccel", "cuda"] if nv else []) + ["-f", "nut", "-i", "pipe:0"], []
                add_top(ins2, ch2, "0:v")
                half = p0 + (p1 - p0) * (0.5 if job.get("clean_out") else 1)
                run_ffmpeg(job, ins2, ch2, [(["-map", "[vout]", "-map", "0:a", *venc, "-c:a", "copy"], job["out"])], total, p0, half,
                           "top", stdin=p1_.stdout)
                p1_.wait(timeout=30)
                if p1_.returncode != 0:
                    raise RuntimeError(b"".join(err1).decode("utf-8", "replace")[-1500:] or f"base pass exit {p1_.returncode}")
            finally:
                if p1_.poll() is None: p1_.kill()
            if job.get("clean_out"):
                render(clean_job(job), half, p1); both = True
        finally:
            for _, cp, _, _ in lays:
                try: Path(cp).unlink()
                except OSError: pass
    if job.get("clean_out") and not both:
        render(clean_job(job), pe, p1)


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    write_status(job, state="running", progress=0.0)
    try:
        render(set_fps(job))
        write_status(job, state="done", progress=1.0)
    except Exception as e:
        write_status(job, state="error", error=f"{type(e).__name__}: {e}"[-1500:]); raise


if __name__ == "__main__":
    main()
