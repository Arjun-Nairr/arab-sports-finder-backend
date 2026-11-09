"""Mention extraction and conservative identity resolution (structured/text methods, no model)."""
import hashlib
import json
import re
import unicodedata

from .config import NATIONS

SPORT_WORDS = {"association football": ["football", "soccer"], "athletics": ["athletics", "track"]}
ROLE_CUE = re.compile(
    r"\b(?:striker|forward|winger|midfielder|defender|goalkeeper|keeper|captain|player|athlete|champion|"
    r"runner|sprinter|swimmer|boxer|fencer|wrestler|weightlifter|judoka|cyclist|driver|rider|shooter|"
    r"archer|gymnast|jumper|thrower|international|star)\s+"
    r"((?:[A-Z][\w'’]+|(?:al|el|bin|bint|ibn|Al|El)-?[A-Z]?[\w'’]*)(?:[ -](?:[A-Z][\w'’]+|al-[A-Z][\w'’]+|bin|bint|ibn)){1,3})")
NOT_PERSON = re.compile(r"\b(?:League|Cup|Club|FC|United|City|Division|Championship|Federation|Stadium|Team|"
                        r"Saudi|Arabia|Pro|Elite|Champions|Round|Sports)\b")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’'`]", "", s)
    return re.sub(r"[^\w]+", " ", s).strip()


def content_hash(text):
    return hashlib.sha256(norm(text).encode()).hexdigest()


def contains(haystack_norm, needle_norm):
    return bool(needle_norm) and f" {needle_norm} " in f" {haystack_norm} "


def context_terms(athlete):
    """Words in an article that support this identity: represented nation names/demonyms and sport words."""
    nation = set()
    for r in athlete["representation"]:
        if r.get("listed") and r.get("nation_qid") in NATIONS:
            name, demonyms = NATIONS[r["nation_qid"]]
            nation |= {norm(name), *demonyms}
        elif r.get("nation"):
            nation.add(norm(r["nation"]))
    sport = set()
    for s in athlete["sports"]:
        label = (s.get("label") or "").lower()
        sport |= set(SPORT_WORDS.get(label, [label])) if label else set()
    return {norm(x) for x in nation if x}, {norm(x) for x in sport if x}


ROLE_CUE_BEFORE = re.compile(r"\b(analyst|pundit|commentator|coach|manager|referee|official|president|chairman|"
                             r"executive|journalist|presenter|director|agent)\s+(?:[A-Z][\w'’-]*\s+){0,2}$", re.I)


def role_conflict(text, name, occupations):
    """A non-competitor role word introducing the name ("sports analyst Ahmed Salah") that the athlete's known
    occupations do not confirm. Unknown occupations count as unconfirmed (conservative)."""
    occ = " ".join(occupations or []).lower()
    for m in re.finditer(re.escape(name), text or "", re.I):
        cue = ROLE_CUE_BEFORE.search(text[max(0, m.start() - 60):m.start()])
        if cue and cue.group(1).lower() not in occ:
            return cue.group(1).lower()
    return None


def name_index(athletes):
    """norm name/alias -> athlete ids, for structured identities only (callers pass source_identity rows;
    provisional/reconciled news leads are never competing match targets).
    Single-token names are skipped: too ambiguous to match in text."""
    idx = {}
    for a in athletes:
        for n in {a["norm_name"], *(norm(x) for x in a["aliases"])}:
            if len(n.split()) >= 2:
                idx.setdefault(n, set()).add(a["id"])
    return idx


def extract_cued_names(text):
    # ponytail: role-word regex finds only names introduced by a cue ("striker X Y"); a grouped
    # model extraction pass would raise recall if news-only discovery proves too thin.
    out = []
    for m in ROLE_CUE.finditer(text or ""):
        name = m.group(1).strip(" -")
        if not NOT_PERSON.search(name) and len(name.split()) >= 2:
            out.append((name, _sentence(text, m.start())))
    return out


def _sentence(text, pos):
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    end = min([i for i in (text.find(".", pos), text.find("\n", pos)) if i != -1] or [len(text)])
    return text[start:end].strip()


def resolve_article(article, athletes_by_id, idx):
    """Return mention dicts for one article. Linking requires a single candidate plus nation/sport context."""
    text = " ".join(filter(None, [article.get("title"), article.get("content")]))
    tnorm = norm(text)
    mentions, linked = {}, set()
    for n, ids in sorted(idx.items()):
        if not contains(tnorm, n):
            continue
        ev = {"matched_name": n}
        if len(ids) > 1:
            mentions[n] = {"surface": n, "status": "ambiguous", "athlete_id": None,
                           "candidates": sorted(ids), "evidence": ev, "method": "library_name_match"}
            continue
        (aid,) = ids
        ath = athletes_by_id[aid]
        nation, sport = context_terms(ath)
        ev["nation_context"] = sorted(t for t in nation if contains(tnorm, t))
        ev["sport_context"] = sorted(t for t in sport if contains(tnorm, t))
        conflict = role_conflict(text, ath["name"], ath.get("occupations"))
        if conflict:
            ev["role_conflict"] = conflict
        ok = (ev["nation_context"] or ev["sport_context"]) and aid not in linked and not conflict
        status = "linked" if ok else "role_conflict" if conflict else "no_context"
        mentions[n] = {"surface": n, "status": status, "athlete_id": aid if ok else None,
                       "candidates": [aid], "evidence": ev, "method": "library_name_match"}
        if ok:
            linked.add(aid)
    for surface, sentence in extract_cued_names(article.get("content") or ""):
        n = norm(surface)
        if n in mentions or any(contains(n, k) or contains(k, n) for k in mentions):
            continue
        snorm = norm(sentence)
        nation_ctx = sorted(q for q, (name, dem) in NATIONS.items()
                            if contains(snorm, norm(name)) or any(contains(snorm, d) for d in dem))
        mentions[n] = {"surface": surface, "status": "provisional", "athlete_id": None, "candidates": [],
                       "evidence": {"sentence": sentence[:300], "nation_context": nation_ctx},
                       "method": "role_cue_regex"}
    return list(mentions.values())


def _rep_key(r):
    refs = sorted(json.dumps({k: sorted(v) for k, v in ref.items()}, sort_keys=True) for ref in r.get("references", []))
    return json.dumps({"nation": r.get("nation_qid"), "rank": r.get("rank"), "start": r.get("start"),
                       "end": r.get("end"), "refs": refs}, sort_keys=True)


def evidence_fingerprint(representation, article_hashes):
    """Normalized representation claims (value, rank, dates, reference contents) plus distinct article
    content hashes. Order and duplicate copies do not matter; substantive changes do."""
    parts = sorted(_rep_key(r) for r in representation) + sorted(set(article_hashes))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def match_fingerprint(athlete):
    """Everything resolve_article uses to link this athlete: names/aliases plus nation and sport context
    terms. Sorted sets, so reordered but equivalent inputs give the same value."""
    nation, sport = context_terms(athlete)
    names = {norm(athlete["name"]), *(norm(a) for a in athlete["aliases"])}
    parts = ["n:" + x for x in sorted(names)] + ["c:" + x for x in sorted(nation)] + ["s:" + x for x in sorted(sport)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def search_text(title, content):
    """Space-padded normalized text so stored articles can be found with LIKE '% name %'."""
    return f" {norm(' '.join(filter(None, [title, content])))} "
