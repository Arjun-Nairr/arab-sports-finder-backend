"""Source adapters. fetch_* do bounded network calls and return raw payloads (saved for replay);
parse_* turn raw payloads into normalized leads without network access."""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .config import NATIONS
from .http import FetchError, get

WBAPI = "https://www.wikidata.org/w/api.php"
AJEL_FEED = "https://english.ajel.sa/ai-feed.json"
AJEL_WIRE = "https://ajel.sa/api/wire/v1"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
TSDB = "https://www.thesportsdb.com/api/v1/json/123/searchplayers.php"  # documented free key

KEEP_CLAIMS = ("P641", "P1532", "P27", "P569")
AJEL_ACCESS = ("AJEL usage policy permits AI-system use and republication with visible attribution and a "
               "story link; withdrawn stories must be removed within 24h; images not covered.")
GDELT_ACCESS = "GDELT DOC metadata only (title, domain, seen date). Full text not retrieved: publisher terms unverified."


def outcome(status, reason, **extra):
    return {"status": status, "reason": reason, **extra}


TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|_ga|ocid|cmpid|ref_src|ref_url)$", re.I)


def canonical_url(url):
    """Drop fragment and known tracking parameters only; semantic query parameters stay (sorted)."""
    p = urlsplit(url.strip())
    path = p.path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)))
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, query, ""))


# ---------- Wikidata (athlete-first) ----------

_sleep = time.sleep  # patched by checks


def query_key(nation):
    return f"wikidata_search:P1532={nation}"


