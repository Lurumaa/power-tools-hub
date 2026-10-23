#!/usr/bin/env python3
"""pod.py - podcast-format pipeline.

Claude drives the Kaleido MCP tools (they cannot be called from here); this script does
everything deterministic: timing plan, prompts, tool arguments, state, CDN polling,
speech/visual QA with local whisper + ffmpeg, take selection and the editor timeline.

  new <id> <tz.txt|tz.json> [--female expert]   create project from a TZ ('Host: ...' lines or JSON), print the plan
  plan <id>                   timing plan + cost estimate
  img <id> wide [--note]      a round of 3 wide variants, each a DIFFERENT cast (labels A1-A3, next round B1-B3)
  img <id> wide --from B2 --note "..."   edit one variant (continues that image)
  img <id> host|guest|expert|expert_b [--note]   close-up args from the approved wide
  img-add <id> <asset> <job_id...>   bind jobs (order of the img lines), derive CDN urls, wait until they exist
  img-sheet <id> <asset...>   download, build a labelled approval sheet
  asset-set <id> <asset> <url> [--job]   register a given/hand-made frame as an approved asset
  approve <id> <asset> [label]   approve a candidate (label like B2 or 1-based index; default latest)
  gen <id>                    launchable clip steps -> exact tool args (JSON lines)
  launched <id> <step> <job_id...>
  wait <id> [--timeout s]     poll CDN, download, QA, auto-pick; prints only results
  fail <id> <job_id> [reason] mark a take failed (provider error / moderation)
  reject <id> <job_id> [reason]  reject a take (e.g. visual artifact), re-opens the step
  pick <id> <step> <job_id>   force a take
  qa-file <clip.mp4> <text> [--kind answer] [--from s]   QA any local clip
  sheet <id>                  contact sheet of all chosen takes (one image)
  timeline <id>               assemble the editor project, print its URL, then run audit
  audit <id>                  final gate: mix the timeline audio like the editor, verbatim-check every turn
  requa <id> [steps]          re-run QA on downloaded takes (after a QA change), re-pick
  step <id> <step> [--note|--text|--reset]   change one step without touching the rest
  status <id>
"""
import argparse, json, math, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"
EDITOR = ROOT.parent / "editor"
CDN = "https://<kaleido-cdn>/video-studio/{}.mp4"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------- state
def pdir(pid): return PROJECTS / pid
def fmt_of(st): return json.loads((ROOT / "formats" / f"{st['format']}.json").read_text(encoding="utf-8"))
def load(pid):
    p = pdir(pid) / "state.json"
    if not p.exists(): sys.exit(f"no project {pid}")
    return json.loads(p.read_text(encoding="utf-8"))
def save(st):
    (pdir(st["id"]) / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- text / timing
def words(s): return re.findall(r"[\w'’]+", s)
def nwords(s): return len(words(s))

ABBREV = re.compile(r"(?i)(?:^|\s)(dr|mr|mrs|ms|st|vs|etc|e\.g|i\.e|a\.m|p\.m|no|jr|sr)\.$")

def sentences(text):
    """split only where . ! ? … is followed by a space and a new sentence; '3.5', 'e.g.' and 'Dr. Lee' stay whole"""
    parts = re.split(r'(?<=[.!?…])(?:["”\')\]]?)\s+(?=["“(\[]?[A-Z0-9])', text.strip())
    out = []
    for p in parts:
        if out and ABBREV.search(out[-1]): out[-1] += " " + p
        else: out.append(p)
    return [p.strip() for p in out if p.strip()]

def clauses(sent, maxw):
    """a sentence as clauses (split on , ; : dashes); a clause longer than maxw words (counted like the
    clip capacity, so '3-minute' is 2) is chunked on whitespace"""
    out = []
    for cl in re.split(r'(?<=[,;:—–])\s+', sent):
        if nwords(cl) <= maxw: out.append(cl); continue
        cur, cw = [], 0
        for tok in cl.split():
            k = nwords(tok)
            if cur and cw + k > maxw: out.append(" ".join(cur)); cur, cw = [], 0
            cur.append(tok); cw += k
        if cur: out.append(" ".join(cur))
    return out

def clip_len(F, lead, speech, first, last):
    """planned seconds of one generation: lead-in (first clip only) + speech + tail. Only the turn's last clip
    gets the full tail; a clip that an extension continues gets seam_tail, so no dead air sits in the monologue"""
    tail = (F["tail"] if first else F["ext_tail"]) if last else F.get("seam_tail", 0.3)
    return (lead if first else 0) + speech + tail

def segment(text, wps, lead, F, ext_only=False):
    """split a turn into one base clip + extensions (ext_only: only extensions, continuing an existing clip).
    Preference: fewest clips, cuts on sentence ends (clause marks next, mid-clause last), one speaking pace.
    Integer clip lengths leave spare seconds: a first clip puts them into its (trimmed) lead-in, the last clip into
    its (trimmed) tail, a sentence-end seam into a natural pause of up to seam_pause_max - only what is left over
    would have to be absorbed by slower speech, and that is what the pace penalty prices."""
    seam, pmax = F.get("seam_tail", 0.3), F.get("seam_pause_max", 0.8)
    units = []  # (text, words, cut penalty after it: 0 sentence end, 10 clause mark, 40 mid-clause)
    maxw = max(4, int((min(F["max_first"], F["max_ext"]) - lead - F["tail"]) * wps))
    for s in sentences(text):
        parts = clauses(s, maxw)
        for k, p in enumerate(parts):
            units.append((p, nwords(p), 0 if k == len(parts) - 1 else (10 if re.search(r"[,;:—–]$", p) else 40)))
    n = len(units)
    pre = [0]
    for u in units: pre.append(pre[-1] + u[1])
    def plan(i, j, first):  # one clip of units[i:j] -> (D, speak start, speak end, stretch) or None
        w = pre[j] - pre[i]; sp = w / wps; last = j == n
        d = clip_len(F, lead, sp, first, last)
        if d > (F["max_first"] if first else F["max_ext"]): return None
        D = max(F["min_dur"], math.ceil(d)); slack = D - d
        if first and not last: start, stretch = max(lead, D - seam - sp), 0.0
        elif first: start, stretch = lead, 0.0
        elif last: start, stretch = 0.0, 0.0
        else:
            absorb = max(0.0, pmax - seam) if units[j - 1][2] == 0 else 0.0
            start, stretch = 0.0, max(0.0, slack - absorb)
        return D, start, start + sp + stretch, stretch
    def cost(i, j, first):
        p = plan(i, j, first)
        if p is None: return None
        sp = (pre[j] - pre[i]) / wps
        pace = F.get("pace_weight", 150) * (p[3] / (sp + p[3])) if p[3] else 0.0
        return 100 + units[j - 1][2] + pace
    g, gn = [0.0] * (n + 1), [None] * (n + 1)   # g[i]: best split of units[i:] into extensions
    for i in range(n - 1, -1, -1):
        g[i] = 1e18
        for j in range(i + 1, n + 1):
            c = cost(i, j, False)
            if c is None: break
            if c + g[j] < g[i]: g[i], gn[i] = c + g[j], j
    best, bj = 1e18, None
    for j in range(1, n + 1):
        c = cost(0, j, not ext_only)
        if c is None: break
        if c + g[j] < best: best, bj = c + g[j], j
    if bj is None or best >= 1e18: sys.exit("a single clause is longer than one clip - add punctuation")
    cuts, i = [0, bj], bj
    while i < n: i = gn[i]; cuts.append(i)
    out = []
    for j, (a, b) in enumerate(zip(cuts, cuts[1:])):
        s = " ".join(u[0] for u in units[a:b]); sp = nwords(s) / wps
        D, start, end, stretch = plan(a, b, j == 0 and not ext_only)
        out.append({"text": s, "speech_s": round(sp, 2), "duration_s": int(D), "speak": [round(start, 1), round(end, 1)],
                    "pace": round(nwords(s) / (end - start), 2)})
    return out

def calibrate_wps(st, who):
    """this speaker's real speaking rate, measured on the chosen base clips (words / speech seconds)"""
    w = d = 0.0
    for s in st["steps"].values():
        if s["who"] != who or s["seg"] != 0 or not s["chosen"]: continue
        t = next(x for x in s["takes"] if x["job"] == s["chosen"])
        sp = (t.get("qa") or {}).get("speech")
        if sp and sp[1] > sp[0] and s["kind"] != "question": w += nwords(s["text"]); d += sp[1] - sp[0]
    return round(w / d, 2) if d else None

ROLES = ("host", "guest", "expert")
FEMALE = {"she", "female", "woman", "f", "w", "ж", "жен", "женщина", "жінка"}
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def is_female(c): return str(c.get("gender") or c.get("pronoun") or "").strip().lower() in FEMALE

def fill(tpl, **kw):
    def rep(m):
        cur = kw
        for k in m.group(1).split("."):
            cur = cur.get(k, "") if isinstance(cur, dict) else ""
        return str(cur)
    return re.sub(r"\{([\w.]+)\}", rep, tpl)

def gendered(prompt, cast, base="he"):
    """templates are written for 'he' (a format's 'pronoun_base' may say 'she', e.g. stage_monologue); swap pronouns
    to the cast member's gender (templates only, never spoken text)"""
    if base == "she":
        if is_female(cast): return prompt
        # object 'her' (at her, behind her stays, of her exactly) -> him; possessive -> his
        prompt = re.sub(r"\bher\b(?=\s*[,.;:)\]]|\s*$|\s+(?:exactly|stays|only|is|are|was|were|and|while)\b)", "him", prompt)
        for a, b in ((r"\bshe\b", "he"), (r"\bShe\b", "He"), (r"\bHER\b", "HIS"), (r"\bher\b", "his"), (r"\bHer\b", "His"),
                     (r"\bherself\b", "himself"), (r"\bwoman\b", "man"), (r"\bwomen\b", "men")):
            prompt = re.sub(a, b, prompt)
        return prompt
    if not is_female(cast): return prompt
    for a, b in ((r"\bhe\b", "she"), (r"\bHe\b", "She"), (r"\bhis\b", "her"), (r"\bhim\b", "her")):
        prompt = re.sub(a, b, prompt)
    return prompt


# ---------------------------------------------------------------- casting
def rng(*parts):
    import hashlib, random
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))

def make_casts(st, F, rnd, n, prev):
    """n different casts for one round of wide shots. Every trait of a role differs between the n variants
    (while the pool allows), group_distinct traits also differ inside one group, and traits used in earlier
    rounds (prev) are avoided when possible. TZ cast fields pin gender/age/ethnicity/look/voice/ref."""
    C = F["casting"]; tzc = st["tz"].get("cast", {})
    fams = C.get("families", {})
    def key(attr, v):
        """trait family for in-group comparison: hair 'light brown' ~ 'dirty-blond'; a top by its first colour word"""
        if attr not in fams or not v: return v
        hits = [(v.find(w), f) for f, ws in fams[attr].items() for w in ws if w in v]
        if not hits: return v
        return min(hits)[1] if attr == "top" else next(f for f, ws in fams[attr].items() if any(w in v for w in ws))
    out = [{} for _ in range(n)]
    refs = [r for r in ROLES if tzc.get(r, {}).get("ref")]
    for role in ROLES:
        spec = tzc.get(role, {})
        g = "female" if is_female(spec) else "male"
        P = C["roles"][role][g]
        soft = {}
        for pc in prev:
            for attr, v in pc.get(role, {}).get("traits", {}).items(): soft.setdefault(attr, set()).add(v)
        used = {}
        for k in range(n):
            tr = {}
            for attr, pool in P.items():
                order = list(pool); rng(st["id"], rnd, role, attr).shuffle(order)
                hard = set(used.get(attr, ()))
                gk = {key(attr, out[k][r]["traits"].get(attr)) for r in out[k]} if attr in C["group_distinct"] else set()
                ok = lambda x: x not in hard and key(attr, x) not in gk
                v = next((x for x in order if ok(x) and x not in soft.get(attr, ())), None) \
                    or next((x for x in order if ok(x)), None) \
                    or next((x for x in order if x not in hard), order[0])
                used.setdefault(attr, set()).add(v); tr[attr] = v
            noun = "woman" if g == "female" else "man"
            age = spec.get("age") or tr["age"]
            eth = spec.get("ethnicity") or st["tz"].get("ethnicity") or rng(st["id"], rnd, role, "eth", k).choice(C["ethnicity"])
            wear = f"wearing {tr['top']}, {tr['bottom']} and {tr['shoes']}" + (f", {tr['extra']}" if tr.get("extra") else "")
            if spec.get("look"):
                look = spec["look"]
            elif spec.get("ref"):
                look = f"the person from reference image {refs.index(role) + 1} (keep that exact face and hair), {wear}; {tr['pose']}, {tr['expression']}"
            else:
                hair = tr["hair_style"].replace("{c}", tr["hair_color"])
                body = [f"a {eth} {noun} {age}", tr["build"], hair] + ([tr["facial"]] if tr.get("facial") else [])
                look = ", ".join(body) + f", {wear}; {tr['pose']}, {tr['expression']}"
            voice = spec.get("voice") or f"{C['accent']} voice of a {noun} {age}, {C['roles'][role]['voice'][g]}."
            out[k][role] = {"look": look, "voice": voice, "gender": g, "traits": tr, **({"ref": spec["ref"]} if spec.get("ref") else {})}
    return out

def proj_cast(st):
    """the cast that drives prompts and voices: the approved wide's cast (podcast casting) or, for formats built on
    given frames (stage monologue), the TZ cast"""
    W = st["assets"].get("wide", {})
    if W.get("approved") is not None and W["cands"][W["approved"]].get("cast"): return W["cands"][W["approved"]]["cast"]
    return st["tz"].get("cast", {})

def find_cand(A, key):
    if key is None: return len(A["cands"]) - 1
    for i, c in enumerate(A["cands"]):
        if str(c.get("label", "")).lower() == str(key).lower(): return i
    if str(key).isdigit() and 0 < int(key) <= len(A["cands"]): return int(key) - 1
    sys.exit(f"no candidate '{key}' (have: {', '.join(c.get('label', '?') for c in A['cands'])})")


