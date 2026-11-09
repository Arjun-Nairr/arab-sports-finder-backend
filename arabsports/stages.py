"""Stages over the stored library (no discovery): T03 screening/selection, T04 composition."""
import json
import secrets
import time

import psycopg
from psycopg.types.json import Jsonb

from . import screen
from . import sources as src
from . import store
from .config import RUNS_DIR, public_config, settings
from .run import safe

LAYA_MODEL = {"repo": screen.LAYA_REPO, "revision": screen.LAYA_REVISION, "checkpoint": "English base (repo root)",
              "runtime": screen.LAYA_RUNTIME, "device": "cpu", "max_len": screen.MAX_LEN,
              "head_max_len": screen.HEAD_MAX_LEN, "state_tokens_per_window": screen.STATE_TOKENS}


def _open(mode, schema):
    s = settings()
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2)
    out_dir = RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    if not s["database_url"]:
        return s, run_id, out_dir, None, "DATABASE_URL not set"
    try:
        conn = store.connect(s["database_url"], schema)
    except psycopg.Error as e:
        return s, run_id, out_dir, None, safe(e)
    store.start_run(conn, run_id, mode, public_config(s))
    return s, run_id, out_dir, conn, None


def _close(conn, run_id, out_dir, name, report, integrations, counts, outcome, notes):
    if conn:
        store.finish_run(conn, run_id, integrations, counts, outcome, notes)
        conn.close()
    (out_dir / name).write_text(report, encoding="utf-8")
    return {"run_id": run_id, "outcome": outcome, "report": str(out_dir / name), "counts": counts}


# ---------- T03 ----------

def screen_run(schema=None):
    s, run_id, out_dir, conn, err = _open("screen", schema)
    integrations, notes, counts = {}, [], {}
    if not conn:
        integrations["neon"] = src.outcome("failed" if s["database_url"] else "not_attempted", err)
        return _close(None, run_id, out_dir, "screening.md", _screen_report(run_id, integrations, counts, [], None,
                      "Neon unavailable; nothing screened", notes), integrations, counts, "blocked", notes)
    L = s["limits"]
    integrations["neon"] = src.outcome("worked", "connected")
    records, selected, reason, outcome = [], None, "", "failed"
    try:
        cands, without = store.screening_candidates(conn, L["screen_candidates"])
        need = [a["source_id"] for a in cands if a["source"] == "wikidata" and "wikidata_roles" not in a["external"]]
        if need:
            roles, integrations["wikidata_roles"] = src.fetch_wikidata_roles(need, L)
            for a in cands:
                if a["source_id"] in roles:
                    a["external"]["wikidata_roles"] = {**roles[a["source_id"]], "run": run_id}
                    store.set_enrichment(conn, a["id"], "wikidata_roles", a["external"]["wikidata_roles"])
        else:
            integrations["wikidata_roles"] = src.outcome("not_attempted", "roles already stored for all candidates")

        wb, cfg_fp, laya_calls = screen.Windows(), screen.config_fingerprint(), 0
        for a in cands:
            pkt = screen.packet_for(a, a["articles"], wb)
            pid, _ = store.upsert_packet(conn, pkt, run_id)
            cached = store.cached_screening(conn, pid, cfg_fp)
            if cached:
                sid, mapped, score, warnings, reused = cached["id"], cached["mapped"], cached["score"], cached["warnings"], True
            else:
                raw, mapped, score, warnings = screen.screen_packet(pkt)
                laya_calls += len(pkt["windows"])
                sid = store.insert_screening(conn, pid, cfg_fp, LAYA_MODEL, screen.RUBRIC_VERSION, raw, mapped, score,
                                             warnings, run_id)
                reused = False
            records.append({"athlete_id": a["id"], "name": a["name"], "packet_id": pid, "packet_fp": pkt["packet_fp"],
                            "screening_id": sid, "reused_screening": reused, "mapped": mapped, "score": score,
                            "n_articles": len({w["article_id"] for w in pkt["windows"]}), "windows": len(pkt["windows"]),
                            "warnings": warnings, "evidence_version": a["evidence_version"], "_athlete": a})

        # Eligibility is separate from narrative. For promising candidates whose only gap is representation,
        # a bounded permitted Ajel Wire search looks for national-team reports.
        searches = 0
        for r in records:
            a = r.pop("_athlete")
            roles = a["external"].get("wikidata_roles")
            extra_articles = []
            el = screen.eligibility(a, roles, a["articles"])
            if (r["mapped"] == "promising" and el["reasons"] == ["no sourced statement or report of representing a listed nation"]
                    and searches < L["representation_searches"] and s["ajel_key"]):
                searches += 1
                q = f"{a['name']} national team"
                wraw, res = src.fetch_ajel_wire({**L, "ajel_wire_articles": 3}, s["ajel_key"], [(a["source_id"], q)])
                integrations[f"ajel_wire_representation_{a['id']}"] = res
                ids = [i for i, _, _ in store.upsert_articles(conn, src.parse_ajel_wire(wraw), run_id)]
                extra_articles = store.article_rows(conn, ids)
                el = screen.eligibility(a, roles, a["articles"] + extra_articles)
                el["representation_search"] = {"q": q, "articles": ids, "outcome": res["status"]}
            r.update({"eligible": el["eligible"], "eligibility": el})
        ordered, selected = screen.order_candidates(records)
        for i, r in enumerate(ordered, 1):
            r["rank"] = i
        n_prom = sum(r["mapped"] == "promising" for r in records)
        if selected:
            reason = f"selected athlete {selected['athlete_id']} ({selected['name']}), rank 1 of {len(ordered)} eligible promising. {screen.ORDERING_DOC}"
        else:
            reason = (f"no selection: {len(records)} screened, {n_prom} promising, {len(ordered)} eligible promising. "
                      + "; ".join(f"{r['name']}: {r['mapped']}" + (f" / not eligible: {', '.join(r['eligibility']['reasons'])}"
                                                                  if not r["eligible"] else "") for r in records))
        store.save_selection(conn, run_id, records, selected, reason)
        integrations["laya"] = (src.outcome("worked", f"{laya_calls} window inferences on CPU")
                                if laya_calls else src.outcome("not_attempted", "all packets reused cached screenings"))
        counts = {"candidates_screened": len(records), "without_evidence_not_screened": without,
                  "new_screenings": sum(not r["reused_screening"] for r in records),
                  "reused_screenings": sum(r["reused_screening"] for r in records), "promising": n_prom,
                  "eligible_promising": len(ordered), "selected_athlete_id": selected and selected["athlete_id"]}
        outcome = "selected" if selected else "no_selection"
    except Exception as e:  # Laya or storage failure: visible stop, no fallback classifier or handpicked athlete
        notes.append(f"screening stopped: {safe(e)}")
        integrations.setdefault("laya", src.outcome("failed", safe(e)))
        reason = "screening failed; no selection"
    return _close(conn, run_id, out_dir, "screening.md",
                  _screen_report(run_id, integrations, counts, records, selected, reason, notes),
                  integrations, counts, outcome, notes)


# ---------- Wikipedia biographies (background evidence) ----------

