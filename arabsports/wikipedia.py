"""English Wikipedia biographies as background evidence (not news, no publication date).

Identity: the title comes only from the athlete's Wikidata enwiki sitelink. After normalisation and redirects the
page must carry pageprops.wikibase_item == the athlete's QID and no disambiguation flag (Action API, query+pageprops).
Content: Core REST `revision/{id}/html` (Parsoid HTML of the checked revision), cached on disk per revision id.
Prose paragraphs only, with their section headings and citation markers mapped to the page's reference list.
No images, no external citation fetching: a citation link is recorded, never treated as verified content."""
import html.parser
import json
import os
import re
import time
import urllib.parse

from .config import ROOT
from .http import UA, FetchError, get

API = "https://en.wikipedia.org/w/api.php"
REST = "https://en.wikipedia.org/w/rest.php/v1"
CACHE = ROOT / "cache" / "wikipedia"
REUSE_DOC = "https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content"
SKIP_SECTIONS = {"see also", "notes", "references", "external links", "further reading", "bibliography", "sources",
                 "citations", "footnotes", "explanatory notes"}
SKIP_CLASS = re.compile(r"\b(?:infobox|navbox|hatnote|shortdescription|sidebar|metadata|mw-references-wrap|reflist|"
                        r"thumb|gallery|mw-empty-elt)\b")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
PARSER_VERSION = "p2"  # p2: label-only paragraphs dropped. A new version re-parses stored revisions (from cache).
REDUCTION = ("kept: prose <p> paragraphs with their section headings. dropped: infobox, tables, lists, figures/images, "
             "navboxes, hatnotes, label-only paragraphs (no sentence punctuation, under 8 words), citation markers (mapped to references instead) and the sections "
             + ", ".join(sorted(SKIP_SECTIONS)) + ".")
LICENCE_NOTES = [
    "Wikipedia text is CC BY-SA 4.0 (and GFDL unless otherwise indicated), not public domain. Reuse needs "
    "attribution (a link to the page/revision), a licence notice, and adapted text must be shared under CC BY-SA "
    "4.0 or later. Source: " + REUSE_DOC,
    "Images and other media have separate licences and are not fetched. Some pages contain non-free quotations.",
    "Cited external references have their own rights; their links are recorded but not retrieved or verified.",
]
_get = get  # patched by checks


def _headers():
    # Wikimedia's User-Agent policy asks for operator contact; set WIKIMEDIA_CONTACT (URL or email) to add one.
    contact = os.environ.get("WIKIMEDIA_CONTACT")
    return {"User-Agent": f"{UA[:-1]}; {contact})" if contact else UA}


def identify(titles, timeout, retries):
    """One Action API request for up to 50 titles. Returns ({title: {requested, normalized, redirects, page}}, rightsinfo)."""
    p = {"action": "query", "format": "json", "formatversion": "2", "redirects": "1", "prop": "pageprops|info|revisions",
         "ppprop": "wikibase_item|disambiguation", "rvprop": "ids|timestamp", "meta": "siteinfo", "siprop": "rightsinfo",
         "titles": "|".join(titles)}
    q = json.loads(_get(API + "?" + urllib.parse.urlencode(p), headers=_headers(), timeout=timeout, retries=retries))["query"]
    normalized = {n["from"]: n["to"] for n in q.get("normalized", [])}
    redirects = {n["from"]: n for n in q.get("redirects", [])}
    pages = {pg["title"]: pg for pg in q.get("pages", [])}
    out = {}
    for t in titles:
        cur, hops = normalized.get(t, t), []
        while cur in redirects and len(hops) < 5:
            hops.append({k: v for k, v in redirects[cur].items() if k in ("from", "to", "tofragment")})
            cur = redirects[cur]["to"]
        out[t] = {"requested": t, "normalized": normalized.get(t), "redirects": hops, "page": pages.get(cur)}
    return out, q.get("rightsinfo", {})


def classify(qid, ident):
    """verified | missing | disambiguation | entity_mismatch (e.g. a redirect to a team or another person)."""
    pg = ident["page"]
    if not pg or pg.get("missing") or pg.get("invalid"):
        return "missing"
    props = pg.get("pageprops") or {}
    if "disambiguation" in props:
        return "disambiguation"
    if props.get("wikibase_item") != qid or any(h.get("tofragment") for h in ident["redirects"]):
        return "entity_mismatch"
    return "verified"