def fetch_wikidata(limits, cursors=None, known=(), refresh_ids=()):
    """Bounded, resumable athlete discovery.
    Per nation: page through `list=search` from the saved cursor (sroffset) until `wikidata_per_nation` NEW ids
    are found, the query is exhausted (no `continue` in the response) or `wikidata_max_pages` pages were read.
    The proposed cursor is returned in raw["queries"]; the caller commits it only after persisting the athletes.
    `refresh_ids` re-fetches existing profiles so pagination never stops evidence updates."""
    t, r = limits["http_timeout_s"], limits["http_retries"]
    cursors, known = cursors or {}, set(known)
    raw = {"ids_by_nation": {}, "queries": {}, "entities": {}, "labels": {}, "errors": [], "refresh_ids": list(refresh_ids)}
    # Wikidata search API (haswbstatement) instead of WDQS: WDQS throttled this machine for 1000s+
    # on 2026-11-06; the operator approved this endpoint. Same P1532 + human rule.
    throttled = False
    for nation in list(NATIONS)[: limits["wikidata_max_nations"]]:
        key = query_key(nation)
        cur = cursors.get(key, {"next_offset": 0, "exhausted": False})
        q = {"start_offset": cur["next_offset"], "pages": [], "new": [], "repeats": [], "exhausted": False,
             "next_offset": cur["next_offset"], "error": None}
        offset = cur["next_offset"]
        while not throttled and len(q["new"]) < limits["wikidata_per_nation"] and len(q["pages"]) < limits["wikidata_max_pages"]:
            params = {"action": "query", "list": "search", "format": "json", "srlimit": limits["wikidata_per_nation"],
                      "sroffset": offset, "srsearch": f"haswbstatement:P1532={nation} haswbstatement:P31=Q5"}
            try:
                body = json.loads(get(f"{WBAPI}?{urlencode(params)}", timeout=t, retries=r))
                ids = [x["title"] for x in body["query"]["search"]]
            except (FetchError, ValueError, KeyError) as e:
                q["error"] = str(e)  # cursor stays at the last fully read page
                raw["errors"].append(f"search {nation} @ {offset}: {e}")
                throttled = "429" in str(e)
                break
            finally:
                _sleep(1)
            cont = (body.get("continue") or {}).get("sroffset") if q["error"] is None else None
            q["pages"].append({"offset": offset, "ids": ids, "continue": cont})
            for i in ids:
                (q["repeats"] if i in known else q["new"]).append(i)
                known.add(i)
            if cont is None:
                q["exhausted"] = True
                offset += len(ids)
                break
            offset = cont
        q["next_offset"] = offset
        raw["queries"][key] = q
        raw["ids_by_nation"][nation] = [i for p in q["pages"] for i in p["ids"]]
    if throttled:
        raw["errors"].append("Wikidata throttled (429); remaining nation queries skipped")
    ids = list(dict.fromkeys([i for v in raw["ids_by_nation"].values() for i in v] + list(refresh_ids)))
    for chunk in _chunks(ids, 50):
        try:
            ents = _wbget(chunk, "labels|aliases|claims|sitelinks", t, r, sitefilter="enwiki")
        except (FetchError, ValueError, KeyError) as e:
            raw["errors"].append(f"wbgetentities: {e}")
            continue
        for qid, e in ents.items():
            if "missing" in e:  # deleted/merged item: processed, nothing to store
                raw.setdefault("missing", []).append(qid)
                continue
            e["claims"] = {p: e.get("claims", {}).get(p, []) for p in KEEP_CLAIMS}
            raw["entities"][qid] = e
    refs = {v for e in raw["entities"].values() for p in ("P641", "P1532", "P27")
            for c in e["claims"][p] if (v := _value(c))}
    refs |= {q for e in raw["entities"].values() for c in e["claims"]["P1532"] for ref in c.get("references", [])
             for p in ("P248", "P143") for s in ref["snaks"].get(p, []) if (q := _snak_id(s))}
    for chunk in _chunks(sorted(refs), 50):
        try:
            for qid, e in _wbget(chunk, "labels", t, r).items():
                raw["labels"][qid] = e.get("labels", {}).get("en", {}).get("value", qid)
        except (FetchError, ValueError, KeyError) as e:
            raw["errors"].append(f"labels: {e}")
    qs = raw["queries"].values()
    requests = sum(len(q["pages"]) for q in qs) + len(raw["errors"])
    summary = (f"{sum(len(q['new']) for q in qs)} new, {sum(len(q['repeats']) for q in qs)} repeats, "
               f"{sum(q['exhausted'] for q in qs)} exhausted, {sum(bool(q['error']) for q in qs)} failed queries; "
               f"{len(refresh_ids)} refreshed")
    if raw["entities"]:
        res = outcome("worked", f"{len(raw['entities'])} athlete entities ({summary})", errors=raw["errors"])
    elif raw["errors"] and not raw["ids_by_nation"]:
        res = outcome("failed", "; ".join(raw["errors"][:3]))
    else:
        res = outcome("empty" if not raw["errors"] else "failed", "no athlete entities returned", errors=raw["errors"])
    res["requests"] = requests
    return raw, res


def committable_cursors(raw):
    """Cursors safe to save: every id on the query's pages was fetched (or reported missing)."""
    done = set(raw["entities"]) | set(raw.get("missing", []))
    return {k: {"next_offset": q["next_offset"], "exhausted": q["exhausted"]}
            for k, q in raw.get("queries", {}).items()
            if all(i in done for p in q["pages"] for i in p["ids"]) and (q["pages"] or q["error"] is None)}


def _wbget(ids, props, timeout, retries, **extra):
    params = {"action": "wbgetentities", "ids": "|".join(ids), "props": props,
              "languages": "en|ar", "format": "json", **extra}
    return json.loads(get(f"{WBAPI}?{urlencode(params)}", timeout=timeout, retries=retries))["entities"]


def _chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def _snak_id(snak):
    v = snak.get("datavalue", {}).get("value")
    return v.get("id") if isinstance(v, dict) else None


def _value(claim):
    return _snak_id(claim["mainsnak"])


def _time(claim, prop):
    for s in claim.get("qualifiers", {}).get(prop, []):
        v = s.get("datavalue", {}).get("value")
        if isinstance(v, dict):
            return v.get("time")
    return None