def biography_run(schema=None):
    """Bounded import of English Wikipedia biographies for discovered athletes, via their verified sitelinks.
    New: up to WIKIPEDIA_BIOGRAPHIES unprocessed athletes in discovery order. Refresh: WIKIPEDIA_REFRESH imported
    ones, least recently checked; an unchanged revision is reused without fetching. Failures change nothing."""
    from . import wikipedia as wp
    s, run_id, out_dir, conn, err = _open("biographies", schema)
    integrations, notes, counts, rows = {}, [], {}, []
    if not conn:
        integrations["neon"] = src.outcome("failed" if s["database_url"] else "not_attempted", err)
        return _close(None, run_id, out_dir, "biographies.md", f"# Biography run {run_id}\n\nBlocked: {err}\n",
                      integrations, counts, "blocked", notes)
    L = s["limits"]
    t, retries, cap = L["http_timeout_s"], L["wikipedia_retries"], L["wikipedia_max_requests"]
    integrations["neon"] = src.outcome("worked", "connected")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new, none, old, pending = store.bio_targets(conn, L["wikipedia_biographies"], L["wikipedia_refresh"])
    for aid, qid, name, _ in none:
        store.set_enrichment(conn, aid, "wikipedia_bio", {"status": "no_sitelink", "run": run_id, "checked_at": now})
        rows.append({"athlete_id": aid, "qid": qid, "name": name, "outcome": "no_sitelink"})
    targets = [(r, None) for r in new] + [(r[:4], r[4]) for r in old]
    requests, affected, deferred = 0, set(), []
    outcome = "completed"
    try:
        idents, rights = {}, {}
        if targets:
            requests += 1
            idents, rights = wp.identify([r[3] for r, _ in targets], t, retries)
        for (aid, qid, name, title), prev in targets:
            ident = idents[title]
            status = wp.classify(qid, ident)
            row = {"athlete_id": aid, "qid": qid, "name": name, "requested_title": title, "kind": "refresh" if prev else "new",
                   "redirects": ident["redirects"], "page": (ident["page"] or {}).get("title")}
            rows.append(row)
            if status != "verified":
                row["outcome"] = status
                if prev:  # stored evidence stays; the problem is recorded for review
                    store.set_enrichment(conn, aid, "wikipedia_bio", {**prev, "checked_at": now, "latest_check": {
                        "status": status, "run": run_id, "note": "stored biography kept; not withdrawn automatically"}})
                else:
                    store.set_enrichment(conn, aid, "wikipedia_bio", {"status": status, "run": run_id, "checked_at": now,
                                                                      "title": title, "redirects": ident["redirects"]})
                continue
            revid = ident["page"]["revisions"][0]["revid"]
            if prev and prev.get("revision") == revid and prev.get("parser") == wp.PARSER_VERSION:
                row["outcome"] = "unchanged_revision_reused"
                store.set_enrichment(conn, aid, "wikipedia_bio", {**prev, "checked_at": now, "latest_check": None})
                continue
            cache_file = wp.CACHE / f"{int(revid)}.html"
            if requests >= cap and not cache_file.exists():
                row["outcome"] = "deferred_request_cap"
                deferred.append(aid)
                continue
            try:
                html_text, cached = wp.revision_html(revid, t, retries)
            except src.FetchError as e:
                row["outcome"] = f"fetch_failed: {e}"  # nothing stored or marked; retried next run
                continue
            requests += 0 if cached else 1
            sections, citations = wp.parse(html_text)
            if not sections:
                row["outcome"] = "no_prose"
                store.set_enrichment(conn, aid, "wikipedia_bio", {"status": "no_prose", "run": run_id, "checked_at": now,
                                                                  "revision": revid})
                continue
            lead = wp.bio_lead(qid, ident, sections, citations, rights)
            ((art_id, st, _),) = store.upsert_articles(conn, [lead], run_id)
            store.upsert_mentions(conn, art_id, [wp.mention({"id": aid, "name": name, "source_id": qid}, lead)], run_id)
            if st in ("new", "changed"):
                affected.add(aid)
            row.update({"outcome": f"imported_{st}", "article_id": art_id, "revision": revid, "cached": cached,
                        "sections": len(sections), "paragraphs": sum(len(x["paragraphs"]) for x in sections),
                        "citations": len(citations), "chars": len(lead["content"])})
            store.set_enrichment(conn, aid, "wikipedia_bio", {"status": "imported", "run": run_id, "checked_at": now,
                                                              "article_id": art_id, "pageid": ident["page"]["pageid"],
                                                              "title": ident["page"]["title"], "revision": revid,
                                                              "parser": wp.PARSER_VERSION, "latest_check": None})
        integrations["wikipedia"] = src.outcome("worked" if targets else "not_attempted",
                                                f"{requests} request(s) of cap {cap}, {retries} retry each")
    except (src.FetchError, ValueError, KeyError) as e:  # identity request failed or malformed: nothing changed
        integrations["wikipedia"] = src.outcome("failed", safe(e))
        outcome = "completed_with_source_failures"
    integrations["wikipedia"]["rows"] = rows
    changes = store.refresh_evidence(conn, affected)
    counts = {"requests": requests, "request_cap": cap, "no_sitelink": len(none),
              "outcomes": {o: sum(r.get("outcome") == o for r in rows) for o in sorted({r.get("outcome") for r in rows if r.get("outcome")})},
              "evidence_version_changes": len(changes), "deferred_request_cap": len(deferred),
              "unprocessed_with_sitelink_remaining": pending + len(deferred)
              + sum(1 for r in rows if str(r.get("outcome", "")).startswith("fetch_failed"))}
    report = [f"# Biography run {run_id}", "", "Background evidence from English Wikipedia (not news; no publication "
              "dates). Identity: Wikidata enwiki sitelink, checked against pageprops.wikibase_item after redirects.", "",
              *(f"- {k}: **{v['status']}** {v['reason']}" for k, v in integrations.items()), "",
              "- Licence handling: " + " ".join(wp.LICENCE_NOTES),
              f"- Licence (siteinfo): {rights.get('text') if targets else '-'} {rights.get('url') if targets else ''}", "",
              "## Counts", "", *(f"- {k}: {v}" for k, v in counts.items()), "",
              "| athlete | QID | kind | requested title | redirects | outcome | revision | sections/paras/cites |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        report.append(f"| {r['athlete_id']} {r['name']} | {r['qid']} | {r.get('kind', '-')} | {r.get('requested_title', '-')} | "
                      f"{' -> '.join(h['to'] for h in r.get('redirects', [])) or '-'} | {r.get('outcome')} | "
                      f"{r.get('revision', '-')} | {r.get('sections', '-')}/{r.get('paragraphs', '-')}/{r.get('citations', '-')} |")
    report += ["", "## Evidence version changes", ""] + ([f"- {c['name']} (id {c['athlete_id']}): v{c['from_version']} -> "
                                                          f"v{c['to_version']}, {c['evidence_status']}" for c in changes] or ["- none"])
    return _close(conn, run_id, out_dir, "biographies.md", "\n".join(report) + "\n", integrations, counts, outcome, notes)


# ---------- T04 ----------

RIGHTS_NOTE = ("Ajel policy (https://wire.ajel.sa/wire/en/policy, read 2026-11-06) permits full or summarised "
               "republication and AI-system use with Ajel cited plus the story link, and forbids distortion, misleading "
               "excerpts and hiding the source. Wire flags say modification_allowed=false. Handling: slides restate "
               "code-checked facts in new words with attribution and links, reproduce no source sentence (8-word check) "
               "and add no quotes. This needs human review and is not a legal clearance.")
WIKI_RIGHTS_NOTE = ("Wikipedia biography sources are CC BY-SA 4.0 (https://en.wikipedia.org/wiki/"
                    "Wikipedia:Reusing_Wikipedia_content): slides adapted from them need attribution with a link to the "
                    "revision, a licence notice and CC BY-SA licensing of the adapted text. Wikipedia is background "
                    "(tertiary), not news, and is not independent of Wikidata. Images and cited sources are not covered.")


def generate_run(schema=None, chat_factory=None):
    from . import compose
    s, run_id, out_dir, conn, err = _open("generate", schema)
    integrations, notes, counts = {}, [], {}
    if not conn:
        integrations["neon"] = src.outcome("failed" if s["database_url"] else "not_attempted", err)
        return _close(None, run_id, out_dir, "carousel.md", f"# Generation run {run_id}\n\nBlocked: {err}\n",
                      integrations, counts, "blocked", notes)
    L = s["limits"]
    integrations["neon"] = src.outcome("worked", "connected")
    sel = store.latest_selection(conn)
    if not sel or not sel["athlete_id"]:
        reason = sel["reason"] if sel else "no screening run stored"
        integrations["groq"] = src.outcome("not_attempted", "no T03 selection to generate from")
        return _close(conn, run_id, out_dir, "carousel.md",
                      f"# Generation run {run_id}\n\n**Blocked at T03: no selected candidate.** Latest selection "
                      f"({sel and sel['run_id']}): {reason}\n\nNo Groq call was made and no package was saved.\n",
                      integrations, {"selection_run": sel and sel["run_id"]}, "no_selection", notes)

    usage_log, status, result, model = [], None, None, s["groq_model"]
    athlete = conn.execute("SELECT id, name, source_id, sports, representation, external, evidence_version, "
                           "evidence_fingerprint FROM athletes WHERE id = %s", (sel["athlete_id"],)).fetchone()
    athlete = dict(zip(("id", "name", "source_id", "sports", "representation", "external", "evidence_version",
                        "evidence_fingerprint"), athlete))
    cand = next(c for c in sel["candidates"] if c["athlete_id"] == athlete["id"])
    rows, _ = store.screening_candidates_for(conn, athlete["id"])
    # A biography is too long for one request: T04 reads the passages that were screened plus the representation
    # passages (verbatim from the stored revision), and records that reduction.
    packet, screened_cfg = conn.execute("SELECT p.content, s.config_fp FROM screenings s JOIN packets p ON p.id = s.packet_id "
                                        "WHERE s.id = %s", (cand["screening_id"],)).fetchone()
    stale = stale_reasons(athlete, packet, screened_cfg, rows)
    if stale:  # never combine a stale selection with current sources: rescreen first
        integrations["groq"] = src.outcome("not_attempted", "selected screening is stale; rescreen required")
        package = build_package(run_id, athlete, {"wikidata": athlete["external"].get("wikidata_url"), "sport": [],
                                                  "represents": [], "occupations": []}, cand, sel, [], [], None,
                                "stale_selection", model, usage_log, notes + stale)
        store.save_package(conn, package, "stale_selection")
        return _close(conn, run_id, out_dir, "carousel.md", carousel_md(package), integrations,
                      {"status": "stale_selection", "reasons": stale, "groq_calls": 0}, "stale_selection", notes + stale)
    # Story gate: the screened evidence above is the discovery record; drafting uses only the passages an independent
    # reviewer cited for an approved angle on exactly this selection.
    angle, rows, blocked, why = angle_gate(conn, sel)
    if blocked:
        integrations["groq"] = src.outcome("not_attempted", f"{blocked}: no usable reviewed angle")
        return _close(conn, run_id, out_dir, "carousel.md",
                      f"# Generation run {run_id}\n\n**Blocked: {blocked}.**\n\n" + "".join(f"- {w}\n" for w in why)
                      + "\nNo Groq call was made and no package was saved.\n", integrations,
                      {"status": blocked, "reasons": why, "groq_calls": 0, "angle_review": angle and angle["id"]},
                      blocked, notes + why)
    return _draft(conn, s, run_id, out_dir, sel, cand, athlete, angle, rows,
                  lambda: _make_chat(s, model, L, usage_log, chat_factory, integrations), usage_log,
                  integrations, notes)


def _make_chat(s, model, L, usage_log, chat_factory, integrations, pacer=None):
    """pacer: a batch's shared Groq rate-limit tracker (token/request window carries over between child runs);
    the call log, call cap and wait budget stay per run."""
    from . import compose
    if pacer:
        pacer.waited = 0.0  # GROQ_MAX_WAIT_S is a per-run budget
    if chat_factory:
        integrations["groq"] = src.outcome("not_attempted", "FIXTURE chat provider (checks only)")
        return chat_factory(usage_log)
    if not s["groq_key"]:
        raise compose.ProviderError("blocked", "GROQ_API_KEY not set")
    if model not in compose.list_models(s["groq_key"]):
        raise compose.ProviderError("blocked", f"model {model} not in account model list")
    return compose.groq_chat(s["groq_key"], model, L, usage_log, pacer=pacer)