def revision_html(revid, timeout, retries):
    """Returns (html, from_cache). A revision's content never changes, so the cache needs no expiry."""
    path = CACHE / f"{int(revid)}.html"
    if path.exists():
        return path.read_text(encoding="utf-8"), True
    text = _get(f"{REST}/revision/{int(revid)}/html", headers=_headers(), timeout=timeout, retries=retries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text, False


class _Bio(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.refs = [], {}
        self.sections = [{"heading": "Lead", "top": "Lead", "paragraphs": []}]
        self.top, self.head, self.level, self.para, self.ref = "Lead", None, 2, None, None

    def _skipping(self):
        return any(skip for _, skip, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls, href = a.get("class") or "", a.get("href") or ""
        if self.ref is not None and tag == "a" and "mw:ExtLink" in (a.get("rel") or "") and href:
            self.ref["urls"].append(href)
        if self.para is not None and tag == "a" and "#cite_note" in href:
            self.para["cites"].append(href.split("#", 1)[1])
        if tag in VOID:
            return
        role = None
        if tag == "li" and (a.get("id") or "").startswith("cite_note"):
            self.ref, role = {"id": a["id"], "text": [], "urls": []}, "ref"
        skip = tag in ("table", "style", "script", "figure") or (tag == "sup" and "reference" in cls) or bool(SKIP_CLASS.search(cls))
        if not self._skipping() and not skip:
            if tag in ("h2", "h3"):
                self.head, self.level, role = [], int(tag[1]), "head"
            elif tag == "p":
                self.para, role = {"text": [], "cites": []}, "para"
        self.stack.append((tag, skip, role))

    def handle_endtag(self, tag):
        if not any(t == tag for t, _, _ in self.stack):
            return  # stray end tag
        while self.stack:
            t, _, role = self.stack.pop()
            if role:
                self._close(role)
            if t == tag:
                break

    def handle_data(self, d):
        if self.ref is not None:
            self.ref["text"].append(d)
        if self._skipping():
            return
        if self.head is not None:
            self.head.append(d)
        elif self.para is not None:
            self.para["text"].append(d)

    def _close(self, role):
        clean = lambda xs: re.sub(r"\s+", " ", "".join(xs)).strip()
        if role == "head":
            text, self.head = clean(self.head), None
            if self.level == 2:
                self.top = text
            self.sections.append({"heading": text if self.level == 2 else f"{self.top} > {text}", "top": self.top,
                                  "paragraphs": []})
        elif role == "para":
            text, cites, self.para = clean(self.para["text"]), list(dict.fromkeys(self.para["cites"])), None
            # Parsoid wraps table/list labels ("Women's singles Mixed doubles") in <p>: not prose, so dropped.
            label = not re.search(r"[.!?]", text) and len(text.split()) < 8
            if text and not label and self.sections[-1]["top"].lower() not in SKIP_SECTIONS:
                self.sections[-1]["paragraphs"].append({"text": text, "cites": cites})
        elif role == "ref":
            r, self.ref = self.ref, None
            self.refs[r["id"]] = {"text": clean(r["text"]).lstrip("↑^ ")[:300], "urls": r["urls"][:5]}


def parse(html_text):
    """Returns (sections with prose paragraphs, citations referenced by those paragraphs)."""
    p = _Bio()
    p.feed(html_text)
    p.close()
    sections = [{"heading": s["heading"], "paragraphs": s["paragraphs"]} for s in p.sections if s["paragraphs"]]
    cited = {c for s in sections for para in s["paragraphs"] for c in para["cites"]}
    return sections, {c: p.refs[c] for c in sorted(cited) if c in p.refs}


def content_text(sections):
    """Stored text: '== Heading ==' blocks then paragraphs, blank-line separated (screen.bio_sections reads it back)."""
    return "\n\n".join(f"== {s['heading']} ==\n\n" + "\n\n".join(p["text"] for p in s["paragraphs"]) for s in sections)


def bio_lead(qid, ident, sections, citations, rights):
    """An article lead for store.upsert_articles: provider 'wikipedia', keyed by page id (stable across renames)."""
    pg = ident["page"]
    rev = pg["revisions"][0]
    page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(pg["title"].replace(" ", "_"))
    rev_url = f"https://en.wikipedia.org/w/index.php?oldid={rev['revid']}"
    licence = {"text": rights.get("text"), "url": rights.get("url"), "source": "siteinfo rightsinfo (Action API)",
               "notes": LICENCE_NOTES}
    return {
        "provider": "wikipedia", "provider_id": str(pg["pageid"]),
        "canonical_url": f"https://en.wikipedia.org/wiki?curid={pg['pageid']}", "url": page_url,
        "publisher": "English Wikipedia", "title": pg["title"], "language": "en",
        "published_at": None, "updated_at_source": None, "seen_at": None,  # background biography, not dated news
        "content": content_text(sections), "content_kind": "full_text",
        "acquisition": "wikipedia_core_rest_revision_html", "channel": "wikipedia_rest", "source_status": "verified_sitelink",
        "access_note": "Background biography (tertiary, openly edited), not current news; no publication date.",
        "usage": {"attribution_required": True, "licence_notice_required": True, "share_alike_required": True},
        "extra": {"kind": "biography",
                  "attribution": {"text": f"\"{pg['title']}\", English Wikipedia, revision {rev['revid']}, "
                                          f"{licence['text']} ({licence['url']}), {rev_url}"},
                  "license": licence,
                  "revision": {"id": rev["revid"], "timestamp": rev["timestamp"], "url": rev_url},
                  "page": {"pageid": pg["pageid"], "title": pg["title"], "wikibase_item": qid, "url": page_url},
                  "identity": {"method": "wikidata_enwiki_sitelink + pageprops.wikibase_item",
                               "requested_title": ident["requested"], "normalized": ident["normalized"],
                               "redirects": ident["redirects"]},
                  "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "parser": PARSER_VERSION, "reduction": REDUCTION, "sections": sections, "citations": citations,
                  "citations_note": "Reference text/links as listed on the page; not retrieved or verified."},
    }


def mention(athlete, lead):
    """The athlete link for a biography comes from the verified sitelink, not from name matching."""
    return {"surface": athlete["name"], "status": "linked", "athlete_id": athlete["id"], "candidates": [athlete["id"]],
            "method": "wikidata_sitelink_verified",
            "evidence": {"qid": athlete["source_id"], "pageid": lead["extra"]["page"]["pageid"],
                         "revision": lead["extra"]["revision"]["id"], "redirects": lead["extra"]["identity"]["redirects"]}}
