"""T05 public export: an accepted package -> one static JSON file + index entry, through an explicit whitelist.
Source passages, verification transcripts, diagnostics, provider ids and local paths never leave the private store.
The static site (showcase/site) is the only deployable output; the layout fixture goes to showcase/preview."""
import json
import pathlib
import re
import shutil

from .compose import TYPO, failed_cited_facts

ROOT = pathlib.Path(__file__).resolve().parent.parent / "showcase"
SITE, PREVIEW, FIXTURE = ROOT / "site", ROOT / "preview", ROOT / "fixture"
SCHEMA = "public-carousel/1"
KINDS = {"figure": ("text", "label"), "pitch": ("text", "label"), "score": ("home", "score", "away", "label")}
HEX = re.compile(r"#[0-9a-fA-F]{6}")
# never public, wherever they appear
PRIVATE_KEYS = {"evidence_span", "passage", "passages", "sources_used", "development_review", "generation",
                "draft_not_ready", "claim_verification", "angle_review", "usage_flags", "content_hash", "article_id",
                "refs", "passage_refs", "evidence_fingerprint", "wikidata", "run_id"}
SECRET = re.compile(r"postgres(ql)?://|gsk_[A-Za-z0-9]|DATABASE_URL|GROQ_API_KEY|\b[A-Za-z]:\\|/Users/", re.I)


class ExportError(Exception):
    pass


def _tokens(s):
    return re.findall(r"[a-z]+|[0-9]+", s.translate(TYPO).lower())


def _check_graphic(i, vis, support):
    """A designed graphic may only show words and numbers that the story's own copy or cited facts contain."""
    if vis.get("kind") not in KINDS:
        raise ExportError(f"slide {i}: unknown graphic kind {vis.get('kind')!r}")
    for field in KINDS[vis["kind"]]:
        for t in _tokens(vis.get(field) or ""):
            if t not in support and not (t.endswith("s") and t[:-1] in support):
                raise ExportError(f"slide {i}: graphic {field} '{t}' is not in the story copy or its cited facts")


def _https(url):
    if not (isinstance(url, str) and url.startswith("https://")):
        raise ExportError(f"source link is not https: {url!r}")
    return url