def parse_wikidata(raw):
    lab = lambda q: raw["labels"].get(q, q)
    leads = []
    for qid, e in raw["entities"].items():
        labels, gaps = e.get("labels", {}), []
        en, ar = labels.get("en", {}).get("value"), labels.get("ar", {}).get("value")
        name = en or ar or qid
        if not (en or ar):
            gaps.append("no English/Arabic label")
        aliases = sorted({a["value"] for lang in ("en", "ar") for a in e.get("aliases", {}).get(lang, [])}
                         | ({ar} if en and ar else set()))
        claims = e["claims"]
        sport_ids = dict.fromkeys(v for c in claims["P641"] if c["rank"] != "deprecated" and (v := _value(c)))
        sports = [{"qid": v, "label": lab(v)} for v in sport_ids]
        rep = []
        for c in claims["P1532"]:
            v = _value(c)
            rep.append({
                "nation_qid": v, "nation": lab(v) if v else None, "listed": v in NATIONS, "rank": c["rank"],
                "statement": c["id"], "start": _time(c, "P580"), "end": _time(c, "P582"),
                "references": [{"stated_in": [lab(q) for s in ref["snaks"].get("P248", []) if (q := _snak_id(s))],
                                "urls": [s["datavalue"]["value"] for s in ref["snaks"].get("P854", []) if "datavalue" in s],
                                "imported_from": [lab(q) for s in ref["snaks"].get("P143", []) if (q := _snak_id(s))]}
                               for ref in c.get("references", [])],
                "source": "Wikidata P1532 (country for sport)",
            })
        listed = [x for x in rep if x["listed"] and x["rank"] != "deprecated"]
        # "Imported from Wikipedia" (P143) alone is not a source: supported needs stated-in or a URL.
        if any(ref["stated_in"] or ref["urls"] for x in listed for ref in x["references"]):
            eligibility = "supported"
        elif listed:
            eligibility = "claimed"
            gaps.append("representation claim has no source reference" +
                        (" (only 'imported from')" if any(x["references"] for x in listed) else ""))
        else:
            eligibility = "unresolved"
            gaps.append("no current P1532 value in the nation list")
        if any(not x["listed"] for x in rep):
            gaps.append("also has P1532 value(s) outside the nation list")
        if not sports:
            gaps.append("sport unknown")
        dob = next((c["mainsnak"].get("datavalue", {}).get("value", {}).get("time") for c in claims["P569"]), None)
        leads.append({
            "source": "wikidata", "source_id": qid, "name": name, "aliases": aliases,
            "sports": sports, "representation": rep,
            "citizenship": [lab(v) for c in claims["P27"] if (v := _value(c))],
            "eligibility": eligibility, "resolution": "source_identity", "gaps": gaps,
            "external": {"wikidata_url": f"https://www.wikidata.org/wiki/{qid}", "dob": dob,
                         "enwiki": e.get("sitelinks", {}).get("enwiki", {}).get("title")},
        })
    return leads


