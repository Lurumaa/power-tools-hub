"""Word-timed subtitle phrases for coach_montage.py (dynamic subs with a moving marker).

  python tools/coach_subs.py <vo.wav> <script.txt> <out.json> [--retime]

Word times: Groq whisper on the VO stem + the script's spelling (karaoke_subs.groq_words/align/snap_onsets).
Phrases: 1-2 lines, broken at logical points - always after . ! ? : , then at commas, then before a conjunction or
preposition; balanced lines, never a one-word last line, numbers as the script writes them (5, 28-day, 3-minute).
Out: {"words": [[word, s, e], ...], "phrases": [{"i0", "i1", "brk"}]}  (brk = index of the first word of line 2, or null)
"""
import argparse, json, re, sys
from pathlib import Path
from PIL import ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent))
import karaoke_subs as ks
import os

FONT = Path(os.environ["WINDIR"]) / "Fonts" / "Montserrat-Black.ttf"
WEAK = {"to", "and", "or", "that", "which", "because", "in", "without", "with", "on", "then", "for", "as", "giving",
        "check", "join", "take", "you", "your", "is", "like", "of", "we", "but", "so"}
NEVER_BEFORE = {"28-day", "3-minute", "5"}   # keep 'our 28-day', 'the 3-minute', '5 men' together


def disp(w):  # what is shown: no periods/colons at the end, commas kept, ! and ? dropped
    w = ks.strip_periods(w)
    return w.rstrip(":!?;") if w.rstrip(":!?;") else w


def width(words, f, size):
    return f.getlength(" ".join(disp(w).upper() for w in words))


def split_lines(words, f, maxw):
    """best break index for 2 lines (None = one line)"""
    if width(words, f, 0) <= maxw and len(words) <= 4: return None
    best, score = None, 1e9
    for k in range(2, len(words) - 1):   # >= 2 words per line
        a, b = width(words[:k], f, 0), width(words[k:], f, 0)
        if a > maxw or b > maxw: continue
        sc = abs(a - b) - (120 if words[k - 1][-1:] in ",;" else 0) - (60 if words[k].lower() in WEAK else 0)
        if words[k].lower() in NEVER_BEFORE: sc += 400
        if sc < score: best, score = k, sc
    if best is None and width(words, f, 0) <= maxw: return None
    return best


def phrases_of(tokens, f, maxw, max_words=8):
    out, i, n = [], 0, len(tokens)
    while i < n:
        j = i
        # grow until a strong stop, or until 2 lines / max_words would overflow
        while j < n:
            cand = tokens[i:j + 1]
            fits = width(cand, f, 0) <= maxw or (split_lines(cand, f, maxw) is not None and len(cand) <= max_words)
            if not fits: break
            j += 1
            if tokens[j - 1][-1:] in ".!?:": break
        if j == i: j = i + 1
        # if we stopped by overflow, step back to the best soft break inside (comma, then weak word)
        if j < n and tokens[j - 1][-1:] not in ".!?:":
            soft = [k for k in range(i + 2, j) if tokens[k - 1][-1:] in ",;"] or \
                   [k for k in range(i + 2, j) if tokens[k].lower() in WEAK and tokens[k].lower() not in NEVER_BEFORE]
            if soft: j = soft[-1]
        if n - j == 1 and tokens[j][-1:] not in ".!?:" and j - i < max_words:  # never leave one word alone
            j = n
        out.append((i, j)); i = j
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav"); ap.add_argument("script"); ap.add_argument("out")
    ap.add_argument("--retime", action="store_true"); ap.add_argument("--size", type=int, default=56)
    ap.add_argument("--maxw", type=int, default=940)
    a = ap.parse_args()
    raw = Path(a.script).read_text(encoding="utf-8").split()
    marked = "|" in raw or "/" in raw      # hand markup: ' | ' = new phrase, ' / ' = line 2 (logical splits by a human)
    script, marks = [], []
    for tok in raw:
        if tok in ("|", "/"): marks.append((len(script), tok))
        else: script.append(tok)
    heard = ks.groq_words(a.wav, Path(a.out).with_suffix(".groq.json"), a.retime)
    times = ks.align(script, heard)
    # onsets from wav2vec2 forced alignment of the script on the VO (Groq/whisper starts a word at the end of the
    # previous one, so after a pause the marker jumped ~0.1-0.4 s early - critic r4: median -0.10 s); Groq = fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pod, hook_qa
    Q = hook_qa.find_q(json.loads((Path(pod.__file__).parent / "formats/podcast3.json").read_text(encoding="utf-8")))
    o = {}; pod.verbatim_checks(Q, pod.pcm16(a.wav), " ".join(script), 0.0, [], [], o)
    per = o.get("per", [])
    # per[] is per TOKEN ('28-day' -> twenty eight day, 'Mid-way' -> mid way): map each script word to its tokens
    span, j = [], 0
    for w in script:
        n = sum(len(pod.norm_tokens(m.group(1))) for m in re.finditer(r"([A-Za-z0-9'’\-]+)", w))
        ps = [x for x in per[j:j + n] if x]; span.append((ps[0][0], ps[-1][1]) if ps else None); j += n
    out, last = [], 0.0
    for i, (s0, e0) in enumerate(times):
        w2 = span[i] if i < len(span) else None
        if w2: s0, e0 = w2[0] - 0.04, w2[1]
        s0 = max(s0, last + 0.02); e0 = max(e0, s0 + 0.08); out.append((s0, e0)); last = s0
    times = out
    words = [[w, round(s, 3), round(e, 3)] for w, (s, e) in zip(script, times)]
    f = ImageFont.truetype(str(FONT), a.size)
    ph = []
    if marked:
        cuts = [0] + [k for k, m in marks if m == "|"] + [len(script)]
        for i0, i1 in zip(cuts, cuts[1:]):
            brk = next((k for k, m in marks if m == "/" and i0 < k < i1), None)
            ph.append({"i0": i0, "i1": i1, "brk": brk})
            for a0, a1 in ([(i0, brk), (brk, i1)] if brk else [(i0, i1)]):
                wd = width(script[a0:a1], f, 0)
                if wd > a.maxw: print(f"WARN line too wide {wd:.0f}px > {a.maxw}: {' '.join(script[a0:a1])}")
    else:
        for i0, i1 in phrases_of(script, f, a.maxw):
            brk = split_lines(script[i0:i1], f, a.maxw)
            ph.append({"i0": i0, "i1": i1, "brk": (i0 + brk) if brk else None})
    Path(a.out).write_text(json.dumps({"words": words, "phrases": ph}, indent=0), encoding="utf-8")
    for p in ph:
        ws = [disp(w).upper() for w, *_ in words[p["i0"]:p["i1"]]]
        k = (p["brk"] - p["i0"]) if p["brk"] else None
        txt = " ".join(ws) if k is None else " ".join(ws[:k]) + " / " + " ".join(ws[k:])
        print(f'{words[p["i0"]][1]:6.2f}-{words[p["i1"] - 1][2]:6.2f}  {txt}')


if __name__ == "__main__":
    main()
