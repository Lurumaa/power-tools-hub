"""Mandatory audio QA for a talking hook clip BEFORE it is shown.
Runs the verbatim wav2vec2 layer of pod.py (repeats, stutters, missing words, extra sounds) + checks that the last
word ends before the clip end. Prints PASS / FAIL with the reasons.

  python tools/hook_qa.py clips/x.mp4 "<full script text of the clip>"
"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pod

TAIL_MIN = 0.15  # s of near-silence required after the last word
HEAD_MIN = 0.1   # s of near-silence required before the first word


def find_q(d):
    if isinstance(d, dict):
        if "w2v_missing" in d: return d
        for v in d.values():
            r = find_q(v)
            if r: return r


def main():
    clip, text = sys.argv[1], sys.argv[2]   # [--max-gap S]
    Q = find_q(json.loads((Path(pod.__file__).parent / "formats/podcast3.json").read_text(encoding="utf-8")))
    x = pod.pcm16(clip); errors, warns, out = [], [], {}
    # start gate: FAIL when the first 40 ms are already at speech level
    # (within 10 dB of the peak = the onset consonant was eaten). A soft ramp-in starting at 0.02-0.06 s sounded fine to
    # the user on V1-V3 ('...') -> only a WARN.
    xf = x.astype(float); hop = 320
    fr = xf[: len(xf) // hop * hop].reshape(-1, hop)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9)
    if (db[:2] > db.max() - 10).any():
        errors.append("speech at full level in the first 40 ms - the first word is cut mid-sound")
    elif (db[: int(HEAD_MIN / 0.02)] > db.max() - 30).any():
        warns.append(f"no quiet lead-in before the first word (< {HEAD_MIN}s), onset ramps in softly")
    # 0.5 s of silence in front: wav2vec2 drops a word that starts on sample 0. Times printed below are +0.5 s.
    x = np.concatenate([np.zeros(8000, dtype=x.dtype), x])
    pod.verbatim_checks(Q, x, text, 0.0, errors, warns, out)
    last = [p for p in out["per"] if p]
    dur = len(x) / 16000
    if last:
        end = last[-1][1]
        hop = 320; tail = x[int(end * 16000):]
        fr = tail[: len(tail) // hop * hop].reshape(-1, hop)
        db = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9) if len(fr) else np.array([])
        quiet = int(np.argmax(db < -45)) * 0.02 if (db < -45).any() else None
        if quiet is None or dur - (end + quiet) < TAIL_MIN:
            errors.append(f"last word runs into the clip end (ends ~{end:.2f}s of {dur:.2f}s)")
    # Substituted words: reported as WARN; only a clearly different word (similarity < 0.5, e.g. 'faith' for 'babe') FAILs.
    import difflib, re
    NUM = {"2": "two", "3": "three", "4": "four", "5": "five", "10": "ten", "30": "thirty"}   # ASR prints digits
    norm = lambda w: re.sub(r"[^a-z]", "", NUM.get(re.sub(r"[^0-9a-z]", "", w.lower()), w.lower()))
    want = [norm(w) for w in text.split() if norm(w)]
    heard = [norm(w) for w in out["heard"] if norm(w)]
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, want, heard, autojunk=False).get_opcodes():
        # inserted words = ad-lib / repeated phrase
        if op == "insert" and b2 - b1 >= 2:
            errors.append(f"extra words heard: '{' '.join(heard[b1:b2])}' (ad-lib/repeat)")
        if op != "replace": continue
        if difflib.SequenceMatcher(None, "".join(want[a1:a2]), "".join(heard[b1:b2])).ratio() >= 0.8:
            continue   # literal ASR split/merged the words ('co ordination', 'confollow') - same sounds
        for sw, hw in zip(want[a1:a2], heard[b1:b2]):
            r = difflib.SequenceMatcher(None, sw, hw).ratio()
            msg = f"word '{sw}' heard as '{hw}'"
            (errors if r < 0.5 else warns).append(msg)
    # Intonation of the last word (tools/end_pitch.py logic; task_2543 '...', task_1550 V3: the gate
    # existed but was not run - feedback). A script ending with
    # '.' or '!' must FALL; a '?' ending is a real question and is only reported.
    if last:
        try:
            import parselmouth
            t0, t1 = last[-1][0], last[-1][1]
            t0 = max(0.0, min(t0, t1 - 0.15))  # a 0.04 s 'it.' crashed Praat: read at least the last 0.15 s
            snd = x.astype(np.float64)
            fa = parselmouth.Sound(snd, 16000).to_pitch(time_step=0.02, pitch_floor=60, pitch_ceiling=350).selected_array["frequency"]
            med = np.median(fa[fa > 0])
            v = parselmouth.Sound(snd[int(t0 * 16000):int(t1 * 16000)], 16000).to_pitch(
                time_step=0.01, pitch_floor=60, pitch_ceiling=350).selected_array["frequency"]
            v = v[v > 0]
            if len(v) >= 6:
                k = max(2, len(v) // 6); a0, b0, peak = np.median(v[:k]), np.median(v[-k:]), v.max()
                falling = b0 <= a0 * 1.05 and (b0 < peak * 0.9 or b0 < med * 0.85)
                # whole last word far below the clip median = low settled / creaky ending
                falling = falling or np.median(v) < med * 0.6
                line = f"last word pitch {a0:.0f} -> peak {peak:.0f} -> end {b0:.0f} Hz (clip median {med:.0f})"
                if text.strip().endswith("?"):
                    print("INTONATION:", line, "(question, not gated)")
                elif falling:
                    print("INTONATION:", line, "FALLING ok")
                else:
                    errors.append(line + " - RISING/FLAT, sounds like a question")
            else:
                warns.append("intonation: too few voiced frames on the last word")
        except ImportError:
            warns.append("intonation not checked (parselmouth missing)")
    # Pauses between words.
    # --max-gap S fails any gap longer than S; without it gaps > 0.6 s are reported.
    mg = float(sys.argv[sys.argv.index("--max-gap") + 1]) if "--max-gap" in sys.argv else None
    spans = [p for p in out["per"] if p]
    gaps = [(round(b[0] - a_[1], 2), round(a_[1], 2)) for a_, b in zip(spans, spans[1:])]
    if gaps:
        big = max(gaps)
        print(f"PAUSES: longest {big[0]:.2f}s at {big[1]:.2f}s")
        for g, at in gaps:
            if mg is not None and g > mg:
                errors.append(f"pause {g:.2f}s at {at:.2f}s (> {mg})")
            elif mg is None and g > 0.6:
                warns.append(f"pause {g:.2f}s at {at:.2f}s")
    print("HEARD:", " ".join(out["heard"]))
    for w in warns: print("WARN:", w)
    for e in errors: print("FAIL:", e)
    print("PASS" if not errors else "FAIL")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
