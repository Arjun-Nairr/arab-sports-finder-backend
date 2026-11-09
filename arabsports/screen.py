"""T03: eligibility gate, source-linked evidence packets, local base Laya screening, deterministic selection.

Eligibility (sporting person + supported representation) is decided by explicit rules on structured/source
evidence, separately from Laya's narrative screening. Laya only answers the rubric questions below."""
import hashlib
import json
import os
import re

from .matching import norm

LAYA_REPO = "convaiinnovations/laya"
LAYA_REVISION = "7b928d828b7b0e022f929d9bd2e44165aa270148"  # English base checkpoint (repo root), Apache-2.0
LAYA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "laya-7b928d82")
LAYA_RUNTIME = "laya==0.3.20 (CPU, torch 2.15.0+cpu)"
MAX_LEN, HEAD_MAX_LEN = 512, 192          # checkpoint defaults, not changed
STATE_TOKENS = 280                        # per window, under the 320-token state budget, measured with Laya's tokenizer
MAX_WINDOWS = 6                           # per candidate; any excess is reported, never silently dropped
BIO_MAX_WINDOWS = 6                       # per biography, chosen across the whole page (see Windows.build_bio)
BIO_RULE = ("biography: prose packed into windows per section; if there are more sections than BIO_MAX_WINDOWS, the "
            "first window of evenly spaced sections (first to last) is screened, otherwise windows are taken round-robin "
            "across sections; every window not screened is counted by section.")
BIO_UNCERTAINTY = ("Wikipedia statement (tertiary, openly edited) in the paragraph shown; its cited references were not "
                   "retrieved or verified. Same Wikimedia ecosystem as Wikidata: agreement with Wikidata is not "
                   "independent corroboration.")

RUBRIC_VERSION = "r1"
FOOTBALL = "Q2736"  # demo scope: association football (Wikidata P641 sport), checked in eligibility, not by Laya
# Two-option/three-option `choice` questions with neutral keys (the card warns `noul` can follow its labels).
RUBRIC = {
    "narrative": {"type": "choice",
                  "instructions": "What does this passage tell us about the named athlete?",
                  "criteria": {"A": "a specific personal story: a setback, comeback, milestone, contribution, "
                                    "community role or identity moment",
                               "B": "routine sporting information: a match result, transfer, schedule or statistics",
                               "C": "too little information about the athlete to tell"}},
    "focus": {"type": "choice",
              "instructions": "How central is the named athlete to this passage?",
              "criteria": {"A": "the passage is mainly about this athlete",
                           "B": "the athlete is only mentioned in passing"}},
}
# Transparent mapping from typed answers to the three domain outcomes.
MAPPING_DOC = ("window: narrative=A and focus=A -> promising; narrative=C -> insufficient; otherwise routine. "
               "candidate: promising if any window is promising, insufficient if every window is insufficient, "
               "else routine. score = highest raw P(narrative=A) among promising windows (uncalibrated, not an "
               "approval probability).")
ORDERING_DOC = ("eligible promising candidates sorted by score desc, then distinct supporting articles desc, "
                "then athlete id asc (stable tie-break).")

COMPETITOR = re.compile(r"player|athlete|competitor|jumper|runner|sprinter|swimmer|cyclist|racer|fencer|boxer|"
                        r"wrestler|weightlifter|judoka|gymnast|archer|shooter|rower|skier|taekwondo|karateka|"
                        r"tennis|chess|thrower|hurdler|marathon|driver|rider|jockey|sailor|diver|lifter|"
                        r"badminton|basketball|footballer|cricketer|golfer|squash|triathlete|canoeist", re.I)
OFFICIAL = re.compile(r"referee|umpire|official|coach|manager|executive|administrator|commentator|journalist|"
                      r"politician|businessperson", re.I)


# Observed on this runtime: each question is encoded as its own <=512-token sequence (state + question text) and
# usage.input_tokens is the sum over questions. Truncation shows as reported < sum(state + per-question overhead).
TRUNCATION_CHECK = "v2-per-question"


def config_fingerprint():
    cfg = {"repo": LAYA_REPO, "revision": LAYA_REVISION, "runtime": LAYA_RUNTIME, "max_len": MAX_LEN,
           "head_max_len": HEAD_MAX_LEN, "state_tokens": STATE_TOKENS, "rubric_version": RUBRIC_VERSION,
           "rubric": RUBRIC, "truncation_check": TRUNCATION_CHECK}
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