def _draft(conn, s, run_id, out_dir, sel, cand, athlete, angle, rows, get_chat, usage_log, integrations, notes,
           auto=None, reopen=None):
    """Shared T04 drafting from an angle whose facts are tied to stored passages: the reviewed angle (generate) or
    the validated system proposal (auto, which also passes its angle-search record). Saves exactly one package."""
    from . import compose
    L, model, result, status, provider_error = s["limits"], s["groq_model"], None, None, None
    label = f"angle proposal {angle['id']}" if auto else f"angle review {angle['id']}"
    for a in rows:
        a["content"] = "\n\n".join(a["passages"])
        a["reduction"] = f"{len(a['passages'])} passage(s) cited in {label}; the rest of this source was not sent"
    sources, usage = [], []
    for i, a in enumerate(rows, 1):
        obs = a["extra"].get("observations") or {}
        attribution = next((o["attribution"].get("text") for o in obs.values()
                            if isinstance(o.get("attribution"), dict) and o["attribution"].get("text")), None)
        flags = (a["usage_terms"] or {}).get("flags", {})
        usage.append({"url": a["canonical_url"], "publisher": a["publisher"], "flags": flags,
                      "excluded": flags.get("republication_allowed") is False, "reduction": a["reduction"],
                      **({"revision_url": a["extra"]["revision"]["url"]} if a["extra"].get("revision") else {}),
                      **({"licence": a["extra"]["license"]["text"]} if a["extra"].get("license") else {}),
                      **({"terms": a["extra"]["terms"]} if a["extra"].get("terms") else {})})
        if flags.get("republication_allowed") is False:
            continue
        sources.append({"sid": f"S{i}", "url": a["canonical_url"], "publisher": a["publisher"], "text": a["content"],
                        "attribution": attribution or f"Source: {a['publisher']}", "usage_flags": flags,
                        "title": a["title"], "provider": a["provider"], "article_id": a["id"],
                        "content_hash": a["content_hash"], "reduction": a.get("reduction"),
                        "revision": (a["extra"].get("revision") or {}).get("url")})
    # Reviewed facts: each keeps its id and is tied to its exact stored passages (already hash-checked by the gate).
    reviewed = None
    if angle["angle"].get("facts"):
        refs, sid = {r["id"]: r for r in angle["refs"] if r.get("id")}, {s["article_id"]: s["sid"] for s in sources}
        reviewed = [{"id": f["id"], "statement": f["statement"].strip(),
                     "source": sid.get(refs[f["refs"][0]]["article_id"]),
                     "evidence": " ".join(refs[x]["passage"] for x in f["refs"]), "refs": list(f["refs"])}
                    for f in angle["angle"]["facts"]]
        for f in reviewed:
            f["passage"] = f["evidence"]
        if missing := [f["id"] for f in reviewed if not f["source"]]:
            why = [f"reviewed fact(s) {', '.join(missing)} cite a source that cannot be sent (republication not allowed)"]
            integrations["groq"] = src.outcome("not_attempted", "angle_evidence_changed: reviewed facts lack a usable source")
            return _close(conn, run_id, out_dir, "carousel.md", f"# Generation run {run_id}\n\n**Blocked.** {why[0]}\n",
                          integrations, {"status": "angle_evidence_changed", "reasons": why, "groq_calls": 0,
                                         "angle_review": angle["id"]}, "angle_evidence_changed", notes + why)
    roles = athlete["external"].get("wikidata_roles") or {}
    profile = {"name": athlete["name"], "wikidata": athlete["external"].get("wikidata_url"),
               "sport": [x["label"] for x in athlete["sports"]], "date_of_birth": athlete["external"].get("dob"),
               "occupations": roles.get("occupations", []),
               "represents": [r["nation"] for r in athlete["representation"] if r.get("listed")],
               # compact for the prompt; the full evidence (passages, citations, uncertainty) stays in the selection
               "representation_evidence": [{k: e[k] for k in ("route", "nation", "section", "sentence", "url") if k in e}
                                           for e in cand["eligibility"]["representation_evidence"]]}
    coverage = [r[0] for r in conn.execute(
        "SELECT title FROM articles WHERE search_text LIKE %s AND content_kind <> 'withdrawn' ORDER BY id LIMIT 20",
        (f"% {athlete['name'].lower()} %",))]
    angle_ctx = {**angle["angle"], "review_id": angle["id"], "reviewer": angle["reviewer"]}
    if auto:
        angle_ctx.update(source="angle_proposal", label="Proposed story angle (its references were checked against "
                         "the stored passages)", coverage_note="System-proposed angle (one Groq call over stored "
                         "passages, references validated by code); not checked for novelty against other coverage.")
    try:
        chat = get_chat()
        live = "groq" not in integrations  # a FIXTURE chat records itself as not_attempted
        result = compose.compose({"athlete": {"name": athlete["name"], "profile": profile}, "sources": sources,
                                  "coverage": coverage, "reviewed_facts": reviewed, "approved_angle": angle_ctx},
                                 chat, L, calls_used=lambda: len(usage_log))
        # A draft that passed every check is only awaiting independent review; nothing here approves it.
        status = "awaiting_review" if result["status"] == "draft_ready" else result["status"]
        if live:
            integrations["groq"] = src.outcome("worked", f"{len(usage_log)} calls to {model}")
    except compose.ProviderError as e:
        status = (e.kind if e.kind in ("blocked", "malformed_output", "provider_failed", "call_limit", "request_too_large",
                                       "daily_limit", "pacing_wait_exceeded", "verification_budget_exhausted")
                  else "quota_or_rate_limited")
        notes.append(str(e))
        provider_error = str(e) if e.kind in compose.PROVIDER_STOPS else None
        integrations.setdefault("groq", src.outcome("failed" if status != "blocked" else "not_attempted", str(e)))
    package = build_package(run_id, athlete, profile, cand, sel, sources, usage, result, status, model, usage_log, notes)
    package["acceptance_blockers"] = acceptance_blockers(sources)
    ranked = (f"{'system-ranked' if cand.get('rank') else 'system-chosen'}: Laya/selection run {sel['run_id']}, "
              f"{_rank(cand.get('rank'))}, screening "
              f"{cand['screening_id']} (score {cand['score']}, raw and uncalibrated)")
    discovery = "the screened windows (and their sources) stay in the screening/selection record"
    if auto:
        _auto_fields(package, angle, status, auto, model, ranked, discovery, notes)
    else:
        package["angle_review"] = {**{k: angle[k] for k in ("id", "decision", "reviewer", "selection_run",
                                                             "screening_id", "angle", "refs")},
                                   "created_at": str(angle["created_at"])}
        package["selection_basis"] = {
            "athlete": ranked,
            "angle": f"editorially chosen: angle review {angle['id']} by {angle['reviewer']}; Laya did not identify "
                     f"this story",
            "discovery_evidence": discovery + " and are not drafting evidence",
            "automated_story_selection": "not demonstrated (T03 story-selection criterion stays open)"}
    last = ((result or {}).get("verification") or [{}])[-1]
    counts = {"status": status, "review_state": "awaiting_review" if status == "awaiting_review" else "not_applicable",
              "groq_calls": len(usage_log), "facts_kept": len(result["facts"]) if result else 0,
              "facts_dropped": len(result["dropped_facts"]) if result else 0, "revisions": result and result["revisions"],
              "verification_rounds": len((result or {}).get("verification") or []),
              "unresolved_claims": last.get("unresolved")}
    if provider_error := provider_error or (result or {}).get("provider_error"):
        counts["provider_error"] = provider_error  # the draft is kept; a batch stops on this
    if auto:
        counts["angle_attempts"] = [f"{a['name']} ({_rank(a['rank'])}): {a['outcome']}" for a in auto["attempts"]]
        counts.update(_tried(auto["attempts"]), package_id=package["package_id"])
    # Model output is saved locally before any database write, so a dropped connection cannot lose it.
    saved = _save_local(out_dir, "draft_result.json", {"run_id": run_id, "status": status, "counts": counts,
                                                       "call_log": usage_log, "package": package})
    step = "reconnect to Neon after the model calls"
    try:
        if reopen:  # minutes of Groq pacing can leave the earlier connection idle long enough to be dropped
            conn = _swap(conn, reopen)
        step = "read the previous attempt"
        prev = conn.execute("SELECT package_id, status, review_state, package_sha256 FROM story_packages WHERE "
                            "athlete_id = %s AND screening_id = %s ORDER BY created_at DESC LIMIT 1",
                            (athlete["id"], cand["screening_id"])).fetchone()
        package["previous_attempt"] = (dict(zip(("package_id", "status", "review_state", "package_sha256"), prev)) | {
            "relationship": "earlier attempt for the same selection and screening; kept unchanged"} if prev else None)
        step = "save the package"
        counts["package_sha256"] = sha = store.save_package(conn, package, status)
        step = "close the run record"
        return _close(conn, run_id, out_dir, "carousel.md", carousel_md(package, sha), integrations, counts, status, notes)
    except psycopg.Error as e:
        return _db_failed(conn, run_id, out_dir, step, e, [saved], integrations, counts, notes)


def _save_local(out_dir, name, obj):
    path = out_dir / name
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def _swap(conn, reopen):
    """Close a possibly dropped connection (errors ignored) and open a fresh one for the next writes."""
    try:
        conn.close()
    except Exception:  # already closed or dropped by the server
        pass
    return reopen()


def _db_failed(conn, run_id, out_dir, step, err, saved, integrations, counts, notes):
    """Neon write failed even on a fresh connection: close locally, name the failed step and the saved files."""
    try:
        conn.close()
    except Exception:
        pass
    why = f"database write failed at step '{step}': {safe(err)}"
    integrations["neon"] = src.outcome("failed", why)
    counts.update(failed_step=step, saved_files=[str(p) for p in saved])
    report = (f"# Run {run_id}\n\n**db_write_failed**: {why}\n\nThe run record in Neon is left unfinished. Model output "
              "was saved locally before the write:\n\n" + "".join(f"- {p}\n" for p in saved))
    return _close(None, run_id, out_dir, "db_write_failed.md", report, integrations, counts, "db_write_failed",
                  notes + [why])


# ---------- automated angle search + drafting (no operator input) ----------

def _athlete(conn, athlete_id):
    cols = ("id", "name", "source_id", "sports", "representation", "external", "evidence_version", "evidence_fingerprint")
    return dict(zip(cols, conn.execute(f"SELECT {', '.join(cols)} FROM athletes WHERE id = %s", (athlete_id,)).fetchone()))


def _tried(attempts):
    """Batch bookkeeping: players not to retry (every recorded attempt except a call-reserve skip, which made no
    decision about the player) and how many players got an angle call."""
    return {"tried_athlete_ids": [a["athlete_id"] for a in attempts if a["outcome"] != "skipped_call_reserve"],
            "players_called": sum(a["outcome"] in CALLED for a in attempts)}