def _scan(obj, path="package"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PRIVATE_KEYS:
                raise ExportError(f"private field {path}.{k} in public package")
            _scan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            _scan(v, f"{path}[{n}]")
    elif isinstance(obj, str) and SECRET.search(obj):
        raise ExportError(f"secret-like or local-path text at {path}")


def public_package(pkg, pres, review, photo, fixture=False, draft=False):
    slides, visuals = pkg.get("slides") or [], pres.get("visuals") or []
    if len(slides) != 5 or len(visuals) != 5:
        raise ExportError(f"need exactly 5 slides and 5 graphics, got {len(slides)} and {len(visuals)}")
    theme = pres.get("theme") or {}
    if not all(HEX.fullmatch(theme.get(k) or "") for k in ("panel", "accent")) or \
            not (theme.get("strip") and all(HEX.fullmatch(c) for c in theme["strip"])):
        raise ExportError("theme needs hex colours for panel, accent and strip")
    facts = {f["id"]: f for f in pkg["facts"]}
    expl = pkg.get("explanation") or {}
    cited = list(dict.fromkeys([i for s in slides for i in s["fact_ids"]] + expl.get("fact_ids", [])))
    if not expl.get("text") or not cited or any(i not in facts for i in cited):
        raise ExportError("explanation missing or slides cite facts the package does not hold")
    if not fixture and (bad := failed_cited_facts(pkg)):
        raise ExportError(f"cited fact(s) {', '.join(bad)} did not pass verification; failed evidence is never public")
    support = set(_tokens(" ".join([pkg["title"], expl["text"]] + [s["heading"] + " " + s["body"] for s in slides]
                                   + [facts[i]["statement"] for i in cited])))
    for n, v in enumerate(visuals, 1):
        _check_graphic(n, v, support)
        if v.get("photo") and not fixture:
            raise ExportError(f"slide {n}: no reviewed photo record allows a photo in a real export")
    a, sc, gen = pkg["athlete"], pkg["screening"], pkg.get("generation") or {}
    manual = gen.get("provider") == "manual"
    rounds = (pkg.get("claim_verification") or {}).get("rounds") or []
    claims = rounds[-1]["claims"] if rounds else []
    if photo is None:
        photo_reason = (pkg.get("photo") or {}).get("reason") or "No photo was sourced; the slides use designed graphics."
    elif photo["outcome"] == "candidate_shortlisted":
        photo_reason = ("A licence-screened Wikimedia Commons candidate was shortlisted but not reviewed for this story; "
                        "the slides use designed graphics.")
    else:
        photo_reason = (f"A bounded Wikimedia Commons lookup ({photo['outcome'].replace('_', ' ')}) found no photo whose "
                        "own metadata supports the athlete, this match, a reusable licence and a credit. The slides use "
                        "designed graphics instead.")
    pub = {
        "schema": SCHEMA, "fixture": fixture, "draft": draft, "package_id": pkg["package_id"], "title": pkg["title"], "language": "en",
        "athlete": {"name": a["name"], "sport": a["sport"], "represents": a["represents"]},
        "theme": {k: theme[k] for k in ("panel", "accent", "strip")},
        "slides": [{"heading": s["heading"], "body": s["body"], "fact_ids": s["fact_ids"],
                    "visual": {k: v[k] for k in (*KINDS[v["kind"]], "kind", "photo") if v.get(k)}}
                   for s, v in zip(slides, visuals)],
        "facts": [{"id": i, "statement": facts[i]["statement"],
                   "slides": [n for n, s in enumerate(slides, 1) if i in s["fact_ids"]],
                   "source": {"publisher": facts[i]["source"]["publisher"], "url": _https(facts[i]["source"]["url"]),
                              "attribution": facts[i]["source"]["attribution"],
                              **({"licence": facts[i]["licence"]} if facts[i].get("licence") else {})}} for i in cited],
        "explanation": {"text": expl["text"], "fact_ids": expl["fact_ids"],
                        "label": ("Editorial explanation written with the " + ("manually revised copy" if manual else
                                  "generated copy") + " from the listed facts. It is not the screening model's "
                                  "reasoning and does not prove the angle is original.")},
        "how_chosen": _how_chosen(pkg, a, sc, manual) if pkg.get("angle_proposal") else {
            "mode": "editorially_assisted",
            "athlete": (f"System-ranked: the local Laya screen placed {a['name']} at rank {sc.get('rank')} "
                        f"(raw score {sc['score_raw_uncalibrated']}, uncalibrated)."),
            "angle": ("Editorially chosen: an editor reviewed the evidence and approved this angle and its facts. The "
                      "screen did not identify this story." if pkg.get("angle_review") else
                      "Generated from the screened evidence."),
            "copy": ("Manually revised: an editor wrote the final slide copy from the reviewed facts only; each claim "
                     "was then checked against its cited passages." if manual else "Generated by Groq and checked."),
            "automated_story_selection": ("Not demonstrated. Automated story selection is still open, so this "
                                          "carousel is an editorially assisted result."),
        },
        "screening": {"result": sc["mapped"], "rank": sc.get("rank"), "score_raw_uncalibrated": sc["score_raw_uncalibrated"],
                      "model": f"{sc['model']['repo']} @ {sc['model']['revision'][:7]} (base, zero-shot)",
                      "rubric": sc["rubric_version"],
                      "note": "Raw choice probability from a weak zero-shot base model. Not an approval probability."},
        "verification": {"claims_checked": len(claims),
                         "claims_supported": sum(c.get("verdict") == "supported" for c in claims),
                         "note": "Code guards and a model verifier checked each claim against its cited passages. A "
                                 "review aid, not certification of factual truth."},
        "review": ({"state": "layout fixture, not reviewed"} if fixture else
                   {"state": "awaiting independent review: not for sharing"} if draft else
                   {"state": "accepted", "reviewer": review["reviewer"], "reviewed_at": review["reviewed_at"],
                    "package_sha256": review["package_sha256"], "resolved_blockers": review["resolved_blockers"]}),
        "photo": {"status": "designed_graphics", "reason": photo_reason,
                  **({"lookup_run": photo["run_id"]} if photo else {})},
        "rights": pkg["rights_handling"],
        "caveats": pkg["integration_caveats"],
    }
    _scan(pub)
    return pub


def _how_chosen(pkg, a, sc, manual):
    search = pkg.get("angle_search") or []
    return {
        "mode": "automated",
        "athlete": (f"System-ranked: the local Laya screen placed {a['name']} at rank {sc.get('rank')} "
                    f"(raw score {sc['score_raw_uncalibrated']}, uncalibrated)." if sc.get("rank") else
                    f"System-chosen: {a['name']} is eligible but the local Laya screen did not mark them promising "
                    f"(raw score {sc['score_raw_uncalibrated']}, uncalibrated); tried after the ranked players."),
        "angle": ("System-proposed: the run made one model call per player over that player's stored passages. "
                  "This was the first proposal whose stakes, turning point and significance cited valid source excerpts "
                  "(their exact text attached by code) and whose facts passed the code checks against the stored "
                  "sources."),
        "copy": "Generated from the proposal's facts; each claim was checked against its cited passages by code guards "
                "and a model verifier.",
        "automated_story_selection": ("Automated in this run: no operator supplied the player, angle, facts, copy or "
                                      "graphics. An independent reviewer decides whether it is shared."),
        "search": [{"player": x["name"], "rank": x["rank"], "outcome": x["outcome"].replace("_", " "),
                    "reason": (x.get("reasons") or [""])[0][:200]} for x in search],
    }


# Flag colours for the accent strip (presentation only); anything else gets a neutral pan-Arab strip.
FLAGS = {"Egypt": ["#C8102E", "#FFFFFF", "#111111"], "Iraq": ["#CE1126", "#FFFFFF", "#111111", "#007A3D"],
         "Morocco": ["#C1272D", "#006233"], "Saudi Arabia": ["#006C35", "#FFFFFF"],
         "Algeria": ["#006233", "#FFFFFF", "#D21034"], "Tunisia": ["#E70013", "#FFFFFF"],
         "Qatar": ["#8A1538", "#FFFFFF"], "Jordan": ["#111111", "#FFFFFF", "#007A3D", "#CE1126"]}
SCORE = re.compile(r"\b(\d{1,2}) ?- ?(\d{1,2})\b")
MINUTE = re.compile(r"\b(\d{1,3})(?:st|nd|rd|th)?[- ]minute", re.I)
SPAN = re.compile(r"\b(\d{1,3})[- ](years?|seasons?|caps|goals|matches|games|titles)\b", re.I)
YEAR = re.compile(r"\b(19\d\d|20\d\d)\b")


def _options(heading, body):
    """Graphic options for one slide, best first, taken only from its own checked copy."""
    text = f"{heading}. {body}".translate(TYPO)
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for m in SCORE.finditer(sent):
            home = re.search(r"([A-Z][\w']+)\s+(?:won|beat|defeated)", sent)
            away = re.search(r"(?:against|over|beat|defeated)\s+([A-Z][\w']+)", sent)
            score = f"{m.group(1)}\u2013{m.group(2)}"
            if home and away and home.group(1) != away.group(1):
                out.append({"kind": "score", "home": home.group(1), "score": score, "away": away.group(1)})
            else:
                out.append({"kind": "figure", "text": score})
    out += [{"kind": "pitch", "text": f"{m.group(1)}\u2032"} for m in MINUTE.finditer(text)]
    out += [{"kind": "figure", "text": f"{m.group(1)} {m.group(2).lower()}"} for m in SPAN.finditer(text)]
    out += [{"kind": "figure", "text": m.group(1)} for m in YEAR.finditer(text)]
    return out


def auto_presentation(pkg):
    """Theme and one designed graphic per slide, chosen from the checked copy: a score, a match minute on pitch
    markings, a figure (span, count or year), else the heading as a typographic panel. No figure repeats."""
    nation = (pkg["athlete"].get("represents") or [""])[0]
    strip = FLAGS.get(nation, ["#111111", "#FFFFFF", "#007A3D", "#CE1126"])
    accent = next(c for c in strip + ["#C8102E"] if c not in ("#FFFFFF", "#111111"))
    used, visuals = set(), []
    for sl in pkg["slides"]:
        pick = next((o for o in _options(sl["heading"], sl["body"]) if (o.get("score") or o["text"]) not in used),
                    {"kind": "figure", "text": sl["heading"]})
        used.add(pick.get("score") or pick["text"])
        if pick.get("text") != sl["heading"]:
            pick["label"] = sl["heading"]
        visuals.append(pick)
    return {"theme": {"panel": "#0E6B3B", "accent": accent, "strip": strip}, "visuals": visuals,
            "source": "chosen automatically from the checked copy"}


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def render_pdf(folder, package_id, out):
    """Five-page portrait PDF of one package from a built static folder (the same renderer in print mode), via
    headless Edge against a temporary local static server. Returns the PDF path."""
    import functools
    import http.server
    import subprocess
    import tempfile
    import threading
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(folder))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
                            f"--user-data-dir={profile}", "--virtual-time-budget=8000", f"--print-to-pdf={out}",
                            f"http://127.0.0.1:{srv.server_address[1]}/?package={package_id}&print=1"],
                           check=True, timeout=180, capture_output=True)
    finally:
        srv.shutdown()
    if not out.exists() or out.stat().st_size < 1000:
        raise ExportError(f"PDF was not written: {out}")
    return out