def fetch_wikidata_roles(qids, limits):
    """Occupation (P106) and team membership (P54) for screening eligibility. Bounded: 50 ids per request.
    Returns ({qid: {"occupations": [...], "national_teams": [...]}}, outcome)."""
    t, r = limits["http_timeout_s"], limits["http_retries"]
    out, errors, ents = {}, [], {}
    for chunk in _chunks(list(qids), 50):
        try:
            ents.update(_wbget(chunk, "claims", t, r))
        except (FetchError, ValueError, KeyError) as e:
            errors.append(str(e))
    ref_ids = {v for e in ents.values() for p in ("P106", "P54") for c in e.get("claims", {}).get(p, []) if (v := _value(c))}
    ref_ids |= {q for e in ents.values() for c in e.get("claims", {}).get("P54", []) for ref in c.get("references", [])
                for s in ref["snaks"].get("P248", []) if (q := _snak_id(s))}
    labels = {}
    for chunk in _chunks(sorted(ref_ids), 50):
        try:
            labels.update({q: e.get("labels", {}).get("en", {}).get("value", q) for q, e in _wbget(chunk, "labels", t, r).items()})
        except (FetchError, ValueError, KeyError) as e:
            errors.append(f"labels: {e}")
    nation_names = {n.lower() for n, _ in NATIONS.values()}
    for qid, e in ents.items():
        claims = e.get("claims", {})
        teams = []
        for c in claims.get("P54", []):
            v = _value(c)
            label = labels.get(v, v or "")
            if v and "national" in label.lower():
                teams.append({"qid": v, "label": label, "rank": c["rank"],
                              "listed": any(n in label.lower() for n in nation_names),
                              "references": [{"stated_in": [labels.get(q, q) for s in ref["snaks"].get("P248", []) if (q := _snak_id(s))],
                                              "urls": [s["datavalue"]["value"] for s in ref["snaks"].get("P854", []) if "datavalue" in s],
                                              "imported_from": ["P143"] if ref["snaks"].get("P143") else []}
                                             for ref in c.get("references", [])]})
        out[qid] = {"occupations": [labels.get(v, v) for c in claims.get("P106", []) if (v := _value(c))],
                    "national_teams": teams}
    status = "worked" if out else ("failed" if errors else "empty")
    return out, outcome(status, f"{len(out)}/{len(qids)} role records", errors=errors)


# ---------- Ajel (news-first, permitted full text) ----------

def fetch_ajel_wire(limits, key, targets):
    """Bounded athlete-first search: GET /v1/search?q=<name> (title/summary search per Ajel docs) for the given
    (source_id, name) targets, then GET /v1/articles/{id} for full text of up to ajel_wire_articles hits."""
    raw = {"searches": [], "articles": {}, "errors": []}
    if not key:
        return raw, outcome("not_attempted", "AJEL_WIRE_API_KEY not set; key requires operator sign-up")
    t, r, h = limits["http_timeout_s"], limits["http_retries"], {"x-api-key": key}
    for sid, name in targets:
        try:
            body = json.loads(get(f"{AJEL_WIRE}/search?{urlencode({'q': name, 'language': 'en', 'limit': 5})}",
                                  headers=h, timeout=t, retries=r))
            raw["searches"].append({"source_id": sid, "q": name, "hits": [a["id"] for a in body.get("data", [])]})
        except (FetchError, ValueError) as e:
            raw["searches"].append({"source_id": sid, "q": name, "error": str(e)})
            if "429" in str(e) or "401" in str(e):
                break
    hits = list(dict.fromkeys(i for s in raw["searches"] for i in s.get("hits", [])))
    for aid in hits[: limits["ajel_wire_articles"]]:
        try:
            raw["articles"][aid] = json.loads(get(f"{AJEL_WIRE}/articles/{aid}", headers=h, timeout=t, retries=r))["data"]
        except (FetchError, ValueError, KeyError) as e:
            raw["errors"].append(f"article {aid}: {e}")
    ok = [s for s in raw["searches"] if "hits" in s]
    errs = [s["error"] for s in raw["searches"] if "error" in s] + raw["errors"]
    status = "failed" if not ok else ("worked" if hits else "empty")
    return raw, outcome(status, f"{len(ok)}/{len(targets)} name searches answered, {len(hits)} hits, "
                        f"{len(raw['articles'])} full articles", errors=errs, requests=len(raw["searches"]) + len(raw["articles"]))


# Ajel does not document status values; observed live: "published", "updated" (an edited, still-live story).
# Only an explicit removal status counts as a withdrawal; any other status keeps the content and is recorded.
WIRE_WITHDRAWN = {"withdrawn", "unpublished", "deleted", "removed", "retracted"}