# ---------------------------------------------------------------- TZ as plain text
ROLE_ALIASES = {"host": "host", "ведущий": "host", "хост": "host", "ведучий": "host",
                "guest": "guest", "гость": "guest", "гість": "guest",
                "expert": "expert", "эксперт": "expert", "експерт": "expert"}

def parse_script(text, female=()):
    """'Host: ...' / 'Guest: ...' / 'Expert (female): ...' lines -> TZ dict. First host line = intro,
    later host lines = questions (listening lead-in), an expert answer right after a host question
    (when the expert already spoke) uses the gesture frame expert_b."""
    cast, turns = {r: {} for r in ROLES}, []
    rx = re.compile(r"^\s*([^\W\d_]+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")
    for line in text.splitlines():
        m = rx.match(line)
        if m and m.group(1).lower() in ROLE_ALIASES:
            role = ROLE_ALIASES[m.group(1).lower()]
            if m.group(2) and m.group(2).strip().lower() in FEMALE: cast[role]["gender"] = "female"
            turns.append({"who": role, "text": m.group(3).strip()})
        elif line.strip() and turns:
            turns[-1]["text"] = (turns[-1]["text"] + " " + line.strip()).strip()
    for r in female: cast[r]["gender"] = "female"
    for i, t in enumerate(turns):
        t["kind"] = ("intro" if i == 0 else "question") if t["who"] == "host" else "answer"
        if t["who"] == "expert" and i > 1 and turns[i - 1]["kind"] == "question" and any(x["who"] == "expert" for x in turns[:i - 1]):
            t["shot"] = "expert_b"
    if not turns or turns[0]["who"] != "host": sys.exit("the script must start with a Host line")
    return {"format": "podcast3", "cast": cast, "turns": turns}


# ---------------------------------------------------------------- project
def cmd_new(a):
    raw = Path(a.tz).read_text(encoding="utf-8-sig")
    try:
        tz = json.loads(raw)
        for r in a.female: tz.setdefault("cast", {}).setdefault(r, {})["gender"] = "female"
    except json.JSONDecodeError:
        tz = parse_script(raw, a.female)
    pid = a.id
    if (pdir(pid) / "state.json").exists() and not a.force: sys.exit(f"{pid} exists (use --force)")
    for d in ("assets", "clips", "qa"): (pdir(pid) / d).mkdir(parents=True, exist_ok=True)
    (pdir(pid) / "tz.json").write_text(json.dumps(tz, ensure_ascii=False, indent=1), encoding="utf-8")
    st = {"id": pid, "format": tz.get("format", "podcast3"), "tz": tz, "assets": {}, "steps": {}, "turns": []}
    build_steps(st)
    for k, u in tz.get("frames", {}).items():  # the user's own frames are approved by definition
        st["assets"][k] = {"cands": [{"label": f"{k} 1", "job": f"external-{k}-1", "url": u, "external": True}], "approved": 0, "tries": 0}
    refresh(st); save(st)
    print(f"project {pid} created -> {pdir(pid)}")
    cmd_plan(argparse.Namespace(id=pid))

def build_steps(st):
    """turns -> generation steps (timing plan); keeps assets"""
    F = fmt_of(st); tz = st["tz"]; st["steps"], st["turns"] = {}, []
    for ti, t in enumerate(tz["turns"]):
        who, kind = t["who"], t.get("kind", "answer")
        lead = F["lead_in"][kind]
        wps = tz.get("wps", {}).get(who) or F["wps"].get(who) or F["wps"]["default"]
        segs = segment(t["text"], wps, lead, F)
        if sum(s["duration_s"] for s in segs) > F["max_total"]:
            sys.exit(f"turn {ti} is longer than {F['max_total']}s - split it into two turns")
        if kind == "intro":  # starts on one frame and cuts inside the clip to the reference frame(s)
            first = t.get("first") or F.get("intro_first", "wide")
            refs = t.get("refs") or [t.get("shot", who)]
        else:
            first, refs = t.get("shot", who), t.get("refs", [])
        turn = {"i": ti, "who": who, "kind": kind, "text": t["text"], "first": first, "refs": refs, "steps": []}
        for j, s in enumerate(segs):
            sid = f"t{ti}s{j}"
            turn["steps"].append(sid)
            st["steps"][sid] = {"id": sid, "turn": ti, "seg": j, "kind": kind if j == 0 else "ext", "who": who,
                                "text": s["text"], "duration_s": s["duration_s"], "speech_s": s["speech_s"], "speak": s["speak"],
                                "parent": f"t{ti}s{j-1}" if j else None, "takes": [], "chosen": None, "launches": 0}
        st["turns"].append(turn)

def cmd_resplit(a):
    """re-split the rest of a turn from <step> on (keeps the steps before it and every image): use when a turn's
    extensions keep failing on timing (rushed / swallowed words, pauses) - re-times with the speaker's MEASURED pace"""
    st = load(a.id); F = fmt_of(st)
    s0 = st["steps"][a.step]; turn = st["turns"][s0["turn"]]
    idx = turn["steps"].index(a.step)
    keep, redo = turn["steps"][:idx], turn["steps"][idx:]
    if keep and not st["steps"][keep[-1]]["chosen"]: sys.exit(f"{keep[-1]} has no chosen take to continue from")
    text = " ".join(st["steps"][x]["text"] for x in redo)
    wps = a.wps or calibrate_wps(st, s0["who"]) or F["wps"].get(s0["who"]) or F["wps"]["default"]
    segs = segment(text, wps, F["lead_in"][turn["kind"]], F, ext_only=bool(keep))
    planned = sum(st["steps"][x]["duration_s"] for x in keep) + sum(x["duration_s"] for x in segs)
    if planned > F["max_total"]: sys.exit(f"{planned}s > {F['max_total']}s for one turn - split the turn")
    arch = st.setdefault("archived_steps", [])
    for x in redo: arch.append(st["steps"].pop(x))
    new = []
    for j, sg in enumerate(segs):
        k = idx + j; sid = f"t{turn['i']}s{k}"; new.append(sid)
        st["steps"][sid] = {"id": sid, "turn": turn["i"], "seg": k, "kind": turn["kind"] if k == 0 else "ext", "who": s0["who"],
                            "text": sg["text"], "duration_s": sg["duration_s"], "speech_s": sg["speech_s"], "speak": sg["speak"],
                            "parent": (keep[-1] if j == 0 and keep else (new[j - 1] if j else None)), "takes": [], "chosen": None, "launches": 0}
    turn["steps"] = keep + new
    st["steps"] = {sid: st["steps"][sid] for t in st["turns"] for sid in t["steps"]}  # parents before children
    save(st)
    print(f"T{turn['i']} {s0['who']} re-split at {wps} wps (measured): kept {', '.join(keep) or '-'}")
    for sid, sg in zip(new, segs):
        print(f"  {sid}: {sg['duration_s']}s speak {sg['speak'][0]}-{sg['speak'][1]} ({sg['pace']} wps)  \"{sg['text']}\"")

def cmd_split_turn(a):
    """split-turn <id> <step> <shot>: the turn's text from <step> on becomes a NEW shot (base clip from frame <shot>),
    inserted right after; earlier steps and every other turn keep their takes. Use when an extension cannot be had
    (example T2: Omni moderation blocked every extension of one clip) - it adds one angle change."""
    st = load(a.id); F = fmt_of(st)
    s0 = st["steps"][a.step]; k = s0["turn"]; turn = st["turns"][k]
    idx = turn["steps"].index(a.step)
    if idx == 0: sys.exit("that is the first step of the turn - change the turn's shot instead")
    if a.shot not in st["assets"] or st["assets"][a.shot].get("approved") is None: sys.exit(f"{a.shot} is not an approved frame")
    moved = turn["steps"][idx:]
    text = " ".join(st["steps"][x]["text"] for x in moved)
    for x in moved: st.setdefault("archived_steps", []).append(st["steps"].pop(x))
    turn["steps"] = turn["steps"][:idx]
    turn["text"] = " ".join(st["steps"][x]["text"] for x in turn["steps"]); st["tz"]["turns"][k]["text"] = turn["text"]
    tzt = dict(st["tz"]["turns"][k]); tzt.update(text=text, shot=a.shot, kind="answer"); tzt.pop("first", None); tzt.pop("refs", None)
    st["tz"]["turns"].insert(k + 1, tzt)
    for t in st["turns"][k + 1:]: t["i"] += 1
    for x in st["steps"].values():
        if x["turn"] > k: x["turn"] += 1
    nt = {"i": k + 1, "who": turn["who"], "kind": "answer", "text": text, "first": a.shot, "refs": [], "steps": []}
    st["turns"].insert(k + 1, nt)
    wps = a.wps or F["wps"].get(turn["who"]) or F["wps"]["default"]
    segs = segment(text, wps, F["lead_in"]["answer"], F)
    for j, sg in enumerate(segs):
        sid = f"t{k}b{chr(97 + j)}"; nt["steps"].append(sid)
        st["steps"][sid] = {"id": sid, "turn": k + 1, "seg": j, "kind": "answer" if j == 0 else "ext", "who": turn["who"],
                            "text": sg["text"], "duration_s": sg["duration_s"], "speech_s": sg["speech_s"], "speak": sg["speak"],
                            "parent": nt["steps"][j - 1] if j else None, "takes": [], "chosen": None, "launches": 0}
    st["steps"] = {sid: st["steps"][sid] for t in st["turns"] for sid in t["steps"]}
    save(st)
    print(f"T{k} keeps {', '.join(turn['steps'])}; new T{k + 1} ({a.shot}): " + "; ".join(f"{x['duration_s']}s \"{x['text']}\"" for x in segs))

def cmd_split_head(a):
    """split-head <id> [--turn 0] [--wide F] [--close F]: an old one-clip headline (kind intro + refs: Omni redraws the
    first frame of an i2v with references - the wide turned into nonsense) becomes the
    current skill's two answer shots: the first sentence on the wide frame, the rest on the close-up, each its own
    base clip with the format's crowd line. Every other turn keeps its takes."""
    st = load(a.id); F = fmt_of(st); k = a.turn; turn = st["turns"][k]
    for f in (a.wide, a.close):
        if f not in st["assets"] or st["assets"][f].get("approved") is None: sys.exit(f"{f} is not an approved frame")
    sents = [x.strip() for x in re.findall(r"[^.!?]+[.!?]+[\"'”’)\]]*", turn["text"])] or [turn["text"]]
    if len(sents) >= 2: parts = [sents[0], " ".join(sents[1:])]
    else:   # one long sentence: cut at the comma nearest the middle
        commas = [m.end() for m in re.finditer(r",\s", turn["text"])]
        if not commas: sys.exit("one sentence without a comma - nothing to split")
        c = min(commas, key=lambda i: abs(i - len(turn["text"]) / 2))
        parts = [turn["text"][:c].strip(), turn["text"][c:].strip()]
    for x in turn["steps"]: st.setdefault("archived_steps", []).append(st["steps"].pop(x))
    keep = {q: v for q, v in st["tz"]["turns"][k].items() if q not in ("refs", "first", "kind", "shot", "text", "crowd")}
    st["tz"]["turns"][k:k + 1] = [dict(keep, kind="answer", shot=a.wide, text=parts[0]), dict(keep, kind="answer", shot=a.close, text=parts[1])]
    for t in st["turns"][k + 1:]: t["i"] += 1
    for x in st["steps"].values():
        if x["turn"] > k: x["turn"] += 1
    wps = F["wps"].get(turn["who"]) or F["wps"]["default"]
    new = []
    for j, (shot, text) in enumerate(zip((a.wide, a.close), parts)):
        nt = {"i": k + j, "who": turn["who"], "kind": "answer", "text": text, "first": shot, "refs": [], "steps": []}
        for q, sg in enumerate(segment(text, wps, F["lead_in"]["answer"], F)):
            sid = f"t{k}h{j}{chr(97 + q)}"; nt["steps"].append(sid)
            st["steps"][sid] = {"id": sid, "turn": k + j, "seg": q, "kind": "answer" if q == 0 else "ext", "who": turn["who"],
                                "text": sg["text"], "duration_s": sg["duration_s"], "speech_s": sg["speech_s"], "speak": sg["speak"],
                                "parent": nt["steps"][q - 1] if q else None, "takes": [], "chosen": None, "launches": 0}
        new.append(nt)
    st["turns"][k:k + 1] = new
    st["steps"] = {sid: st["steps"][sid] for t in st["turns"] for sid in t["steps"]}
    save(st)
    for nt in new: print(f"T{nt['i']} {nt['first']}: " + "; ".join(f"{s}: {st['steps'][s]['duration_s']}s \"{st['steps'][s]['text']}\"" for s in nt["steps"]))

def cmd_replan(a):
    """re-time all turns with the current planner/format, keeping every image; refused once clips exist"""
    st = load(a.id)
    if any(s["takes"] for s in st["steps"].values()) and not a.force: sys.exit("clips already launched - replan would drop them (use --force)")
    build_steps(st); save(st); cmd_plan(argparse.Namespace(id=a.id))

