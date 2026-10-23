"""Cut a video with a Power Tools Hub transition - the same GLSL the hub previews, rendered on the GPU (moderngl).

  python tools/transitions.py --preset projects/<id>/trans_whip-pan.json --video clean.mp4 --cuts auto --out out.mp4
  python tools/transitions.py --preset projects/<id>/trans_whip-pan.json --clips a.mp4 b.mp4 [c.mp4 ...] --out out.mp4
    --cuts auto | 1.20,3.45   hard cuts in --video (auto = frame-difference detection, printed)
    --param blur=1.4          override a preset param (repeatable)
    --bitrate 5M              NVENC bitrate (falls back to libx264 crf 17 when NVENC is not available)

The preset is the hub document ("use transitions X": ArtifactData get translib/X -> this JSON): name, duration, anchor,
params [{name, value, ...}], shader (GLSL body defining vec4 transition(vec2 uv)); "shader_file" works too for local work.
Picture only: run it on the clean render (no captions), then put subtitles / plates on top. Audio is copied unchanged.
Window of each cut = [cut - anchor*duration, cut + (1 - anchor)*duration]; inside it the outgoing shot plays up to the cut
and then holds its last frame, the incoming shot holds its first frame until the cut and then plays - no handles needed,
the length never changes. Cuts closer than one transition get shorter windows so they never overlap.
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path
import numpy as np

HEAD = """
uniform sampler2D uA;
uniform sampler2D uB;
uniform float progress;
uniform float ratio;
uniform vec2 resolution;
uniform float time;
{params}
in vec2 vUv;
out vec4 fragColor;
vec2 mirrorUV(vec2 uv) {{ vec2 m = mod(uv, 2.0); return mix(m, 2.0 - m, step(1.0, m)); }}
vec4 getA(vec2 uv) {{ return texture(uA, mirrorUV(uv)); }}
vec4 getB(vec2 uv) {{ return texture(uB, mirrorUV(uv)); }}
float luma(vec3 c) {{ return dot(c, vec3(0.2126, 0.7152, 0.0722)); }}
"""
VERT = """#version 330
in vec2 in_pos; out vec2 vUv;
void main() { vUv = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""


def load_preset(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    p = raw.get('data', raw) if isinstance(raw.get('data'), dict) and 'shader' not in raw else raw
    if not p.get('shader') and p.get('shader_file'): p['shader'] = Path(p['shader_file']).read_text(encoding='utf-8')
    if not p.get('shader'): sys.exit(f'{path}: no shader in the preset')
    return p


class GPU:
    def __init__(self, preset, w, h):
        import moderngl
        self.mgl = moderngl
        self.ctx = moderngl.create_standalone_context(require=330)
        params = '\n'.join(f"uniform float {q['name']};" for q in preset.get('params', []))
        frag = '#version 330\n' + HEAD.format(params=params) + '\n' + preset['shader'] + '\nvoid main() { fragColor = transition(vUv); }\n'
        self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=frag)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], 'f4')
        self.vao = self.ctx.vertex_array(self.prog, [(self.ctx.buffer(quad), '2f', 'in_pos')])
        self.fbo = self.ctx.simple_framebuffer((w, h), components=4)
        self.ta, self.tb = (self.ctx.texture((w, h), 3) for _ in range(2))
        for t in (self.ta, self.tb): t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.w, self.h, self.preset = w, h, preset

    def _set(self, k, v):
        if k in self.prog: self.prog[k].value = v

    def frame(self, a, b, p, t, params):
        self.ta.write(np.ascontiguousarray(a[::-1]).tobytes()); self.tb.write(np.ascontiguousarray(b[::-1]).tobytes())
        self.ta.use(0); self.tb.use(1); self._set('uA', 0); self._set('uB', 1)
        self._set('progress', float(p)); self._set('ratio', self.w / self.h); self._set('resolution', (float(self.w), float(self.h))); self._set('time', float(t))
        for q in self.preset.get('params', []): self._set(q['name'], float(params.get(q['name'], q['value'])))
        self.fbo.use(); self.vao.render(self.mgl.TRIANGLE_STRIP)
        return np.frombuffer(self.fbo.read(components=3), np.uint8).reshape(self.h, self.w, 3)[::-1]


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate,nb_frames',
                          '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout
    j = json.loads(out); s = j['streams'][0]; num, den = map(int, s['r_frame_rate'].split('/'))
    has_audio = bool(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', str(path)],
                                    capture_output=True, text=True).stdout.strip())
    return int(s['width']), int(s['height']), num / den, float(j['format']['duration']), has_audio


def frames(path, w, h):
    proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    n = w * h * 3
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n: break
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    proc.wait()


