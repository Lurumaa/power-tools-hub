"""Lay a Power Tools Hub music track under a video: the track's drop lands on the cut you name, the bed sits at a fixed loudness
under the voice and fades out at the end. The picture is copied untouched; the new audio is exactly as long as the picture.

  python tools/music_bed.py --video creo.mp4 --music track.mp4 --out out.mp4 [--drop 14.42 --at 3.97] [--start 0] [--lufs -30] [--fade 0.4]
    --drop / --at  the track's drop (seconds, from the hub card) plays at --at seconds of the video, e.g. the hook->body cut;
                   the track starts earlier or later to make that happen (silence before it when it would start after 0)
    --start        without --drop: the second of the track that plays at video 0 (default 0)
    --lufs         bed loudness, integrated over the part that plays (-30 = 16 LU under a -14 voice-over, the team's level;
                   -14 when the video has no voice)
    --fade         fade-out over the last seconds of the video (default 0.4); the fade-in is 20 ms - never a hard audio cut
"use music X": ArtifactData get musiclib/X (drop, BPM, factory name), Artifact read path=<asset> downloads the .mp4 audio.
The video's own audio (voice-over) is kept as it is; flat gain only, no loudnorm (it shifts the timeline); if the mix would
peak over -1 dBFS the bed alone is turned down until it does not.
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path
import numpy as np

SR = 48000


def probe_video(path):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,duration,nb_frames,r_frame_rate',
                                   '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    has_a = any(s['codec_type'] == 'audio' for s in j['streams'])
    num, den = map(int, v['r_frame_rate'].split('/'))
    dur = float(v.get('duration') or j['format']['duration'])
    if v.get('nb_frames'): dur = int(v['nb_frames']) * den / num     # exact picture length on the frame grid
    return dur, has_a


def pcm(path):
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0', '-ac', '2', '-ar', str(SR), '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).copy()


def lufs(x, tmp):
    f = Path(tmp) / 'meas.f32'; f.write_bytes(np.ascontiguousarray(x, np.float32).tobytes())
    err = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', str(f), '-af', 'ebur128=framelog=quiet', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    vals = [l for l in err.splitlines() if l.strip().startswith('I:')]
    return float(vals[-1].split()[1]) if vals else -70.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--video', required=True); ap.add_argument('--music', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--drop', type=float); ap.add_argument('--at', type=float); ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--lufs', type=float, default=-30.0); ap.add_argument('--fade', type=float, default=0.4)
    a = ap.parse_args()
    if (a.drop is None) != (a.at is None): sys.exit('--drop and --at go together')
    dur, has_a = probe_video(a.video)
    n = int(round(dur * SR))
    vo = pcm(a.video) if has_a else np.zeros((0, 2), np.float32)
    vo = np.vstack([vo, np.zeros((max(0, n - len(vo)), 2), np.float32)])[:n]
    track = pcm(a.music)
    off = (a.drop - a.at) if a.drop is not None else a.start          # track second that plays at video 0
    o = int(round(off * SR))
    bed = np.zeros((n, 2), np.float32)
    src0, dst0 = max(0, o), max(0, -o)
    k = max(0, min(n - dst0, len(track) - src0))
    bed[dst0:dst0 + k] = track[src0:src0 + k]
    with tempfile.TemporaryDirectory() as tmp:
        used = bed[dst0:dst0 + k]
        if k <= 0 or np.abs(used).max() < 1e-5: sys.exit('the track has no sound in the part that plays under this video')
        cur = lufs(used, tmp)
        g = 10 ** ((a.lufs - cur) / 20)
        fi = min(int(0.02 * SR), k); fo = min(int(a.fade * SR), n)
        env = np.ones(n, np.float32)
        env[dst0:dst0 + fi] = np.linspace(0, 1, fi, dtype=np.float32)            # 20 ms in where the bed starts
        if fo: env[n - fo:] *= np.linspace(1, 0, fo, dtype=np.float32)            # fade-out over the video's tail
        bed *= env[:, None]
        mix = vo + bed * g
        peak = float(np.abs(mix).max()); lim = 10 ** (-1 / 20); g2 = g
        if peak > lim and float(np.abs(vo).max()) < lim:                          # only the bed gives way, the voice is untouched
            lo_, hi_ = 0.0, g
            for _ in range(30):
                mid = (lo_ + hi_) / 2
                if np.abs(vo + bed * mid).max() > lim: hi_ = mid
                else: lo_ = mid
            g2 = lo_; mix = vo + bed * g2
        wav = Path(tmp) / 'mix.f32'; wav.write_bytes(np.ascontiguousarray(mix, np.float32).tobytes())
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(a.video), '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', str(wav),
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(a.out)], check=True)
    where = f'drop {a.drop:.2f}s of the track at {a.at:.2f}s of the video' if a.drop is not None else f'track from {off:.2f}s'
    print(f'{where}; track starts at video {dst0 / SR:.2f}s; bed {cur:.1f} -> {a.lufs:.1f} LUFS (gain {20 * np.log10(g):+.1f} dB'
          f'{f", lowered {20 * np.log10(g2 / g):+.1f} dB for the -1 dBFS peak" if g2 != g else ""}); {"voice kept" if has_a else "no voice in the video"}; '
          f'{dur:.3f}s; wrote {a.out}')


if __name__ == '__main__':
    main()