def preview(pkg, pres, photo, folder):
    """Review copy of a stored (unaccepted) package: site app files + a draft-labelled public file, never the site."""
    shutil.rmtree(folder, ignore_errors=True)
    shutil.copytree(SITE, folder, ignore=shutil.ignore_patterns("data"))
    return write(public_package(pkg, pres, None, photo, draft=True), folder)


def write(pub, site=SITE):
    """Write the package and replace its index entry. Fixture content can never be written into the deployable site."""
    site = pathlib.Path(site)
    if (pub["fixture"] or pub.get("draft")) and site.resolve() == SITE.resolve():
        raise ExportError("layout fixtures and unreviewed drafts cannot be written to the deployable site")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", pub["package_id"]):
        raise ExportError("unsafe package id")
    (site / "data" / "packages").mkdir(parents=True, exist_ok=True)
    rel = f"data/packages/{pub['package_id']}.json"
    (site / rel).write_text(json.dumps(pub, indent=1, ensure_ascii=False), encoding="utf-8")
    idx_path = site / "data" / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"packages": []}
    entry = {"package_id": pub["package_id"], "title": pub["title"], "athlete": pub["athlete"]["name"], "path": rel}
    idx = {"schema": "public-index/1",
           "packages": [entry] + [p for p in idx["packages"] if p["package_id"] != pub["package_id"]]}
    idx_path.write_text(json.dumps(idx, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"package_id": pub["package_id"], "site": str(site), "file": rel}


def fixture_preview():
    """Local layout preview: the site's app files + the labelled fixture (plus a missing and an invalid entry to show
    the error states). Never deployed."""
    shutil.rmtree(PREVIEW, ignore_errors=True)
    shutil.copytree(SITE, PREVIEW, ignore=shutil.ignore_patterns("data"))
    pkg = json.loads((FIXTURE / "layout_fixture.json").read_text(encoding="utf-8"))
    pres = json.loads((FIXTURE / "layout_presentation.json").read_text(encoding="utf-8"))
    out = write(public_package(pkg, pres, None, None, fixture=True), PREVIEW)
    (PREVIEW / "data" / "packages" / "fixture-invalid.json").write_text('{"schema": "something-else"}', encoding="utf-8")
    idx_path = PREVIEW / "data" / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    idx["packages"] += [{"package_id": "fixture-missing", "title": "missing file", "athlete": "-",
                         "path": "data/packages/fixture-missing.json"},
                        {"package_id": "fixture-invalid", "title": "invalid file", "athlete": "-",
                         "path": "data/packages/fixture-invalid.json"}]
    idx_path.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return {**out, "preview": str(PREVIEW)}