def detect_cuts(path, fps, min_gap=0.5):
    """frame-difference peaks on a small grey copy: hard cuts only"""
    sw, sh = 72, 128
    proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'scale={sw}:{sh}', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], stdout=subprocess.PIPE)
    prev, diffs = None, []
    while True:
        buf = proc.stdout.read(sw * sh)
        if len(buf) < sw * sh: break
        f = np.frombuffer(buf, np.uint8).astype(np.int16)
        diffs.append(0.0 if prev is None else float(np.abs(f - prev).mean())); prev = f
    proc.wait()
    d = np.array(diffs); thr = max(18.0, float(np.median(d)) * 5)
    cuts = []
    for i in range(1, len(d) - 1):
        if d[i] > thr and d[i] >= d[i - 1] and d[i] >= d[i + 1] and (not cuts or i - cuts[-1] >= min_gap * fps): cuts.append(i)
    return cuts   # frame index of the first frame of the new shot


def join_clips(clips, tmp, fps):
    w, h, _, _, _ = probe(clips[0])
    lens, has_a = [], []
    for c in clips:
        _, _, _, dur, a = probe(c); lens.append(dur); has_a.append(a)
    inputs, filt = [], []
    for i, c in enumerate(clips):
        inputs += ['-i', str(c)]
        filt.append(f'[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},setsar=1[v{i}]')
        filt.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]' if has_a[i] else f'anullsrc=r=48000:cl=stereo,atrim=0:{lens[i]}[a{i}]')
    filt.append(''.join(f'[v{i}][a{i}]' for i in range(len(clips))) + f'concat=n={len(clips)}:v=1:a=1[v][a]')
    out = Path(tmp) / 'joined.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *inputs, '-filter_complex', ';'.join(filt), '-map', '[v]', '-map', '[a]',
                    '-c:v', 'libx264', '-crf', '12', '-preset', 'fast', '-c:a', 'pcm_s16le', str(out.with_suffix('.mkv'))], check=True)
    cuts, t = [], 0.0
    for L in lens[:-1]: t += L; cuts.append(round(t * fps))
    return out.with_suffix('.mkv'), cuts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--preset', required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--video'); src.add_argument('--clips', nargs='+')
    ap.add_argument('--cuts', default='auto'); ap.add_argument('--out', required=True)
    ap.add_argument('--param', action='append', default=[]); ap.add_argument('--bitrate', default='5M')
    a = ap.parse_args()
    preset = load_preset(a.preset)
    params = {k: float(v) for k, v in (x.split('=', 1) for x in a.param)}
    with tempfile.TemporaryDirectory() as tmp:
        if a.clips:
            fps = probe(a.clips[0])[2]; video, cuts = join_clips(a.clips, tmp, fps)
        else:
            video = Path(a.video); fps = probe(video)[2]
            cuts = detect_cuts(video, fps) if a.cuts == 'auto' else [round(float(x) * fps) for x in a.cuts.split(',') if x.strip()]
        w, h, fps, dur, has_audio = probe(video)
        total = int(round(dur * fps))
        print(f"{preset.get('name')}: {len(cuts)} cut(s) at {', '.join(f'{c / fps:.2f}s' for c in cuts) or '-'}")
        D = float(preset.get('duration', 0.5)); anchor = float(preset.get('anchor', 0.5))
        wins = []
        for k, c in enumerate(cuts):
            na, nb = round(anchor * D * fps), round((1 - anchor) * D * fps)
            lo = (cuts[k - 1] + c) // 2 if k else 0; hi = (c + cuts[k + 1]) // 2 if k + 1 < len(cuts) else total
            na, nb = max(1, min(na, c - lo)), max(1, min(nb, hi - c))
            wins.append((c - na, c, c + nb))
        gpu = GPU(preset, w, h)
        enc_v = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-b:v', a.bitrate, '-maxrate', a.bitrate, '-bufsize', '10M']
        def encoder(vargs):
            cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', f'{fps}', '-i', '-']
            if has_audio: cmd += ['-i', str(video), '-map', '0:v', '-map', '1:a', '-c:a', 'aac', '-b:a', '192k']
            return subprocess.Popen(cmd + vargs + ['-pix_fmt', 'yuv420p', '-shortest', '-movflags', '+faststart', str(a.out)], stdin=subprocess.PIPE)
        enc = encoder(enc_v)
        it = frames(video, w, h); buf = {}; wi = 0; i = 0
        def get(n):
            nonlocal i
            while n >= i:
                try: buf[i] = next(it)
                except StopIteration: return buf.get(n) if n in buf else buf[max(buf)]
                i += 1
            return buf[n]
        n = 0
        try:
            while n < total:
                if wi < len(wins) and n == wins[wi][0]:
                    s, c, e = wins[wi]; N = e - s
                    for j in range(N):
                        f = s + j; p = j / (N - 1) if N > 1 else 1.0
                        A = get(min(f, c - 1)); B = get(max(f, c))
                        enc.stdin.write(gpu.frame(A, B, p, j / fps, params).tobytes())
                    for k in [k for k in buf if k < e]: del buf[k]
                    n = e; wi += 1; continue
                fr = get(n); enc.stdin.write(fr.tobytes())
                for k in [k for k in buf if k <= n]: del buf[k]
                n += 1
        except BrokenPipeError:
            pass
        enc.stdin.close(); rc = enc.wait()
        if rc != 0:
            sys.exit('the encoder failed (no NVENC?) - rerun after installing NVENC drivers, or edit enc_v to libx264')
    print('wrote', a.out)


if __name__ == '__main__':
    main()