def parse_ajel_wire(raw):
    leads = []
    for aid, a in raw.get("articles", {}).items():
        withdrawn = (a.get("status") or "").lower() in WIRE_WITHDRAWN
        text = None if withdrawn else a.get("content_text") or a.get("summary")
        kind = "withdrawn" if withdrawn else "full_text" if a.get("content_text") else "summary"
        hits_for = [s["source_id"] for s in raw.get("searches", []) if aid in s.get("hits", [])]
        leads.append({
            "provider": "ajel", "provider_id": aid, "url": a["canonical_url"], "canonical_url": canonical_url(a["canonical_url"]),
            "publisher": (a.get("attribution") or {}).get("brand_latin") or "Ajel", "title": a.get("title"),
            "language": a.get("language"), "published_at": a.get("published_at"), "updated_at_source": a.get("updated_at"),
            "seen_at": None, "content": text, "content_kind": kind,
            "channel": "ajel_wire", "usage": a.get("usage"), "source_status": a.get("status"),
            "acquisition": f"ajel_wire /v1/articles/{{id}} (status {a.get('status')})",
            "access_note": AJEL_ACCESS,
            "extra": {"route": "athlete_first", "queried_for": hits_for, "section": (a.get("section") or {}).get("name_en"),
                      "summary": a.get("summary"), "attribution": a.get("attribution")},
        })
    return leads


def fetch_ajel(limits):
    t, r = limits["http_timeout_s"], limits["http_retries"]
    raw = {"feed": None, "markdown": {}, "errors": []}
    results = {}
    try:
        raw["feed"] = json.loads(get(AJEL_FEED, timeout=t, retries=r))
    except (FetchError, ValueError) as e:
        results["ajel_feed"] = outcome("failed", str(e))
        return raw, results
    sports = [i for i in raw["feed"].get("items", []) if (i.get("section") or "").lower() == "sports"]
    raw["selected"] = [i["url"] for i in sports[: limits["ajel_max_articles"]]]
    for item in sports[: limits["ajel_max_articles"]]:
        if item.get("markdown_url"):
            try:
                raw["markdown"][item["url"]] = get(item["markdown_url"], timeout=t, retries=r)
            except FetchError as e:
                raw["errors"].append(f"markdown {item['url']}: {e}")
                if str(e).startswith("HTTP 410"):  # explicit "Gone": treated as a withdrawal
                    raw.setdefault("withdrawn", []).append(item["url"])
    n = min(len(sports), limits["ajel_max_articles"])
    status = "worked" if n else "empty"
    results["ajel_feed"] = outcome(status, f"{n} sports items, {len(raw['markdown'])} full texts; feed updated_at "
                                   f"{raw['feed'].get('updated_at')}", errors=raw["errors"], requests=1 + n)
    return raw, results


def _md_story(md):
    m = re.search(r"^## Story\s*$(.*)", md, re.M | re.S)
    return m.group(1).strip() if m else None


def parse_ajel(raw):
    if not raw.get("feed"):
        return []
    leads = []
    selected = set(raw.get("selected", []))
    for item in raw["feed"].get("items", []):
        if item["url"] not in selected:
            continue
        story = _md_story(raw["markdown"].get(item["url"], ""))
        kind = "withdrawn" if item["url"] in raw.get("withdrawn", []) else "full_text" if story else "summary"
        leads.append({
            "provider": "ajel", "provider_id": item["url"].rstrip("/").rsplit("/", 1)[-1],
            "url": item["url"], "canonical_url": canonical_url(item["url"]),
            "publisher": raw["feed"].get("source") or "AJEL English", "title": item.get("title"),
            "language": raw["feed"].get("language"), "published_at": item.get("published_at"),
            "updated_at_source": item.get("updated_at"), "seen_at": None,
            "content": None if kind == "withdrawn" else story or item.get("summary"), "content_kind": kind,
            "channel": "ajel_feed", "usage": None, "source_status": "http_410" if kind == "withdrawn" else None,
            "acquisition": {"withdrawn": "ajel markdown_url HTTP 410", "full_text": "ajel_ai_feed + markdown_url",
                            "summary": "ajel_ai_feed summary"}[kind],
            "access_note": AJEL_ACCESS,
            "extra": {"route": "news_first", "section": item.get("section"), "summary": item.get("summary"),
                      "attribution": raw["feed"].get("attribution")},
        })
    return leads