def _rank(r):
    return f"rank {r}" if r else "unranked: eligible, not marked promising by Laya"


def _attempt_lines(attempts):
    return [f"- **{a['name']}** ({_rank(a['rank'])}, proposal {a.get('proposal_id')}): **{a['outcome']}**"
            + (f". {a['passage_note']}" if a.get("passage_note") else "")
            + "".join(f"\n  - {r}" for r in a["reasons"]) for a in attempts] or ["- none"]


CALLED = ("advanced", "insufficient_evidence", "invalid_references", "malformed_output", "provider_failed")


def candidates_for(sel, exclude=(), wider=False):
    """Ranked eligible players (screen order); with wider, then the screen's other eligible players (not marked
    promising by Laya), by score, articles, id. Players in exclude are left out. Eligibility rules are unchanged."""
    cands = (sel or {}).get("candidates") or []
    ranked = sorted((c for c in cands if c.get("rank")), key=lambda c: c["rank"])
    extra = sorted((c for c in cands if wider and c.get("eligible") and not c.get("rank")),
                   key=lambda c: (-(c.get("score") or 0), -(c.get("n_articles") or 0), c["athlete_id"]))
    return [c for c in ranked + extra if c["athlete_id"] not in set(exclude)]


def auto_run(screen_run, schema=None, chat_factory=None, prior=None, exclude=(), call_cap=None,
             max_players=None, wider=False, pacer=None):
    """Automated T03/T04 path on the shortlist of screening run `screen_run` (never an older one), with no operator
    input: up to max_players (default angles.MAX_PLAYERS) eligible players get one angle call each over relevant
    stored passages. The first proposal whose references pass the code checks is drafted in the same run, under the
    same Groq call and wait limits (angles.RESERVE calls are kept for drafting). Saves one package (awaiting
    independent review, or a failure status) or a no-angle outcome.
    Batch use: exclude (athlete ids already tried), call_cap (lowers GROQ_MAX_CALLS for this run, never raises it),
    wider (after the ranked players, the screen's other eligible players; see candidates_for), pacer (the batch's
    shared Groq rate-limit tracker)."""
    from . import angles, compose
    s, run_id, out_dir, conn, err = _open("auto", schema)
    integrations, notes, attempts, usage_log = {}, [], [], []
    head = f"# Automated run {run_id}\n\n" + "".join(f"- {k}: `{v}`\n" for k, v in (prior or {}).items())
    if not conn:
        integrations["neon"] = src.outcome("failed" if s["database_url"] else "not_attempted", err)
        return _close(None, run_id, out_dir, "auto.md", head + f"\nBlocked: {err}\n", integrations, {}, "blocked", notes)
    integrations["neon"] = src.outcome("worked", "connected")
    if call_cap is not None:
        s = {**s, "limits": {**s["limits"], "groq_max_calls": max(0, min(s["limits"]["groq_max_calls"], call_cap))}}
    L, model = s["limits"], s["groq_model"]
    max_players = angles.MAX_PLAYERS if max_players is None else min(max_players, angles.MAX_PLAYERS)
    screened = conn.execute("SELECT outcome FROM runs WHERE id = %s", (screen_run,)).fetchone()
    sel = store.latest_selection(conn, screen_run) if screened and screened[0] in ("selected", "no_selection") else None
    ranked = candidates_for(sel, exclude, wider)

    def finish(outcome, why):
        report = [head, f"**{outcome}**: {why}", "", "## Angle attempts", ""] + _attempt_lines(attempts)
        failed = outcome == "provider_failed" and attempts[-1]["reasons"]
        return _close(conn, run_id, out_dir, "auto.md", "\n".join(report + ["", "No package was saved."]) + "\n",
                      integrations, {"selection_run": sel and sel["run_id"], "groq_calls": len(usage_log),
                                     "angle_attempts": [f"{a['name']}: {a['outcome']}" for a in attempts],
                                     **_tried(attempts), **({"provider_error": failed[0]} if failed else {})},
                      outcome, notes + [why])
    if not sel:
        return finish("screening_failed", f"screening run {screen_run} ended "
                      f"{screened[0] if screened else 'unrecorded'} with no saved shortlist; no older shortlist is used")
    if not ranked:
        return finish("no_selection", f"no eligible {'' if wider else 'promising '}candidate left in selection "
                      f"{sel['run_id']}" + (f" ({len(set(exclude))} already tried in this batch)" if exclude else ""))
    try:
        chat = _make_chat(s, model, L, usage_log, chat_factory, integrations, pacer)
    except compose.ProviderError as e:
        integrations["groq"] = src.outcome("not_attempted", str(e))
        return finish("blocked", str(e))
    chosen, saved = None, []
    reopen = lambda: store.connect(s["database_url"], schema)
    for cand in ranked:
        if sum(a["outcome"] in CALLED for a in attempts) >= max_players:
            break
        athlete = _athlete(conn, cand["athlete_id"])
        rows, _ = store.screening_candidates_for(conn, athlete["id"])
        packet, cfg = conn.execute("SELECT p.content, s.config_fp FROM screenings s JOIN packets p ON p.id = s.packet_id "
                                   "WHERE s.id = %s", (cand["screening_id"],)).fetchone()
        rec = {"athlete_id": athlete["id"], "name": athlete["name"], "rank": cand.get("rank"),
               "screening_id": cand["screening_id"], "reasons": []}
        out = angle = refs = None
        passages = []
        left = L["groq_max_calls"] - len(usage_log)
        if stale := stale_reasons(athlete, packet, cfg, rows):
            rec.update(outcome="stale_selection", reasons=stale)
        elif left < 1 + angles.RESERVE:
            rec.update(outcome="skipped_call_reserve",
                       reasons=[f"{left} Groq call(s) left; {angles.RESERVE} are reserved to draft and verify one story"])
        else:
            passages, found = angles.select_passages(athlete["name"], rows)
            rec["passage_note"] = (f"{len(passages)} of {found} cue-matching paragraphs sent "
                                   f"({sum(len(p['passage']) for p in passages)} chars, cap {angles.PASSAGE_CHARS})")
            if not passages:
                rec.update(outcome="no_usable_passages", reasons=["no stored paragraph with story cues may be sent"])
            else:
                nations = [r["nation"] for r in athlete["representation"] if r.get("listed")]
                try:
                    out = chat("angle_proposal", angles.ask(athlete["name"], nations, passages))
                    outcome, reasons, angle, refs = angles.validate(out, passages)
                    rec.update(outcome=outcome, reasons=reasons)
                except compose.ProviderError as e:
                    rec.update(outcome="malformed_output" if e.kind == "malformed_output" else "provider_failed",
                               reasons=[str(e)])
        called = rec["outcome"] in CALLED
        if called:  # the model output is kept locally first, then written on a fresh connection
            saved.append(_save_local(out_dir, f"angle_attempt_{len(attempts) + 1}.json",
                                     {**rec, "angle": angle, "refs": refs, "passages_sent": passages, "model_output": out}))
        step = f"record the angle attempt for {athlete['name']}"
        try:
            if called:
                conn = _swap(conn, reopen)
            rec["proposal_id"] = conn.execute(
                "INSERT INTO angle_proposals (run_id, athlete_id, screening_id, selection_run, rank, outcome, reasons, "
                "angle, refs, passages_sent, model_output) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (run_id, athlete["id"], cand["screening_id"], sel["run_id"], cand.get("rank"), rec["outcome"],
                 Jsonb(rec["reasons"]), Jsonb(angle), Jsonb(refs), Jsonb(passages), Jsonb(out))).fetchone()[0]
        except psycopg.Error as e:
            attempts.append(rec)
            counts = {"selection_run": sel["run_id"], "groq_calls": len(usage_log),
                      "angle_attempts": [f"{a['name']}: {a['outcome']}" for a in attempts], **_tried(attempts)}
            return _db_failed(conn, run_id, out_dir, step, e, saved, integrations, counts, notes)
        attempts.append(rec)
        if rec["outcome"] == "advanced":
            chosen = (cand, athlete, angle, refs, rec)
            break
        if rec["outcome"] == "provider_failed":
            break  # a provider failure is not a story gap: stop instead of spending more calls
    if not chosen:
        last = attempts[-1]["outcome"] if attempts else "none"
        return finish("provider_failed" if last == "provider_failed" else "no_supported_angle",
                      f"none of {len(attempts)} attempted ranked player(s) produced an angle that passed the checks")
    cand, athlete, angle, refs, rec = chosen
    rows, why = rows_for_refs(conn, refs)
    if why:
        return finish("angle_evidence_changed", "; ".join(why))
    proposal = {"id": rec["proposal_id"], "decision": "system_proposed", "selection_run": sel["run_id"],
                "reviewer": f"none: system proposal ({model}, prompt {angles.PROMPT_VERSION}); not a review",
                "screening_id": cand["screening_id"], "angle": angle, "refs": refs}
    return _draft(conn, s, run_id, out_dir, sel, cand, athlete, proposal, rows, lambda: chat, usage_log, integrations,
                  notes, auto={"attempts": attempts, "prior": prior or {}}, reopen=reopen)


BATCH = {"ready": 3, "players": 5, "calls": 24}


