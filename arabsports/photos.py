"""T06: bounded Wikimedia Commons lookup for one story, screened by a deterministic policy (no LLM).
A candidate is usable only when identity, story context, licence and credit are all backed by the file's own
Commons metadata. This is licence screening, not legal clearance; anything less leaves the designed graphics."""
import html
import json
import re
import urllib.parse

from . import http

API = "https://commons.wikimedia.org/w/api.php"
SEARCH_LIMIT = 8  # per query; at most len(queries) * SEARCH_LIMIT files are inspected


def _api(params):
    return json.loads(http.get(API + "?" + urllib.parse.urlencode({**params, "format": "json", "formatversion": 2})))


def _plain(s):
    """Commons extmetadata values are HTML fragments; keep text only (never rendered as HTML)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def licence_decision(short_name, url=""):
    """CC0/public domain/CC BY -> shortlist; CC BY-SA and unrecognised terms -> review; NC/ND/missing/ARR -> reject."""
    n = (short_name or "").lower().replace("_", "-")
    if not n:
        return "reject", "no licence in file metadata"
    if re.search(r"\bnc\b|non-?commercial|\bnd\b|no-?deriv|all rights reserved", n):
        return "reject", f"terms not reusable here: {short_name}"
    if re.search(r"\bcc0\b|public domain|^pd\b", n):
        return "shortlist", f"public domain/CC0: {short_name}"
    if re.search(r"cc[ -]by[ -]sa", n):
        return "review", f"share-alike terms need a review: {short_name}"
    if re.search(r"cc[ -]by[ -]\d", n) or n == "cc by" or n == "cc-by":
        return "shortlist", f"attribution licence: {short_name}"
    return "review", f"unrecognised or custom terms: {short_name}"


def screen_file(info, subject_terms, context_terms, context_date):
    """Deterministic decision for one file from its own metadata."""
    m = {k: _plain(v.get("value")) for k, v in (info.get("extmetadata") or {}).items()}
    text = " ".join([info.get("title", ""), m.get("ImageDescription", ""), m.get("ObjectName", ""),
                     m.get("Categories", "")]).lower()
    lic, lic_reason = licence_decision(m.get("LicenseShortName"), m.get("LicenseUrl"))
    identity = all(t in text for t in subject_terms)
    taken = m.get("DateTimeOriginal", "")
    context = taken.startswith(context_date) or all(t in text for t in context_terms)
    credit = m.get("Artist") and (m.get("Credit") or m.get("Artist"))
    reasons = [lic_reason]
    reasons.append("identity: subject named in file metadata" if identity else "identity not supported by file metadata")
    reasons.append("context: matches the story event" if context else
                   f"context not the story event (taken '{taken or 'unknown'}'; story event {context_date})")
    if not credit:
        reasons.append("no creator/credit in file metadata")
    usable = lic == "shortlist" and identity and context and bool(credit)
    return {"file_page": info.get("descriptionurl"), "title": info.get("title"), "creator": m.get("Artist") or None,
            "credit": m.get("Credit") or None, "licence": m.get("LicenseShortName") or None,
            "licence_url": m.get("LicenseUrl") or None, "attribution_required": m.get("AttributionRequired"),
            "taken": taken or None, "description": m.get("ImageDescription", "")[:300] or None,
            "licence_decision": lic, "identity_supported": identity, "context_supported": context,
            "decision": "shortlisted_for_review" if usable else ("review" if lic == "review" and identity and context
                                                                 else "not_used"),
            "reasons": reasons}


def lookup(queries, subject_terms, context_terms, context_date):
    """One search per query (File namespace) and one metadata call for the union. Returns outcome + candidates."""
    titles, attempts = [], []
    for q in queries:
        try:
            hits = _api({"action": "query", "list": "search", "srsearch": q, "srnamespace": 6,
                         "srlimit": SEARCH_LIMIT})["query"]["search"]
            attempts.append({"query": q, "outcome": "ok", "results": len(hits)})
            titles += [h["title"] for h in hits if h["title"] not in titles]
        except (http.FetchError, KeyError, ValueError) as e:
            attempts.append({"query": q, "outcome": "failed", "error": str(e)[:200]})
    candidates = []
    if titles:
        try:
            pages = _api({"action": "query", "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                          "titles": "|".join(titles[:50])})["query"]["pages"]
            for p in pages:
                for ii in p.get("imageinfo") or []:
                    if (ii.get("mime") or "").startswith("image/"):
                        candidates.append(screen_file({**ii, "title": p["title"]}, subject_terms, context_terms, context_date))
        except (http.FetchError, KeyError, ValueError) as e:
            attempts.append({"query": "imageinfo", "outcome": "failed", "error": str(e)[:200]})
    usable = [c for c in candidates if c["decision"] == "shortlisted_for_review"]
    if all(a["outcome"] == "failed" for a in attempts):
        outcome = "lookup_failed"
    elif not candidates:
        outcome = "no_results"
    else:
        outcome = "candidate_shortlisted" if usable else "no_suitable_photo"
    return {"outcome": outcome, "attempts": attempts, "candidates": candidates}


if __name__ == "__main__":  # policy self-check, no network
    assert licence_decision("CC BY 4.0")[0] == "shortlist"
    assert licence_decision("CC0")[0] == "shortlist" and licence_decision("Public domain")[0] == "shortlist"
    assert licence_decision("CC BY-SA 4.0")[0] == "review" and licence_decision("Custom licence")[0] == "review"
    assert licence_decision("CC BY-NC 2.0")[0] == "reject" and licence_decision("CC BY-ND 3.0")[0] == "reject"
    assert licence_decision("")[0] == "reject"
    f = {"title": "File:X.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
         "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "Artist": {"value": "<a>Someone</a>"},
                         "ImageDescription": {"value": "Mohamed Salah in 2018"}, "DateTimeOriginal": {"value": "2018-06-19"}}}
    r = screen_file(f, ["salah"], ["congo"], "2017-10-08")
    assert r["identity_supported"] and not r["context_supported"] and r["decision"] == "not_used", r
    print("ok photos policy")