# ---------- GDELT (news-first discovery and athlete-first report search) ----------

_gdelt_last = [0.0]
SPORT_TERMS = ["athlete", "olympic", "\"national team\"", "championship", "medal"]


def _gdelt(query, maxrecords, limits):
    wait = 10 - (time.monotonic() - _gdelt_last[0])  # GDELT asks for <=1 request/5s; 429s seen at 6.5s
    if wait > 0:
        time.sleep(wait)
    try:
        body = get(f"{GDELT}?{urlencode({'query': query, 'mode': 'artlist', 'format': 'json', 'maxrecords': maxrecords, 'timespan': '30d', 'sort': 'datedesc'})}",
                   timeout=limits["http_timeout_s"], retries=0)  # retrying a GDELT 429 prolongs the throttle
    finally:
        _gdelt_last[0] = time.monotonic()
    try:
        return json.loads(body).get("articles", [])
    except ValueError:
        raise FetchError("non-JSON response: " + body[:160].replace("\n", " ")) from None


def news_first_queries(group=4):
    """GDELT rejects long queries, so the nation list is covered in groups of four."""
    names = [f'"{n}"' if " " in n else n for n, _ in NATIONS.values()]
    return [f"({' OR '.join(SPORT_TERMS)}) ({' OR '.join(names[i:i + group])}) sourcelang:english"
            for i in range(0, len(names), group)]


