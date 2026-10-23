#!/usr/bin/env python3
"""plan.py - split a stick-figure ad script into Omni pieces and size each one from the speaker's pace.

    python stick/plan.py <script.txt> --char boy|girl|expert [--wps 3.5]

script.txt: first line "HOOK: <text>", second line "BODY: <text>" (one paragraph each).
moods.json next to it (optional): {"base": "...", "e1": "...", ...} - one short tone per piece by its meaning.
Prints JSON: hook piece + body pieces (base + extends) with seconds, cost and the exact Kaleido prompt of each.

Rules (learned on real runs - see .claude/skills/stickman-talking/SKILL.md):
  secs = words / wps + 0.5 + 0.4 per extra sentence, rounded; too tight -> stutter / last word cut,
  too loose -> the model pads with repeated phrases. Seams only on sentence ends. Base piece <= 9 s, extends <= 7 s.
  One Omni chain holds 40 s, and every extend blurs a little more (boy: 44 % sharpness at e4), so a chain is a fresh
  base + at most max_extend_depth (2) extends; the next chain starts again from the character image at a sentence end.
  moods.json keys stay by global piece order: base, e1, e2, ... (independent of chains).
  Numbers are written as words in the lipsync text (digits got misread: "15 minutes, 10 minutes").
"""
import json, re, sys
from pathlib import Path

FMT = json.loads((Path(__file__).resolve().parent.parent / "formats" / "stick_talking.json").read_text(encoding="utf-8"))
NUM = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
       "10": "ten", "12": "twelve", "15": "fifteen", "20": "twenty", "30": "thirty", "60": "sixty", "100": "a hundred"}


def words_of(s): return len(re.findall(r"[A-Za-z0-9']+", s))


def sentences(t): return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s.strip()]


def secs(text, wps, extend=False, body=False):
    """articulation rate + measured sentence pauses + lead/tail (+ join for an extend) - see stick_talking.json _sizing"""
    return max(3, min(10, round(secs_raw(text, wps, extend, body))))


def secs_raw(text, wps, extend=False, body=False):
    n = len(sentences(text))
    pause = FMT.get("sentence_pause_body_s", FMT["sentence_pause_s"]) if body else FMT["sentence_pause_s"]
    lt = FMT.get("lead_tail_extend_s", FMT["lead_tail_s"]) if extend else FMT["lead_tail_s"]
    return words_of(text) / wps + pause * (n - 1) + lt + (FMT["extend_join_s"] if extend else 0)


def spell_numbers(t):
    def rep(m):
        w = NUM.get(m.group(0))
        if not w: sys.exit(f"write the number {m.group(0)} as words in the script")
        return w
    return re.sub(r"\b\d+\b", rep, t)


def group(sents, wps, first_max, next_max, first_is_extend=False):
    pieces, cur = [], []
    for s in sents:
        cap = first_max if not pieces else next_max
        if cur and round(secs_raw(" ".join(cur + [s]), wps, extend=bool(pieces) or first_is_extend, body=True)) > cap:
            pieces.append(" ".join(cur)); cur = [s]
        else:
            cur.append(s)
    if cur: pieces.append(" ".join(cur))
    return pieces


def main():
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    char = a[a.index("--char") + 1] if "--char" in a else "boy"
    wps = float(a[a.index("--wps") + 1]) if "--wps" in a else FMT["art_wps"].get(char, FMT["wps"])
    who = "her" if char == "girl" else "his"
    voice = FMT["voices"][char]
    mf = Path(a[0]).parent / "moods.json"
    moods = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    mline = lambda step: FMT["mood_line"].replace("{MOOD}", moods[step].rstrip(".") + ".") if moods.get(step) else ""
    g = {"girl": ("woman", "her", "she")}.get(char, ("man", "his", "he"))
    ctx = FMT.get("context", "").replace("{GENDER}", g[0]).replace("{POSS}", g[1]).replace("{PRON}", g[2])
    fill = lambda tpl, t, step="": tpl.replace("{HOOK_TEMPLATE}", FMT["prompts"]["hook"].replace("{DELIVERY}", FMT["delivery_body"])).replace(
        "{DELIVERY}", FMT["delivery_hook"]).replace("{MOOD}", (moods.get(step) or "natural and engaged").rstrip(".") + ".").replace(
        "{BODY_SHORT}", FMT.get("body_short", {}).get(char, FMT["body_short_default"])).replace("{BODY_HOOK}", FMT.get("body_hook", {}).get(char, FMT.get("body_hook_default", ""))).replace("{CONTEXT}", ctx).replace("{CHARACTER}", FMT["characters"][char]).replace("{MOOD_LINE}", mline(step)).replace("{TEXT}", t).replace("{VOICE}", voice).replace("{WHO_CAP}", "She" if who == "her" else "He").replace(
        "{EXT_CONTEXT}", FMT.get("ext_context", {}).get(char, "")).replace("{WHO_PRON}", "she" if who == "her" else "he").replace("{WHO}", who)
    lines = {l.split(":", 1)[0].strip().upper(): l.split(":", 1)[1].strip() for l in Path(a[0]).read_text(encoding="utf-8").splitlines() if ":" in l}
    hook, body = spell_numbers(lines["HOOK"]), spell_numbers(lines["BODY"])
    P = FMT["prompts"]
    out = {"char": char, "wps": wps, "hook": {"text": hook, "secs": secs(hook, wps), "prompt": fill(P["hook"], hook)}, "body": []}
    depth = FMT.get("max_extend_depth", 9)
    bwps = float(a[a.index("--wps") + 1]) if "--wps" in a else FMT.get("art_wps_body", {}).get(char, wps)
    blocks = [b.strip() for b in body.split("|") if b.strip()]   # "|" in BODY = forced seam (e.g. before the CTA, own mood)
    pieces = []
    for b in blocks:
        g_ = group(sentences(b), bwps, FMT["base_max_s"] if not pieces else FMT["extend_max_s"], FMT["extend_max_s"], first_is_extend=bool(pieces))
        pieces += g_
    for k, t in enumerate(pieces):
        c, j = k // (depth + 1) + 1, k % (depth + 1)            # chain number, position in the chain (0 = fresh base)
        tpl = P["base"] if j == 0 else P["extend"]
        out["body"].append({"step": f"c{c}_base" if j == 0 else f"c{c}_e{j}", "chain": c, "text": t, "secs": secs(t, bwps, extend=j > 0, body=True),
                            "prompt": fill(tpl, t, "base" if k == 0 else f"e{k}")})
    of = Path(a[0]).parent / "secs_override.json"   # {step: {secs, why}} - only with a RUNLOG reason (a logged failure at that size)
    for st, ov in (json.loads(of.read_text(encoding="utf-8")) if of.exists() else {}).items():
        for p in out["body"]:
            if p["step"] == st: p["secs"], p["override"] = ov["secs"], ov["why"]
    total = sum(p["secs"] for p in out["body"])
    out["body_total_s"] = total
    out["cost_usd"] = round((out["hook"]["secs"] + total) * FMT["usd_per_s"], 2)
    for c in {p["chain"] for p in out["body"]}:
        if sum(p["secs"] for p in out["body"] if p["chain"] == c) > FMT.get("chain_max_s", 39): out["warning"] = f"chain {c} > {FMT.get('chain_max_s', 39)} s (Kaleido floors the room; the last piece gets clamped)"
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