def cmd_plan(a):
    st = load(a.id); F = fmt_of(st); T = F["timeline"]
    tl, gen_s, rows = 0.0, 0, []
    for turn in st["turns"]:
        steps = [st["steps"][s] for s in turn["steps"]]
        lead = F["lead_in"][turn["kind"]]
        speech = sum(s["speech_s"] for s in steps)
        start = tl - (min(T["listen_overlap"], lead - T["gap"]) if turn["kind"] == "question" else (T["cut_lead"] if tl else 0))
        tl = max(tl, start) + (T["gap"] if tl else 0) + speech
        gen_s += sum(s["duration_s"] for s in steps)
        segs = " + ".join(f"{s['duration_s']}s{'(ext)' if s['seg'] else ''}" for s in steps)
        rows.append(f"T{turn['i']} {turn['who']:<7}{turn['kind']:<9} shot={turn['first']:<9} on screen ~{max(0,start):5.1f}-{tl:5.1f}s  gens: {segs}")
        for s in steps:
            rows.append(f"     {s['id']}: {s['duration_s']}s  ~{s['speech_s']}s speech  \"{s['text'][:70]}{'…' if len(s['text'])>70 else ''}\"")
    print("\n".join(rows))
    tk = F["takes"]
    est = sum(s["duration_s"] * (tk["first"] if not s["seg"] else tk["ext_first"]) for s in st["steps"].values())
    cast = st["tz"].get("cast", {})
    print(f"\ntotal ~{tl + T['end_hold']:.1f}s | {len(st['steps'])} generation steps, {gen_s}s per take | "
          f"first wave ≈ ${est * F['usd_per_s']:.2f} ({F['engine']} {F['resolution']}, {tk['first']} takes), retries extra | "
          + (f"images: {F['casting']['variants']} wide variants (different casts) + close-ups {', '.join(needed_assets(st)[1:])} ({F['image_model']}) | "
             if "casting" in F else f"frames: {', '.join(needed_assets(st))} | ")
          + "cast: " + ", ".join(f"{r} {'female' if is_female(c) else 'male'}" for r, c in cast.items()))

def needed_assets(st):
    used = {"wide"} if "casting" in fmt_of(st) else set()
    for t in st["turns"]: used |= {t["first"], *t["refs"]}
    if "expert_b" in used: used.add("expert")
    return [x for x in ASSET_ORDER if x in used] + sorted(x for x in used if x not in ASSET_ORDER)


# ---------------------------------------------------------------- images
ASSET_ORDER = ["wide", "host", "guest", "expert", "expert_b"]

def wide_prompt(st, F, cast):
    """the wide-shot prompt for one cast; for the roles the TZ leaves open (no ethnicity / ref / look) it adds the
    format's casting.default_look (models drift to one stock look otherwise); empty or missing = no casting line"""
    tz = st["tz"]
    default = [r for r in ROLES if not (tz.get("cast", {}).get(r, {}).get("ethnicity") or tz.get("ethnicity")
                                        or tz.get("cast", {}).get(r, {}).get("ref") or tz.get("cast", {}).get(r, {}).get("look"))]
    names = {"host": "the host", "guest": "the guest", "expert": "the expert"}
    look = F.get("casting", {}).get("default_look", "")
    line = "" if not (default and look) else (f" All three people are {look}." if len(default) == 3
                                   else f" {' and '.join(names[r] for r in default).capitalize()} {'is' if len(default) == 1 else 'are'} {look}.")
    return fill(F["assets"]["wide"]["prompt"], scene=tz.get("scene") or F["scene"], rules=F["image_rules"], casting_line=line, **cast)

MODEL_SHORT = {"openai-flare": "GPT", "openai-sunburst": "GPT Sunburst", "openai": "GPT 2", "gemini-pro": "Nano Banana Pro",
               "gemini": "Nano Banana 2", "seedream-5": "Seedream 5", "grok": "Grok"}

def cmd_img(a):
    """wide: one round = every cast of the round on every model of --models (default: the format's image_model),
    one generate_images call per line; wide --from <label> --note: edit that variant (continues its image);
    close-ups: one line, built from the approved wide on the same model as that wide"""
    st = load(a.id); F = fmt_of(st); tz = st["tz"]
    A = st["assets"].setdefault(a.asset, {"cands": [], "approved": None, "tries": 0})
    rules, scene = F["image_rules"], tz.get("scene") or F.get("scene", "")
    extra = (" " + tz["style"] if tz.get("style") else "") + ("\n" + a.note if a.note else "")
    models = [m.strip() for m in a.models.split(",")] if a.models else None
    def args_for(model, prompt, **kw):
        return {"model": model, "aspect_ratio": F["aspect_ratio"], "resolution": F["image_resolution"], "count": 1, "prompt": prompt, **kw}
    out, pending = [], []
    if a.asset == "wide" and a.frm:
        if not a.note: sys.exit("--from needs --note \"<what to change>\"")
        base = A["cands"][find_cand(A, a.frm)]
        m = 1 + sum(1 for c in A["cands"] + A.get("pending", []) if c.get("base") == base["label"])
        lab, model = f"{base['label']}.{m}", (models or [base.get("model", F["image_model"])])[0]
        prompt = fill(F["assets"]["wide"]["edit_prompt"], note=a.note, rules=rules) + (f"\n(variant {m})" if m > 1 else "")
        pending.append({"label": lab, "cast": base["cast"], "round": base.get("round"), "base": base["label"],
                        "edits": base.get("edits", []) + [a.note], "model": model})
        out.append({"label": lab, "tool": "generate_images", "args": args_for(model, prompt, parent_job_id=base["job"])})
    elif a.asset == "wide" and a.redo:  # same cast and model, generated again from scratch (self-check failures)
        for key in [k.strip() for k in a.redo.split(",") if k.strip()]:
            base = A["cands"][find_cand(A, key)]
            m = 1 + sum(1 for c in A["cands"] if c.get("redo_of") == base["label"])
            lab, model = f"{base['label']}r{m}", (models or [base.get("model", F["image_model"])])[0]
            kw = {}
            refs = [base["cast"][r]["ref"] for r in ROLES if base["cast"][r].get("ref")]
            if refs: kw["reference_image_urls"] = refs
            prompt = wide_prompt(st, F, base["cast"]) + extra + f"\n(variant {m + 1})"
            pending.append({"label": lab, "cast": base["cast"], "round": base.get("round"), "model": model, "redo_of": base["label"]})
            out.append({"label": lab, "model": MODEL_SHORT.get(model, model), "tool": "generate_images", "args": args_for(model, prompt, **kw)})
    elif a.asset == "wide":
        rnd = 1 + max([c.get("round") or 0 for c in A["cands"]], default=0)
        prev = [c["cast"] for c in A["cands"] if c.get("cast")]
        casts = make_casts(st, F, rnd, F["casting"]["variants"], prev)
        for mi, model in enumerate(models or [F["image_model"]]):
            for k, cast in enumerate(casts):
                lab = f"{LETTERS[(rnd - 1) % 26]}{mi * len(casts) + k + 1}"
                kw = {}
                refs = [cast[r]["ref"] for r in ROLES if cast[r].get("ref")]
                if refs: kw["reference_image_urls"] = refs
                prompt = wide_prompt(st, F, cast) + extra
                pending.append({"label": lab, "cast": cast, "round": rnd, "model": model})
                out.append({"label": lab, "model": MODEL_SHORT.get(model, model), "tool": "generate_images", "args": args_for(model, prompt, **kw)})
    elif a.asset in F.get("angles", {}) and a.frm:  # edit an angle frame in place (continues its image)
        if not a.note: sys.exit("--from needs --note \"<what to change>\"")
        base = A["cands"][find_cand(A, a.frm)]
        lab = f"{a.asset} {len(A['cands']) + 1}"
        model = (models or [base.get("model", F["image_model"])])[0]
        pending.append({"label": lab, "model": model, "base": base["label"], "edits": base.get("edits", []) + [a.note]})
        out.append({"label": lab, "tool": "generate_images",
                    "args": args_for(model, fill(F["angle_edit"], note=a.note, rules=rules), parent_job_id=base["job"])})
    elif a.asset in F.get("angles", {}):  # a camera angle of a given-frame format, built from the user's frame(s)
        refs = [u for k, u in tz.get("frames", {}).items() if k != a.asset]
        if not refs: sys.exit("tz 'frames' has no frame to build the angle from")
        hero, pb = tz.get("cast", {}).get("hero", {}), F.get("pronoun_base", "he")
        prompt = fill(gendered(F["angle_common"], hero, pb), look=tz.get("look", {}), rules=rules,
                      topic=tz.get("topic") or F.get("topic_default", ""),
                      angle=fill(gendered(F["angles"][a.asset]["angle"], hero, pb), look=tz.get("look", {}))) + extra
        n_prev = len(A["cands"]) + len(A.get("pending", []))
        if n_prev: prompt += f"\n(variant {n_prev + 1})"
        lab = f"{a.asset} {len(A['cands']) + 1}"
        model = (models or [F["image_model"]])[0]
        pending.append({"label": lab, "model": model})
        out.append({"label": lab, "tool": "generate_images", "args": args_for(model, prompt, reference_image_urls=refs)})
    else:
        W = approved(st, "wide")
        spec, who = F["assets"][a.asset], a.asset.split("_")[0]
        cast = W["cast"][who]
        model = (models or [W.get("model", F["image_model"])])[0]
        if "continue" in spec:  # expert_b continues the latest expert close-up (they are approved together)
            B = st["assets"].get(spec["continue"])
            if not B or not B["cands"]: sys.exit(f"make '{spec['continue']}' first")
            base = B["cands"][B["approved"] if B.get("approved") is not None else -1]
            prompt, kw, meta = fill(gendered(spec["prompt"], cast), rules=rules), {"parent_job_id": base["job"]}, {"base": base["job"]}
        else:
            # after an edit of the wide the look text may be stale - then the reference image alone defines the person
            look = "exactly as in the reference image" if W.get("edits") else cast["look"]
            prompt = fill(gendered(F["assets"]["closeup_prompt"], cast), seat=spec["seat"], look=look, framing=F["cu_framing"], rules=rules)
            kw, meta = {"reference_image_urls": [W["url"]]}, {}
        prompt += extra
        n_prev = len(A["cands"]) + len(A.get("pending", []))
        if n_prev: prompt += f"\n(variant {n_prev + 1})"  # identical args would replay an earlier launch
        lab = f"{a.asset} {len(A['cands']) + 1}"
        pending.append({"label": lab, "model": model, "from_wide": W["job"], **meta})
        out.append({"label": lab, "tool": "generate_images", "args": args_for(model, prompt, **kw)})
    A["pending"] = pending; save(st)
    for o in out: print(json.dumps(o, ensure_ascii=False))

def approved(st, asset):
    A = st["assets"].get(asset)
    if not A or A.get("approved") is None: sys.exit(f"asset '{asset}' is not approved yet")
    return A["cands"][A["approved"]]

IMG_CDN = "https://<kaleido-cdn>/image-studio/{}.png"

def cmd_img_add(a):
    """img-add <id> <asset> <job_id...> - binds jobs to the img lines (same order), derives CDN urls, waits until they exist"""
    import requests
    st = load(a.id)
    A = st["assets"].setdefault(a.asset, {"cands": [], "approved": None, "tries": 0})
    known = {c["job"] for c in A["cands"]}
    jobs = [j for j in dict.fromkeys(a.jobs) if not j.startswith("http")]
    dup = [j for j in jobs if j in known]
    jobs = [j for j in jobs if j not in known]
    if dup: print(f"ignored {len(dup)} job id(s) already recorded (a replayed launch?)")
    pend = A.get("pending") or []
    if len(jobs) != len(pend):
        sys.exit(f"expected {len(pend)} new job id(s), in the order of the img lines: {', '.join(p['label'] for p in pend) or 'run img first'}")
    batch = 1 + max([c.get("batch", 0) for c in A["cands"]], default=0)
    for p, job in zip(pend, jobs):
        A["cands"].append({**p, "job": job, "url": IMG_CDN.format(job), "batch": batch})
    A["pending"] = []
    if a.asset != "wide": A["approved"] = None
    save(st)
    t_end, todo = time.time() + a.timeout, set(jobs)
    while todo and time.time() < t_end:
        for j in list(todo):
            try:
                if requests.head(IMG_CDN.format(j), timeout=20).status_code == 200: todo.discard(j)
            except Exception: pass
        if todo: time.sleep(5)
    print(f"{a.asset}: {len(A['cands'])} candidate(s)" + (f"; NOT READY after {a.timeout}s: {' '.join(todo)} - check get_image_generation_job" if todo else "; all ready"))

def download(url, dst):
    import requests
    if dst.exists() and dst.stat().st_size > 0: return dst
    r = requests.get(url, timeout=120); r.raise_for_status()
    dst.write_bytes(r.content); return dst

def cmd_img_sheet(a):
    from PIL import Image, ImageDraw, ImageFont
    st = load(a.id)
    tiles = []
    for asset in a.assets:
        A = st["assets"].get(asset)
        if not A or not A["cands"]: continue
        if a.all:
            show = list(A["cands"])
        elif asset == "wide":  # the latest round; a redo replaces its original, an edit sits next to its original
            last = max(c.get("round") or 0 for c in A["cands"])
            rnd = [c for c in A["cands"] if (c.get("round") or 0) == last]
            redone = {c["redo_of"] for c in rnd if c.get("redo_of")}
            latest_redo = {}
            for c in rnd:
                if c.get("redo_of"): latest_redo[c["redo_of"]] = c
            show = []
            for c in rnd:
                if c.get("redo_of"): continue
                show.append(latest_redo.get(c["label"], c) if c["label"] in redone else c)
            show += [c for c in rnd if c.get("base") and c not in show]
        else:
            show = [A["cands"][-1]]
        multi = len({c.get("model") for c in show}) > 1
        for c in show:
            ext = os.path.splitext(c["url"].split("?")[0])[1] or ".png"
            f = download(c["url"], pdir(a.id) / "assets" / f"{asset}_{c['label'].replace(' ', '_')}{ext}")
            c["file"] = str(f)
            label = c["label"] if asset == "wide" else asset.replace("_b", " gesture")
            if multi: label += f" · {MODEL_SHORT.get(c.get('model'), c.get('model'))}"
            tiles.append((label, f))
    save(st)
    if not tiles: sys.exit("nothing to show")
    H, per_row = 900, (3 if len(tiles) > 4 else len(tiles))
    try: font = ImageFont.truetype("arial.ttf", 44)
    except Exception: font = ImageFont.load_default()
    ims = []
    for label, f in tiles:
        im = Image.open(f).convert("RGB"); im = im.resize((int(im.width * H / im.height), H))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, im.width, 64], fill=(0, 0, 0)); d.text((16, 8), label, fill=(255, 255, 255), font=font)
        ims.append(im)
    rows = [ims[i:i + per_row] for i in range(0, len(ims), per_row)]
    W = max(sum(i.width for i in r) + 12 * (len(r) - 1) for r in rows)
    sheet = Image.new("RGB", (W, H * len(rows) + 12 * (len(rows) - 1)), (30, 30, 30))
    for ri, r in enumerate(rows):
        x = 0
        for im in r: sheet.paste(im, (x, ri * (H + 12))); x += im.width + 12
    out = pdir(a.id) / "assets" / f"sheet_{'_'.join(a.assets)}.jpg"
    sheet.save(out, quality=88)
    print(out)

