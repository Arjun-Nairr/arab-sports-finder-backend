"""Workflow coordination for T01-T02: discover -> persist -> attach news -> report."""
import json
import re
import secrets
import time

import psycopg

from . import sources as src
from . import store
from .config import NATIONS, RUNS_DIR, public_config, settings
from .matching import name_index, norm, resolve_article


def safe(e):
    msg = (str(e).strip().splitlines() or [""])[0]
    msg = re.sub(r"postgres(?:ql)?://\S+", "postgresql://[redacted]", msg)
    return f"{type(e).__name__}: {msg}"[:300]


def provisional_lead(m, article):
    """Source-local identity: one provisional row per (article, name). Name and nation cues alone never
    make two mentions the same person; only reconciliation to a structured identity joins them."""
    nat = m["evidence"].get("nation_context", [])
    return {
        "source": "news_mention", "source_id": f"{article['canonical_url']}#{norm(m['surface'])}",
        "name": m["surface"], "aliases": [], "sports": [], "representation": [], "citizenship": [],
        "eligibility": "unresolved", "resolution": "provisional",
        "gaps": ["identity from a news mention only", "representation not verified", "sport not structured"],
        "external": {"news_context": {"nations_in_sentence": [NATIONS[q][0] for q in nat],
                                      "first_article": article["canonical_url"],
                                      "section": article["extra"].get("section")}},
    }


def recheck_stored_articles(conn, exclude_ids, L):
    """Bounded, resumable reconsideration of stored articles for structured identities whose names or
    matching context changed (match_fp != match_checked_fp). Each identity pages through its matches by
    article id from its saved cursor; it is complete only when a page comes back short."""
    rows, pending = store.identities_to_recheck(conn, L["recheck_identities"])
    budget, picked, completed, cursors, notes = L["recheck_articles"], {}, [], {}, []
    for aid, name, aliases, cursor in rows:
        if budget <= 0:
            break
        names = [n for n in {norm(name), *(norm(x) for x in aliases)} if len(n.split()) >= 2]
        found = store.stored_articles_matching(conn, names, cursor, budget + 1) if names else []
        page = found[:budget]
        for a in page:
            if a["id"] not in exclude_ids:  # current-batch articles are resolved anyway
                picked.setdefault(a["id"], a)
        budget -= len(page)
        if len(found) > len(page):
            cursors[aid] = page[-1]["id"]  # more remain: resume after this article next run
            notes.append(f"recheck for athlete {aid} paused at article {page[-1]['id']} (budget reached)")
            break
        completed.append(aid)
    deferred = pending - len(completed)
    if deferred:
        notes.append(f"{deferred} identities still pending stored-article recheck (limits {L['recheck_identities']} "
                     f"identities / {L['recheck_articles']} articles per run)")
    return list(picked.items()), {"completed": completed, "cursors": cursors, "deferred": deferred, "notes": notes}