# ---------- eligibility (rules, not Laya) ----------

def sporting_role(roles):
    occ = (roles or {}).get("occupations") or []
    if not roles:
        return "unknown", "no occupation data fetched"
    if any(COMPETITOR.search(o) for o in occ):
        return "competitor", "P106: " + ", ".join(occ)
    if any(OFFICIAL.search(o) for o in occ):
        return "official_or_non_competitor", "P106: " + ", ".join(occ)
    return "unknown", "P106: " + (", ".join(occ) or "none")


def _sourced(refs):
    return any(r.get("stated_in") or r.get("urls") for r in refs)


def representation_support(athlete, roles, articles):
    """Supported representation of a listed nation, by route. Returns (status, [evidence])."""
    ev = []
    for r in athlete["representation"]:
        if r.get("listed") and r.get("rank") != "deprecated" and _sourced(r.get("references", [])):
            ev.append({"route": "wikidata_P1532_sourced", "nation": r["nation"], "references": r["references"]})
    for t in (roles or {}).get("national_teams", []):
        if t.get("listed") and _sourced(t.get("references", [])):
            ev.append({"route": "wikidata_P54_national_team_sourced", "team": t["label"], "references": t["references"]})
    names = {norm(athlete["name"]), *(norm(a) for a in athlete["aliases"])}
    surname = norm(athlete["name"]).split()[-1] if athlete["name"] else ""
    nations = {r["nation"] for r in athlete["representation"] if r.get("listed")}
    for a in articles:
        bio = a.get("provider") == "wikipedia"
        extra = a.get("extra") or {}
        units = ([(sec["heading"], p["text"], p["cites"]) for sec in extra["sections"] for p in sec["paragraphs"]]
                 if bio else [(None, a["content"] or "", [])])
        for heading, text, cites in units:
            for sent in re.split(r"(?<=[.!?])\s+", text):
                s = norm(sent)
                if not (any(f" {n} " in f" {s} " for n in names) or (surname and f" {surname} " in f" {s} ")):
                    continue
                for nat in nations:
                    if not re.search(rf"\b{norm(nat)} (?:men s |women s )?national (?:\w+ )?team\b|\brepresent\w* {norm(nat)}\b", s):
                        continue
                    if bio:
                        ev.append({"route": "wikipedia_biography", "nation": nat, "page": a["canonical_url"],
                                   "revision_url": extra["revision"]["url"], "section": heading, "passage": text[:600],
                                   "sentence": sent[:300], "uncertainty": BIO_UNCERTAINTY, "ecosystem": "wikimedia",
                                   "cited_references": [extra["citations"][c] for c in cites if c in extra["citations"]][:5]})
                    else:
                        ev.append({"route": "news_report", "nation": nat, "url": a["canonical_url"], "sentence": sent[:300]})
    return ("supported" if ev else "unsupported"), ev


# ---------- packets ----------