def cmd_approve(a):
    st = load(a.id)
    A = st["assets"][a.asset]
    n = find_cand(A, a.n)
    c = A["cands"][n]
    # a close-up must come from the approved wide, expert_b from the approved expert close-up
    if a.asset != "wide":
        W = st["assets"].get("wide", {})
        wj = W["cands"][W["approved"]]["job"] if W.get("approved") is not None else None
        if c.get("from_wide") and c["from_wide"] != wj:
            sys.exit(f"{a.asset} {c.get('label')} was made from another wide shot - regenerate: img {a.id} {a.asset}")
    if a.asset == "expert_b":
        E = st["assets"].get("expert", {})
        ej = E["cands"][E["approved"]]["job"] if E.get("approved") is not None else None
        base_job = c.get("base") or A.get("base")  # older candidates kept it on the asset
        if ej and base_job and base_job != ej:
            sys.exit(f"expert_b {c.get('label')} continues another expert close-up - regenerate: img {a.id} expert_b")
    changed = A.get("approved") != n
    A["approved"] = n
    regen = []
    if changed:  # images built on the old one are no longer valid: they must be regenerated, not re-approved
        for d in {"wide": ["host", "guest", "expert", "expert_b"], "expert": ["expert_b"]}.get(a.asset, []):
            D = st["assets"].get(d, {})
            if not D.get("cands"): continue
            if d == "expert_b" and (D["cands"][-1].get("base") or D.get("base")) == c["job"]:
                continue  # the gesture frame already continues this expert close-up
            D["approved"] = None; regen.append(d)
    refresh(st); save(st)
    print(f"{a.asset} -> {c.get('label', n + 1)} approved" + (f"; regenerate: {', '.join(regen)}" if regen else ""))


# ---------------------------------------------------------------- clip steps
def take_files(st, t): return pdir(st["id"]) / "clips" / f"{t['step']}_{t['job'][:8]}.mp4"

def step_src(st, s):
    """what a take of this step is made from right now (approved images / chosen parent); None = not available"""
    if s["kind"] == "ext":
        p = st["steps"][s["parent"]]
        return {"parent": p["chosen"]} if p["chosen"] else None
    turn = st["turns"][s["turn"]]
    try:
        src = {"image": approved(st, turn["first"])["url"], "refs": [approved(st, r)["url"] for r in turn["refs"]]}
        if turn["first"] == "expert_b":  # the gesture frame must continue the approved expert close-up
            eb, ex = approved(st, "expert_b"), approved(st, "expert")
            base_job = eb.get("base") or st["assets"]["expert_b"].get("base")
            if base_job and base_job != ex["job"]: return None
    except SystemExit:
        return None
    return src

def refresh(st):
    """re-open steps whose chosen take was made from images/parents that are no longer current, then pick the best
    passing take of every finished step (also after a sibling take failed or a wait was interrupted)"""
    for s in st["steps"].values():  # stored in turn/segment order: parents before children
        if s["chosen"]:
            t = next((x for x in s["takes"] if x["job"] == s["chosen"]), None)
            src = step_src(st, s)
            if t is None or ("src" in t and t["src"] != src):
                for x in s["takes"]:
                    if x["status"] in ("launched", "done") and x.get("src") != src: x["status"] = "stale"
                s["chosen"] = None; s["launches"] = 0
                invalidate_children(st, s["id"])
        if not s["chosen"] and not any(x["status"] == "launched" for x in s["takes"]):
            pick_best(st, s)

def step_state(st, s):
    if s["chosen"]: return "done"
    if any(t["status"] == "launched" for t in s["takes"]): return "running"
    if step_src(st, s) is None: return "blocked"
    return "ready"

def cmd_gen(a):
    st = load(a.id); F = fmt_of(st); refresh(st); out = []
    L = len(F["retry_lines"])
    for s in st["steps"].values():
        if step_state(st, s) != "ready": continue
        if s["launches"] >= F["max_launches"]:
            out.append({"step": s["id"], "blocked": f"{s['launches']} launches used - ask the user, then: step {st['id']} {s['id']} --note \"...\" (or --text \"...\") --reset, or pick a take"}); continue
        cast = proj_cast(st)[s["who"]]
        src = step_src(st, s)
        # pronouns are swapped in the template only - the spoken text stays verbatim
        pb = F.get("pronoun_base", "he")
        tpl = gendered(F["prompts"][s["kind"]], cast, pb)
        # who is really in this frame: tz turn override > the angle's own line > the generic line (never name people
        # who are not visible - Omni paints them in)
        ang = F.get("angles", {}).get(st["turns"][s["turn"]]["first"], {})
        crowd = st["tz"]["turns"][s["turn"]].get("crowd") or gendered(ang["crowd"] if "crowd" in ang else F.get("crowd", ""), cast, pb)
        crowd = fill(crowd, look=st["tz"].get("look", {}))
        base = fill(tpl, text=s["text"], voice=cast.get("voice", ""), seat=F.get("seats", {}).get(s["who"], ""), crowd=crowd)
        if s.get("speak"):  # pace control: when to start and finish, so base and extensions keep one tempo
            a0, a1 = s["speak"]; last = s["id"] == st["turns"][s["turn"]]["steps"][-1]
            base += "\n" + gendered(fill(F["timing"]["ext" if s["kind"] == "ext" else "base"], start=f"{a0:g}", end=f"{a1:g}",
                                         after=F["timing"]["after_last" if last else "after_seam"]), cast, pb)
        if s.get("note"): base += "\n" + s["note"]
        seq = s.get("seq", 0)
        def with_line(k):  # every call of a step gets its own args, or Kaleido replays an earlier launch
            ln = (F["retry_lines"][k % L] + (f" (take {k + 1})" if k >= L else "")).strip()
            return base + ("\n" + ln if ln else "")
        retry = s["launches"] > 0
        if s["kind"] != "ext":
            args = {"engine": F["engine"], "mode": "image2video", "prompt": with_line(seq), "duration_s": s["duration_s"],
                    "resolution": F["resolution"], "image_url": src["image"], "count": F["takes"]["retry" if retry else "first"]}
            if src["refs"]: args["reference_image_urls"] = src["refs"]
            out.append({"step": s["id"], "tool": "generate_videos", "args": args})
        else:
            for k in range(F["takes"]["ext_retry" if retry else "ext_first"]):
                out.append({"step": s["id"], "tool": "generate_video_extension",
                            "args": {"job_id": src["parent"], "prompt": with_line(seq + k), "duration_s": s["duration_s"]}})
        s["wave"] = {"open": True, "src": src}
    save(st)
    if not out:
        states = {sid: step_state(st, s) for sid, s in st["steps"].items()}
        if all(v == "done" for v in states.values()): print("ALL STEPS DONE -> run: timeline"); return
        print("nothing to launch now: " + ", ".join(f"{k}={v}" for k, v in states.items() if v != "done")); return
    for o in out: print(json.dumps(o, ensure_ascii=False))

def cmd_launched(a):
    """register the job ids of one gen wave of a step (all its calls; one or several invocations are fine)"""
    st = load(a.id); s = st["steps"][a.step]
    known = {t["job"] for x in st["steps"].values() for t in x["takes"]}
    new = [j for j in dict.fromkeys(a.jobs) if j not in known]
    w = s.get("wave") or {}
    if new and w.get("open"):
        s["launches"] += 1; w["open"] = False
    for j in new:
        s["takes"].append({"step": a.step, "job": j, "url": CDN.format(j), "status": "launched", "t": time.time(),
                           "seq": s.get("seq", 0), "src": w.get("src", step_src(st, s))})
        s["seq"] = s.get("seq", 0) + 1
    save(st)
    skipped = len(a.jobs) - len(new)
    print(f"{a.step}: +{len(new)} take(s), launch #{s['launches']}" + (f"; {skipped} already known (replayed launch?) - ignored" if skipped else ""))

def find_take(st, job):
    for s in st["steps"].values():
        for t in s["takes"]:
            if t["job"].startswith(job): return s, t
    sys.exit(f"take {job} not found")

def invalidate_children(st, sid):
    for s in st["steps"].values():
        if s["parent"] == sid:
            for t in s["takes"]:
                if t["status"] in ("launched", "done"): t["status"] = "stale"
            s["chosen"] = None; s["launches"] = 0
            invalidate_children(st, s["id"])

def cmd_fail(a):
    st = load(a.id); s, t = find_take(st, a.job)
    t["status"] = "failed"; t["reason"] = a.reason or "provider failure"
    refresh(st); save(st)
    print(f"{s['id']} {t['job'][:8]} marked failed" + (f"; picked {s['chosen'][:8]}" if s["chosen"] else ""))

def cmd_reject(a):
    st = load(a.id); s, t = find_take(st, a.job)
    t["status"] = "rejected"; t["reason"] = a.reason or "rejected"
    if s["chosen"] == t["job"]:
        s["chosen"] = None; invalidate_children(st, s["id"])
    refresh(st); save(st); print(f"{s['id']}: {t['job'][:8]} rejected; chosen={(s['chosen'] or '-')[:8]}")

def cmd_pick(a):
    st = load(a.id); s = st["steps"][a.step]; _, t = find_take(st, a.job)
    if t["step"] != a.step: sys.exit(f"{a.job} belongs to {t['step']}")
    if s["chosen"] != t["job"]: invalidate_children(st, s["id"])
    s["chosen"] = t["job"]; t["status"] = "done"; save(st); print(f"{a.step} -> {t['job'][:8]}")

def cmd_step(a):
    """change one step without touching the rest of the project: --note (extra prompt line), --text (new words for
    this segment, re-timed), --shot (another approved frame for the whole turn: its base clip and extensions are
    redone; e.g. T7 -> a close-up like the last shot), --reset (fresh launch budget)"""
    st = load(a.id); F = fmt_of(st); s = st["steps"][a.step]
    if getattr(a, "shot", None):
        if s["seg"] != 0: sys.exit("--shot goes on the first step of the turn")
        if a.shot not in st["assets"] or st["assets"][a.shot].get("approved") is None: sys.exit(f"{a.shot} is not an approved frame")
        turn = st["turns"][s["turn"]]; turn["first"] = a.shot; st["tz"]["turns"][s["turn"]]["shot"] = a.shot
        for t in s["takes"]:
            if t["status"] in ("launched", "done"): t["status"] = "stale"
        s["chosen"] = None; invalidate_children(st, s["id"]); s["launches"] = 0
    if a.note is not None: s["note"] = a.note.strip()
    if a.text is not None:
        turn = st["turns"][s["turn"]]
        wps = st["tz"].get("wps", {}).get(s["who"]) or F["wps"].get(s["who"]) or F["wps"]["default"]
        d = clip_len(F, F["lead_in"][turn["kind"]], nwords(a.text) / wps, s["seg"] == 0, s["id"] == turn["steps"][-1])
        cap = F["max_first"] if s["seg"] == 0 else F["max_ext"]
        if d > cap: sys.exit(f"{nwords(a.text)} words need ~{d:.1f}s > {cap}s for one clip - shorten it")
        s["text"] = a.text.strip(); s["speech_s"] = round(nwords(a.text) / wps, 2)
        s["duration_s"] = int(min(cap, max(F["min_dur"], math.ceil(d))))
        for t in s["takes"]:
            if t["status"] in ("launched", "done"): t["status"] = "stale"
        s["chosen"] = None; invalidate_children(st, s["id"])
    if a.reset or a.note is not None or a.text is not None: s["launches"] = 0
    save(st); print(f"{a.step}: {s['duration_s']}s, launches={s['launches']}, note={s.get('note') or '-'}")

def pick_best(st, s, quiet=True):
    ok = [t for t in s["takes"] if t["status"] == "done" and t.get("qa", {}).get("pass")]
    if not ok: return None
    best = min(ok, key=lambda t: t["qa"]["score"])
    s["chosen"] = best["job"]; return best


# ---------------------------------------------------------------- QA
STOP = set("a an the and but or so to of in on at for with that this it just then than as if".split())
# meaning-carrying short words: never fuzzy-matched, a swap is an error (he/she, can/can't, did/didn't ...)
CRITICAL = set(("he she him her his hers they them their we us our you your i me my "
                "not no never can cant cannot did didnt do dont does doesnt is isnt was wasnt will wont would wouldnt "
                "should shouldnt could couldnt are arent have havent has hasnt had hadnt yes").split())
NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
       "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
       "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
       "hundred": "100", "percent": "%"}

def norm_tokens(text):
    t = text.lower().replace("’", "'").replace("-", " ").replace("%", " percent ")
    out = []
    for w in re.findall(r"[a-z0-9']+", t):
        w = w.strip("'")
        if not w: continue
        out.append(NUM.get(w, w).replace("'", ""))
    return out

def sim(a, b):
    from difflib import SequenceMatcher
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()

def match(a, b, th):
    """'exact' | 'fuzzy' | None. Fuzzy only for longer non-critical words that are not negation variants."""
    if a == b: return "exact"
    if a in CRITICAL or b in CRITICAL or min(len(a), len(b)) < 4: return None
    if a + "nt" == b or b + "nt" == a or a.endswith("nt") != b.endswith("nt"): return None
    return "fuzzy" if sim(a, b) >= th else None