def batch_run(screen_run, schema=None, chat_factory=None, prior=None, previews=True, pacer=None, **limits):
    """Repeated auto_run on one screen's shortlist for up to BATCH['ready'] drafts awaiting review, each about a
    different player. Each child run gives ONE player an angle call (stale players or players with no usable passages
    are skipped without using a player slot) and keeps the rest of its own GROQ_MAX_CALLS, within the calls left in
    the batch, to draft, revise and verify that player's story. Players already tried are skipped; a failed angle or
    draft moves on to another player. One Groq rate-limit tracker (pacer) is shared by every child run; call logs,
    call caps and wait budgets stay per run. The batch stops at BATCH['players'] players given an angle call,
    BATCH['calls'] Groq calls in total, BATCH['ready'] drafts, no candidate left, or a provider failure (the draft
    is kept and the provider's error is reported).
    Each ready draft gets a private review preview (showcase/review/<id>/), never the public site."""
    from . import compose
    lim = {**BATCH, **limits}
    pacer = pacer or compose.Pacer(settings()["limits"])
    runs, tried, players, calls, ready, stop, provider_error = [], [], 0, 0, [], None, None
    while not stop:
        try:
            r = auto_run(screen_run, schema, chat_factory, prior, exclude=tried, call_cap=lim["calls"] - calls,
                         max_players=1, wider=True, pacer=pacer)
        except Exception as e:  # never lose the batch report: record the error and stop
            r = {"run_id": "(not recorded)", "outcome": "run_crashed", "report": "-",
                 "counts": {"failed_step": f"auto_run raised {type(e).__name__}: {safe(e)}"}}
        c = r["counts"]
        runs.append(r)
        calls += c.get("groq_calls") or 0
        players += c.get("players_called") or 0
        tried += [a for a in c.get("tried_athlete_ids") or [] if a not in tried]
        if r["outcome"] == "awaiting_review":
            ready.append(c["package_id"])
        provider_error = c.get("provider_error")
        stop = (r["outcome"] if r["outcome"] in ("db_write_failed", "run_crashed") else
                "provider_failed" if provider_error else
                "ready_target" if len(ready) >= lim["ready"] else "player_limit" if players >= lim["players"]
                else "call_limit" if calls >= lim["calls"]
                else r["outcome"] if r["outcome"] in ("blocked", "screening_failed", "provider_failed") else
                "no_candidates" if r["outcome"] == "no_selection" else
                "call_limit" if not c.get("tried_athlete_ids") else None)  # too few calls left for one more attempt
    shown = {}
    for pid in ready if previews else []:
        try:
            shown[pid] = preview_run(pid, schema=schema)
        except Exception as e:  # a preview failure never changes the stored draft; report it
            shown[pid] = {"error": safe(e)}
    out_dir = RUNS_DIR / ("batch-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2))
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Automated batch on screen run {screen_run}", "",
             f"Stopped: **{stop}**. Ready for review: {len(ready)}. Players given an angle call: {players}. "
             f"Groq calls: {calls} (batch cap {lim['calls']}). Nothing here is accepted or exported.", "",
             *([f"**Provider error:** {provider_error} (the draft, if any, is kept)", ""] if provider_error else []),
             *(f"- {k}: `{v}`" for k, v in (prior or {}).items()), "", "## Runs", ""]
    for r in runs:
        c = r["counts"]
        lines += [f"- run `{r['run_id']}`: **{r['outcome']}**, {c.get('groq_calls', 0)} call(s)"
                  + (f", package `{c['package_id']}` sha `{c.get('package_sha256')}`" if c.get("package_id") else "")
                  + (f", unresolved claims {c['unresolved_claims']}" if c.get("unresolved_claims") else "")
                  + (f". **Failed step: {c['failed_step']}**" if c.get("failed_step") else ""),
                  *(f"  - saved locally: {x}" for x in c.get("saved_files") or []),
                  *(f"  - {a}" for a in c.get("angle_attempts") or []), f"  - report: {r['report']}"]
    lines += ["", "## Review previews (private, draft-labelled)", ""] + (
        [f"- `{pid}`: {p.get('site', '')} {p.get('pdf', '')} {p.get('error', '')}".rstrip() for pid, p in shown.items()]
        or ["- none"])
    (out_dir / "batch.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"run_id": out_dir.name, "outcome": stop, "report": str(out_dir / "batch.md"),
            "counts": {"ready": ready, "players_called": players, "groq_calls": calls, "provider_error": provider_error,
                       "runs": [f"{r['run_id']}: {r['outcome']}" for r in runs], "previews": shown}}


def _auto_fields(package, proposal, status, auto, model, ranked, discovery, notes):
    from . import angles, export
    attempts = auto["attempts"]
    package["angle_proposal"] = {**{k: proposal[k] for k in ("id", "decision", "reviewer", "selection_run",
                                                              "screening_id", "angle", "refs")},
                                 "prompt_version": angles.PROMPT_VERSION}
    package["angle_search"] = [{k: a.get(k) for k in ("name", "rank", "outcome", "reasons", "proposal_id",
                                                      "passage_note")} for a in attempts]
    package["run_chain"] = auto["prior"]
    package["selection_basis"] = {
        "athlete": ranked,
        "angle": (f"system-proposed: angle proposal {proposal['id']} (one {model} call over stored passages), the first "
                  f"of {len(attempts)} attempted player(s) whose stakes, turning point and significance excerpt ids "
                  "and fact references passed the code checks against stored text"),
        "copy": f"generated by Groq ({model}) from the proposal's facts; checked by code guards and a verifier",
        "graphics": "chosen automatically from numbers and words in the checked copy",
        "discovery_evidence": discovery,
        "automated_story_selection": ("attempted in this run with no operator-supplied player, angle, facts, copy or "
                                      "graphics; needs independent review before it counts")}
    package["photo"] = {"status": "placeholder", "reason": "No photo was sourced in this automated run; designed "
                        "graphics are used.", "credit": None, "licence": None}
    if status == "awaiting_review":
        package["presentation"] = export.auto_presentation(package)
        try:
            export.public_package(package, package["presentation"], None, None, draft=True)
        except export.ExportError as e:
            notes.append(f"public export dry run failed: {e}")


def angle_gate(conn, sel):
    """The latest angle review must approve the current selection (same athlete and screening), and every cited passage
    must still be in its stored source with the content hash frozen at review time.
    Returns (angle, source rows with their cited passages, blocked outcome or None, reasons)."""
    a = store.latest_angle(conn)
    if not a:
        return None, [], "angle_not_approved", ["no reviewed angle recorded (python -m arabsports angle FILE)"]
    why = []
    if a["decision"] != "approved":
        why.append(f"latest angle review {a['id']} is {a['decision']}")
    if a["athlete_id"] != sel["athlete_id"]:
        why.append(f"angle review {a['id']} is for athlete {a['athlete_id']}; the current selection ({sel['run_id']}) "
                   f"is athlete {sel['athlete_id']}")
    elif a["screening_id"] != sel["screening_id"]:
        why.append(f"angle review {a['id']} was for screening {a['screening_id']}; the current selection uses "
                   f"screening {sel['screening_id']}")
    if why:
        return a, [], "angle_not_approved", why
    rows, why = rows_for_refs(conn, a["refs"])
    if why:
        return a, [], "angle_evidence_changed", why
    return a, rows, None, []


def rows_for_refs(conn, refs):
    """Source rows carrying only the cited passages; each passage must still be in its stored source with the content
    hash frozen when it was cited. Returns (rows, reasons)."""
    cols = ("id", "canonical_url", "publisher", "title", "content", "content_kind", "usage_terms", "extra", "provider",
            "content_hash")
    rows, why = {}, []
    for r in refs:
        row = conn.execute(f"SELECT {', '.join(cols)} FROM articles WHERE id = %s", (r["article_id"],)).fetchone()
        row = dict(zip(cols, row)) if row else None
        if not row or row["content_kind"] not in ("full_text", "summary"):
            why.append(f"cited source {r['url']} is missing or withdrawn")
        elif row["content_hash"] != r["content_hash"]:
            why.append(f"cited source {r['url']} changed since the angle review (hash {r['content_hash'][:12]} -> "
                       f"{row['content_hash'][:12]})")
        elif r["passage"] not in row["content"]:
            why.append(f"cited passage no longer in {r['url']}: {r['passage'][:60]}")
        else:
            rows.setdefault(row["id"], {**row, "passages": []})["passages"].append(r["passage"])
    return ([] if why else list(rows.values())), why


