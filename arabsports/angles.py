"""Automated angle search: for each ranked player, one Groq call over relevant stored passages proposes the stakes,
turning point and significance, each citing numbered excerpts (code attaches their exact text), plus facts tied
to passage ids (a valid excerpt id in a fact resolves to its passage).
Code checks every reference against the stored text; nothing is accepted on the model's word."""
import re

from . import compose
from .store import ANGLE_FIELDS, ReviewError, check_reviewed_facts

PROMPT_VERSION = "a2"   # a2: stakes/turning point/significance cite excerpt ids; code attaches the text
MAX_PLAYERS = 5
RESERVE = 4               # calls kept for compose, verify, repair and re-verify of the chosen story
PASSAGE_CHARS = 9000      # ~3000 input tokens, so one angle request fits the 8000 tokens/minute account
MAX_PASSAGE = 2000
# Story cues used only to order paragraphs (relevance), never to judge a story.
CUE = re.compile(r"injur|return|comeback|recover|debut|first|qualif|world cup|final|captain|champion|title|record|"
                 r"penalt|winner|decisive|refugee|\bwar\b|conflict|died|death|illness|overc|setback|banned|exile|"
                 r"award|histor|absence|retire|dedicat|charit|donat|communit|academy|crisis|sanction", re.I)


def select_passages(name, rows):
    """Paragraphs of the player's stored sources (republication not refused), ordered by story cues plus mentions
    of the player, kept best-first within PASSAGE_CHARS and shown in document order. Deterministic."""
    surname = name.split()[-1].lower()
    found = []
    for r in rows:
        if ((r.get("usage_terms") or {}).get("flags") or {}).get("republication_allowed") is False:
            continue
        for k, para in enumerate(p.strip() for p in (r["content"] or "").split("\n\n")):
            if not 80 <= len(para) <= MAX_PASSAGE:
                continue
            score = len(CUE.findall(para)) + (2 if surname in para.lower() else 0)
            if score:
                found.append((-score, r["id"], k, para, r))
    chosen, used = [], 0
    for c in sorted(found, key=lambda c: c[:3]):
        if used + len(c[3]) <= PASSAGE_CHARS:
            chosen.append(c)
            used += len(c[3])
    chosen.sort(key=lambda c: (c[1], c[2]))
    return [{"id": f"P{i}", "article_id": r["id"], "url": r["canonical_url"], "publisher": r["publisher"],
             "content_hash": r["content_hash"], "paragraph": k, "passage": para}
            for i, (_, _, k, para, r) in enumerate(chosen, 1)], len(found)


def excerpts(passages):
    """Numbered excerpts: each stored passage split into sentences, id 'P<passage>.<n>'. Deterministic, so the ids the
    model saw resolve to the same exact stored text in validate()."""
    out = {}
    for p in passages:
        for n, sent in enumerate((x for x in re.split(r"(?<=[.!?])\s+", p["passage"].strip()) if x.strip()), 1):
            out[f"{p['id']}.{n}"] = (p["id"], sent)
    return out


def ask(name, nations, passages):
    ex = excerpts(passages)
    block = "\n\n".join(f"[{p['id']}] ({p['publisher']})\n" + " ".join(
        f"[{i}] {t}" for i, (pid, t) in ex.items() if pid == p["id"]) for p in passages)
    return [{"role": "system", "content": compose.SYSTEM}, {"role": "user", "content": (
        f"Player: {name} (football; represents {', '.join(nations) or 'unknown'})\n"
        "Stored passages, copied verbatim from the player's sources and split into numbered excerpts. They are the "
        "only evidence you may use:\n\n"
        f"{block}\n\n"
        f"Decide whether these passages support ONE specific story about {name}: a straightforward, sourced account "
        "of a documented achievement is enough. It needs:\n"
        "- stakes: what was at risk or wanted, for the player or the team;\n"
        "- turning_point: what changed, or what the player did;\n"
        "- significance: why it mattered.\n"
        "A match recap, transfer update, list of statistics or general career summary is routine: answer "
        '"insufficient". Use no outside knowledge. Do not invent feelings, quotes, causes or claims of being first.\n'
        "Each of stakes, turning_point and significance cites 1-2 excerpt ids (such as \"P1.2\") whose text shows "
        "it. Give the ids only; do not copy the excerpt text.\n"
        "Give 3-6 facts: short factual sentences in your own words, each citing passages (such as \"P1\") from ONE "
        "source, using only names, numbers and words that the cited passages contain. Every passage whose excerpt is "
        "cited by stakes, turning_point or significance must also be cited by a fact.\n"
        'Return {"verdict":"angle" or "insufficient","reason":"one sentence",'
        '"stakes":{"text":"...","excerpts":["P1.1"]},"turning_point":{"text":"...","excerpts":["P1.2"]},'
        '"significance":{"text":"...","excerpts":["P1.3"]},'
        '"facts":[{"id":"A1","statement":"...","passages":["P1"]}],"omit":"details to leave out, or empty"}')}]