def align(exp, got, th):
    """edit-distance alignment: exact match 0, fuzzy match 0.15 (an exact take always ranks first), sub/ins/del 1,
    and 2<->1 merges (anyone ~ any one) only when neither half is itself the word"""
    n, m = len(exp), len(got)
    INF = 1e9
    D = [[INF] * (m + 1) for _ in range(n + 1)]; B = [[None] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            if D[i][j] >= INF: continue
            c = D[i][j]
            cand = []
            if i < n and j < m:
                mt = match(exp[i], got[j], th)
                cand.append((i + 1, j + 1, c + (0 if mt == "exact" else 0.15 if mt else 1), ("ok" if mt == "exact" else "fz" if mt else "sub", i, j)))
            if i < n: cand.append((i + 1, j, c + 1, ("del", i, None)))
            if j < m: cand.append((i, j + 1, c + 1, ("ins", None, j)))
            if i < n and j + 1 < m and exp[i] not in CRITICAL and sim(exp[i], got[j] + got[j + 1]) >= max(th, 0.8) \
                    and max(sim(exp[i], got[j]), sim(exp[i], got[j + 1])) < 0.9:
                cand.append((i + 1, j + 2, c + 0.15, ("fz", i, j)))
            if i + 1 < n and j < m and got[j] not in CRITICAL and sim(exp[i] + exp[i + 1], got[j]) >= max(th, 0.8) \
                    and max(sim(exp[i], got[j]), sim(exp[i + 1], got[j])) < 0.9:
                cand.append((i + 2, j + 1, c + 0.15, ("fz", i, j)))
            for ni, nj, nc, op in cand:
                if nc < D[ni][nj]: D[ni][nj] = nc; B[ni][nj] = (i, j, op)
    ops, i, j = [], n, m
    while (i, j) != (0, 0):
        pi, pj, op = B[i][j]; ops.append(op); i, j = pi, pj
    return ops[::-1]

_MODEL = None
def whisper(F):
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        dev = os.environ.get("POD_WHISPER_DEVICE", "cpu")
        # POD_WHISPER_MODEL=medium when RAM is short (large-v3 needs a ~4 GB peak while loading; mkl_malloc = out of RAM)
        _MODEL = WhisperModel(os.environ.get("POD_WHISPER_MODEL") or F["qa"]["whisper_model"], device=dev,
                              compute_type="float16" if dev == "cuda" else "int8", cpu_threads=os.cpu_count() or 8)
    return _MODEL

def ff(args):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr

def probe_dur(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: raise RuntimeError(f"ffprobe could not read a duration from {f}")

def energy(f):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768
    hop = 320  # 20 ms
    k = len(x) // hop
    if k == 0: return np.array([]), 0.02
    fr = x[:k * hop].reshape(k, hop)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9), 0.02

# ---- verbatim layer: wav2vec2 CTC has no language model, so it does NOT smooth over repeats ("what what"),
# stutters or swallowed words the way whisper does (whisper even "hears" expected words that were never said)
_W2V = None
def w2v():
    global _W2V
    if _W2V is None:
        import torchaudio
        b = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
        _W2V = (b.get_model().eval(), b.get_labels())
    return _W2V

def pcm16(f):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()

INV_NUM = {v: k for k, v in NUM.items() if v.isdigit()}
def spell(tok):
    if tok == "%": return ["PERCENT"]
    if tok in INV_NUM: return [INV_NUM[tok].upper()]
    if tok.isdigit() and len(tok) == 2 and tok[0] != "1" and tok[0] + "0" in INV_NUM:
        return [INV_NUM[tok[0] + "0"].upper()] + ([INV_NUM[tok[1]].upper()] if tok[1] != "0" else [])
    return [re.sub(r"[^A-Z]", "", tok.upper()) or "A"]

def script_tokens(text):
    """normalized tokens of the script + whether the script has punctuation (a natural pause) after each"""
    out = []
    for m in re.finditer(r"([A-Za-z0-9'’\-]+)([^A-Za-z0-9'’\-]*)", text):
        toks = norm_tokens(m.group(1))
        for k, tok in enumerate(toks):
            out.append((tok, k == len(toks) - 1 and bool(re.search(r"[.,;:!?…—–]", m.group(2)))))
    return out

def verbatim(x, text, t0=0.0):
    """x: 16 kHz mono samples of the checked window. Returns the literal CTC transcript and, per script token,
    (start, end, score) from forcing the script onto the audio (a missing word gets squeezed to ~0 s, score ~0)"""
    import torch, torchaudio
    model, labels = w2v(); dic = {c: i for i, c in enumerate(labels)}
    with torch.inference_mode():
        em, _ = model(torch.from_numpy(x).unsqueeze(0))
    em = torch.log_softmax(em, dim=-1)[0]
    ids, chars, prev = em.argmax(-1).tolist(), [], None
    for i in ids:
        if i != prev and i != 0: chars.append(labels[i])
        prev = i
    heard = norm_tokens("".join(chars).replace("|", " ").lower())
    toks = script_tokens(text)
    words, owner = [], []
    for k, (tok, _) in enumerate(toks):
        for w in spell(tok): words.append(w); owner.append(k)
    sep = dic["|"]
    target = []
    for n, w in enumerate(words):
        if n: target.append(sep)
        target += [dic[c] for c in w if c in dic]
    ali, sc = torchaudio.functional.forced_align(em.unsqueeze(0), torch.tensor([target], dtype=torch.int32), blank=0)
    ali, sc = ali[0].tolist(), sc[0].exp().tolist()
    frames, scores = [[]], [[]]
    for f, (tok, s) in enumerate(zip(ali, sc)):
        if tok == sep:
            if frames[-1]: frames.append([]); scores.append([])
        elif tok != 0:
            frames[-1].append(f); scores[-1].append(s)
    frames = [fr for fr in frames if fr]; scores = [s for s in scores if s]
    per = [None] * len(toks)
    for n, k in enumerate(owner):
        if n >= len(frames): continue
        a, b, s = t0 + frames[n][0] * 0.02, t0 + (frames[n][-1] + 1) * 0.02, sum(scores[n]) / len(scores[n])
        per[k] = (a, b, s) if per[k] is None else (per[k][0], b, min(per[k][2], s))
    return heard, toks, per

def verbatim_checks(Q, x, text, t0, errors, warns, out=None):
    heard, toks, per = verbatim(x, text, t0)
    if out is not None: out["toks"], out["per"], out["heard"] = toks, per, heard
    exp = [t for t, _ in toks]
    # 1) repeats: an n-gram said twice in a row that the script does not repeat
    def reps(seq):
        r = set()
        for n in (1, 2, 3):
            for p in range(len(seq) - 2 * n + 1):
                if seq[p:p + n] == seq[p + n:p + 2 * n]:
                    r.add(" ".join(seq[p:p + n]))
        return r
    for g in sorted(reps(heard) - reps(exp)):
        errors.append(f"repeat '{g} {g}' (verbatim ASR)")
    # 1b) stutter: a cut-off start of the next word ("the ult ultimate" - seen in a QA-passed take)
    exp_set = set(exp)
    for p in range(len(heard) - 1):
        a_, b_ = heard[p], heard[p + 1]
        if 2 <= len(a_) < len(b_) and b_.startswith(a_) and a_ not in exp_set:
            errors.append(f"stutter '{a_} {b_}' (verbatim ASR)")
    # 2) missing / garbled: dropped by the literal transcript AND barely present when the script is forced on
    ops = align(exp, heard, 0.6)
    dropped = {i for o, i, j in ops if o == "del"}
    for i, tok in enumerate(exp):
        p = per[i]
        weak = p is None or p[2] < Q["w2v_missing"] or (p[1] - p[0]) < 0.06
        if i in dropped and weak and tok not in STOP:
            errors.append(f"missing '{tok}'" + (f" @{p[0]:.1f}s" if p else "") + " (verbatim ASR)")
        elif i in dropped and weak:
            warns.append(f"maybe swallowed '{tok}'" + (f" @{p[0]:.1f}s" if p else ""))
    # 2b) extra words at the edges: the literal transcript has something before the first / after the last script word
    #     (example T2 opened with a clipped "us" before "I analyzed" - whisper ignored it)
    oks = [k for k, (o, i, j) in enumerate(ops) if o in ("ok", "fz", "sub")]
    if oks:
        pre = [heard[j] for o, i, j in ops[:oks[0]] if o == "ins"]
        post = [heard[j] for o, i, j in ops[oks[-1] + 1:] if o == "ins"]
        if pre: errors.append(f"extra sound/word before the first word: '{' '.join(pre)}' (verbatim ASR)")
        if post: errors.append(f"extra sound/word after the last word: '{' '.join(post)}' (verbatim ASR)")
    # 3) unnatural pause inside a sentence (no punctuation in the script there): the aligner only locates the gap,
    #    its length is the longest real silence (< -45 dB) between the two words
    import numpy as np
    hop = 320
    fr = x[: len(x) // hop * hop].reshape(-1, hop)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9) if len(fr) else np.array([])
    # 4) unexplained sound: a VOICED (or loud) stretch between two aligned script words - a foreign voice, a mumble,
    #    an artifact. A breath is unvoiced and quiet, so it passes.
    try:
        import librosa
        for i in range(len(toks) - 1):
            if not (per[i] and per[i + 1]): continue
            g0, g1 = per[i][1] + 0.1, per[i + 1][0] - 0.1
            if g1 - g0 < 0.25: continue
            seg = x[int((g0 - t0) * 16000):int((g1 - t0) * 16000)]
            if len(seg) < 1100: continue
            _, voiced, _ = librosa.pyin(seg, fmin=70, fmax=400, sr=16000, frame_length=1024, hop_length=320)
            fr = seg[: len(seg) // 320 * 320].reshape(-1, 320)
            lv = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9)
            n = min(len(lv), len(voiced))
            bad = sum(1 for k in range(n) if lv[k] > Q["noise_db"] and (voiced[k] or lv[k] > -30))
            if bad >= 8: errors.append(f"unexplained voiced sound between '{toks[i][0]}' and '{toks[i + 1][0]}' @{g0:.1f}s")
    except ImportError:
        pass
    for i in range(len(toks) - 1):
        if not (per[i] and per[i + 1]) or toks[i][1]: continue
        a, b = int((per[i][1] - 0.15 - t0) * 50), int((per[i + 1][0] + 0.15 - t0) * 50)
        run = best = 0
        for k in range(max(0, a), min(len(db), b)):
            run = run + 1 if db[k] < -45 else 0; best = max(best, run)
        gap = best * 0.02
        if gap > Q["mid_pause"]: errors.append(f"unnatural {gap:.1f}s pause after '{toks[i][0]}' @{per[i][1]:.1f}s")
        elif gap > Q["mid_pause_warn"]: warns.append(f"{gap:.1f}s pause after '{toks[i][0]}' @{per[i][1]:.1f}s")
    return " ".join(heard)

def panel_motion(f, t0, t1, Q, W=72, H=128):
    """mean frame-to-frame change per seat column of the wide panel shot (format qa panel_band / panel_cols).
    Calibrated: frozen / barely moving extras 0.35-0.84, lively ones 1.06-3.18"""
    import numpy as np
    b = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{max(0.3, t1 - t0):.2f}", "-i", str(f), "-vf",
                        f"fps=12,scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    v = np.frombuffer(b[: len(b) // (W * H) * W * H], np.uint8).reshape(-1, H, W).astype(np.float32)
    if len(v) < 3: return [0.0] * Q.get("panel_cols", 4)
    y0, y1 = Q.get("panel_band", [0.25, 0.62]); n = Q.get("panel_cols", 4)
    d = np.abs(np.diff(v, axis=0))[:, int(H * y0):int(H * y1), :]
    return [float(d[:, :, int(W * i / n):int(W * (i + 1) / n)].mean()) for i in range(n)]

def bg_change(f, t0=0.0):
    """(time, size) of the largest change of the top-left/top-right corners against the shot's first half second"""
    import numpy as np
    W, H = 36, 64
    b = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-i", str(f), "-vf", f"fps=8,scale={W}:{H},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True).stdout
    v = np.frombuffer(b[: len(b) // (W * H) * W * H], np.uint8).reshape(-1, H, W).astype(np.float32)
    if len(v) < 8: return None
    reg = np.concatenate([v[:, :20, :7].reshape(len(v), -1), v[:, :20, -7:].reshape(len(v), -1)], axis=1)
    d = np.abs(reg - reg[:4].mean(0)).mean(1)
    return round(t0 + int(d.argmax()) / 8, 2), float(d.max())

def qa_clip(F, f, text, kind="answer", win_from=0.0, prev_dur=None, has_child=False):
    """speech + picture QA of one take. win_from > 0 = an extension: only words after it are checked against text,
    plus the join with the parent part. has_child = an extension will continue this clip (its tail stays in the
    monologue, so trailing silence/noise matter)."""
    Q, T = F["qa"], F["timeline"]
    dur = probe_dur(f)
    segs, _ = whisper(F).transcribe(str(f), language=F.get("language", "en"), word_timestamps=True, vad_filter=True,
                                     condition_on_previous_text=False, beam_size=5)
    W = [(float(w.start), float(w.end), w.word.strip()) for sg in segs for w in sg.words]
    # a word belongs to the extension when it ENDS after the join (whisper often starts the first new word early)
    in_ext = lambda w: w[1] > win_from + 0.05
    inwin = [w for w in W if in_ext(w)]
    exp, got, got_t = norm_tokens(text), [], []
    for w in inwin:
        for tok in norm_tokens(w[2]): got.append(tok); got_t.append(w)
    ops = align(exp, got, Q["fuzzy"])
    errors, warns, content, minor, fuzzy = [], [], 0, 0, 0
    subs = 0
    for o, i, j in ops:
        if o == "ok": continue
        if o == "fz": fuzzy += 1; continue
        if o == "sub":
            msg = f"'{exp[i]}' heard '{got[j]}' @{got_t[j][0]:.1f}s"
            if exp[i] in CRITICAL or got[j] in CRITICAL: errors.append("wrong word " + msg); content += 1
            elif exp[i] in STOP: minor += 1
            else: content += 1; subs += 1; warns.append("check " + msg)
        elif o == "del":
            if exp[i] in STOP: minor += 1
            else: content += 1; errors.append(f"missing '{exp[i]}'")
        elif o == "ins":
            rep = (j > 0 and got[j] == got[j - 1]) or (j + 1 < len(got) and got[j] == got[j + 1])
            where = f"@{got_t[j][0]:.1f}s"
            if rep: errors.append(f"repeat '{got[j]}' {where}"); content += 1
            elif got[j] in ("uh", "um", "uhm", "hmm", "ah", "er", "mm"): errors.append(f"filler '{got[j]}' {where}"); content += 1
            elif got[j] in STOP: minor += 1
            else: content += 1; errors.append(f"extra '{got[j]}' {where}")
    # one misheard ordinary word is a warning (whisper can be wrong too), two or more fail
    if subs >= 2: errors.append(f"{subs} words differ from the script")
    if minor > Q["max_minor"]: errors.append(f"{minor} small-word differences")
    last_op = next((o for o, i, j in reversed(ops) if i is not None and i == len(exp) - 1), None) if exp else None
    if last_op in ("sub", "del"): errors.append(f"last word '{exp[-1]}' wrong or missing")

    # speech window over the whole clip (for the timeline): onset/offset from the audio inside the first/last word
    db, hop = energy(f)
    def onset(w0):
        if not len(db): return w0[0]
        a, b = max(0, int((w0[0] - 0.05) / hop)), min(len(db), int(w0[1] / hop) + 1)
        k0 = next((k for k in range(a, b) if db[k] > Q["speech_db"]), None)
        if k0 is None: return w0[0]
        k, quiet, lim = k0, 0, max(0, k0 - int(0.3 / hop))
        while k - 1 >= lim and quiet <= 3:
            k -= 1; quiet = 0 if db[k] > Q["noise_db"] else quiet + 1
        return (k + quiet) * hop
    def quiet_after(wl):
        """start of the first >=80 ms quiet run after the last word got going, or None (word cut by the clip end).
        Whisper stretches a word over a following breath, so the audio decides where the word ends."""
        if not len(db): return None
        quiet, k0 = 0, max(0, int((wl[0] + 0.03) / hop))
        for k in range(k0, len(db)):
            quiet = quiet + 1 if db[k] < Q["noise_db"] else 0
            if quiet >= 4: return (k - 3) * hop
        return None
    def offset(wl):
        q = quiet_after(wl)
        return q if q is not None else min(wl[1] + 0.1, dur)
    sp, extra = None, {}
    if W:
        s0 = min(max(onset(W[0]), W[0][0] - 0.3), W[0][1])
        e0 = min(max(offset(W[-1]), W[-1][0]), W[-1][1] + 0.4, dur)
        sp = [round(s0, 2), round(e0, 2)]
        # the last word must finish (a quiet gap after it) before the clip ends. No 'it is only a breath' excuse: an
        # unvoiced-frames test passed example's truncated 'exhausting' (its h/s/t are unvoiced) and
        if quiet_after(W[-1]) is None:
            errors.append(f"last word cut off by the clip end ({dur:.2f}s)")
        elif dur - sp[1] < 0.15: warns.append(f"only {dur - sp[1]:.2f}s after the last word")
        wsp = [w for w in W if in_ext(w)] or W
        # whisper cuts word edges short: the first/last word spans reach to the audio onset/offset
        spans = [(w[0] - 0.12, w[1] + 0.12) for w in W] + [(sp[0] - 0.05, W[0][1]), (W[-1][0], sp[1] + 0.1)]
        def loud_outside(t0, t1):
            if not len(db): return []
            return [k * hop for k in range(max(0, int(t0 / hop)), min(len(db), int(t1 / hop)))
                    if db[k] > Q["noise_db"] and not any(a <= k * hop <= b for a, b in spans)]
        pre = loud_outside(max(win_from, 0), (wsp[0][0] if win_from > 0 else min(wsp[0][0], sp[0])) - 0.12)
        post = loud_outside(max(W[-1][1] + 0.25, sp[1] + 0.15), dur)
        if len(pre) > 5: warns.append(f"sound before speech @{pre[0]:.1f}s")
        if len(post) > 5:
            (errors if has_child else warns).append(f"sound after speech @{post[0]:.1f}s" + ("" if has_child else " (trimmed on timeline)"))
        mid = []
        for (a0, a1, _), (b0, b1, _) in zip(wsp, wsp[1:]):
            if b0 - a1 > 0.45:
                h = loud_outside(a1 + 0.12, b0 - 0.12)
                if len(h) > 8: mid.append(h[0])
            if b0 - a1 > Q["long_pause"]: warns.append(f"long pause {b0-a1:.1f}s @{a1:.1f}s")
        # whisper word edges are too loose for this - only a hint; the verbatim layer decides (aligned words + voicing)
        if mid: warns.append("whisper: sound between words @" + ",".join(f"{t:.1f}s" for t in mid[:3]))
        if has_child:  # this tail plays inside the monologue: keep it short
            tail = dur - W[-1][1]; extra["tail_sil"] = round(tail, 2)
            if tail > Q["seam_tail_max"]: errors.append(f"{tail:.1f}s of silence before the extension")
        if win_from > 0:  # the join between the parent part and this extension
            prev = [w for w in W if not in_ext(w)]
            if prev:  # measured on the audio: end of the parent's last word -> onset of the first new word
                p_end, n_on = offset(prev[-1]), onset(wsp[0])
                gap = n_on - p_end; extra["join_gap"] = round(gap, 2)
                if gap > Q["seam_max"]: errors.append(f"dead air {gap:.1f}s at the join @{p_end:.1f}s")
                h = [k * hop for k in range(int((p_end + 0.1) / hop), int((n_on - 0.1) / hop)) if 0 <= k < len(db) and db[k] > Q["noise_db"]]
                if len(h) > 8: errors.append(f"sound at the join @{h[0]:.1f}s")
    else:
        errors.append("no speech recognised")
    # second, literal opinion on the same window (see verbatim)
    x = pcm16(f); t0 = max(0.0, win_from - 0.05)
    if kind == "question" and sp: t0 = max(t0, sp[0] - 0.3)  # the listening lead-in is never heard on the timeline
    ali = {}
    extra["verbatim"] = verbatim_checks(Q, x[int(t0 * 16000):], text, t0, errors, warns, ali)
    # whisper ends a word early when it trails off (example: "exhausting" ran 0.34 s past whisper's end and the
    # timeline cut it) - the speech window is the union with the literal forced alignment
    P = [p for p in ali.get("per") or [] if p and p[1] - p[0] >= 0.06 and p[2] >= Q["w2v_missing"]]
    if sp and P:
        # a sound fragment at the very start, then >= 0.5 s of silence before the first script word (Omni opens a clip
        # "mid-word" from a talking first frame): the speech starts at the first word, the timeline trims the fragment
        # by the audio itself: sound at 0, then >= 0.5 s below the noise floor, then the speech onset (the aligner may
        # glue the first short word onto the fragment, so it cannot decide this)
        lone, w1 = False, None
        if win_from == 0 and len(db) and max(db[:5]) > Q["speech_db"]:
            k = next(i for i in range(5) if db[i] > Q["speech_db"])  # skip the encoder's silent first frame
            while k < len(db) and db[k] >= Q["noise_db"]: k += 1
            run = 0
            while k < len(db) and db[k] < Q["noise_db"]: run += 1; k += 1
            if run * hop >= 0.5 and k < len(db) and k * hop < sp[1] - 0.5:
                lone, w1 = True, k * hop
        if lone:
            sp = [round(max(0.0, w1 - 0.05), 2), sp[1]]; extra["lead_fragment"] = True
            for e in [e for e in errors if e.startswith(("extra sound/word before", "clip starts mid-sound"))]:
                errors.remove(e); warns.append(e + " - separate from the speech, trimmed on the timeline")
        elif win_from == 0: sp = [round(min(sp[0], P[0][0]), 2), sp[1]]
        sp = [sp[0], round(max(sp[1], P[-1][1]), 2)]
        if not has_child and P[-1][1] > dur - 0.08: errors.append(f"last word runs into the clip end ({P[-1][1]:.2f}s of {dur:.2f}s)")
    # whisper drops short words (a sentence-initial "I" in example T1): when the literal transcript has the word and
    # the forced alignment finds it clearly, whisper's "missing" is only a warning
    for e in [e for e in errors if re.fullmatch(r"missing '([^']+)'", e)]:
        tok = e[9:-1]
        if tok in (ali.get("heard") or []) and any(tk[0] == tok and p and p[2] >= 0.9 and p[1] - p[0] >= 0.02
                                                   for tk, p in zip(ali.get("toks") or [], ali.get("per") or [])):
            errors.remove(e); warns.append(f"whisper missed '{tok}', literal ASR hears it")

    # picture: detectors first, then the scene filter (select drops frames)
    log = ff(["-i", str(f), "-vf", f"blackdetect=d=0.15:pix_th=0.08,freezedetect=n=-60dB:d=1.5,select='gt(scene,{Q['scene_th']})',showinfo", "-an", "-f", "null", "-"])
    cuts = [round(float(x), 2) for x in re.findall(r"pts_time:([\d.]+)", log)]
    shown_to = dur if (has_child or not sp) else min(dur, sp[1] + T["end_hold"] + 0.2)  # what the timeline can show
    in_view = lambda t: win_from - 0.2 <= t <= shown_to
    blacks = [float(x) for x in re.findall(r"black_start:([\d.]+)", log) if in_view(float(x))]
    freezes = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log) if in_view(float(x))]
    cw = [c for c in cuts if c >= win_from - 0.2]
    if kind == "intro":
        lo, hi = Q["intro_cut_rel"]
        ok_cuts = [c for c in cw if sp and sp[0] + lo <= c <= min(sp[0] + hi, sp[1] - 0.8)]
        if not ok_cuts: errors.append(f"no wide->close-up cut {lo}-{hi}s after speech starts (cuts: {cw or 'none'})")
        else: extra["cut"] = ok_cuts[0]
        extra_cuts = [c for c in cw if c not in ok_cuts[:1]]
        if extra_cuts: errors.append(f"extra cut @{extra_cuts}")
    elif cw:
        errors.append(f"scene cut/jump @{cw}")
    if blacks: errors.append(f"black frames @{blacks[0]:.1f}s")
    if kind == "intro" and "cut" in extra and Q.get("panel_min_motion"):
        pm = panel_motion(f, 0.05, extra["cut"] - 0.05, Q)
        extra["panel_motion"] = [round(x, 2) for x in pm]
        others = [x for i, x in enumerate(pm) if i != Q.get("panel_hero_col", 1)]
        if min(others) < Q["panel_min_motion"]:  # warn only: the wide part is replaced by a Kling wide-insert anyway
            warns.append(f"panelists barely move on the wide ({' '.join(f'{x:.1f}' for x in others)} < {Q['panel_min_motion']}) - frozen extras -> wide-insert (Kling)")
    if win_from == 0 and len(db) and max(db[:5]) > Q["speech_db"]:  # loud in the first 0.1 s = the clip opens mid-sound
        (warns if extra.get("lead_fragment") else errors).append(f"clip starts mid-sound ({max(db[:5]):.0f} dB in the first 0.1 s) - cropped word or breath")
    # the set behind the speaker must not morph (example: spectators materialised on the back wall mid-clip;
    # the scene filter misses a gradual swap). Top corners vs the first frames of the shot: good clips <= 3, close-up
    # with hair in the corners ~12, swaps 36-83
    bgc = bg_change(f, (extra["cut"] + 0.2) if "cut" in extra else 0.0)
    if bgc and bgc[1] > Q.get("bg_change", 20): errors.append(f"background changed @{bgc[0]:.1f}s (set swapped / people appeared, {bgc[1]:.0f})")
    elif bgc and bgc[1] > Q.get("bg_change_warn", 8): warns.append(f"background moved @{bgc[0]:.1f}s ({bgc[1]:.0f})")
    if freezes: warns.append(f"frozen picture @{freezes[0]:.1f}s")
    penalty = 0.0
    if sp and win_from == 0:  # the J-cut needs silence before the speech
        if kind == "question":
            if sp[0] < Q["question_lead_min"]: errors.append(f"asks too early ({sp[0]:.1f}s) - no listening part")
            elif sp[0] < Q["question_lead_ok"]: warns.append(f"short listening part ({sp[0]:.1f}s)")
            if sp[0] < T["gap"] + T["listen_overlap"]: penalty += Q["lead_penalty"]
            # the listening part the timeline SHOWS (over the expert's last words) must be silent: a murmur there
            # means visibly moving lips while someone else talks
            if len(db):
                a0 = max(0.0, sp[0] - T["gap"] - T["listen_overlap"] - 0.2)
                loud = [k * hop for k in range(int(a0 / hop), int(max(a0, sp[0] - 0.15) / hop)) if db[k] > Q["noise_db"]]
                if len(loud) > 5: errors.append(f"talks/murmurs during the listening shot @{loud[0]:.1f}s (visible lips over the other speaker)")
        elif kind == "answer" and sp[0] < T["gap"] + T["cut_lead"]:
            warns.append(f"short lead-in ({sp[0]:.2f}s) - the cut lands after the previous line"); penalty += Q["lead_penalty"]
    if prev_dur and dur < prev_dur + 2.5: errors.append(f"extension too short ({dur:.1f}s)")
    if "tail_sil" in extra: penalty += max(0.0, extra["tail_sil"] - 0.5) * 1.5
    if "join_gap" in extra: penalty += max(0.0, extra["join_gap"] - 0.5)
    if "cut" in extra and sp: penalty += abs(extra["cut"] - (sp[0] + 1.3)) * 0.3
    score = content + 0.3 * minor + 0.1 * fuzzy + 0.1 * len(warns) + penalty + (10 if errors else 0)
    return {"pass": not errors, "score": round(score, 2), "errors": errors, "warns": warns, "speech": sp,
            "dur": round(dur, 2), "cuts": cuts, "heard": " ".join(w[2] for w in inwin), "win_from": win_from, **extra}

def contact(f, out, t0, t1, n=6, w=200):
    from PIL import Image
    tmp = out.parent / "_frames"; tmp.mkdir(exist_ok=True)
    ims = []
    for k in range(n):
        t = t0 + (t1 - t0) * (k + 0.5) / n
        p = tmp / f"{out.stem}_{k}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(f), "-frames:v", "1", "-vf", f"scale={w}:-2", str(p)])
        if p.exists(): ims.append(Image.open(p).convert("RGB"))
    if not ims: return None
    sheet = Image.new("RGB", (sum(i.width for i in ims) + 4 * (len(ims) - 1), max(i.height for i in ims)), (20, 20, 20)); x = 0
    for i in ims: sheet.paste(i, (x, 0)); x += i.width + 4
    sheet.save(out, quality=85); return out

def motion_sheet(f, out, t0, t1, w=270):
    """first frame | heat map of movement (mean frame-to-frame change, 12 fps). Every person in frame must glow a little
    and differently: a dark person = frozen extra, identical blobs = people moving in unison"""
    import numpy as np
    from PIL import Image
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{max(0.5, t1 - t0):.2f}", "-i", str(f),
                        "-vf", f"fps=12,scale={w}:-2,format=gray", "-f", "rawvideo", "-"], capture_output=True)
    first = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-i", str(f), "-frames:v", "1",
                            "-vf", f"scale={w}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    h = len(first) // (w * 3)
    if not h or len(r.stdout) < w * h * 2: return None
    v = np.frombuffer(r.stdout[: len(r.stdout) // (w * h) * w * h], np.uint8).reshape(-1, h, w).astype(np.float32)
    # mean frame-to-frame change: a slow camera drift barely registers, a head turn or a hand does
    heat = np.clip(np.abs(np.diff(v, axis=0)).mean(axis=0) * 255 / 5, 0, 255).astype(np.uint8)
    img = Image.fromarray(np.frombuffer(first, np.uint8).reshape(h, w, 3))
    hm = Image.fromarray(np.stack([heat, (heat * 0.6).astype(np.uint8), np.zeros_like(heat)], -1))
    sheet = Image.new("RGB", (w * 2 + 4, h), (20, 20, 20)); sheet.paste(img, (0, 0)); sheet.paste(hm, (w + 4, 0))
    sheet.save(out, quality=85); return out

def cmd_wait(a):
    import requests
    st = load(a.id); F = fmt_of(st); refresh(st)
    t_end = time.time() + a.timeout
    before = {sid: s["chosen"] for sid, s in st["steps"].items()}
    pending = lambda: [(s, t) for s in st["steps"].values() for t in s["takes"] if t["status"] == "launched"]
    touched = set()
    while pending() and time.time() < t_end:
        for s, t in pending():
            try:
                if requests.head(t["url"], timeout=20).status_code != 200: continue
            except Exception:
                continue
            try:  # one bad file must not stop the others or the picking below
                f = take_files(st, t); download(t["url"], f)
                parent_dur = None
                if s["parent"]:
                    pj = (t.get("src") or {}).get("parent") or st["steps"][s["parent"]]["chosen"]
                    _, pt = find_take(st, pj)
                    parent_dur = pt["qa"]["dur"]
                has_child = any(x["parent"] == s["id"] for x in st["steps"].values())
                t["qa"] = qa_clip(F, f, s["text"], kind=s["kind"], win_from=parent_dur or 0.0, prev_dur=parent_dur, has_child=has_child)
                t["status"] = "done"; t["file"] = str(f)
            except Exception as e:
                t["status"] = "error"; t["reason"] = f"{type(e).__name__}: {e}"[:300]
            touched.add(s["id"]); save(st)
        if pending(): time.sleep(a.every)
    refresh(st); save(st)
    lines = []
    for sid, s in st["steps"].items():
        if s["chosen"] and s["chosen"] != before.get(sid):
            best = next(t for t in s["takes"] if t["job"] == s["chosen"]); q = best["qa"]
            contact(Path(best["file"]), pdir(st["id"]) / "qa" / f"{sid}_{best['job'][:8]}.jpg", q["win_from"], q["dur"])
            if st["tz"]["turns"][s["turn"]].get("crowd") or F.get("crowd"):  # look at it: everyone must move, independently
                motion_sheet(Path(best["file"]), pdir(st["id"]) / "qa" / f"{sid}_{best['job'][:8]}_motion.jpg",
                             q["win_from"], q.get("cut") or q["dur"])
            lines.append(f"PICK {sid} {best['job'][:8]} score={q['score']} speech={q['speech']} dur={q['dur']}"
                         + (f" warn: {'; '.join(q['warns'])}" if q["warns"] else ""))
        elif sid in touched and not s["chosen"] and not any(t["status"] == "launched" for t in s["takes"]):
            why = [f"{t['job'][:8]}: {'; '.join(t['qa']['errors'][:3])}" for t in s["takes"] if t["status"] == "done" and t.get("qa")]
            why += [f"{t['job'][:8]}: {t['reason']}" for t in s["takes"] if t["status"] == "error"]
            lines.append(f"FAIL {sid} ({s['launches']}/{F['max_launches']} launches) -> " + " | ".join(why))
    for s, t in pending():
        lines.append(f"PENDING {s['id']} {t['job']} not on CDN after {int(time.time()-t['t'])}s - check get_video_generation_job (failed? -> fail)")
    print("\n".join(lines) or "no new results")
    states = {sid: step_state(st, s) for sid, s in st["steps"].items()}
    if all(v == "done" for v in states.values()): print("ALL STEPS DONE -> run: timeline")
    elif any(v == "ready" for v in states.values()): print("NEXT -> run: gen")

def cmd_requa(a):
    """re-run QA on already downloaded takes (after a QA fix) - no new generations; then re-pick"""
    st = load(a.id); F = fmt_of(st); n = 0
    for s in st["steps"].values():
        if a.steps and s["id"] not in a.steps: continue
        if s["chosen"] and not a.steps: continue
        for t in s["takes"]:
            if t["status"] not in ("done", "error"): continue
            if not t.get("file") and take_files(st, t).exists(): t["file"] = str(take_files(st, t))  # QA crashed after download
            if not t.get("file") or not Path(t["file"]).exists(): continue
            pdur = None
            if s["parent"]:
                pj = (t.get("src") or {}).get("parent") or st["steps"][s["parent"]]["chosen"]
                pdur = find_take(st, pj)[1]["qa"]["dur"]
            has_child = any(x["parent"] == s["id"] for x in st["steps"].values())
            t["qa"] = qa_clip(F, Path(t["file"]), s["text"], kind=s["kind"], win_from=pdur or 0.0, prev_dur=pdur, has_child=has_child)
            t["status"] = "done"; n += 1
        if a.steps:  # re-pick; extensions are only dropped when a different take wins
            old = s["chosen"]; s["chosen"] = None; pick_best(st, s)
            if s["chosen"] != old: invalidate_children(st, s["id"])
    refresh(st); save(st)
    for s in st["steps"].values():
        if a.steps and s["id"] not in a.steps: continue
        t = next((x for x in s["takes"] if x["job"] == s["chosen"]), None)
        print(f"{s['id']}: " + (f"PICK {t['job'][:8]} score={t['qa']['score']} speech={t['qa']['speech']}" if t else
              "FAIL " + " | ".join(f"{x['job'][:8]}: {'; '.join(x['qa']['errors'][:3])}" for x in s["takes"] if x.get("qa"))))
    print(f"re-checked {n} take(s)")

def cmd_qa_file(a):
    st_fmt = json.loads((ROOT / "formats" / f"{a.format}.json").read_text(encoding="utf-8"))
    q = qa_clip(st_fmt, Path(a.file), a.text, kind=a.kind, win_from=a.frm)
    print(json.dumps(q, ensure_ascii=False, indent=1))

def cmd_sheet(a):
    from PIL import Image
    st = load(a.id); rows = []
    for s in st["steps"].values():
        if not s["chosen"]: continue
        t = next(x for x in s["takes"] if x["job"] == s["chosen"])
        out = pdir(a.id) / "qa" / f"{s['id']}_{t['job'][:8]}.jpg"
        if not out.exists(): contact(Path(t["file"]), out, t["qa"]["win_from"], t["qa"]["dur"])
        rows.append(Image.open(out))
    if not rows: sys.exit("no chosen takes")
    W = max(r.width for r in rows)
    sheet = Image.new("RGB", (W, sum(r.height for r in rows) + 4 * len(rows)), (20, 20, 20)); y = 0
    for r in rows: sheet.paste(r, (0, y)); y += r.height + 4
    out = pdir(a.id) / "qa" / "all_chosen.jpg"; sheet.save(out, quality=82); print(out)


# ---------------------------------------------------------------- timeline
def quiet_point(c, t0, t1):
    """the quietest 20 ms frame of clip c in [t0, t1] - audio edit points go there, never through a breath or a word"""
    if "_db" not in c: c["_db"] = energy(Path(c["take"]["file"]))[0]
    db = c["_db"]
    if t1 <= t0 or not len(db): return max(0.0, t0)
    a, b = max(0, int(t0 / 0.02)), min(len(db), int(t1 / 0.02) + 1)
    if b <= a: return max(0.0, t0)
    k = min(range(a, b), key=lambda i: max(db[max(0, i - 1):i + 2]))
    return round(k * 0.02 + 0.01, 3)

def speech_lufs(f, s, e):
    """integrated loudness (LUFS) of the speech window [s, e] of a clip"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{max(0, s):.3f}", "-to", f"{e:.3f}", "-i", str(f),
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None

def norm_copy(src, dst, s, e, target):
    """copy a clip with its audio gained so the speech hits `target` LUFS (true-peak limited); video stream copied"""
    L = speech_lufs(src, s, e)
    if L is None or L < -70: shutil.copyfile(src, dst); return 0.0
    g = round(max(-20.0, min(20.0, target - L)), 2)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                    "-af", f"volume={g}dB,alimiter=limit=0.89:level=false", "-c:a", "aac", "-b:a", "192k", str(dst)], check=True)
    return g

def cmd_timeline(a):
    st = load(a.id); F = fmt_of(st); T = F["timeline"]
    clips = []
    for turn in st["turns"]:
        last = st["steps"][turn["steps"][-1]]
        if not last["chosen"]: sys.exit(f"turn {turn['i']} not finished ({last['id']})")
        t = next(x for x in last["takes"] if x["job"] == last["chosen"])
        clips.append({"turn": turn, "take": t, "s": t["qa"]["speech"][0], "e": t["qa"]["speech"][1], "dur": t["qa"]["dur"],
                      "cut": st["steps"][turn["steps"][0]]["takes"] and next((x["qa"].get("cut") for x in st["steps"][turn["steps"][0]]["takes"]
                                                                              if x["job"] == st["steps"][turn["steps"][0]]["chosen"]), None)})
    mdir = EDITOR / "videos" / st["id"]; mdir.mkdir(parents=True, exist_ok=True)
    media, items, table, notes = [], [], [], []
    prev = None
    for i, c in enumerate(clips):
        name = f"t{i}_{c['turn']['who']}_{c['turn']['kind']}.mp4"
        # every clip is a separate generation with its own level: bring the speech of each to one loudness
        # - the editor/render volume is capped at 1, so it is baked in
        c["gain"] = norm_copy(c["take"]["file"], mdir / name, c["s"], c["e"], T.get("lufs", -16.0))
        media.append({"id": f"m{i}", "src": f"videos/{st['id']}/{name}", "name": name, "gain": c["gain"]})
        if prev is None:  # trim leading silence, but keep at least 0.8 s of the wide shot before the cut to the close-up
            vin = max(0.0, c["s"] - 0.15)
            if c.get("cut"): vin = max(0.0, min(vin, c["cut"] - 0.8))
            origin = -vin; cut = 0.0
        else:
            prev_e = prev["origin"] + prev["e"]
            origin = prev_e + T["gap"] - c["s"]
            want = T["listen_overlap"] if c["turn"]["kind"] == "question" else T["cut_lead"]
            lead = max(0.0, min(want, c["s"] - T["gap"]))
            if lead < want - 0.05:
                notes.append(f"WARN T{i}: lead-in {c['s']:.2f}s < {T['gap'] + want:.2f}s - the cut comes {want - lead:.2f}s later than planned")
            cut = max(prev_e - lead, prev["vstart"] + T["min_shot"])
            vin = max(0.0, cut - origin); cut = origin + vin
            prev_last = prev["origin"] + prev["dur"] - 0.04  # never cut past the previous clip's last frame
            if cut > prev_last:
                cut = prev_last; origin = min(origin, cut); vin = cut - origin
            prev["vout"] = cut - prev["origin"]
            prev["aout"] = quiet_point(prev, prev["e"] + 0.02, min(prev["dur"], prev["e"] + T["tail"] + 0.15))
        c.update(origin=origin, vstart=cut, vin=vin)
        c["ain"] = max(vin, quiet_point(c, max(vin, c["s"] - 0.3), max(vin, c["s"] - 0.02)))
        prev = c
    prev["vout"] = min(prev["dur"], prev["e"] + T["end_hold"])
    prev["aout"] = quiet_point(prev, prev["e"] + 0.02, min(prev["dur"], prev["e"] + T["tail"] + 0.35))
    vs = [round(c["vstart"], 3) for c in clips]  # seams are rounded once, so V1 has no 1 ms holes
    for i, c in enumerate(clips):
        link = f"L{i}"
        vin = round(c["vin"], 3)
        vout = round(vin + (vs[i + 1] - vs[i]), 3) if i + 1 < len(clips) else round(c["vout"], 3)
        ins = st["steps"][c["turn"]["steps"][0]].get("wide_insert") if i == 0 else None
        if ins and c.get("cut") and Path(ins["file"]).exists():
            # the wide part of the intro comes from a general video model (natural extras - Omni freezes or
            # choreographs them); the speaking close-up and ALL the audio stay from the Omni clip
            iname = f"t0_wide_insert{Path(ins['file']).suffix}"
            shutil.copyfile(ins["file"], mdir / iname)
            media.append({"id": "mw", "src": f"videos/{st['id']}/{iname}", "name": iname})
            iin = round(ins.get("in", 0.2), 3)
            # hold the wide for `show` s (the audio is Omni's anyway), never past the insert's end or Omni's own cut
            cut_at = round(max(c["origin"] + c["cut"], min(vs[i] + T.get("wide_show", 1.8), vs[i] + probe_dur(ins["file"]) - iin - 0.05)), 3)
            items.append({"kind": "video", "track": 0, "media": "mw", "start": vs[i], "in": iin, "out": round(iin + cut_at - vs[i], 3)})
            items.append({"kind": "video", "track": 0, "media": f"m{i}", "start": cut_at, "in": round(cut_at - c["origin"], 3), "out": vout, "link": link})
        else:
            items.append({"kind": "video", "track": 0, "media": f"m{i}", "start": vs[i], "in": vin, "out": vout, "link": link})
        items.append({"kind": "audio", "track": i % 2, "media": f"m{i}", "start": round(c["origin"] + c["ain"], 3), "in": round(c["ain"], 3), "out": round(c["aout"], 3), "fade": T.get("fade", 0.04), "link": link})
        nxt = clips[i + 1]["vstart"] if i + 1 < len(clips) else None
        ov = (c["origin"] + c["e"]) - nxt if nxt is not None else 0
        table.append(f"T{i} {c['turn']['who']:<7}{c['turn']['kind']:<9} video {c['vstart']:6.2f}-{c['origin']+c['vout']:6.2f}  "
                     f"speech {c['origin']+c['s']:6.2f}-{c['origin']+c['e']:6.2f}" + (f"  next shot {ov:+.2f}s before end of line" if nxt is not None else ""))
    proj = {"name": st["id"], "media": media, "tracks": {"v": 1, "a": 2}, "items": items}
    (EDITOR / "projects").mkdir(exist_ok=True)
    (EDITOR / "projects" / f"{st['id']}.json").write_text(json.dumps(proj, indent=1), encoding="utf-8")
    total = max(c["origin"] + c["vout"] for c in clips)
    print("\n".join(table + notes)); print(f"total {total:.2f}s")
    print(f"editor: http://localhost:8765/?project=projects/{st['id']}.json")
    if not getattr(a, "no_audit", False): cmd_audit(a)


def voiced_at(x, k, sr=16000):
    """is there confidently voiced (pitched - a word, not a breath or room tone) sound within +-60 ms of sample k;
    pyin's voiced_flag alone is not enough here: it marks quiet breath frames voiced at probability 0.01"""
    import librosa, numpy as np
    seg = x[max(0, k - 1920):k + 1920]
    if len(seg) < 1024: return False
    _, vf, vp = librosa.pyin(seg.astype(np.float32), fmin=70, fmax=500, sr=sr, frame_length=1024, hop_length=160)
    return int(np.sum(vf & (vp >= 0.5))) >= 3


def cmd_audit(a):
    """final gate on what the editor will PLAY: mix the timeline audio like the editor (hard cuts, tracks summed),
    run the verbatim checks per turn against the script, and check every audio edit point for cut-through sound"""
    import numpy as np, wave
    st = load(a.id); F = fmt_of(st); Q = F["qa"]
    proj = json.loads((EDITOR / "projects" / f"{st['id']}.json").read_text(encoding="utf-8"))
    media = {m["id"]: EDITOR / m["src"] for m in proj["media"]}
    pcm = {mid: pcm16(p) for mid, p in media.items()}
    items = [i for i in proj["items"] if i["kind"] == "audio"]
    total = max(i["start"] + i["out"] - i["in"] for i in items)
    mix = np.zeros(int(total * 16000) + 16000, dtype=np.float32)
    for i in items:
        seg = pcm[i["media"]][int(i["in"] * 16000):int(i["out"] * 16000)].copy()
        nf = min(int((i.get("fade") or 0) * 16000), len(seg) // 2)
        if nf: r = np.linspace(0, 1, nf, dtype=np.float32); seg[:nf] *= r; seg[-nf:] *= r[::-1]
        s0 = int(i["start"] * 16000); mix[s0:s0 + len(seg)] += seg
    out = pdir(st["id"]) / "qa" / "timeline_mix.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes())
    lvl = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9) if len(x) else -120
    bad = 0
    # loudness match between shots: speech loudness of every turn in the played files vs the median
    loud = [speech_lufs(media[i["media"]], i["in"], i["out"]) for i in items]
    ok_l = [x for x in loud if x is not None]
    med = sorted(ok_l)[len(ok_l) // 2] if ok_l else None
    mgain = {m["id"]: m.get("gain") for m in proj["media"]}
    for turn, i, Lt in zip(st["turns"], items, loud):
        errors, warns = [], []
        t0, t1 = i["start"], i["start"] + i["out"] - i["in"]
        if med is not None and Lt is not None and abs(Lt - med) > Q.get("loud_tol", 1.5):
            errors.append(f"loudness {Lt:.1f} LUFS vs median {med:.1f} (> {Q.get('loud_tol', 1.5)} LU) - rebuild timeline (normalizes)")
        # the words of a turn are checked on its OWN audio item (the mix slice also held the next turn's first sound
        # under this one's faded tail - "extra sound 't'"), with the noise floor raised by the
        # loudness gain baked into the copy (+9 dB lifted a word's fading tail over the fixed -42 dB floor -> "voiced
        # sound between 'calmer' and 'safer'", example); the edit-point checks below still look at the edges
        g = mgain.get(i["media"])
        if g is None:   # timelines built before the gain was stored: measure it against the picked take
            last = st["steps"][turn["steps"][-1]]
            tk = next((x for x in last["takes"] if x["job"] == last["chosen"]), None)
            La = Lb = None
            if tk and tk.get("file") and tk.get("qa", {}).get("speech"):
                s_, e_ = tk["qa"]["speech"]; La, Lb = speech_lufs(media[i["media"]], s_, e_), speech_lufs(tk["file"], s_, e_)
            g = (La - Lb) if La is not None and Lb is not None else 0.0
        own = pcm[i["media"]][int(i["in"] * 16000):int(i["out"] * 16000)].copy()
        nf = min(int((i.get("fade") or 0) * 16000), len(own) // 2)
        if nf: r = np.linspace(0, 1, nf, dtype=np.float32); own[:nf] *= r; own[-nf:] *= r[::-1]
        verbatim_checks(dict(Q, noise_db=Q["noise_db"] + max(0.0, g)), own, turn["text"], t0, errors, warns)
        src = pcm[i["media"]]
        for name, t in (("start", i["in"]), ("end", i["out"])):
            k = int(t * 16000)
            L = lvl(src[max(0, k - 320):k + 320])
            if L <= -40: continue
            at = t0 if name == "start" else t1
            if not i.get("fade") or L > -30 or voiced_at(src, k): errors.append(f"audio {name} cuts through sound @{at:.2f}s")
            else: warns.append(f"audio {name} fades through breath/room tone @{at:.2f}s")
        bad += bool(errors)
        print(f"T{turn['i']} {turn['who']:<7}{turn['kind']:<9} {t0:6.2f}-{t1:6.2f}s  " + ("OK" if not errors else "FAIL: " + "; ".join(errors))
              + (f"  (warn: {'; '.join(warns)})" if warns else ""))
    print(("AUDIT FAIL - reject the takes behind the failing turns, then gen/wait/timeline again" if bad else "AUDIT PASS") + f"  mix: {out}")

def cmd_asset_set(a):
    """register a frame made outside the img flow (given by the user, or made by hand in Kaleido) as an APPROVED asset"""
    st = load(a.id)
    A = st["assets"].setdefault(a.asset, {"cands": [], "approved": None, "tries": 0})
    A["cands"].append({"label": f"{a.asset} {len(A['cands']) + 1}", "job": a.job or f"external-{a.asset}-{len(A['cands']) + 1}",
                       "url": a.url, "external": True})
    A["approved"] = len(A["cands"]) - 1
    refresh(st); save(st); print(f"{a.asset} approved: {a.url}")

def cmd_wide_insert(a):
    """wide-insert <id> <job|url> [--in s]: the intro's wide part comes from this clip (e.g. Kling image2video of the
    approved wide frame, no audio); the timeline shows it until the intro's cut, Omni's close-up + audio after"""
    st = load(a.id); sid = st["turns"][0]["steps"][0]
    url = a.src if a.src.startswith("http") else f"https://<kaleido-cdn>/video-studio/{a.src}.mp4"
    f = pdir(st["id"]) / "clips" / f"{sid}_wide_{url.rsplit('/', 1)[-1][:8]}.mp4"; download(url, f)
    st["steps"][sid]["wide_insert"] = {"url": url, "file": str(f), "in": a.inn}
    save(st); print(f"{sid}: wide insert {f.name} (from {a.inn}s)")

def cmd_status(a):
    st = load(a.id)
    need = needed_assets(st)
    for k in need + [x for x in st["assets"] if x not in need]:
        A = st["assets"].get(k)
        print(f"asset {k:<9} " + ("-" if not A else f"{len(A['cands'])} cand, approved={A['cands'][A['approved']].get('label') if A.get('approved') is not None else 'no'}"))
    for s in st["steps"].values():
        tk = " ".join(f"{t['job'][:8]}:{t['status']}{'/ok' if t.get('qa',{}).get('pass') else ('/x' if t.get('qa') else '')}" for t in s["takes"])
        print(f"{s['id']:<6} {step_state(st, s):<8} {s['duration_s']}s launches={s['launches']} chosen={(s['chosen'] or '-')[:8]}  {tk}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("new"); x.add_argument("id"); x.add_argument("tz"); x.add_argument("--force", action="store_true"); x.add_argument("--female", action="append", default=[], choices=ROLES); x.set_defaults(f=cmd_new)
    x = sp.add_parser("resplit"); x.add_argument("id"); x.add_argument("step"); x.add_argument("--wps", type=float); x.set_defaults(f=cmd_resplit)
    x = sp.add_parser("replan"); x.add_argument("id"); x.add_argument("--force", action="store_true"); x.set_defaults(f=cmd_replan)
    x = sp.add_parser("plan"); x.add_argument("id"); x.set_defaults(f=cmd_plan)
    x = sp.add_parser("img"); x.add_argument("id"); x.add_argument("asset"); x.add_argument("--note"); x.add_argument("--from", dest="frm"); x.add_argument("--models"); x.add_argument("--redo"); x.set_defaults(f=cmd_img)
    x = sp.add_parser("img-add"); x.add_argument("id"); x.add_argument("asset"); x.add_argument("jobs", nargs="+"); x.add_argument("--timeout", type=int, default=240); x.set_defaults(f=cmd_img_add)
    x = sp.add_parser("img-sheet"); x.add_argument("id"); x.add_argument("assets", nargs="+"); x.add_argument("--all", action="store_true"); x.set_defaults(f=cmd_img_sheet)
    x = sp.add_parser("asset-set"); x.add_argument("id"); x.add_argument("asset"); x.add_argument("url"); x.add_argument("--job"); x.set_defaults(f=cmd_asset_set)
    x = sp.add_parser("approve"); x.add_argument("id"); x.add_argument("asset"); x.add_argument("n", nargs="?"); x.set_defaults(f=cmd_approve)
    x = sp.add_parser("gen"); x.add_argument("id"); x.set_defaults(f=cmd_gen)
    x = sp.add_parser("launched"); x.add_argument("id"); x.add_argument("step"); x.add_argument("jobs", nargs="+"); x.set_defaults(f=cmd_launched)
    x = sp.add_parser("wait"); x.add_argument("id"); x.add_argument("--timeout", type=int, default=900); x.add_argument("--every", type=int, default=10); x.set_defaults(f=cmd_wait)
    x = sp.add_parser("fail"); x.add_argument("id"); x.add_argument("job"); x.add_argument("reason", nargs="?"); x.set_defaults(f=cmd_fail)
    x = sp.add_parser("reject"); x.add_argument("id"); x.add_argument("job"); x.add_argument("reason", nargs="?"); x.set_defaults(f=cmd_reject)
    x = sp.add_parser("step"); x.add_argument("id"); x.add_argument("step"); x.add_argument("--note"); x.add_argument("--text"); x.add_argument("--shot"); x.add_argument("--reset", action="store_true"); x.set_defaults(f=cmd_step)
    x = sp.add_parser("pick"); x.add_argument("id"); x.add_argument("step"); x.add_argument("job"); x.set_defaults(f=cmd_pick)
    x = sp.add_parser("requa"); x.add_argument("id"); x.add_argument("steps", nargs="*"); x.set_defaults(f=cmd_requa)
    x = sp.add_parser("qa-file"); x.add_argument("file"); x.add_argument("text"); x.add_argument("--kind", default="answer"); x.add_argument("--from", dest="frm", type=float, default=0.0); x.add_argument("--format", default="podcast3"); x.set_defaults(f=cmd_qa_file)
    x = sp.add_parser("sheet"); x.add_argument("id"); x.set_defaults(f=cmd_sheet)
    x = sp.add_parser("timeline"); x.add_argument("id"); x.add_argument("--no-audit", action="store_true"); x.set_defaults(f=cmd_timeline)
    x = sp.add_parser("audit"); x.add_argument("id"); x.set_defaults(f=cmd_audit)
    x = sp.add_parser("wide-insert"); x.add_argument("id"); x.add_argument("src"); x.add_argument("--in", dest="inn", type=float, default=0.2); x.set_defaults(f=cmd_wide_insert)
    x = sp.add_parser("split-turn"); x.add_argument("id"); x.add_argument("step"); x.add_argument("shot"); x.add_argument("--wps", type=float); x.set_defaults(f=cmd_split_turn)
    x = sp.add_parser("split-head"); x.add_argument("id"); x.add_argument("--turn", type=int, default=0); x.add_argument("--wide", default="angle_3_wide"); x.add_argument("--close", default="angle_4_closeup"); x.set_defaults(f=cmd_split_head)
    x = sp.add_parser("status"); x.add_argument("id"); x.set_defaults(f=cmd_status)
    a = p.parse_args(); a.f(a)

if __name__ == "__main__":
    main()