def angle_run(path, schema=None):
    """Record an independent angle review (JSON file) for the current selection. Report URLs under `fetch` (FIFA
    pages) are retrieved and stored first as reviewer-cited drafting evidence. They are not linked to the athlete, so
    screening and the discovery record do not change."""
    import pathlib
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    s, run_id, out_dir, conn, err = _open("angle_review", schema)
    integrations, notes, counts = {}, [], {"angle_file": str(path)}
    (out_dir / "angle_review.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    head = f"# Angle review run {run_id}\n\n"
    if not conn:
        return _close(None, run_id, out_dir, "angle.md", head + f"Blocked: {err}\n", integrations, counts, "blocked", notes)
    sel = store.latest_selection(conn)
    if not sel or sel["athlete_id"] != spec["athlete_id"]:
        why = (f"current selection {sel and sel['run_id']} is athlete {sel and sel['athlete_id']}; the angle is for "
               f"athlete {spec['athlete_id']}. Nothing recorded.")
        return _close(conn, run_id, out_dir, "angle.md", head + f"**angle_mismatch**: {why}\n", integrations,
                      counts, "angle_mismatch", [why])
    stored = []
    for url in spec.get("fetch", []):
        try:
            lead = src.fetch_fifa(url, s["limits"])
        except (src.FetchError, ValueError, KeyError) as e:
            integrations["fifa"] = src.outcome("failed", f"{url}: {e}")
            return _close(conn, run_id, out_dir, "angle.md", head + f"**blocked**: could not store {url}: {e}\n",
                          integrations, counts, "blocked", notes)
        stored += [(aid, st, a["canonical_url"], a["title"]) for aid, st, a in store.upsert_articles(conn, [lead], run_id)]
    if spec.get("fetch"):
        integrations["fifa"] = src.outcome("worked", f"{len(stored)} report page(s) stored: "
                                           + ", ".join(f"{a} {st}" for a, st, _, _ in stored))
    refs = [{**r, "url": src.canonical_url(r["url"])} for r in spec["refs"]]
    try:
        aid = store.record_angle(conn, sel, spec["decision"], spec["reviewer"], spec["angle"], refs, run_id)
    except store.ReviewError as e:
        return _close(conn, run_id, out_dir, "angle.md", head + f"**refused**: {e}\n", integrations, counts,
                      "refused", [str(e)])
    counts.update({"angle_review_id": aid, "athlete_id": sel["athlete_id"], "screening_id": sel["screening_id"],
                   "selection_run": sel["run_id"], "refs": len(refs), "stored_sources": stored})
    L = [head.rstrip(), "", f"- Angle review **{aid}**: {spec['decision']} by {spec['reviewer']}",
         f"- For selection `{sel['run_id']}`: athlete {sel['athlete_id']}, screening {sel['screening_id']}", ""]
    L += [f"- **{k}**: {spec['angle'][k]}" for k in store.ANGLE_FIELDS] + ["", "## Cited passages", ""]
    L += [f"- {r['url']} ({', '.join(r.get('supports', []))}): \"{r['passage']}\"" for r in refs]
    L += ["", "## Stored sources", ""] + [f"- article {a} ({st}): {t} {u}" for a, st, u, t in stored]
    return _close(conn, run_id, out_dir, "angle.md", "\n".join(L) + "\n", integrations, counts, "recorded", notes)


FIFA_RIGHTS_NOTE = ("FIFA report text is FIFA copyright. FIFA's Terms of Service (https://inside.fifa.com/terms-of-service, "
                    "last updated 27 January 2026) s.6.4 allows non-commercial display on a website with attribution to "
                    "FIFA and, where possible, a link back to the story; s.6.2 bars using FIFA content, names, logos or "
                    "marks without written agreement. Handling: slides restate checked facts in new words (8-word copy "
                    "check), cite FIFA with links and use no FIFA logos or images. Not a legal clearance.")


def photo_run(package_id, path, schema=None):
    """T06: one bounded Commons lookup for a stored package's story, recorded as a run. The package is not changed;
    the export reads the latest record for the package. No suitable photo is a valid outcome (designed graphics)."""
    import pathlib
    from . import photos
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    s, run_id, out_dir, conn, err = _open("photos", schema)
    if err:
        return _close(None, run_id, out_dir, "photo_lookup.md", f"# Photo lookup {run_id}\n\nblocked: {err}\n", {}, {},
                      "blocked", [err])
    row = conn.execute("SELECT package FROM story_packages WHERE package_id = %s", (package_id,)).fetchone()
    if not row:
        return _close(conn, run_id, out_dir, "photo_lookup.md", f"# Photo lookup {run_id}\n\nno package {package_id}\n",
                      {}, {}, "blocked", [f"no package {package_id}"])
    res = photos.lookup(spec["queries"], spec["subject_terms"], spec["context_terms"], spec["context_date"])
    record = {"run_id": run_id, "package_id": package_id, "athlete": row[0]["athlete"]["name"], "source": "Wikimedia Commons",
              "policy": photos.__doc__.strip(), "query": spec, **res}
    (out_dir / "photo_lookup.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    L = [f"# Photo lookup {run_id} for `{package_id}` ({record['athlete']})", "", f"**Outcome: {res['outcome']}**", "",
         "Searches:", ""] + [f"- `{a['query']}`: {a['outcome']}" + (f", {a['results']} files" if "results" in a else
                                                                     f" ({a.get('error')})") for a in res["attempts"]]
    L += ["", "| file | licence | decision | identity | context | reasons |", "|---|---|---|---|---|---|"]
    L += [f"| [{c['title']}]({c['file_page']}) | {c['licence']} | {c['decision']} | {c['identity_supported']} | "
          f"{c['context_supported']} | {'; '.join(c['reasons'])} |" for c in res["candidates"]]
    counts = {"candidates": len(res["candidates"]),
              "shortlisted": sum(c["decision"] == "shortlisted_for_review" for c in res["candidates"])}
    commons = "failed" if res["outcome"] == "lookup_failed" else "ok"
    return _close(conn, run_id, out_dir, "photo_lookup.md", "\n".join(L) + "\n", {"commons": commons}, counts,
                  res["outcome"], [])


def _presentation(pkg, path):
    import pathlib
    from . import export
    if path:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not pkg.get("presentation"):
        raise export.ExportError(f"{pkg['package_id']} has no saved presentation; pass a presentation file")
    return pkg["presentation"]


def _latest_photo(conn, package_id):
    for (rid,) in conn.execute("SELECT id FROM runs WHERE mode = 'photos' ORDER BY started_at DESC").fetchall():
        f = RUNS_DIR / rid / "photo_lookup.json"
        if f.exists() and json.loads(f.read_text(encoding="utf-8"))["package_id"] == package_id:
            return json.loads(f.read_text(encoding="utf-8"))
    return None


def export_run(package_id, presentation_path=None, schema=None, site=None):
    """T05: write an accepted package to the static site through the public whitelist. Refused unless an independent
    review accepted exactly this stored version with every acceptance blocker explicitly resolved. The presentation is
    the package's own (auto run) unless a file is given."""
    from . import export
    s = settings()
    with store.connect(s["database_url"], schema) as conn:
        pkg = store.exportable_package(conn, package_id)
        if pkg is None:
            state = conn.execute("SELECT review_state FROM story_packages WHERE package_id = %s", (package_id,)).fetchone()
            raise export.ExportError(f"{package_id} is not exportable (review state: {state[0] if state else 'missing'}); "
                                     "it needs an independent acceptance of its exact stored version")
        rev = conn.execute("SELECT reviewer, reviewed_at, resolved_blockers, package_sha256 FROM package_reviews WHERE "
                           "package_id = %s AND decision = 'accepted' ORDER BY id DESC LIMIT 1", (package_id,)).fetchone()
        photo = _latest_photo(conn, package_id)
    review = {"reviewer": rev[0], "reviewed_at": rev[1].date().isoformat(), "resolved_blockers": rev[2],
              "package_sha256": rev[3]}
    return export.write(export.public_package(pkg, _presentation(pkg, presentation_path), review, photo),
                        site or export.SITE)


def preview_run(package_id, presentation_path=None, schema=None):
    """Review copy of a stored package awaiting review: draft-labelled web preview + draft PDF under showcase/review.
    Never written to the deployable site."""
    from . import export
    s = settings()
    with store.connect(s["database_url"], schema) as conn:
        row = conn.execute("SELECT package, review_state FROM story_packages WHERE package_id = %s", (package_id,)).fetchone()
        if not row or row[1] != "awaiting_review":
            raise export.ExportError(f"{package_id}: only a package awaiting review gets a review preview "
                                     f"(state: {row[1] if row else 'missing'})")
        photo = _latest_photo(conn, package_id)
    folder = export.ROOT / "review" / package_id
    out = export.preview(row[0], _presentation(row[0], presentation_path), photo, folder)
    pdf = export.render_pdf(folder, package_id, folder / f"{package_id}-DRAFT-not-for-sharing.pdf")
    return {**out, "pdf": str(pdf)}


def pdf_run(package_id):
    """PDF of an exported (accepted) package from the deployable site build -> showcase/exports."""
    from . import export
    idx = json.loads((export.SITE / "data" / "index.json").read_text(encoding="utf-8"))
    if not any(p["package_id"] == package_id for p in idx["packages"]):
        raise export.ExportError(f"{package_id} is not in the exported site; accept and export it first")
    return {"pdf": str(export.render_pdf(export.SITE, package_id, export.ROOT / "exports" / f"{package_id}.pdf"))}


def revise_run(original_run, path, schema=None, chat_factory=None):
    """Save one manually revised copy of a stored draft as a new package linked to the original (which is not
    changed). The copy cites the original's reviewed facts; it goes through the same structure checks and one claim
    verification (code guards + Groq verifier). Nothing is regenerated."""
    import copy
    import pathlib
    from . import compose
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    s, run_id, out_dir, conn, err = _open("revise", schema)
    integrations, notes = {}, []
    (out_dir / "revision_input.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    head = f"# Revision run {run_id}\n\n"

    def stop(outcome, why):
        integrations.setdefault("groq", src.outcome("not_attempted", f"{outcome}: nothing verified"))
        return _close(conn, run_id, out_dir, "carousel.md", head + f"**Blocked: {outcome}.**\n\n"
                      + "".join(f"- {w}\n" for w in why) + "\nNo Groq call was made and no package was saved.\n",
                      integrations, {"status": outcome, "reasons": why, "groq_calls": 0}, outcome, notes + why)
    if not conn:
        return _close(None, run_id, out_dir, "carousel.md", head + f"Blocked: {err}\n", integrations, {}, "blocked", notes)
    row = conn.execute("SELECT package, package_sha256 FROM story_packages WHERE run_id = %s ORDER BY created_at LIMIT 1",
                       (original_run,)).fetchone()
    if not row:
        return stop("revision_blocked", [f"no stored package for run {original_run}"])
    orig, orig_sha = row
    editor = (spec.get("editor") or "").strip()
    if not editor:
        return stop("revision_blocked", ["the revision needs an editor"])
    # Same checks generate relies on: the angle review behind the original still approves the current selection and its
    # cited passages are unchanged; the athlete's evidence has not moved since.
    sel = store.latest_selection(conn)
    angle, _, blocked, why = angle_gate(conn, sel) if sel else (None, [], "angle_not_approved", ["no selection stored"])
    if blocked:
        return stop(blocked, why)
    why = []
    old = orig.get("angle_review") or {}
    # A later review may supersede the original's only to correct citations: same fact ids and statements, explicitly
    # naming the review it supersedes. The facts then cite the later review's passages.
    same = lambda a: [(f["id"], f["statement"]) for f in a.get("facts") or []]
    superseded = (old and angle["id"] != old.get("id") and angle["angle"].get("supersedes_review") == old.get("id")
                  and same(angle["angle"]) == same(old["angle"]) and same(old["angle"]))
    if not old or (angle["id"] != old.get("id") and not superseded):
        why.append(f"the original used angle review {old.get('id')}; the current approved review is {angle['id']} "
                   "(a later review is accepted only if it names the original's review in supersedes_review and keeps "
                   "every fact id and statement)")
    if (sel["athlete_id"], sel["screening_id"]) != (orig["athlete"]["id"], orig["screening"]["screening_id"]):
        why.append(f"the current selection ({sel['run_id']}) is not the original's athlete/screening")
    ev = conn.execute("SELECT evidence_version FROM athletes WHERE id = %s", (orig["athlete"]["id"],)).fetchone()
    if not ev or ev[0] != orig["athlete"]["evidence_version"]:
        why.append("the athlete's evidence changed since the original draft (rescreen and regenerate instead)")
    if why:
        return stop("stale_selection", why)

    sources = [{**x, "text": x["text"]} for x in orig["sources_used"]]
    by_url = {x["url"]: x["sid"] for x in sources}
    if superseded:  # re-cite each fact from the later review; every passage must be in the source text actually sent
        refs_by = {r["id"]: r for r in angle["refs"]}
        new_refs = {f["id"]: f["refs"] for f in angle["angle"]["facts"]}
        text_by = {x["url"]: x["text"] or "" for x in sources}
        for f in orig["facts"]:
            parts = [refs_by[x]["passage"] for x in new_refs[f["id"]]]
            if not all(p in text_by[f["source"]["url"]] for p in parts):
                return stop("angle_evidence_changed", [f"fact {f['id']}: a passage cited by angle review {angle['id']} "
                                                       "is not in the source text the original sent"])
            f.update(passage_refs=new_refs[f["id"]], evidence_span=" ".join(parts))
    facts = [{"id": f["id"], "statement": f["statement"], "source": by_url[f["source"]["url"]],
              "evidence": f["evidence_span"], **({"passage": f["evidence_span"], "refs": f["passage_refs"]}
                                                   if f.get("passage_refs") else {})} for f in orig["facts"]]
    draft = {"title": spec.get("title"), "explanation": spec.get("explanation"),
             "slides": [{"role": role, **{k: x.get(k) for k in ("heading", "body", "fact_ids")}}
                        for role, x in zip(compose.ROLES, spec.get("slides") or [])]}
    if len(spec.get("slides") or []) != 5:
        draft["slides"] = spec.get("slides")  # let check_draft report the wrong count
    errs, warns = compose.check_draft(draft, facts, sources, orig["athlete"]["name"])
    usage_log, rnd, model = [], None, s["groq_model"]
    if errs:
        structural = any(k in e for e in errs for k in ("need exactly 5", "role must be", "not an object", "missing title"))
        status = "malformed_output" if structural else "insufficient_evidence"
        integrations["groq"] = src.outcome("not_attempted", "the copy failed the structure checks; not verified")
    else:
        try:
            if chat_factory:
                chat = chat_factory(usage_log)
                integrations["groq"] = src.outcome("not_attempted", "FIXTURE chat provider (checks only)")
            elif not s["groq_key"]:
                raise compose.ProviderError("blocked", "GROQ_API_KEY not set")
            else:
                chat = compose.groq_chat(s["groq_key"], model, s["limits"], usage_log)
            rnd = compose.verify(chat, "verify", facts, sources, draft)
            status = ("malformed_output" if rnd["malformed"] else
                      "insufficient_evidence" if rnd["unresolved"] or rnd["failed_facts"] else "awaiting_review")
            if not chat_factory:
                integrations["groq"] = src.outcome("worked", f"{len(usage_log)} call to {model}")
        except compose.ProviderError as e:
            status = e.kind if e.kind == "blocked" else "provider_failed"
            notes.append(str(e))
            integrations.setdefault("groq", src.outcome("failed", str(e)))
    ready = status == "awaiting_review"
    fact_v = {r["id"]: r for r in (rnd["facts"] if rnd else [])}
    p = copy.deepcopy(orig)
    for f in p["facts"]:
        f["verification"] = ({k: fact_v[f["id"]][k] for k in ("verdict", "passage", "code", "model")}
                             if f["id"] in fact_v else {"verdict": "not_verified"})
    p.update({
        "package_id": f"pkg-{run_id}", "run_id": run_id, "status": status, "title": draft["title"],
        "slides": draft["slides"] if ready else None, "draft_not_ready": None if ready else draft,
        "review": {**orig["review"], "initial_state": "awaiting_review" if ready else "not_applicable"},
        "explanation": {**(draft["explanation"] or {}), "label": f"Editorial explanation written by {editor} (manual "
                        "revision). Not Laya's reasoning and not proof that the angle is original."} if ready else None,
        "claim_verification": {**orig["claim_verification"], "rounds": [rnd] if rnd else []},
        "dropped_facts": [],
        "revision_of": {"package_id": orig["package_id"], "package_sha256": orig_sha, "run_id": orig["run_id"],
                        "status": orig["status"], "relationship": "manual editorial revision of this draft's copy; the "
                        "original package is kept unchanged"},
        "previous_attempt": {"package_id": orig["package_id"], "status": orig["status"], "package_sha256": orig_sha,
                             "review_state": "not_applicable" if orig["status"] != "awaiting_review" else "awaiting_review",
                             "relationship": "the draft this revision replaces; kept unchanged"},
        "selection_basis": {**orig["selection_basis"], "copy": f"manually revised by {editor}; facts, passages and "
                            "angle are the original's reviewed ones; not generated",
                            **({"angle": f"editorially chosen: angle review {angle['id']} by {angle['reviewer']} "
                                         f"(supersedes review {old['id']}: citations corrected, facts unchanged); Laya "
                                         "did not identify this story"} if superseded else {})},
        **({"angle_review": {**{k: angle[k] for k in ("id", "decision", "reviewer", "selection_run", "screening_id",
                                                      "angle", "refs")}, "created_at": str(angle["created_at"])}}
           if superseded else {}),
        "development_review": {**orig["development_review"], "errors": errs, "warnings": warns, "revisions": 0,
                               "notes": notes + [f"manual revision of {orig['package_id']} by {editor}"]},
        "generation": {"provider": "manual", "copy_author": editor, "verifier_model": model,
                       "prompt_version": compose.PROMPT_VERSION, "calls": usage_log},
    })
    sha = store.save_package(conn, p, status)
    counts = {"status": status, "review_state": "awaiting_review" if ready else "not_applicable", "package_id": p["package_id"],
              "package_sha256": sha, "revision_of": orig["package_id"], "groq_calls": len(usage_log),
              "structure_errors": errs, "unresolved_claims": rnd and rnd["unresolved"]}
    return _close(conn, run_id, out_dir, "carousel.md", carousel_md(p, sha), integrations, counts, status, notes)


def acceptance_blockers(sources):
    """Source-use questions that claim verification cannot settle. Each must be explicitly resolved by the reviewer
    (store.record_review resolves=...) before a package can be accepted."""
    out = []
    if any(x.get("provider") == "fifa" for x in sources):
        out.append({"id": "rights_fifa_terms",
                    "issue": "FIFA Terms s.6.4 permit non-commercial display of FIFA content with attribution and a "
                             "link back, while s.6.2 bars using FIFA content, names or marks without written agreement. "
                             "Restating match facts in original words does not by itself settle whether this carousel "
                             "may use the reports or the 'FIFA World Cup' name; a rights decision is needed."})
    if any(x.get("usage_flags", {}).get("modification_allowed") is False for x in sources):
        out.append({"id": "rights_ajel_modification",
                    "issue": "Ajel Wire flags modification_allowed=false, while the Ajel policy permits summarised "
                             "republication with attribution. Restating facts with attribution and no copied wording "
                             "does not by itself resolve this conflict; a rights decision is needed."})
    if any(x.get("provider") == "wikipedia" for x in sources):
        out.append({"id": "rights_wikipedia_share_alike",
                    "issue": "Wikipedia text is CC BY-SA 4.0: slides adapted from it need attribution, a licence notice "
                             "and CC BY-SA licensing of the adapted text; confirm this is acceptable for the output."})
    return out


def review(package_id, decision, reviewer, reasons, sha, schema=None, resolves=()):
    """Local, explicit record of an independent review decision for one exact package version."""
    s = settings()
    with store.connect(s["database_url"], schema) as conn:
        store.record_review(conn, package_id, decision, reviewer, reasons, sha, resolves)
        state = conn.execute("SELECT review_state FROM story_packages WHERE package_id = %s", (package_id,)).fetchone()[0]
    return {"package_id": package_id, "review_state": state, "reviewer": reviewer}


def stale_reasons(athlete, packet, screened_cfg, rows):
    """The selected screening must describe exactly the evidence about to be used, under the current configuration."""
    out = []
    if (packet["evidence_version"], packet["evidence_fingerprint"]) != (athlete["evidence_version"], athlete["evidence_fingerprint"]):
        out.append(f"evidence changed since screening (v{packet['evidence_version']} -> v{athlete['evidence_version']})")
    then, now = {x["article_id"] for x in packet["sources"]}, {a["id"] for a in rows}
    if then != now:
        out.append(f"source set changed since screening: removed/withdrawn {sorted(then - now)}, added {sorted(now - then)}")
    if screened_cfg != screen.config_fingerprint():
        out.append("screening configuration changed since the selection")
    return out


def build_package(run_id, athlete, profile, cand, sel, sources, usage, result, status, model, usage_log, notes):
    from . import compose
    by_sid = {x["sid"]: x for x in sources}
    rounds = (result or {}).get("verification") or []
    fact_v = {r["id"]: r for r in (rounds[-1]["facts"] if rounds else [])}
    ready = status == "awaiting_review"
    facts = [{"id": f["id"], "statement": f["statement"], "evidence_span": f["evidence"],
              **({"origin": (result or {}).get("fact_source") or "angle_review", "passage_refs": f["refs"]} if f.get("refs") else {"origin": "model_extraction"}),
              "verification": ({k: fact_v[f["id"]][k] for k in ("verdict", "passage", "code", "model")}
                               if f["id"] in fact_v else {"verdict": "not_verified"}),
              "source": {"url": by_sid[f["source"]]["url"], "publisher": by_sid[f["source"]]["publisher"],
                         "attribution": by_sid[f["source"]]["attribution"]},
              # each fact cites exactly one source, so it is never corroborated by the others in the package
              "corroboration": f"single source ({by_sid[f['source']]['publisher']}); not independently corroborated",
              **({"licence": "CC BY-SA 4.0 (Wikipedia); adapted text must carry attribution and share-alike"}
                 if by_sid[f["source"]].get("provider") == "wikipedia" else {})}
             for f in (result or {}).get("facts", [])]
    draft = (result or {}).get("draft") or {}
    return {
        "package_id": f"pkg-{run_id}", "schema_version": compose.SCHEMA_VERSION, "run_id": run_id, "status": status,
        "title": draft.get("title"), "language": "en",
        "athlete": {"id": athlete["id"], "name": athlete["name"], "wikidata": profile["wikidata"], "sport": profile["sport"],
                    "represents": profile["represents"], "occupations": profile["occupations"],
                    "evidence_version": athlete["evidence_version"], "evidence_fingerprint": athlete["evidence_fingerprint"]},
        # Slides/explanation are filled only for a draft that passed every check; it still awaits independent review.
        "slides": draft.get("slides") if ready else None,
        "draft_not_ready": (draft or None) if not ready else None,
        "review": {"initial_state": "awaiting_review" if ready else "not_applicable",
                   "rule": "Not a successful story until an independent reviewer accepts this exact package version "
                           "(package_reviews). The generator and automatic checks cannot approve it."},
        "claim_verification": {"version": compose.VERIFY_VERSION, "rounds": rounds,
                               "note": "Code guards and a Groq verifier judge each claim against the passages of its "
                                       "cited facts; the stricter verdict wins. Fallible aid for review, not "
                                       "certification of factual truth."},
        "sources_used": [{k: x.get(k) for k in ("sid", "url", "publisher", "attribution", "provider", "article_id",
                                                 "content_hash", "revision", "reduction", "usage_flags", "text")}
                         for x in sources],
        "facts": facts,
        "dropped_facts": [{"statement": f.get("statement"), "reason": f["reason"]} for f in (result or {}).get("dropped_facts", [])],
        "angle": (result or {}).get("angle"),
        "explanation": {**(draft.get("explanation") or {}),
                        "label": "Editorial explanation generated by Groq from the listed facts. Not Laya's reasoning "
                                 "and not proof that the angle is original."} if ready else None,
        "screening": {"selection_run": sel["run_id"], "screening_id": cand["screening_id"], "mapped": cand["mapped"],
                      "score_raw_uncalibrated": cand["score"], "rank": cand.get("rank"), "model": LAYA_MODEL,
                      "rubric_version": screen.RUBRIC_VERSION, "mapping": screen.MAPPING_DOC},
        "photo": {"status": "placeholder", "reason": "No photo was sourced for this package; designed graphics are used.",
                  "credit": None, "licence": None},
        "source_usage": usage,
        # only the terms of providers whose text actually went into this package
        "rights_handling": " ".join(n for prov, n in (("ajel", RIGHTS_NOTE), ("wikipedia", WIKI_RIGHTS_NOTE),
                                                      ("fifa", FIFA_RIGHTS_NOTE))
                                    if any((x.get("provider") or "").startswith(prov) for x in sources)) or "no source text used",
        "integration_caveats": ["GDELT throttled (HTTP 429) from this network; not used.",
                                "Laya base checkpoint is weak zero-shot; its scores are uncalibrated.",
                                "Facts come only from the passages listed in sources_used; several reports from one "
                                "publisher are not independent confirmation, and Wikipedia agreeing with Wikidata is "
                                "not independent corroboration."],
        "development_review": {"checks": ["fact spans verbatim in cited source", "slide numbers/names within cited facts",
                                          "no quotes", f"no {compose.COPY_NGRAM}-word copy from sources",
                                          "5 slides in role order", "English only", "word budget",
                                          "claim-to-evidence verification (code guards + Groq verifier)"],
                               "errors": (result or {}).get("errors"), "warnings": (result or {}).get("warnings"),
                               "revisions": (result or {}).get("revisions"), "notes": notes},
        "generation": {"provider": "groq", "model": model, "prompt_version": compose.PROMPT_VERSION, "calls": usage_log},
    }


def carousel_md(p, sha=None):
    head = ("**DRAFT, awaiting independent review. Not accepted, not exportable.**" if p["status"] == "awaiting_review"
            else f"**Not a usable story: {p['status']}.**")
    L = [f"# {p['title'] or '(no carousel)'}", "", head, "", f"- Package `{p['package_id']}` ({p['schema_version']}), status "
         f"**{p['status']}**" + (f", version sha256 `{sha}`" if sha else ""),
         f"- Athlete: {p['athlete']['name']} ({', '.join(p['athlete']['sport'])}; represents "
         f"{', '.join(p['athlete']['represents']) or 'unknown'})", ""]
    if p.get("revision_of"):
        ro = p["revision_of"]
        L += [f"- Revision of `{ro['package_id']}` ({ro['status']}, sha256 `{ro['package_sha256']}`): {ro['relationship']}", ""]
    elif p.get("previous_attempt"):
        pa = p["previous_attempt"]
        L += [f"- Previous attempt: `{pa['package_id']}` ({pa['status']}, review {pa['review_state']}), kept unchanged", ""]
    if p.get("selection_basis"):
        L += ["## How this story was chosen", ""] + [f"- **{k}**: {v}" for k, v in p["selection_basis"].items()] + [""]
    if p.get("angle_review"):
        ar = p["angle_review"]
        L += [f"## Reviewed angle (review {ar['id']}, {ar['decision']} by {ar['reviewer']})", ""]
        L += [f"- **{k}**: {ar['angle'].get(k)}" for k in store.ANGLE_FIELDS]
        L += ["", "Cited passages (the only text sent for drafting):", ""]
        L += [f"- {r['publisher']}, {r['url']} ({', '.join(r.get('supports', []))}): \"{r['passage']}\"" for r in ar["refs"]] + [""]
    if p.get("angle_search"):
        L += ["## Automated angle search", ""] + _attempt_lines(p["angle_search"]) + [""]
    if p.get("angle_proposal"):
        ap = p["angle_proposal"]
        L += [f"## System-proposed angle (proposal {ap['id']}; not a review)", ""]
        cites = ap["angle"].get("excerpts") or {}
        L += [f"- **{k}**: {ap['angle'].get(k)} (excerpts {', '.join(cites.get(k) or []) or 'quote'}; exact stored "
              f"text: \"{ap['angle']['quotes'][k]}\")" for k in store.ANGLE_FIELDS]
        L += ["", "Cited passages (the only text sent for drafting):", ""]
        L += [f"- {r['id']} {r['publisher']}, {r['url']} ({', '.join(r.get('supports', []))}): \"{r['passage']}\""
              for r in ap["refs"]] + [""]
    if p.get("presentation"):
        L += ["## Graphics (chosen automatically)", ""] + [f"- Slide {i}: `{json.dumps(v, ensure_ascii=False)}`"
                                                          for i, v in enumerate(p["presentation"]["visuals"], 1)] + [""]
    if p.get("acceptance_blockers"):
        L += ["## Acceptance blockers (must be explicitly resolved by the reviewer)", ""]
        L += [f"- **{b['id']}**: {b['issue']}" for b in p["acceptance_blockers"]] + [""]
    if p.get("draft_not_ready"):
        L += ["## Draft that did not pass (kept for review)", "", f"`{json.dumps(p['draft_not_ready'], ensure_ascii=False)}`", ""]
    for i, sl in enumerate(p["slides"] or [], 1):
        L += [f"## Slide {i} — {sl.get('heading')}", "", sl.get("body", ""), "", f"_facts: {', '.join(sl.get('fact_ids', []))}_", ""]
    if p["explanation"]:
        L += ["## Why this story (editorial explanation)", "", p["explanation"].get("text", ""), "",
              f"_{p['explanation']['label']}_", ""]
    L += ["## Facts and sources", ""] + [f"- **{f['id']}** [{f.get('verification', {}).get('verdict', 'n/a')}] "
                                          f"{f['statement']} — {f['source']['attribution']}, {f['source']['url']} "
                                          f"({f['corroboration']})" for f in p["facts"]]
    for r in (p.get("claim_verification") or {}).get("rounds", []):
        L += ["", f"## Claim verification: {r['step']}", "", "| claim | location | text | facts | code | verifier | verdict |",
              "|---|---|---|---|---|---|---|"]
        L += [f"| {c['id']} | {c['location']} | {c['text']} | {', '.join(c['fact_ids'])} | {c['code']['verdict']} "
              f"{'; '.join(c['code']['reasons'])} | {c['model']['verdict']}: {c['model']['reason']} | **{c['verdict']}** |"
              for c in r["claims"]]
        L += ["", "| fact | passage (context) | code | verifier | verdict |", "|---|---|---|---|---|"]
        L += [f"| {f['id']} | {(f['passage'] or '')[:300]} | {f['code']['verdict']} {'; '.join(f['code']['reasons'])} | "
              f"{f['model']['verdict']}: {f['model']['reason']} | **{f['verdict']}** |" for f in r["facts"]]
    L += ["", "## Screening snapshot", "", f"`{json.dumps(p['screening'])}`", "", "## Photo", "",
          f"{p['photo']['status']}: {p['photo']['reason']}", "", "## Rights handling", "", p["rights_handling"], "",
          "## Caveats", "", *(f"- {c}" for c in p["integration_caveats"]), "", "## Development review", "",
          f"`{json.dumps(p['development_review'])}`", "", "## Generation", "",
          f"`{json.dumps({k: v for k, v in p['generation'].items()})}`"]
    return "\n".join(L) + "\n"


def _screen_report(run_id, integrations, counts, records, selected, reason, notes):
    L = [f"# Screening run {run_id}", "", f"- Model: `{json.dumps(LAYA_MODEL)}`",
         f"- Rubric {screen.RUBRIC_VERSION}: `{json.dumps(screen.RUBRIC)}`", f"- Mapping: {screen.MAPPING_DOC}",
         f"- Ordering: {screen.ORDERING_DOC}",
         "- Scores are raw, uncalibrated Laya choice probabilities. They are not editor-approval probabilities, "
         "and Laya gives no prose reasoning.", "", "## Integrations", "",
         *(f"- {k}: **{v['status']}** {v['reason']}" for k, v in integrations.items()), "", "## Counts", "",
         *(f"- {k}: {v}" for k, v in counts.items()), "", "## Candidates", "",
         "| rank | athlete | mapped | score | windows | eligible | eligibility reasons | reused | warnings |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(records, key=lambda r: (r.get("rank") or 999, r["athlete_id"])):
        L.append(f"| {r.get('rank', '-')} | {r['athlete_id']} {r['name']} | {r['mapped']} | "
                 f"{'' if r['score'] is None else round(r['score'], 4)} | {r['windows']} | {r['eligible']} | "
                 f"{'; '.join(r['eligibility']['reasons']) or '-'} | {r['reused_screening']} | {'; '.join(r['warnings']) or '-'} |")
    L += ["", "## Selection", "", f"- {reason}", f"- selected: {selected and selected['athlete_id']}", "",
          "## Notes", "", *(f"- {n}" for n in notes or ["none"])]
    return "\n".join(L) + "\n"