class Windows:
    def __init__(self):
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(os.path.join(LAYA_DIR, "tokenizer", "tokenizer.json"))

    def count(self, text):
        return len(self.tok.encode(text).ids)

    def build(self, athlete, articles):
        """Paragraphs that mention the athlete, packed into windows within STATE_TOKENS. A single paragraph
        that is too long is split at sentence boundaries; nothing relevant is cut off silently."""
        names = {norm(athlete["name"]), *(norm(a) for a in athlete["aliases"])}
        surname = norm(athlete["name"]).split()[-1]
        header = f"Athlete: {athlete['name']}\nPassage: "
        budget = STATE_TOKENS - self.count(header)
        windows = []
        for a in articles:
            paras = [p.strip() for p in re.split(r"\n\s*\n", a["content"] or "") if p.strip()]
            rel = [p for p in paras if any(f" {n} " in f" {norm(p)} " for n in names) or f" {surname} " in f" {norm(p)} "]
            if a.get("title"):
                rel = [a["title"] + "."] + rel
            windows += [self._win(a, t, header) for t in self._pack(rel, budget)]
        return windows

    def _pack(self, paras, budget):
        pieces, out, cur = [], [], ""
        for p in paras:
            pieces += [p] if self.count(p) <= budget else [s for s in re.split(r"(?<=[.!?])\s+", p) if s]
        for piece in pieces:
            cand = (cur + " " + piece).strip()
            if cur and self.count(cand) > budget:
                out.append(cur)
                cur = piece
            else:
                cur = cand
        return out + [cur] if cur else out

    def build_bio(self, athlete, a):
        """An entity-verified biography is about the athlete, so every prose paragraph is relevant. Returns
        (screened windows in page order, reduction record). See BIO_RULE: never just the start of the page."""
        per = []
        for sec in bio_sections(a["content"]):
            header = f"Athlete: {athlete['name']}\nSection: {sec['heading']}\nPassage: "
            per.append((sec["heading"], header, self._pack(sec["paras"], STATE_TOKENS - self.count(header))))
        firsts = [(i, 0) for i, (_, _, ts) in enumerate(per) if ts]
        k = BIO_MAX_WINDOWS
        if len(firsts) > k:
            chosen = [firsts[round(j * (len(firsts) - 1) / (k - 1))] for j in range(k)]
        else:
            depth = max((len(ts) for _, _, ts in per), default=0)
            chosen = [(i, w) for w in range(depth) for i, (_, _, ts) in enumerate(per) if w < len(ts)][:k]
        chosen = sorted(chosen)
        windows = [{**self._win(a, per[i][2][w], per[i][1]), "kind": "biography", "section": per[i][0]} for i, w in chosen]
        left = {}
        for i, (heading, _, ts) in enumerate(per):
            n = sum(1 for w in range(len(ts)) if (i, w) not in chosen)
            if n:
                left[heading] = n
        return windows, {"article_id": a["id"], "sections": len(per), "windows_total": sum(len(ts) for _, _, ts in per),
                         "windows_screened": len(windows), "not_screened_by_section": left, "rule": BIO_RULE}

    def _win(self, a, text, header):
        return {"article_id": a["id"], "url": a["canonical_url"], "publisher": a["publisher"],
                "usage_flags": (a.get("usage_terms") or {}).get("flags", {}), "text": header + text,
                "state_tokens": self.count(header + text)}


def bio_sections(content):
    """Reads back wikipedia.content_text: '== Heading ==' blocks followed by paragraphs."""
    secs = []
    for block in re.split(r"\n\s*\n", content or ""):
        m = re.fullmatch(r"== (.+) ==", block.strip())
        if m:
            secs.append({"heading": m.group(1), "paras": []})
        elif secs and block.strip():
            secs[-1]["paras"].append(block.strip())
    return secs


def packet_for(athlete, articles, windows_builder):
    """What Laya reads, plus provenance. The fingerprint covers exactly the screened input (athlete, evidence
    version/fingerprint, window texts), so unchanged evidence reuses its screening. News windows are built as
    before; biography windows (provider wikipedia) follow them, with their reduction record."""
    windows = windows_builder.build(athlete, [a for a in articles if a.get("provider") != "wikipedia"])
    content = {
        "athlete_id": athlete["id"], "name": athlete["name"], "source": f"{athlete['source']}:{athlete['source_id']}",
        "sport": [s["label"] for s in athlete["sports"]], "evidence_version": athlete["evidence_version"],
        "evidence_fingerprint": athlete["evidence_fingerprint"],
        "sources": [{"article_id": a["id"], "url": a["canonical_url"], "publisher": a["publisher"],
                     "content_kind": a["content_kind"], "usage_flags": (a.get("usage_terms") or {}).get("flags", {}),
                     **({"kind": "biography", "revision": a["extra"]["revision"], "license": a["extra"]["license"]["text"]}
                        if a.get("provider") == "wikipedia" else {})}
                    for a in articles],
        "windows": windows[:MAX_WINDOWS], "windows_not_screened": max(0, len(windows) - MAX_WINDOWS),
        "gaps": list(athlete["gaps"]),
    }
    bios = [a for a in articles if a.get("provider") == "wikipedia"]
    if bios:
        content["biography_reduction"] = []
        for b in bios:
            w, red = windows_builder.build_bio(athlete, b)
            content["windows"] += w
            content["biography_reduction"].append(red)
    key = {k: content[k] for k in ("athlete_id", "evidence_version", "evidence_fingerprint", "windows", "windows_not_screened",
                                   "biography_reduction") if k in content}
    content["packet_fp"] = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    return content