def run(mode="live", replay_of=None, schema=None):
    s = settings()
    L = s["limits"]
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2)
    out_dir = RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    integrations, notes, counts, raw = {}, [], {}, {}
    ctx = {"run_id": run_id, "mode": mode, "replay_of": replay_of, "config": public_config(s)}

    if not s["database_url"]:
        integrations["neon"] = src.outcome("not_attempted", "DATABASE_URL not set in demo/.env or environment")
        return _finish(None, ctx, integrations, counts, "blocked", ["Neon unavailable; nothing fetched or stored"], out_dir)
    try:
        conn = store.connect(s["database_url"], schema)
    except psycopg.Error as e:
        integrations["neon"] = src.outcome("failed", safe(e))
        return _finish(None, ctx, integrations, counts, "blocked", ["Neon connection failed; nothing stored"], out_dir)
    integrations["neon"] = src.outcome("worked", "connected; schema applied")
    store.start_run(conn, run_id, mode, ctx["config"], replay_of)

    outcome = "failed"
    try:
        if mode == "replay":
            raw = json.loads((RUNS_DIR / replay_of / "raw.json").read_text(encoding="utf-8"))
            for k in ("wikidata", "ajel_feed", "ajel_wire", "gdelt", "thesportsdb"):
                integrations[k] = src.outcome("not_attempted", f"replay of stored input from run {replay_of}")
        else:
            raw["wikidata"], integrations["wikidata"] = src.fetch_wikidata(
                L, store.load_cursors(conn), store.known_source_ids(conn, "wikidata"),
                store.stale_profiles(conn, "wikidata", L["wikidata_refresh"]))
        athlete_leads = src.parse_wikidata(raw["wikidata"])
        athletes = store.upsert_athletes(conn, athlete_leads, run_id)
        if mode != "replay":  # cursors advance only after their athletes are stored; replays never move them
            committed = src.committable_cursors(raw["wikidata"])
            store.save_cursors(conn, committed, run_id)
            qs = raw["wikidata"].get("queries", {})
            counts["discovery"] = {
                "new_ids": sorted({i for q in qs.values() for i in q["new"]}),
                "repeats": sum(len(q["repeats"]) for q in qs.values()),
                "exhausted_queries": sum(q["exhausted"] for q in qs.values()),
                "failed_queries": sorted(k for k, q in qs.items() if q["error"]),
                "cursors_committed": len(committed), "cursors_held": sorted(set(qs) - set(committed)),
                "refreshed": len(raw["wikidata"].get("refresh_ids", []))}

        if mode != "replay":
            raw["ajel"], res = src.fetch_ajel(L)
            integrations.update(res)
            targets = store.athletes_not_searched(conn, "ajel_wire_search", L["ajel_name_searches"])
            raw["ajel_wire"], integrations["ajel_wire"] = src.fetch_ajel_wire(L, s["ajel_key"], [(t[1], t[2]) for t in targets])
            if L["gdelt_enabled"]:
                raw["gdelt"], integrations["gdelt"] = src.fetch_gdelt(L, athlete_leads)
            else:
                raw["gdelt"] = {"news_first": [], "by_name": []}
                integrations["gdelt"] = src.outcome("not_attempted", "GDELT_ENABLED=0: throttled (HTTP 429) from this network")
        wire = raw.get("ajel_wire", {})
        ids_by_source = {lead["source_id"]: aid for aid, _, lead in athletes}
        for s_ in wire.get("searches", []):
            if "hits" in s_ and s_["source_id"] in ids_by_source:
                store.set_enrichment(conn, ids_by_source[s_["source_id"]], "ajel_wire_search",
                                     {"run": run_id, "q": s_["q"], "hits": s_["hits"]})
        seen, article_leads = set(), []
        for a in src.parse_ajel_wire(wire) + src.parse_ajel(raw["ajel"]) + src.parse_gdelt(raw["gdelt"]):
            if a["canonical_url"] not in seen:
                seen.add(a["canonical_url"])
                article_leads.append(a)
        articles = store.upsert_articles(conn, article_leads, run_id)
        store.backfill_search_text(conn)

        library = store.load_identities(conn)
        by_id, idx = {a["id"]: a for a in library}, name_index(library)
        status_by_id = {aid: st for aid, st, _ in articles}
        affected = set()  # athletes whose stored associations were invalidated
        for aid, st in status_by_id.items():
            if st == "withdrawn":
                affected |= store.supersede_stale(conn, aid, [], run_id, "withdrawn by provider")
        # Resolve against retained authoritative content, never against a poorer incoming observation.
        retained = store.load_articles(conn, [a for a, st in status_by_id.items() if st != "withdrawn"])
        current = [(aid, row) for aid, row in retained.items() if row["content_kind"] != "withdrawn"]
        stored, recheck = recheck_stored_articles(conn, set(status_by_id), L)
        # Bounded revalidation: links made under earlier rules/context are re-resolved; unsupported ones are
        # superseded below (history kept) and their athletes refreshed.
        revalidate = {a["id"]: a for a in store.articles_with_links(conn, set(status_by_id) | {i for i, _ in stored},
                                                                    L["recheck_articles"])}
        resolved = [(aid, lead, resolve_article(lead, by_id, idx)) for aid, lead in current + stored + list(revalidate.items())]
        prov_leads = {}
        for _, lead, ms in resolved:
            for m in ms:
                if m["status"] == "provisional":
                    pl = provisional_lead(m, lead)
                    m["_key"] = pl["source_id"]
                    prov_leads.setdefault(pl["source_id"], pl)
        prov = store.upsert_athletes(conn, list(prov_leads.values()), run_id)
        prov_ids = {lead["source_id"]: aid for aid, _, lead in prov}
        new_mentions = upgraded = 0
        reconciled = []
        for aid, _, ms in resolved:
            for m in ms:
                if m["status"] == "provisional":
                    m["athlete_id"] = prov_ids[m.pop("_key")]
            # Every resolved article (current, rechecked or revalidated) keeps only mentions its retained text supports
            # under the current rules; a no-op for new articles, which have no stored mentions.
            affected |= store.supersede_stale(conn, aid, ms, run_id, "authoritative revision" if status_by_id.get(aid) == "changed"
                                              else "link revalidation under current matching rules")
            n, u, r = store.upsert_mentions(conn, aid, ms, run_id)
            new_mentions, upgraded, reconciled = new_mentions + n, upgraded + u, reconciled + r
        store.save_recheck_progress(conn, recheck["completed"], recheck["cursors"])
        ctx["reconciled"] = reconciled

        linked_ids = [m["athlete_id"] for _, _, ms in resolved for m in ms if m["status"] == "linked"]
        targets = list(dict.fromkeys(linked_ids + [aid for aid, st, _ in prov if st == "new"]))[: L["thesportsdb_lookups"]]
        names = [by_id[t]["name"] if t in by_id else next(l["name"] for i, _, l in prov if i == t) for t in targets]
        if mode != "replay":
            raw["thesportsdb"], integrations["thesportsdb"] = src.fetch_thesportsdb(names, L)
        for t, n in zip(targets, names):
            players = raw.get("thesportsdb", {}).get(n)
            if players is None:
                notes.append(f"TheSportsDB lookup for {n} failed or skipped; athlete kept unchanged")
                continue
            store.set_enrichment(conn, t, "thesportsdb", {
                "status": "unverified_candidates" if players else "no_results",
                "note": "name search only; not used for identity or eligibility",
                "candidates": src.tsdb_candidates(players)})

        touched = ({aid for aid, _, _ in athletes} | set(prov_ids.values()) | set(linked_ids)
                   | {i for pair in reconciled for i in pair} | affected)
        changes = store.refresh_evidence(conn, touched)
        counts = {
            **({"discovery": counts["discovery"]} if "discovery" in counts else {}),
            "athlete_leads": len(athletes),
            "athletes_new": sum(st == "new" for _, st, _ in athletes),
            "athletes_existing": sum(st == "existing" for _, st, _ in athletes),
            "athletes_eligibility_claimed_or_unresolved": sum(l["eligibility"] != "supported" for _, _, l in athletes),
            "provisional_news_only": len(prov), "provisional_new": sum(st == "new" for _, st, _ in prov),
            "articles": len(articles), **{f"articles_{k}": sum(st == k for _, st, _ in articles)
                                          for k in ("new", "existing", "changed", "poorer_observation", "withdrawn")},
            "stored_articles_rechecked": len(stored), "links_revalidated_articles": len(revalidate),
            "identities_rechecked": len(recheck["completed"]),
            "identities_recheck_deferred": recheck["deferred"],
            "mentions_detected": sum(len(ms) for _, _, ms in resolved), "mentions_new": new_mentions,
            "mentions_upgraded_to_linked": upgraded, "provisional_reconciled": len(reconciled),
            "athletes_affected_by_revisions": len(affected), "identities_recheck_paused": len(recheck["cursors"]),
            "evidence_version_changes": len(changes),
        }
        notes += recheck["notes"]
        ctx["changes"] = changes
        ok = integrations.get("wikidata", {}).get("status") in ("worked", "not_attempted")
        outcome = "completed" if ok else "completed_with_source_failures"
    except Exception as e:  # report any failure readably; partial transactions already committed stay
        notes.append(f"run stopped: {safe(e)}")
    finally:
        if mode != "replay" and raw:
            (out_dir / "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return _finish(conn, ctx, integrations, counts, outcome, notes, out_dir)


def _finish(conn, ctx, integrations, counts, outcome, notes, out_dir):
    for k, v in integrations.items():
        for err in v.get("errors", [])[:5]:
            notes.append(f"{k}: {err}")
    if conn:
        store.finish_run(conn, ctx["run_id"], integrations, counts, outcome, notes)
    report = _report(conn, ctx, integrations, counts, outcome, notes)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    if conn:
        conn.close()
    return {"run_id": ctx["run_id"], "outcome": outcome, "report": str(out_dir / "report.md"), "counts": counts}


def _report(conn, ctx, integrations, counts, outcome, notes):
    rid = ctx["run_id"]
    lines = [f"# Run {rid}", "", f"- Mode: **{ctx['mode']}**" + (f" (replay of {ctx['replay_of']}, no network)" if ctx["replay_of"] else ""),
             f"- Outcome: **{outcome}**", f"- Limits: `{json.dumps(ctx['config']['limits'])}`",
             f"- Credentials present: `{json.dumps(ctx['config']['credentials_present'])}`", "",
             "## Integration outcomes", "", "| Integration | Status | Reason |", "|---|---|---|"]
    lines += [f"| {k} | {v['status']} | {v['reason'][:200]} |" for k, v in integrations.items()]
    lines += ["", "## Counts", "", *(f"- {k}: {v}" for k, v in counts.items())]
    if conn and counts:
        q = lambda sql: conn.execute(sql, (rid,)).fetchall()
        lines += ["", "## Athletes touched this run (read back from Neon)", "",
                  "| id | source | name | sport | represented (P1532) | eligibility | evidence | v | gaps |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in q("SELECT id, source, source_id, name, sports, representation, eligibility, evidence_status, "
                   "evidence_version, gaps FROM athletes WHERE last_run = %s ORDER BY source, id"):
            sport = ", ".join(x["label"] for x in r[4]) or "unknown"
            tag = lambda x: (" [sourced ref]" if any(f["stated_in"] or f["urls"] for f in x["references"])
                             else " [imported-from only]" if x["references"] else "")
            rep = ", ".join(f"{x['nation']}{'' if x['listed'] else ' (not listed)'}{tag(x)}" for x in r[5]) or "none"
            lines.append(f"| {r[0]} | {r[1]}:{r[2]} | {r[3]} | {sport} | {rep} | {r[6]} | {r[7]} | {r[8]} | {'; '.join(r[9])} |")
        lines += ["", "## Mentions in articles touched this run", "",
                  "| status | surface | athlete | article | method | evidence |", "|---|---|---|---|---|---|"]
        for r in q("SELECT m.status, m.surface, a.name, ar.publisher, ar.title, m.method, m.evidence, m.candidates "
                   "FROM mentions m JOIN articles ar ON ar.id = m.article_id LEFT JOIN athletes a ON a.id = m.athlete_id "
                   "WHERE ar.last_run = %s ORDER BY m.status, m.id LIMIT 60"):
            ev = {k: v for k, v in r[6].items() if k != "sentence"}
            lines.append(f"| {r[0]} | {r[1]} | {r[2] or 'candidates ' + str(r[7])} | {r[3]}: {(r[4] or '')[:70]} | {r[5]} | `{json.dumps(ev)[:120]}` |")
        lines += ["", "## Provisional leads reconciled to structured identities", ""]
        lines += [f"- provisional athlete {p} -> athlete {s}" for p, s in ctx.get("reconciled", [])] or ["- none"]
        lines += ["", "## Articles by acquisition (this run)", ""]
        for r in q("SELECT provider, content_kind, acquisition, count(*) FROM articles WHERE last_run = %s GROUP BY 1,2,3"):
            lines.append(f"- {r[0]} / {r[1]} / {r[2]}: {r[3]}")
        dup = q("SELECT content_hash, count(*), array_agg(DISTINCT publisher), min(title) FROM articles "
                "WHERE last_run = %s GROUP BY content_hash HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 10")
        lines += ["", "## Copied/syndicated reports (same normalized content, counted once as evidence)", ""]
        lines += [f"- {r[1]} URLs, publishers {', '.join(p or '?' for p in r[2])}: {(r[3] or '')[:90]}" for r in dup] or ["- none detected"]
        distinct = q("SELECT count(DISTINCT content_hash), count(*) FROM articles WHERE last_run = %s")[0]
        lines.append(f"- distinct contents {distinct[0]} across {distinct[1]} URLs")
        lines += ["", "## Evidence version changes", ""]
        lines += [f"- {c['name']} (id {c['athlete_id']}): v{c['from_version']} -> v{c['to_version']}, "
                  f"{c['articles']} distinct article(s), {c['evidence_status']}" for c in ctx.get("changes", [])] or ["- none"]
    lines += ["", "## Notes and failures", "", *(f"- {n}" for n in notes)] if notes else ["", "## Notes and failures", "", "- none"]
    return "\n".join(lines) + "\n"