def fetch_gdelt(limits, athlete_leads):
    raw = {"news_first": [], "by_name": []}
    per_query = max(1, limits["gdelt_max_records"] // len(news_first_queries()))
    picks = [a for a in athlete_leads if a["eligibility"] != "unresolved" and " " in a["name"]]
    # Name searches first: if GDELT throttles mid-run, the queries that can produce links have run.
    jobs = [("by_name", a["source_id"], f'"{a["name"]}"', 10) for a in picks[: limits["gdelt_name_queries"]]]
    jobs += [("news_first", None, q, per_query) for q in news_first_queries()]
    throttled = False
    for kind, sid, q, n in jobs:
        rec = {"source_id": sid, "query": q}
        if throttled:
            rec["error"] = "skipped after GDELT throttling"
        else:
            try:
                rec["articles"] = _gdelt(q, n, limits)
            except FetchError as e:
                rec["error"] = str(e)
                throttled = "429" in str(e)
        raw[kind].append(rec)
    calls = [*raw["news_first"], *raw["by_name"]]
    errs = [c["error"] for c in calls if "error" in c]
    n = sum(len(c.get("articles", [])) for c in calls)
    status = "worked" if n else ("failed" if len(errs) == len(calls) else "empty")
    return raw, outcome(status, f"{len(calls)} queries, {n} article records, {len(errs)} errors",
                        errors=errs, requests=len(calls))


def _seen(s):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def parse_gdelt(raw):
    leads = []
    calls = [("news_first", None, c) for c in raw.get("news_first", [])]
    calls += [("athlete_first", c["source_id"], c) for c in raw.get("by_name", [])]
    for route, sid, c in calls:
        for a in c.get("articles", []):
            leads.append({
                "provider": "gdelt", "provider_id": None, "url": a["url"], "canonical_url": canonical_url(a["url"]),
                "publisher": a.get("domain"), "title": a.get("title"), "language": a.get("language"),
                "published_at": None, "updated_at_source": None, "seen_at": _seen(a.get("seendate")),
                "content": a.get("title"), "content_kind": "title_only", "acquisition": "gdelt_doc_artlist",
                "channel": "gdelt", "usage": None, "source_status": None,
                "access_note": GDELT_ACCESS,
                "extra": {"route": route, "query": c["query"], "queried_for": sid, "sourcecountry": a.get("sourcecountry")},
            })
    return leads


# ---------- TheSportsDB (optional enrichment, never identity) ----------

def fetch_thesportsdb(names, limits):
    raw, errs = {}, []
    for n in names[: limits["thesportsdb_lookups"]]:
        try:
            raw[n] = json.loads(get(f"{TSDB}?p={quote(n.replace(' ', '_'))}", timeout=limits["http_timeout_s"],
                                    retries=limits["http_retries"])).get("player") or []
        except (FetchError, ValueError) as e:
            raw[n] = None
            errs.append(f"{n}: {e}")
    if not names:
        return raw, outcome("not_attempted", "no linked or new athletes to enrich")
    hits = sum(1 for v in raw.values() if v)
    status = "worked" if hits else ("failed" if errs and len(errs) == len(raw) else "empty")
    return raw, outcome(status, f"{len(raw)} lookups, {hits} with candidates", errors=errs, requests=len(raw))


def tsdb_candidates(players):
    keep = ("idPlayer", "strPlayer", "strSport", "strNationality", "strTeam", "dateBorn", "strPosition")
    return [{k: p.get(k) for k in keep} for p in (players or [])]


# ---------- FIFA reports (reviewer-cited drafting evidence, not discovery) ----------

FIFA_TERMS = "https://inside.fifa.com/terms-of-service"
FIFA_ACCESS = ("FIFA Terms of Service (last updated 27 January 2026, read 2026-11-07): content is FIFA's copyright; s.6.4 "
               "allows non-commercial display on a website with attribution to FIFA and, where possible, a link back to "
               "the story; s.6.2 bars using FIFA content, names, logos or marks without written agreement. robots.txt "
               "does not disallow article pages.")


def parse_fifa(url, html):
    """One inside.fifa.com article from its embedded Next.js data: rich-text paragraphs only (lists, embeds and page
    chrome dropped). Raises ValueError if the page carries no article text."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    page = json.loads(m.group(1))["props"]["pageProps"] if m else {}
    doc = (page.get("richTextProps") or {}).get("document") or {}
    text = lambda n: n.get("value", "") + "".join(text(c) for c in n.get("content", []))
    paras = [t.strip() for n in doc.get("content", []) if n.get("nodeType") == "paragraph" and (t := text(n)).strip()]
    parent = page.get("parent") or {}
    if not paras or not parent.get("id"):
        raise ValueError("no FIFA article text in page data")
    return {
        "provider": "fifa", "provider_id": parent["id"], "canonical_url": canonical_url(url), "url": url,
        "publisher": "FIFA (inside.fifa.com)", "title": parent.get("title", "").removeprefix("Article Page > "),
        "language": "en", "published_at": parent.get("contentDate"), "updated_at_source": None, "seen_at": None,
        "content": "\n\n".join(paras), "content_kind": "full_text", "acquisition": "fifa_article_page_next_data",
        "channel": "fifa_page", "source_status": "reviewer_cited",
        "access_note": FIFA_ACCESS,
        "usage": {"attribution_required": True, "link_back_required": True, "commercial_use_allowed": False},
        "extra": {"kind": "report", "attribution": {"text": f"FIFA, \"{parent.get('title', '').removeprefix('Article Page > ')}\", "
                                                            f"{(parent.get('contentDate') or '')[:10]}, {url}"},
                  "terms": {"url": FIFA_TERMS, "summary": FIFA_ACCESS},
                  "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "paragraphs": len(paras)},
    }


def fetch_fifa(url, limits):
    return parse_fifa(url, get(url, timeout=limits["http_timeout_s"], retries=limits["http_retries"]))