def eligibility(athlete, roles, articles):
    """Sporting-person and representation checks, independent of narrative screening."""
    rep_status, rep_ev = representation_support(athlete, roles, articles)
    role, role_basis = sporting_role(roles)
    reasons = []
    if athlete["resolution"] != "source_identity":
        reasons.append(f"identity {athlete['resolution']} (news-only lead, no structured identity)")
    if role != "competitor":
        reasons.append(f"sporting role {role} ({role_basis})")
    if rep_status != "supported":
        reasons.append("no sourced statement or report of representing a listed nation")
    sports = athlete.get("sports") or []
    if not any(x.get("qid") == FOOTBALL for x in sports):
        reasons.append(f"outside demo scope: Wikidata sport is not association football ({FOOTBALL}); recorded: "
                       f"{', '.join(x.get('label') or x.get('qid') or '?' for x in sports) or 'none'}")
    routes = sorted({e["route"] for e in rep_ev})
    return {"eligible": not reasons, "reasons": reasons, "representation": rep_status,
            "representation_evidence": rep_ev, "representation_routes": routes,
            # Wikidata and Wikipedia are one ecosystem: their agreement is never counted as independent corroboration.
            "independent_of_wikimedia": [r for r in routes if r == "news_report"], "sporting_role": role, "role_basis": role_basis,
            "wikidata_eligibility": athlete["eligibility"]}


# ---------- Laya ----------

_agent = [None, None]


def laya_agent():
    """Load the pinned local snapshot once, and measure each rubric question's token overhead with a tiny probe."""
    if _agent[0] is None:
        os.environ.setdefault("USE_TF", "0")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import laya
        agent = laya.load(LAYA_DIR, device="cpu")
        probe = "Athlete: X\nPassage: probe"
        n = Windows().count(probe)
        _agent[1] = {q: agent.predict(probe, {q: spec})["usage"]["input_tokens"] - n for q, spec in RUBRIC.items()}
        _agent[0] = agent
    return _agent[0]


def expected_tokens(state_tokens):
    return sum(min(MAX_LEN, state_tokens + o) for o in _agent[1].values())


def screen_packet(packet):
    """Run actual Laya on every window. Returns (raw outputs, mapped outcome, score, warnings)."""
    agent, raw, warnings = laya_agent(), [], []
    for i, w in enumerate(packet["windows"]):
        r = agent.predict(w["text"], RUBRIC)
        used, want = r.get("usage", {}).get("input_tokens"), sum(w["state_tokens"] + o for o in _agent[1].values())
        if used is None or used < want:
            warnings.append(f"window {i}: Laya read {used} of {want} expected tokens (truncated)")
        raw.append({"window": i, "article_id": w["article_id"], **({"section": w["section"]} if "section" in w else {}),
                    "state_tokens": w["state_tokens"],
                    "input_tokens": used, "expected_tokens": want, "question_overhead": _agent[1],
                    "answers": r["answers"]})
    if packet["windows_not_screened"]:
        warnings.append(f"{packet['windows_not_screened']} window(s) beyond MAX_WINDOWS={MAX_WINDOWS} not screened")
    for red in packet.get("biography_reduction", []):
        if red["windows_screened"] < red["windows_total"]:
            warnings.append(f"biography {red['article_id']}: {red['windows_screened']} of {red['windows_total']} windows "
                            f"screened across {red['sections']} sections (BIO_RULE); rest listed in packet")
    mapped, score = map_outputs(raw)
    return raw, mapped, score, warnings


def map_window(answers):
    n, f = answers["narrative"]["choice"], answers["focus"]["choice"]
    return "promising" if (n, f) == ("A", "A") else "insufficient" if n == "C" else "routine"


def map_outputs(raw):
    if not raw:
        return "insufficient", None
    per = [(map_window(r["answers"]), r["answers"]["narrative"]["probabilities"]["A"]) for r in raw]
    prom = [p for m, p in per if m == "promising"]
    if prom:
        return "promising", max(prom)
    return ("insufficient" if all(m == "insufficient" for m, _ in per) else "routine"), None


def order_candidates(cands):
    """cands: dicts with eligible, mapped, score, n_articles, athlete_id. Returns (ordered, selected or None)."""
    ok = [c for c in cands if c["eligible"] and c["mapped"] == "promising"]
    ok.sort(key=lambda c: (-c["score"], -c["n_articles"], c["athlete_id"]))
    return ok, (ok[0] if ok else None)