def _fact_gaps(statement, text):
    """Code-only support check of one fact against its passages: cue words, numbers and names must occur there."""
    gaps = compose.cue_gaps(statement, text)
    gaps += [f"number {n} not in passages" for n in sorted(compose.numbers(statement) - compose.numbers(text))]
    support = compose._ws(text)
    gaps += [f"name '{w}' not in passages" for w in sorted(compose.proper_tokens(statement))
             if not compose.name_supported(w, support)]
    return gaps


def _type_errors(out):
    """Wrong-type fields in valid JSON (a missing field is a reference problem, not a type problem)."""
    def bad(v, kind):
        return v is not None and not (isinstance(v, kind) if kind is not list else
                                      isinstance(v, list) and all(isinstance(x, str) for x in v))
    why = [f"{k} is not text" for k in ("verdict", "reason", "omit") if bad(out.get(k), str)]
    for k in ANGLE_FIELDS:
        f = out.get(k)
        if bad(f, dict):
            why.append(f"{k} is not an object")
        elif f:
            why += [f"{k}.text is not text"] if bad(f.get("text"), str) else []
            why += [f"{k}.excerpts is not a list of ids"] if bad(f.get("excerpts"), list) else []
    facts = out.get("facts")
    if facts is not None and not isinstance(facts, list):
        return why + ["facts is not a list"]
    for n, f in enumerate(facts or [], 1):
        if not isinstance(f, dict):
            why.append(f"fact {n} is not an object")
            continue
        why += [f"fact {n}.{x} is not text" for x in ("statement",) if bad(f.get(x), str)]
        why += [f"fact {n}.id is not text or a number"] if bad(f.get("id"), (str, int)) else []
        why += [f"fact {n}.passages is not a list of ids"] if bad(f.get("passages"), list) else []
    return why


def validate(out, passages):
    """-> (outcome, reasons, angle, refs). outcome: advanced | insufficient_evidence | invalid_references |
    malformed_output. Facts failing the code check are dropped (and listed); the rest must still cover every
    passage the angle cites, with at least three facts."""
    if not isinstance(out, dict):
        return "malformed_output", ["response is not a JSON object"], None, None
    if why := _type_errors(out):
        return "malformed_output", why, None, None
    if out.get("verdict") != "angle":
        return "insufficient_evidence", [f"model: {out.get('reason') or 'no supported angle'}"], None, None
    by, why, facts, dropped = {p["id"]: p for p in passages}, [], [], []
    ex, quotes, cites = excerpts(passages), {}, {}
    for k in ANGLE_FIELDS:
        f = out.get(k)
        if not isinstance(f, dict) or not (f.get("text") or "").strip():
            why.append(f"{k}: missing")
            continue
        ids = f.get("excerpts") or []
        if not ids or any(i not in ex for i in ids):
            why.append(f"{k}: cites unknown or no excerpt ids {ids}")
            continue
        # code, not the model, attaches the exact stored text of each cited excerpt
        quotes[k] = " ".join(ex[i][1] for i in dict.fromkeys(ids))
        cites[k] = list(dict.fromkeys(ex[i][0] for i in ids))
    for f in out.get("facts") or []:
        cited_ids = (f.get("passages") or []) if isinstance(f, dict) else []
        # a valid excerpt id (P8.3) resolves to its stored passage (P8); the cited excerpt is kept on the fact
        ids = [ex[i][0] if i in ex else i for i in cited_ids]
        if not ids or any(i not in by for i in ids) or not (f.get("statement") or "").strip():
            dropped.append(f"fact {f.get('id') if isinstance(f, dict) else f!r}: unknown or no passages")
            continue
        refs_ = list(dict.fromkeys(ids))
        gaps = _fact_gaps(f["statement"], " ".join(by[i]["passage"] for i in refs_))
        if gaps:
            dropped.append(f"fact {f.get('id')}: " + "; ".join(gaps))
            continue
        facts.append({"id": str(f.get("id")), "statement": f["statement"].strip(), "refs": refs_,
                      **({"excerpts": [i for i in cited_ids if i in ex]} if any(i in ex for i in cited_ids) else {})})
    cited = {i for f in facts for i in f["refs"]}
    for k in ANGLE_FIELDS:
        if k in cites and not set(cites[k]) <= cited:
            why.append(f"{k}: cites passages that no kept fact covers")
    if len(facts) < 3:
        why.append(f"only {len(facts)} fact(s) passed the code checks (3 needed)")
    refs = [{**{x: by[i][x] for x in ("id", "article_id", "url", "publisher", "content_hash", "passage")},
             "supports": [f["id"] for f in facts if i in f["refs"]]} for i in sorted(cited, key=lambda i: int(i[1:]))]
    if not why:
        try:
            check_reviewed_facts(facts, refs)
        except ReviewError as e:
            why.append(str(e))
    if why:
        return "invalid_references", why + dropped, None, None
    angle = {**{k: out[k]["text"].strip() for k in ANGLE_FIELDS}, "omit": (out.get("omit") or "").strip(),
             "facts": facts, "quotes": quotes, "excerpts": {k: out[k]["excerpts"] for k in ANGLE_FIELDS},
             "dropped_facts": dropped, "model_reason": out.get("reason")}
    return "advanced", dropped, angle, refs
