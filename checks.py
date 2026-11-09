"""Focused functional checks for T01-T02 risks. All inputs here are labelled FIXTURES, not API results.
DB checks run in a temporary Neon schema that is dropped afterwards; the real library is untouched.
Persistence is asserted through a second, fresh connection.
Run: .venv\\Scripts\\python.exe checks.py"""
import json
import os
import secrets
import shutil
import time

import psycopg

from arabsports import sources, store
from arabsports.config import RUNS_DIR, settings
from arabsports.http import FetchError, get
from arabsports.matching import extract_cued_names, evidence_fingerprint, name_index, norm, resolve_article
from arabsports.run import run

MOR = {"nation_qid": "Q1028", "nation": "Morocco", "listed": True, "rank": "normal", "statement": "S1",
       "start": None, "end": None, "references": [{"stated_in": [], "urls": ["https://fixture.test/ref1"], "imported_from": []}]}
EGY = {**MOR, "nation_qid": "Q79", "nation": "Egypt", "statement": "S2"}


def athlete(id_, name, sport, rep):
    return {"id": id_, "name": name, "norm_name": norm(name), "aliases": [], "sports": [{"label": sport}], "representation": [rep]}


def article(text, title="FIXTURE"):
    return {"title": title, "content": text}


# ---------- offline ----------

def check_matching():
    lib = [athlete(1, "Ali Hassan", "association football", MOR), athlete(2, "Ali Hassan", "athletics", EGY),
           athlete(3, "Nour Salem", "fencing", MOR), athlete(4, "Karim", "boxing", EGY)]
    by_id, idx = {a["id"]: a for a in lib}, name_index(lib)
    (m,) = [x for x in resolve_article(article("Ali Hassan scored for Morocco in football."), by_id, idx) if x["surface"] == "ali hassan"]
    assert m["status"] == "ambiguous" and m["athlete_id"] is None and m["candidates"] == [1, 2], m
    (m,) = resolve_article(article("Nour Salem spoke to reporters."), by_id, idx)
    assert m["status"] == "no_context" and m["athlete_id"] is None, m
    (m,) = resolve_article(article("Moroccan Nour Salem reached the final."), by_id, idx)
    assert m["status"] == "linked" and m["athlete_id"] == 3, m
    assert "karim" not in idx, "single-token names must not be matched in text"
    names = [n for n, _ in extract_cued_names("Saudi striker Salem Al-Dawsari scored twice in the Saudi Pro League.")]
    assert names == ["Salem Al-Dawsari"], names
    assert extract_cued_names("The player said the Saudi Pro League resumes Friday.") == []
    (m,) = resolve_article(article("Moroccan sports analyst Nour Salem previewed the final."), by_id, idx)
    assert m["status"] == "role_conflict" and m["athlete_id"] is None and m["evidence"]["role_conflict"] == "analyst", m
    ref = {**athlete(5, "Hadi Karam", "association football", EGY), "occupations": ["association football referee"]}
    (m,) = resolve_article(article("Egyptian referee Hadi Karam took charge of the final."), {5: ref}, name_index([ref]))
    assert m["status"] == "linked", "a role the athlete's occupation confirms still links"
    print("ok  matching: same-name athletes stay separate; links need context; cue extraction conservative; "
          "unconfirmed role cue ('sports analyst X') blocks a link")


def check_revalidation_db():
    """A stored link the current rules reject is superseded (history kept) and the athlete refreshed. FIXTURE."""
    url = settings()["database_url"]
    if not url:
        print("SKIP revalidation db check: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    A = "https://fixture.test/sports/rv1"
    try:
        replay("r1", raw_input(entities=[("QRK", "Karim Benali", "Q2736", "Q1028")],
                               stories=[(A, "FIXTURE", "Moroccan forward Karim Benali scored the winner.")]), schema, made)
        txt = "Moroccan sports analyst Karim Benali previewed the final."
        conn.execute("UPDATE articles SET content = %s, search_text = %s WHERE canonical_url = %s",
                     (txt, " " + norm(txt) + " ", A))  # FIXTURE: stored text the current rules no longer link
        c = replay("r2", raw_input(), schema, made)
        with store.connect(url, schema) as f:
            m = f.execute("SELECT status, athlete_id, jsonb_array_length(evidence->'history') FROM mentions").fetchone()
            ev = f.execute("SELECT evidence_status FROM athletes WHERE source_id = 'QRK'").fetchone()[0]
        assert c["links_revalidated_articles"] == 1 and c["athletes_affected_by_revisions"] == 1, c
        assert m == ("role_conflict", None, 1) and ev == "needs_evidence", (m, ev)
        print("ok  revalidation: stored link rejected by current rules -> superseded with history, now role_conflict; "
              "athlete back to needs_evidence")
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def check_fingerprint():
    a = evidence_fingerprint([MOR, EGY], ["h1", "h2"])
    assert a == evidence_fingerprint([EGY, MOR], ["h2", "h1", "h1"]), "order/duplicates must not change it"
    assert a != evidence_fingerprint([MOR, EGY], ["h1", "h2", "h3"]), "new article content must change it"
    same_count = {**MOR, "references": [{"stated_in": [], "urls": ["https://fixture.test/ref2"], "imported_from": []}]}
    assert a != evidence_fingerprint([same_count, EGY], ["h1", "h2"]), "changed reference URL (same count) must change it"
    assert a != evidence_fingerprint([{**MOR, "rank": "deprecated"}, EGY], ["h1", "h2"]), "rank change must change it"
    assert a != evidence_fingerprint([{**MOR, "nation_qid": "Q846"}, EGY], ["h1", "h2"]), "claim value change must change it"
    reordered = {**MOR, "references": [{"urls": ["https://fixture.test/ref1"], "imported_from": [], "stated_in": []}]}
    assert a == evidence_fingerprint([reordered, EGY], ["h1", "h2"]), "key order must not change it"
    print("ok  fingerprint: reference contents, rank and value count; ordering and copies do not")


def check_urls():
    c = sources.canonical_url
    assert c("https://x.test/article.php?id=1") != c("https://x.test/article.php?id=2"), "semantic query collided"
    assert c("https://www.X.test/a?id=1&utm_source=t&fbclid=z#top") == c("https://x.test/a?id=1"), "tracking not removed"
    assert c("https://x.test/a?b=2&a=1") == c("https://x.test/a?a=1&b=2"), "param order should not matter"
    print("ok  urls: semantic query kept, tracking/fragment dropped")


def check_wire_parse():
    art = lambda st: {"canonical_url": "https://english.ajel.sa/sports/fx", "title": "FIXTURE", "status": st,
                      "content_text": "FIXTURE body", "language": "en", "usage": {"modification_allowed": False}}
    raw = {"searches": [{"source_id": "QFIX", "q": "FIXTURE", "hits": ["p", "u", "w"]}],
           "articles": {"p": art("published"), "u": art("updated"), "w": art("withdrawn")}}
    p, u, w = sources.parse_ajel_wire(raw)
    assert (p["content_kind"], p["content"], p["extra"]["queried_for"]) == ("full_text", "FIXTURE body", ["QFIX"]), p
    assert (u["content_kind"], u["source_status"]) == ("full_text", "updated"), "an 'updated' story is still live"
    assert (w["content_kind"], w["content"]) == ("withdrawn", None), w
    print("ok  wire parse: published/updated -> full text (status kept); explicit removal status -> withdrawal")


def check_windows():
    from arabsports import screen
    w = screen.Windows()
    sent = "Nour Salem trained every morning before school and later coached children in her neighbourhood club."
    body = "\n\n".join([" ".join([sent] * 6)] * 4 + ["An unrelated paragraph about ticket prices."])
    ath = {"name": "Nour Salem", "aliases": []}
    wins = w.build(ath, [{"id": 1, "canonical_url": "u", "publisher": "FIXTURE", "title": "FIXTURE", "content": body}])
    assert all(x["state_tokens"] <= screen.STATE_TOKENS for x in wins), [x["state_tokens"] for x in wins]
    covered = sum(x["text"].count(sent) for x in wins)
    assert covered == 24 and not any("ticket prices" in x["text"] for x in wins), (covered, len(wins))
    print(f"ok  windows: long evidence split into {len(wins)} windows <= {screen.STATE_TOKENS} tokens, all 24 relevant "
          f"sentences kept, unrelated paragraph excluded")


def check_bounded_failure():
    t = time.monotonic()
    try:
        get("https://10.255.255.1/", timeout=1, retries=1)
        raise AssertionError("unreachable host returned")
    except FetchError as e:
        assert time.monotonic() - t < 10, "retries must be finite and short"
        print(f"ok  bounded failure: {e} after {time.monotonic() - t:.1f}s")
    real_get = sources.get
    sources.get = lambda *a, **k: (_ for _ in ()).throw(FetchError("FIXTURE outage"))
    try:
        raw, res = sources.fetch_thesportsdb(["Nour Salem"], settings()["limits"])
    finally:
        sources.get = real_get
    assert res["status"] == "failed" and raw == {"Nour Salem": None}, res
    print("ok  optional enrichment outage is recorded as failed, nothing returned to apply")


def check_missing_db():
    saved = os.environ.get("DATABASE_URL")
    try:
        for value, label in (("", "missing DATABASE_URL"), ("postgresql://fixture@127.0.0.1:1/none", "unreachable Neon")):
            os.environ["DATABASE_URL"] = value  # set value wins over demo/.env (setdefault)
            r = run("live")
            shutil.rmtree(RUNS_DIR / r["run_id"], ignore_errors=True)
            assert r["outcome"] == "blocked" and not r["counts"], r
            print(f"ok  {label} -> blocked, nothing fetched or stored")
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL")
        else:
            os.environ["DATABASE_URL"] = saved


# ---------- FIXTURE raw inputs for the real entry point (run('replay')) ----------

def wd_entity(qid, name, sport, nation, aliases=()):
    claim = lambda v, i: {"id": f"{qid}${i}", "rank": "normal", "mainsnak": {"datavalue": {"value": {"id": v}}},
                          "references": [{"snaks": {"P854": [{"datavalue": {"value": f"https://fixture.test/{qid}"}}]}}]}
    return {"labels": {"en": {"value": name}}, "aliases": {"en": [{"value": a} for a in aliases]}, "sitelinks": {},
            "claims": {"P641": [claim(sport, "s")] if sport else [], "P1532": [claim(nation, "r")], "P27": [], "P569": []}}


def raw_input(entities=(), stories=(), gdelt=(), wire=()):
    items = [{"url": u, "title": t, "section": "Sports", "published_at": "2026-11-01T00:00:00Z",
              "updated_at": "2026-11-01T00:00:00Z", "summary": "FIXTURE", "markdown_url": u + ".md"} for u, t, _ in stories]
    return {
        "wikidata": {"ids_by_nation": {}, "errors": [], "entities": {e[0]: wd_entity(*e) for e in entities},
                     "labels": {"Q2736": "association football", "Q542": "athletics", "Q1028": "Morocco", "Q79": "Egypt",
                                "Q810": "Jordan", "Q12100": "fencing"}},
        "ajel_wire": {"searches": [], "errors": [], "articles": {
            wid: {"canonical_url": u, "title": t, "status": st, "content_text": body, "language": "en", "usage": usage,
                  "published_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z",
                  "attribution": {"brand_latin": "FIXTURE Ajel", "text": "Source: FIXTURE"}}
            for wid, u, t, body, usage, st in wire}},
        "ajel": {"feed": {"source": "FIXTURE Ajel", "language": "en", "attribution": "FIXTURE", "items": items},
                 "markdown": {u: f"# {t}\n\n## Story\n\n{body}" for u, t, body in stories},
                 "selected": [u for u, _, _ in stories], "errors": []},
        "gdelt": {"news_first": [{"source_id": None, "query": "FIXTURE", "articles": [
            {"url": u, "title": t, "domain": "fixture-gdelt.test", "language": "English", "seendate": "20261101T000000Z"}
            for u, t in gdelt]}], "by_name": []},
        "thesportsdb": {},
    }


def replay(step, raw, schema, made):
    d = RUNS_DIR / f"fixture-{schema}-{step}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    made.append(d)
    r = run("replay", replay_of=d.name, schema=schema)
    made.append(RUNS_DIR / r["run_id"])
    assert r["outcome"] == "completed", (step, r)
    return r["counts"]


def check_pipeline_db():
    url = settings()["database_url"]
    if not url:
        print("SKIP db checks: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    fresh = lambda: store.connect(url, schema)
    A = lambda n: f"https://fixture.test/sports/a{n}"
    try:
        # 1. News arrives before any structured identity. Two different reports of a same-name, same-nation
        #    "Salem Hamdan" must stay two provisional people.
        c1 = replay("1", raw_input(stories=[
            (A(1), "FIXTURE derby", "Moroccan striker Nour Salem scored twice in the derby."),
            (A(2), "FIXTURE cup", "Saudi striker Salem Hamdan scored in the cup."),
            (A(3), "FIXTURE youth", "Saudi striker Salem Hamdan, 17, debuted for the youth side."),
            (A(4), "FIXTURE final", "Egyptian striker Ali Hassan scored in the final."),
        ]), schema, made)
        with fresh() as f:
            prov = dict(f.execute("SELECT norm_name, count(*) FROM athletes WHERE resolution = 'provisional' GROUP BY 1"))
        assert prov == {"nour salem": 1, "salem hamdan": 2, "ali hassan": 1}, prov
        print(f"ok  pipeline 1: same-name/same-nation news mentions stay separate provisional people {prov}")

        # 2. Structured identities appear later; the stored articles are absent from this feed. Recheck limit 1
        #    defers the rest. Nour Salem (one candidate + Morocco context) reconciles.
        os.environ["RECHECK_IDENTITIES"] = "1"
        ents = [("QFIXN", "Nour Salem", "Q2736", "Q1028"), ("QFIXA1", "Ali Hassan", "Q2736", "Q79"),
                ("QFIXA2", "Ali Hassan", "Q542", "Q79")]
        c2 = replay("2", raw_input(entities=ents), schema, made)
        os.environ.pop("RECHECK_IDENTITIES")
        assert c2["stored_articles_rechecked"] == 1 and c2["identities_recheck_deferred"] == 2, c2
        with fresh() as f:
            nour = f.execute("SELECT id, evidence_status, evidence_version FROM athletes WHERE source_id = 'QFIXN'").fetchone()
            rec = f.execute("SELECT resolution, reconciled_to, evidence_status FROM athletes "
                            "WHERE source = 'news_mention' AND norm_name = 'nour salem'").fetchone()
            link = f.execute("SELECT m.status, m.athlete_id, m.evidence->>'previous_status' FROM mentions m "
                             "JOIN articles a ON a.id = m.article_id WHERE a.canonical_url = %s", (A(1),)).fetchone()
        assert rec == ("reconciled", nour[0], "reconciled") and link == ("linked", nour[0], "provisional"), (rec, link)
        assert nour[1] == "has_full_text_news", nour
        print(f"ok  pipeline 2: stored article imported before its athlete attached on recheck; provisional "
              f"reconciled to athlete {nour[0]} (kept for provenance); 2 identities deferred by limit")

        # 3. Deferred identities resume. Two structured "Ali Hassan" = genuine ambiguity: provisional stays.
        #    A new report links to the structured Nour (provisional row is not a competing candidate).
        #    GDELT title-only sighting of a full-text URL must not erase it. Enrichment must survive refresh.
        store.set_enrichment(conn, nour[0], "thesportsdb", {"status": "FIXTURE"})
        step3 = raw_input(entities=ents, stories=[(A(5), "FIXTURE return", "Nour Salem returned to the Morocco squad.")],
                          gdelt=[(A(1), "FIXTURE poorer title-only sighting")])
        c3 = replay("3", step3, schema, made)
        assert c3["identities_recheck_deferred"] == 0 and c3["articles_poorer_observation"] == 1, c3
        with fresh() as f:
            ali = f.execute("SELECT resolution, reconciled_to FROM athletes WHERE source = 'news_mention' "
                            "AND norm_name = 'ali hassan'").fetchone()
            a4 = f.execute("SELECT m.status, jsonb_array_length(m.evidence->'structured_candidates') FROM mentions m "
                           "JOIN articles a ON a.id = m.article_id WHERE a.canonical_url = %s", (A(4),)).fetchone()
            a5 = f.execute("SELECT m.status, m.athlete_id FROM mentions m JOIN articles a ON a.id = m.article_id "
                           "WHERE a.canonical_url = %s", (A(5),)).fetchone()
            a1 = f.execute("SELECT provider, content_kind, content LIKE 'Moroccan striker%%', "
                           "extra->'observations'->'gdelt'->>'content_kind' FROM articles WHERE canonical_url = %s",
                           (A(1),)).fetchone()
            ext, v3 = f.execute("SELECT external, evidence_version FROM athletes WHERE id = %s", (nour[0],)).fetchone()
        assert ali == ("provisional", None) and a4 == ("provisional", 2), (ali, a4)
        assert a5 == ("linked", nour[0]), a5
        assert a1 == ("ajel", "full_text", True, "title_only"), a1
        assert ext.get("thesportsdb") == {"status": "FIXTURE"} and "wikidata_url" in ext, ext
        assert v3 == nour[2] + 1, (nour, v3)
        print(f"ok  pipeline 3: ambiguous 'Ali Hassan' stays provisional; new report links to structured Nour "
              f"(v{nour[2]}->v{v3}); full text kept after title-only sighting; enrichment survived refresh")

        # 4. Identical replay: nothing new, no version bumps.
        c4 = replay("4", step3, schema, made)
        assert c4["mentions_new"] == 0 and c4["evidence_version_changes"] == 0 and c4["articles_new"] == 0, c4
        with fresh() as f:
            totals = f.execute("SELECT (SELECT count(*) FROM athletes), (SELECT count(*) FROM articles), "
                               "(SELECT count(*) FROM mentions)").fetchone()
        print(f"ok  pipeline 4: identical replay adds nothing (athletes/articles/mentions = {totals})")

        # 5. Same-provider revision and explicit withdrawal; another provider cannot withdraw.
        store.start_run(conn, "fixture-direct", "fixture", {})
        lead = sources.parse_ajel(raw_input(stories=[(A(5), "FIXTURE return", "Revised: Nour Salem returned.")])["ajel"])[0]
        ((_, st, _),) = store.upsert_articles(conn, [lead], "fixture-direct")
        gone = {**lead, "content": None, "content_kind": "withdrawn", "acquisition": "FIXTURE 410"}
        ((_, st_g, _),) = store.upsert_articles(conn, [{**gone, "provider": "gdelt"}], "fixture-direct")
        ((_, st_w, _),) = store.upsert_articles(conn, [gone], "fixture-direct")
        changes = store.refresh_evidence(conn, [nour[0]])
        with fresh() as f:
            row = f.execute("SELECT content_kind, content IS NULL FROM articles WHERE canonical_url = %s", (A(5),)).fetchone()
        assert (st, st_g, st_w) == ("changed", "poorer_observation", "withdrawn") and row == ("withdrawn", True), (st, st_g, st_w, row)
        assert changes and changes[0]["articles"] == 1, changes
        print("ok  pipeline 5: same-provider revision = changed; other provider cannot withdraw; "
              "withdrawal clears content and drops it from evidence")
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def check_followup_db():
    """Follow-up review corrections, end to end through run('replay') on FIXTURE inputs."""
    url = settings()["database_url"]
    if not url:
        print("SKIP follow-up db checks: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    fresh = lambda: store.connect(url, schema)
    A = lambda n: f"https://fixture.test/sports/f{n}"
    KARIM, OMAR = ("QFK", "Karim Benali", "Q2736", "Q1028"), ("QFO", "Omar Zidane", "Q542", "Q79")
    WIRE_USAGE = {"republication_allowed": True, "attribution_required": True, "modification_allowed": False}

    def athlete_row(f, qid):
        return f.execute("SELECT id, evidence_status, evidence_version, evidence_fingerprint FROM athletes "
                         "WHERE source_id = %s", (qid,)).fetchone()

    def mention(f, n, name):
        return f.execute("SELECT m.status, m.athlete_id, jsonb_array_length(coalesce(m.evidence->'history', '[]')) "
                         "FROM mentions m JOIN articles a ON a.id = m.article_id "
                         "WHERE a.canonical_url = %s AND m.norm = %s", (A(n), norm(name))).fetchone()
    try:
        # 1. Feed copy first, then identical Wire text with usage flags.
        replay("a", raw_input(entities=[KARIM, OMAR], stories=[
            (A(1), "FIXTURE derby", "Moroccan forward Karim Benali scored the winner."),
            (A(2), "FIXTURE sprint", "Egyptian sprinter Omar Zidane won the 200m in athletics.")]), schema, made)
        c = replay("b", raw_input(wire=[("w1", A(1), "FIXTURE derby", "Moroccan forward Karim Benali scored the winner.",
                                          WIRE_USAGE, "published")]), schema, made)
        with fresh() as f:
            terms, chans = f.execute("SELECT usage_terms, extra->'observations' FROM articles WHERE canonical_url = %s",
                                     (A(1),)).fetchone()
        assert c["articles_existing"] == 1 and c["articles_changed"] == 0, c
        assert terms["flags"] == WIRE_USAGE and terms["flag_sources"] == ["ajel_wire"], terms
        assert set(chans) == {"ajel_feed", "ajel_wire"} and chans["ajel_wire"]["source_status"] == "published", chans
        print("ok  follow-up 1: identical Wire text kept as 'existing' but its usage flags (modification_allowed=false), "
              "attribution and status survive alongside the feed observation (fresh connection)")

        # 2+3. Authoritative revision removes Karim; Karim is absent from this run's discovery input.
        with fresh() as f:
            k0 = athlete_row(f, "QFK")
        rev = raw_input(stories=[(A(1), "FIXTURE derby", "The derby ended goalless after a late red card.")])
        c = replay("c", rev, schema, made)
        with fresh() as f:
            k1, m1 = athlete_row(f, "QFK"), mention(f, 1, "Karim Benali")
        assert c["articles_changed"] == 1 and c["athletes_affected_by_revisions"] == 1, c
        assert m1 == ("superseded", None, 1), m1
        assert k1[1] == "needs_evidence" and k1[2] == k0[2] + 1 and k1[3] != k0[3], (k0, k1)
        c = replay("d", rev, schema, made)
        with fresh() as f:
            k2, m2 = athlete_row(f, "QFK"), mention(f, 1, "Karim Benali")
        assert c["mentions_new"] == 0 and c["evidence_version_changes"] == 0 and (k2, m2) == (k1, m1), (c, k2, m2)
        print(f"ok  follow-up 2/3: revision removing Karim superseded the link (history kept); Karim, absent from "
              f"discovery, refreshed v{k0[2]}->v{k1[2]} {k0[1]}->{k1[1]}; repeat import stable")

        # 3. Withdrawal of Omar's only article while Omar is absent from discovery.
        with fresh() as f:
            o0 = athlete_row(f, "QFO")
        c = replay("e", raw_input(wire=[("w2", A(2), "FIXTURE sprint", "", WIRE_USAGE, "withdrawn")]), schema, made)
        with fresh() as f:
            o1, m3 = athlete_row(f, "QFO"), mention(f, 2, "Omar Zidane")
            kind = f.execute("SELECT content_kind, content IS NULL FROM articles WHERE canonical_url = %s", (A(2),)).fetchone()
        assert c["articles_withdrawn"] == 1 and kind == ("withdrawn", True) and m3[0] == "superseded", (c, kind, m3)
        assert o1[1] == "needs_evidence" and o1[2] == o0[2] + 1 and o1[3] != o0[3], (o0, o1)
        print(f"ok  follow-up 3: withdrawal of Omar's only article (Omar absent from input): v{o0[2]}->v{o1[2]}, "
              f"{o0[1]}->{o1[1]}, fingerprint changed")

        # 4. Five stored reports name Lina Haddad before she is discovered; budget 2 articles per run.
        replay("f", raw_input(stories=[(A(10 + i), f"FIXTURE bout {i}", f"Jordanian fencer Lina Haddad won bout {i}.")
                                       for i in range(5)]), schema, made)
        lina = raw_input(entities=[("QFL", "Lina Haddad", "Q12100", "Q810")])
        os.environ["RECHECK_ARTICLES"] = "2"
        progress = []
        try:
            for step in ("g1", "g2", "g3", "g4"):
                c = replay(step, lina, schema, made)
                with fresh() as f:
                    row = f.execute("SELECT recheck_cursor, coalesce(match_checked_fp = match_fp, false), (SELECT count(*) FROM mentions "
                                    "WHERE athlete_id = a.id AND status = 'linked') FROM athletes a WHERE source_id = 'QFL'").fetchone()
                progress.append((c["stored_articles_rechecked"], *row))
        finally:
            os.environ.pop("RECHECK_ARTICLES")
        done = [p for p in progress if p[2]]
        assert [p[3] for p in progress] == [2, 4, 5, 5] and progress[0][2] is False and progress[1][2] is False, progress
        assert progress[2][2] and progress[3][0] == 0 and len(done) == 2, progress
        print(f"ok  follow-up 4: 5 matches, budget 2 -> runs processed {[p[0] for p in progress]} articles, "
              f"links {[p[3] for p in progress]}; complete only after exhaustion, no rework after")

        # 5. A no_context mention becomes linkable when the athlete gains a sport (no article re-fetch).
        replay("h", raw_input(entities=[("QFS", "Sami Nasser", None, "Q810", ("S. Nasser", "Sami Nasser Ali"))],
                              stories=[(A(20), "FIXTURE final", "Sami Nasser won the 400m final in athletics.")]), schema, made)
        with fresh() as f:
            before = mention(f, 20, "Sami Nasser")
        c = replay("i", raw_input(entities=[("QFS", "Sami Nasser", "Q542", "Q810", ("S. Nasser", "Sami Nasser Ali"))]),
                   schema, made)
        with fresh() as f:
            after = mention(f, 20, "Sami Nasser")
        c2 = replay("j", raw_input(entities=[("QFS", "Sami Nasser", "Q542", "Q810", ("Sami Nasser Ali", "S. Nasser"))]),
                    schema, made)
        assert before[0] == "no_context" and after[0] == "linked" and c["mentions_upgraded_to_linked"] == 1, (before, after, c)
        assert c2["identities_rechecked"] == 0 and c2["stored_articles_rechecked"] == 0, c2
        print("ok  follow-up 5: sport added -> stored no_context mention linked without re-fetch; reordered aliases "
              "caused no recheck")
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


FIX_BODY = ("Moroccan forward Karim Benali scored the winner in the 2024 cup final. Benali grew up in Fes and started "
            "playing football at a local academy. After a knee injury in 2022 he spent eight months in rehabilitation. "
            "He returned to captain the youth side and now mentors young players in Fes.")
FIX_FACTS = {"facts": [
    {"id": "F1", "statement": "Karim Benali scored the winning goal in the 2024 cup final.", "source": "S1",
     "evidence": "Karim Benali scored the winner in the 2024 cup final"},
    {"id": "F2", "statement": "Benali grew up in Fes and began at a local academy.", "source": "S1",
     "evidence": "Benali grew up in Fes and started playing football at a local academy"},
    {"id": "F3", "statement": "A 2022 knee injury kept him in rehabilitation for eight months.", "source": "S1",
     "evidence": "After a knee injury in 2022 he spent eight months in rehabilitation"},
    {"id": "F4", "statement": "He now mentors young players in Fes.", "source": "S1", "evidence": "now mentors young players in Fes"},
    {"id": "F9", "statement": "He won 9 caps.", "source": "S1", "evidence": "he won 9 caps"}]}
FIX_SLIDES = [
    ("hook", "From injury to cup winner", "Karim Benali turned a long and difficult recovery into the deciding moment "
     "of a 2024 cup final, and now into a role guiding younger players.", ["F1", "F3", "F4"]),
    ("background", "Learning the game in Fes", "Benali grew up in Fes, where his path in football began at a small "
     "local academy in the city where he still works with young players.", ["F2"]),
    ("turning_point", "Eight months away", "A knee injury in 2022 forced him into rehabilitation for eight months, a "
     "long stretch away from competitive matches and his teammates.", ["F3"]),
    ("outcome", "Back for the final", "He returned to decide the 2024 cup final with the winning goal for his side, "
     "completing a comeback that had seemed uncertain.", ["F1", "F3"]),
    ("why_it_matters", "Passing it on", "Today he mentors young players in his home city, showing how one setback can "
     "become something shared with the next generation.", ["F4"]),
]


def fifa_html(pid, title, date, paras):
    """FIXTURE inside.fifa.com page: Next.js data with rich-text paragraphs plus a list that must be ignored."""
    doc = [{"nodeType": "paragraph", "content": [{"nodeType": "text", "value": x}]} for x in paras]
    doc.append({"nodeType": "unordered-list", "content": [{"nodeType": "text", "value": "LIST"}]})
    data = {"props": {"pageProps": {"parent": {"id": pid, "title": "Article Page > " + title, "contentDate": date},
                                    "richTextProps": {"document": {"content": doc}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def fixture_verdicts(messages, claim_verdicts=None, fact_verdicts=None):
    """FIXTURE verifier: reads the claim/fact ids from the verification prompt; everything 'supported' unless overridden
    (by id, or by a function of the claim)."""
    text = messages[-1]["content"]
    facts = json.loads(text.split("Facts with their source passages:\n", 1)[1].split("\n\nClaims,", 1)[0])
    claims = json.loads(text.split("relies on:\n", 1)[1].split("\n\nGive a verdict", 1)[0])
    cv = claim_verdicts or (lambda c: "supported")
    return {"facts": [{"id": f["id"], "verdict": (fact_verdicts or {}).get(f["id"], "supported"), "reason": "FIXTURE"}
                      for f in facts],
            "claims": [{"id": c["id"], "verdict": cv(c), "reason": "FIXTURE verifier"} for c in claims]}


def fake_chat(mode, calls):
    """FIXTURE stand-in for Groq. Returns canned JSON per step; records every call."""
    def factory(usage_log):
        def chat(step, messages):
            calls.append(step)
            usage_log.append({"step": step, "fixture": True})
            if mode == "forbidden":
                raise AssertionError("Groq must not be called without a selection")
            if step == "extract_facts":
                return FIX_FACTS
            if step == "angle":
                return {"angle": "A comeback from injury that became mentoring", "fact_ids": ["F1", "F3", "F4"],
                        "coverage_note": "FIXTURE"}
            if step.startswith(("verify", "reverify")):
                return fixture_verdicts(messages)
            slides = [{"role": r, "heading": h, "body": b, "fact_ids": f} for r, h, b, f in FIX_SLIDES]
            if mode == "malformed":
                slides = slides[:4]
            if mode == "unsupported":
                slides[3] = {**slides[3], "body": slides[3]["body"] + " He did it in 2019."}
            return {"title": "Karim Benali's comeback", "slides": slides,
                    "explanation": {"text": "The report links a documented injury, a comeback goal and mentoring.",
                                    "fact_ids": ["F1", "F3", "F4"]}}
        return chat
    return factory


def check_claim_verification():
    """compose.verify / compose.compose on FIXTURE facts and a scripted FIXTURE verifier (no Groq)."""
    from arabsports import compose
    body = FIX_BODY + (" Benali did not win the 2023 league title. In the sixth round of the season he scored again "
                       "after four earlier goals.")
    src = [{"sid": "S1", "url": "https://fixture.test/s1", "publisher": "FIXTURE", "text": body}]
    facts = [{"id": f["id"], "statement": f["statement"], "source": "S1", "evidence": f["evidence"]} for f in FIX_FACTS["facts"][:4]]
    facts += [{"id": "F5", "statement": "Benali won the 2023 league title.", "source": "S1",
               "evidence": "Benali did not win the 2023 league title"},
              {"id": "F6", "statement": "Benali scored on his debut.", "source": "S1",
               "evidence": "In the sixth round of the season he scored again"}]
    slides = [{"role": r, "heading": h, "body": b, "fact_ids": f} for r, h, b, f in FIX_SLIDES]
    draft = {"title": "Karim Benali's comeback", "slides": slides,
             "explanation": {"text": "The report links a documented injury, a comeback goal and mentoring.", "fact_ids": ["F1", "F3", "F4"]}}
    bad = json.loads(json.dumps(draft))
    bad["slides"][0]["body"] = "Benali scored within minutes of his debut and won the 2023 league title."
    bad["slides"][0]["fact_ids"] = ["F5", "F6"]
    bad["slides"][3]["body"] = "Because of the knee injury, he scored the winner in the cup final."  # cause not in evidence
    bad["slides"][4]["body"] = "He mentors young players in Fes, which he began after the final."   # order: verifier only
    order_claim = lambda c: "insufficient" if "after the final" in c["text"] else "supported"
    rnd = compose.verify(lambda step, m: fixture_verdicts(m, order_claim), "verify", facts, src, bad)
    fv = {f["id"]: f for f in rnd["facts"]}
    cv = {c["location"] + ":" + c["text"][:20]: c for c in rnd["claims"]}
    assert fv["F5"]["verdict"] == "contradicted" and fv["F5"]["model"]["verdict"] == "supported", fv["F5"]
    assert fv["F6"]["verdict"] == "insufficient" and "debut" in fv["F6"]["code"]["reasons"][0] and "sixth round" in fv["F6"]["passage"]
    s1 = cv["slide 1 body:Benali scored within"]
    assert s1["verdict"] == "insufficient" and any("debut" in r for r in s1["code"]["reasons"]), s1
    s4 = cv["slide 4 body:Because of the knee "]
    assert s4["verdict"] == "insufficient" and any("because" in r for r in s4["code"]["reasons"]), s4
    s5 = cv["slide 5 body:He mentors young pla"]
    assert s5["code"]["verdict"] == "supported" and s5["verdict"] == "insufficient", s5  # 'after' is in the evidence
    assert {"F5", "F6"} <= set(rnd["failed_facts"]) and all(c["sources"] for c in rnd["claims"] if c["fact_ids"])
    print("ok  verify 1: 'did not win' vs 'won' -> contradicted although the verifier said supported; a later match "
          "called a debut -> insufficient with its passage; unsupported cause (code) and chronology (verifier) flagged")

    dated = "Iraq won the 2002 and 2005 finals with goals from Mahmoud. He scored the winner in the 2007 Asian Cup final."
    dated_facts = [{"id": "F1", "statement": "Mahmoud scored in Iraq's 2002 and 2005 final wins.", "source": "S1",
                    "evidence": dated}]
    dated_draft = {"title": "Mahmoud", "slides": [{"heading": "Mahmoud’s Earlier Wins", "fact_ids": ["F1"],
                                                   "body": "He scored in the 2007 final before the 2005 final."}]}
    order_from_dates = lambda c: "contradicted" if "before the 2005" in c["text"] else "supported"
    dated_round = compose.verify(lambda step, m: fixture_verdicts(m, order_from_dates), "verify", dated_facts,
                                 [{**src[0], "text": dated}], dated_draft)
    dated_claims = {c["location"]: c["verdict"] for c in dated_round["claims"]}
    assert dated_claims["slide 1 heading"] == "supported", dated_round["claims"]
    assert dated_claims["slide 1 body"] == "contradicted", dated_round["claims"]
    print("ok  chronology: cited dates support 'earlier' without that word; reversed order is still rejected")

    limits = {"groq_max_calls": 6, "max_revisions": 2, "groq_tpm": 8000, "groq_max_tokens": 2000}
    ctx = {"athlete": {"name": "Karim Benali", "profile": {}}, "sources": [{**src[0], "text": FIX_BODY}], "coverage": []}

    def scripted(drafts, claim_verdicts, fail_verify=None):
        log = []
        def chat(step, m):
            log.append(step)
            if fail_verify and step == fail_verify:
                raise compose.ProviderError("pacing_wait_exceeded", "FIXTURE wait budget")
            if step == "extract_facts":
                return FIX_FACTS
            if step == "angle":
                return {"angle": "FIXTURE comeback", "fact_ids": ["F1", "F3", "F4"], "coverage_note": "FIXTURE"}
            if step.startswith(("verify", "reverify")):
                return fixture_verdicts(m, claim_verdicts)
            return drafts.pop(0)
        return chat, log
    unsupported = lambda c: "insufficient" if "never lost" in c["text"] else "supported"
    stuck = json.loads(json.dumps(draft))
    stuck["slides"][3]["body"] = "He returned to decide the 2024 cup final and never lost a match again for his side."
    chat, log = scripted([stuck, json.loads(json.dumps(stuck))], unsupported)
    r = compose.compose(ctx, chat, limits, calls_used=lambda: len(log))
    assert r["status"] == "insufficient_evidence" and log == ["extract_facts", "angle", "compose", "verify", "repair_1",
                                                               "reverify_1"], (r["status"], log)
    assert len(r["verification"]) == 2 and r["verification"][1]["unresolved"] and r["draft"], r["errors"]
    print("ok  verify 2: repaired claim still unsupported -> re-verified, insufficient_evidence within 6 calls; both "
          "rounds and the draft kept")

    chat, log = scripted([json.loads(json.dumps(stuck)), json.loads(json.dumps(draft))], unsupported)
    r = compose.compose(ctx, chat, limits, calls_used=lambda: len(log))
    assert r["status"] == "draft_ready" and len(r["verification"]) == 2 and not r["verification"][1]["unresolved"], r["status"]
    chat, log = scripted([json.loads(json.dumps(draft))], unsupported)
    r = compose.compose(ctx, chat, {**limits, "groq_max_calls": 3}, calls_used=lambda: len(log))
    assert r["status"] == "verification_budget_exhausted" and log == ["extract_facts", "angle", "compose"] and r["draft"], (r["status"], log)
    chat, log = scripted([json.loads(json.dumps(stuck))], unsupported)
    r = compose.compose(ctx, chat, {**limits, "groq_max_calls": 5}, calls_used=lambda: len(log))
    assert r["status"] == "verification_budget_exhausted" and log[-1] == "verify", (r["status"], log)
    chat, log = scripted([json.loads(json.dumps(draft))], unsupported, fail_verify="verify")
    r = compose.compose(ctx, chat, limits, calls_used=lambda: len(log))
    assert r["status"] == "verification_budget_exhausted" and "pacing_wait_exceeded" in r["errors"][-1] and r["draft"]
    # a real-client 429 on the last allowed call (verify): draft kept, rate-limit error preserved for the batch stop
    reply = lambda body: (200, {}, json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode())
    seq = iter([reply(FIX_FACTS), reply({"angle": "FIXTURE comeback", "fact_ids": ["F1", "F3", "F4"], "coverage_note": "F"}),
                reply(json.loads(json.dumps(draft))), (429, {"retry-after": "1"}, b"{}")])
    lim4 = {**limits, "groq_max_calls": 4, "groq_tpm": 10 ** 6, "groq_rpm": 30, "groq_max_wait_s": 300, "groq_retries": 1}
    vc, glog = VClock(), []
    chat = compose.groq_chat("FIXTURE-KEY", "openai/gpt-oss-120b", lim4, glog, lambda *a: next(seq),
                             compose.Pacer(lim4, clock=vc.clock, sleep=vc.sleep))
    r = compose.compose(ctx, chat, lim4, calls_used=lambda: len(glog))
    assert r["status"] == "verification_budget_exhausted" and r["draft"] and vc.sleeps == [], (r["status"], vc.sleeps)
    assert r["provider_error"].startswith("quota_or_rate_limited: HTTP 429") and [e["status"] for e in glog] == [200] * 3 + [429], r
    hsrc = [{"sid": "S1", "url": "u", "publisher": "p", "text": "Benali topped the scoring chart, ahead of Omar Zidane of Raja."}]
    hf = [{"id": "F1", "statement": "Benali topped the scoring chart.", "source": "S1", "evidence": "Benali topped the scoring chart"}]
    mk = lambda h, b: {"title": "t", "explanation": {"text": "x", "fact_ids": ["F1"]},
                       "slides": [{"role": ro, "heading": h, "body": b, "fact_ids": ["F1"]} for ro in compose.ROLES]}
    names = lambda h, b: [e for e in compose.check_draft(mk(h, b), hf, hsrc, "Karim Benali")[0] if "names" in e]
    assert names("Chart Leader", "He topped the scoring chart.") == [], "Title Case heading is not a name"
    assert names("Raja Rivalry", "He topped the scoring chart.") and names("Chart Leader", "He passed Zidane.")
    assert names("Chart Leader", "He topped Benali’s chart.") == [] == names("Chart Leader", "He topped Benali's chart.")
    assert compose.check_facts([{"id": "F1", "statement": "s", "source": "S1", "evidence": "Benali's scoring chart"}],
                               [{"sid": "S1", "text": "He topped Benali’s scoring chart."}])[0], "curly vs straight apostrophe"
    print("ok  verify 3: a successful repair is re-verified -> draft_ready (never 'completed'); no call left to verify, "
          "no room to repair, or a wait-budget stop during verification -> verification_budget_exhausted, draft kept")


def check_failed_facts_and_types():
    """QA regressions without a database or model: a claim citing a failed fact is repaired or blocked (never ready),
    a fact stays failed in later rounds, and wrong-type draft fields are malformed output, not a crash."""
    from arabsports import compose
    src = [{"sid": "S1", "url": "https://fixture.test/s1", "publisher": "FIXTURE", "text": FIX_BODY}]
    facts = [{**f, "source": "S1"} for f in FIX_FACTS["facts"][:4]]
    draft = {"title": "Karim Benali's comeback", "slides": [{"role": r, "heading": h, "body": b, "fact_ids": f}
                                                           for r, h, b, f in FIX_SLIDES],
             "explanation": {"text": "The report links a documented injury, a comeback goal and mentoring.",
                             "fact_ids": ["F1", "F3", "F4"]}}
    # slide 4 cites F1 (contradicted) + F3 (supported); the verifier calls every claim supported
    rnd = compose.verify(lambda s, m: fixture_verdicts(m, fact_verdicts={"F1": "contradicted"}), "verify", facts, src, draft)
    s4 = next(c for c in rnd["claims"] if c["location"] == "slide 4 body")
    assert rnd["failed_facts"] == ["F1"] and s4["fact_ids"] == ["F1", "F3"] and s4["verdict"] == "insufficient", s4
    assert s4["failed_facts_cited"] == ["F1"] and s4["id"] in rnd["unresolved"]
    # a later round cannot clear a fact that already failed
    again = compose.verify(lambda s, m: fixture_verdicts(m), "reverify_1", facts, src, draft, failed_before=["F1"])
    assert again["failed_facts"] == ["F1"] and s4["id"] in again["unresolved"], again["unresolved"]
    limits = {"groq_max_calls": 8, "max_revisions": 2, "groq_tpm": 8000, "groq_max_tokens": 2000}
    ctx = {"athlete": {"name": "Karim Benali", "profile": {}}, "sources": src, "coverage": []}

    def run(same_draft, fact_verdicts=None):  # every compose/revise/repair returns the same draft
        log = []
        def chat(step, m):
            log.append(step)
            if step == "extract_facts":
                return FIX_FACTS
            if step == "angle":
                return {"angle": "FIXTURE comeback", "fact_ids": ["F1", "F3", "F4"], "coverage_note": "FIXTURE"}
            if step.startswith(("verify", "reverify")):
                return fixture_verdicts(m, fact_verdicts=fact_verdicts if step == "verify" else None)
            return json.loads(json.dumps(same_draft))
        return compose.compose(ctx, chat, limits, calls_used=lambda: len(log)), log
    r, log = run(draft, {"F1": "contradicted"})
    assert r["status"] == "insufficient_evidence" and log[:5] == ["extract_facts", "angle", "compose", "verify", "repair_1"], (r["status"], log)
    assert r["verification"][0]["unresolved"], r["verification"][0]
    typed = json.loads(json.dumps(draft)); typed["slides"][1]["body"] = 42
    r, log = run(typed)
    assert r["status"] == "malformed_output" and any("slide 2 body has the wrong type" in e for e in r["errors"]), r
    assert log == ["extract_facts", "angle", "compose", "revise_1", "revise_2"] and "verification" not in r, log
    # malformed verifier responses (valid JSON, wrong types): no crash, nothing verified, draft not approved, no repair
    good = lambda m: fixture_verdicts(m)
    numeric_reason = lambda m: {**good(m), "claims": [{**c, "reason": 7} for c in good(m)["claims"]]}
    for label, resp in [("facts: 42", lambda m: {"facts": 42}), ("claim id []", lambda m: {"claims": [{"id": []}]}),
                        ("numeric reason", numeric_reason), ("list response", lambda m: [good(m)])]:
        rnd = compose.verify(lambda s, m: resp(m), "verify", facts, src, draft)
        assert rnd["malformed"] and rnd["unresolved"], label
        assert all(x["model"]["verdict"] == "insufficient" for x in rnd["facts"] + rnd["claims"]), label
        log = []
        def chat(step, m):
            log.append(step)
            if step == "extract_facts":
                return FIX_FACTS
            if step == "angle":
                return {"angle": "FIXTURE comeback", "fact_ids": ["F1", "F3", "F4"], "coverage_note": "FIXTURE"}
            return resp(m) if step.startswith("verify") else json.loads(json.dumps(draft))
        r = compose.compose(ctx, chat, limits, calls_used=lambda: len(log))
        assert r["status"] == "malformed_output" and log[-1] == "verify" and r["draft"], (label, r["status"], log)
        assert any("verifier" in e for e in r["errors"]) and r["verification"][0]["model_raw"] is not None, label
    for mutate in (lambda d: d.update(explanation="text"), lambda d: d["slides"][0].update(fact_ids="F1"),
                   lambda d: d.update(title=7), lambda d: d["slides"][2].update(heading=None)):
        d = json.loads(json.dumps(draft)); mutate(d)
        errs = compose.check_draft(d, facts, src, "Karim Benali")[0]
        assert errs and all("wrong type" in e for e in errs), errs
    print("ok  failed facts: a claim citing a contradicted fact beside a supported one is unresolved (repaired or "
          "blocked), and a failed fact stays failed in re-verification; wrong-type draft fields -> bounded revisions, "
          "then malformed_output with no verification call; malformed verifier responses (facts: 42, claim id [], "
          "numeric reason, a list) -> nothing verified, malformed_output after one verify call, draft kept unapproved")


def check_repair_and_wording():
    """Regressions from batch-20261108T152317Z-cab5 (no database or model): the saved oversized Salah repair now fits
    the per-minute limit; 'MVP' matches a spelled-out 'Most Valuable Player'; quarter-final is not 'final'."""
    import pathlib
    from arabsports import angles, compose
    from arabsports.config import settings
    fx = json.loads((pathlib.Path(__file__).parent / "fixtures" / "repair_salah_8eeb.json").read_text(encoding="utf-8"))
    L = settings()["limits"]
    msgs = compose.repair_messages(fx["name"], fx["angle"] + compose.FOLLOW, fx["facts"], fx["round"], fx["draft"])
    need = compose.estimate_tokens(msgs, L["groq_max_tokens"])  # includes the reserved output/reasoning tokens
    assert need <= L["groq_tpm"], (need, L["groq_tpm"])
    body = msgs[1]["content"]
    assert len(msgs) == 2 and json.dumps(fx["draft"], ensure_ascii=False) in body and "Headings:" in body
    for p in {f["passage"] for f in fx["round"]["facts"] if f["passage"]}:
        assert body.count(json.dumps(p, ensure_ascii=False)[1:-1]) == 1, "each cited passage is sent once"
    unresolved = [c["id"] for c in fx["round"]["claims"] if c["verdict"] != "supported"]
    assert all(f"{c} [" in body for c in unresolved) and "never copy wording" in body
    # wording: abbreviation spelled out in the passage; stage words with every hyphen variant seen
    passage = "He finished as top scorer and was named Most Valuable Player of the 2007 Asian Cup."
    assert compose.name_supported("MVP", compose._ws(passage)) and not compose.name_supported("MVP", compose._ws("top scorer"))
    assert angles._fact_gaps("Mahmoud was the tournament MVP.", passage) == []
    qf = "following Salah's goal against Ivory Coast in the quarter-finals of the 2025 Africa Cup of Nations"
    for claim in ("He scored in the quarter-final.", "He scored in the quarter‑final.", "He scored in the "
                  "quarter–final.", "He scored in the quarter final.", "He scored in the quarterfinals."):
        assert compose.cue_gaps(claim, qf) == [], claim
    assert compose.cue_gaps("He scored in the final.", qf) == ["'final' (event identity) not in evidence"]
    assert compose.cue_gaps("He scored in the semi-final.", qf) == ["'semifinal' (event identity) not in evidence"]
    assert compose.cue_gaps("He won the final.", "He won the final in 2007.") == []
    assert "Headings:" in compose.HEADINGS and "Why it matters" in compose.HEADINGS
    # the untouched 81-word Younis draft (run 20261108T170715Z-8503) now reaches verification; the target stays 100-150
    yx = json.loads((pathlib.Path(__file__).parent / "fixtures" / "draft_younis_8503.json").read_text(encoding="utf-8"))
    assert yx["errors"][0] == "total words 81, need about 100-150"
    errs, warns = compose.check_draft(yx["draft"], yx["facts"], yx["sources"], yx["name"])
    assert errs == [] and "total words 81 (target 100-150)" in warns, (errs, warns)
    log = []
    def chat(step, m):
        log.append(step)
        return fixture_verdicts(m) if step == "verify" else json.loads(json.dumps(yx["draft"]))
    r = compose.compose({"athlete": {"name": yx["name"], "profile": {}}, "sources": yx["sources"], "coverage": [],
                         "reviewed_facts": yx["facts"], "approved_angle": {"stakes": "s", "turning_point": "t",
                                                                           "significance": "x", "source": "angle_proposal"}},
                        chat, {**L, "groq_max_calls": 4}, calls_used=lambda: len(log))
    # reached verification straight after compose (no length revision); the claim checks still apply: the code guard
    # flags 'historic' (slide 2, explanation) and 'maiden' (slide 5), which no passage states, so the draft goes to
    # repair rather than straight to review
    assert log[:2] == ["compose", "verify"] and r["verification"], (log, r["errors"])
    first = r["verification"][0]
    assert [(c["location"], c["code"]["reasons"]) for c in first["claims"] if c["id"] in first["unresolved"]] == [
        ("slide 2 body", ["'historic' (event identity) not in evidence"]),
        ("slide 5 body", ["'maiden' (event identity) not in evidence"]),
        ("explanation", ["'historic' (event identity) not in evidence"])], first["unresolved"]
    thin = json.loads(json.dumps(yx["draft"])); thin["slides"][2]["body"] = "He scored."
    assert any("total words" in e and "[3]" in e for e in compose.check_draft(thin, yx["facts"], yx["sources"], yx["name"])[0])
    long = json.loads(json.dumps(yx["draft"])); long["slides"][0]["body"] += " Iraq" + " won" * 80
    assert any(e.startswith("total words") for e in compose.check_draft(long, yx["facts"], yx["sources"], yx["name"])[0])
    print(f"ok  repair/wording: the saved Salah repair (was 8480 est. tokens) is now {need} incl. "
          f"{L['groq_max_tokens']} reserved output, under {L['groq_tpm']}; draft, failed claims and each passage once; "
          "'MVP' = 'Most Valuable Player'; quarter-final (any hyphen) is not 'final'; factual headings requested; the "
          "untouched 81-word Younis draft reaches verification (a thin slide or >160 words is still refused)")


def check_correction_pass():
    """Regressions from run 20261106T162212Z-5705 (FIXTURE data, no Groq)."""
    from arabsports import compose, stages
    # Passage start was off by one when no sentence break preceded the window ("gyptian star ...").
    text = "Egyptian star Karim Benali " + "kept training hard every week " * 15 + "and scored four times for the club."
    ctx_ = compose.context_for("scored four times for the club", text)
    assert ctx_.startswith("Egyptian star"), ctx_[:30]
    print("ok  correction 1: passage starts at the text start (no lost character)")

    # A structural revision used the call that repair needed. Now: revise only if verify + repair + re-verify fit,
    # and the correction rules plus source passages reach compose and every repair.
    limits = {"groq_max_calls": 8, "max_revisions": 2, "groq_tpm": 8000, "groq_max_tokens": 2000}
    good = {"title": "Karim Benali's comeback", "explanation": {"text": "The report links a documented injury, a comeback "
            "goal and mentoring.", "fact_ids": ["F1", "F3", "F4"]},
            "slides": [{"role": r, "heading": h, "body": b, "fact_ids": f} for r, h, b, f in FIX_SLIDES]}
    stuck = json.loads(json.dumps(good))
    stuck["slides"][3]["body"] = "He returned to decide the 2024 cup final and never lost a match again for his side."
    short = {**json.loads(json.dumps(good)), "slides": good["slides"][:4]}
    seen, log = {}, []

    def chat(step, m):
        log.append(step)
        seen[step] = m[-1]["content"]
        if step == "extract_facts":
            return FIX_FACTS
        if step == "angle":
            return {"angle": "FIXTURE comeback", "fact_ids": ["F1", "F3", "F4"], "coverage_note": "FIXTURE"}
        if step.startswith(("verify", "reverify")):
            return fixture_verdicts(m, lambda c: "insufficient" if "never lost" in c["text"] else "supported")
        return {"compose": short, "revise_1": stuck, "repair_1": good}[step]
    ctx = {"athlete": {"name": "Karim Benali", "profile": {}}, "coverage": [],
           "sources": [{"sid": "S1", "url": "https://fixture.test/s1", "publisher": "FIXTURE", "text": FIX_BODY}]}
    r = compose.compose(ctx, chat, limits, calls_used=lambda: len(log))
    assert r["status"] == "draft_ready" and log == ["extract_facts", "angle", "compose", "revise_1", "verify", "repair_1",
                                                      "reverify_1"], (r["status"], log)
    assert "long-term" in seen["compose"] and "running totals" in seen["repair_1"] and "Source passages" in seen["repair_1"]
    assert all(k in seen["repair_1"] for k in ("never copy wording", "100-150 words", "only names and numbers")), seen["repair_1"][:300]
    # 'Leading the Race' was flagged as a causal claim (bare 'leading'); causal phrases are still caught.
    assert compose.cue_gaps("Leading the Race", "Salah moved to the top of the scoring chart") == []
    assert compose.cue_gaps("The goal led to his recall", "Salah scored in the 4th minute")
    log.clear()
    r = compose.compose(ctx, chat, {**limits, "groq_max_calls": 6}, calls_used=lambda: len(log))
    assert "revise_1" not in log and r["status"] == "malformed_output" and any("reserved" in e for e in r["errors"]), (log, r["errors"])
    print("ok  correction 2: after a structural revision, repair and re-verification still fit in 8 calls; with 6 the "
          "revision is skipped instead of spending the repair room; rules + source passages reach compose and repair, and repair restates the copy/names/word rules; "
          "'Leading the Race' is not a causal claim, 'led to' still is")


def check_t03_t04_db():
    """T03 selection/caching and T04 package contracts through screen_run/generate_run on FIXTURE data.
    Laya and Groq are replaced by labelled stand-ins here; the live runs use the real models."""
    from arabsports import screen, stages
    url = settings()["database_url"]
    if not url:
        print("SKIP t03/t04 db checks: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    fresh = lambda: store.connect(url, schema)
    real_screen, laya_calls = screen.screen_packet, []
    MAPPED = {"Karim Benali": ("promising", 0.6), "Omar Zidane": ("promising", 0.6), "Hassan Kamal": ("promising", 0.9),
              "Sami Nasser": ("routine", None), "Yousef Haddad": ("promising", 0.95)}

    def fixture_screen(pkt):
        laya_calls.append(pkt["name"])
        m, sc = MAPPED[pkt["name"]]
        return [{"fixture": True, "windows": len(pkt["windows"])}], m, sc, []
    screen.screen_packet = fixture_screen
    A = lambda n: f"https://fixture.test/sports/t{n}"
    try:
        ents = [("QTK", "Karim Benali", "Q2736", "Q1028"), ("QTO", "Omar Zidane", "Q2736", "Q79"),
                ("QTH", "Hassan Kamal", "Q2736", "Q79"), ("QTS", "Sami Nasser", "Q542", "Q810"),
                ("QTY", "Yousef Haddad", "Q5372", "Q810")]
        replay("t1", raw_input(entities=ents, stories=[
            (A(1), "FIXTURE comeback", FIX_BODY),
            (A(2), "FIXTURE derby", "Egyptian forward Omar Zidane scored twice in football after a hard season."),
            (A(7), "FIXTURE hoops", "Jordanian basketball player Yousef Haddad scored 30 points in basketball."),
            # role after the name: the link forms; the official is then excluded by eligibility, not by matching
            (A(3), "FIXTURE whistle", "Hassan Kamal, an Egyptian referee, officiated the football final in Cairo."),
            (A(4), "FIXTURE meet", "Jordanian runner Sami Nasser finished fourth at the athletics meet.")]), schema, made)
        roles = {"QTK": ["association football player"], "QTO": ["association football player"],
                 "QTH": ["association football referee"], "QTS": ["athletics competitor"], "QTY": ["basketball player"]}
        for aid, qid in conn.execute("SELECT id, source_id FROM athletes WHERE source_id LIKE 'QT%%'").fetchall():
            store.set_enrichment(conn, aid, "wikidata_roles", {"occupations": roles[qid], "national_teams": [], "fixture": True})

        r1 = stages.screen_run(schema)
        made.append(RUNS_DIR / r1["run_id"])
        with fresh() as f:
            sel = store.latest_selection(f)
        by = {c["name"]: c for c in sel["candidates"]}
        assert r1["outcome"] == "selected" and by["Hassan Kamal"]["eligible"] is False, (r1, by["Hassan Kamal"])
        yh = by["Yousef Haddad"]
        assert yh["mapped"] == "promising" and yh["eligible"] is False and any(
            "not association football (Q2736)" in r and "Q5372" in r for r in yh["eligibility"]["reasons"]), yh
        assert by["Karim Benali"]["eligible"] and by["Omar Zidane"]["eligible"], "football players stay eligible"
        assert sel["athlete_id"] == by["Karim Benali"]["athlete_id"], "tie on score: more articles, then lower id wins"
        assert [c["name"] for c in sorted(sel["candidates"], key=lambda c: c.get("rank") or 99)][:2] == ["Karim Benali", "Omar Zidane"]
        print("ok  t03 1: official excluded despite top score; top-scoring basketball player excluded with a sport "
              "reason (football players stay eligible); tie broken by articles then id; selection persisted")

        laya_calls.clear()
        r2 = stages.screen_run(schema)
        made.append(RUNS_DIR / r2["run_id"])
        with fresh() as f:
            sel2 = store.latest_selection(f)
        assert laya_calls == [] and r2["counts"]["reused_screenings"] == 5 and sel2["athlete_id"] == sel["athlete_id"], (laya_calls, r2)
        print("ok  t03 2: unchanged evidence/config reuses all 5 screenings; same selection")

        # New evidence for Omar -> only Omar is rescreened. Then a revision removes Sami -> Sami drops out.
        replay("t2", raw_input(stories=[(A(5), "FIXTURE recovery", "Egyptian forward Omar Zidane returned from injury in football.")]), schema, made)
        replay("t3", raw_input(stories=[(A(4), "FIXTURE meet", "The athletics meet was postponed because of rain.")]), schema, made)
        laya_calls.clear()
        MAPPED["Omar Zidane"] = ("promising", 0.5)  # Omar's new packet scores lower, so Karim stays rank 1
        r3 = stages.screen_run(schema)
        made.append(RUNS_DIR / r3["run_id"])
        with fresh() as f:
            sel3 = store.latest_selection(f)
        names3 = {c["name"] for c in sel3["candidates"]}
        assert laya_calls == ["Omar Zidane"] and "Sami Nasser" not in names3, (laya_calls, names3)
        assert sel3["athlete_id"] == sel["athlete_id"], "Karim (0.6) outranks Omar's rescreened 0.5"
        print("ok  t03 3: changed evidence rescreens only that athlete; superseded evidence leaves the candidate set")

        # T04 story gate. A FIXTURE FIFA-style report (unlinked: drafting evidence only) carries the angle passages.
        report_url = "https://inside.fifa.com/fixture/karim-benali-final"
        paras = [x.strip() + "." for x in FIX_BODY.rstrip(".").split(". ")]
        lead = sources.parse_fifa(report_url, fifa_html("0xfixture", "FIXTURE Benali final", "2024-05-01T20:00:00", paras))
        assert lead["content"] == "\n\n".join(paras) and "LIST" not in lead["content"], lead["content"]
        store.upsert_articles(conn, [lead], r3["run_id"])
        calls = []
        g0 = stages.generate_run(schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / g0["run_id"])
        assert g0["outcome"] == "angle_not_approved" and calls == [], g0
        sel_now = store.latest_selection(conn)
        angle = {"stakes": "A 2022 knee injury put his career on hold.", "turning_point": "He came back and scored the "
                 "winner in the 2024 cup final.", "significance": "He now mentors young players in Fes."}
        refs = [{"url": report_url, "passage": x, "supports": ["FIXTURE"]} for x in paras]
        for bad, why in (({"reviewer": "groq verifier"}, "not an independent reviewer"),
                         ({"refs": [{"url": report_url, "passage": "He won the league."}]}, "not found verbatim"),
                         ({"angle": {**angle, "stakes": ""}}, "missing: stakes")):
            args = {"reviewer": "codex (FIXTURE reviewer)", "angle": angle, "refs": refs, **bad}
            try:
                store.record_angle(conn, sel_now, "approved", args["reviewer"], args["angle"], args["refs"], r3["run_id"])
                raise AssertionError(f"angle recorded despite: {why}")
            except store.ReviewError as e:
                assert why in str(e), (why, e)
        omar = next(c for c in sel_now["candidates"] if c["name"] == "Omar Zidane")
        store.record_angle(conn, {**sel_now, "athlete_id": omar["athlete_id"], "screening_id": omar["screening_id"]},
                           "approved", "codex (FIXTURE reviewer)", angle, refs, r3["run_id"])
        g0 = stages.generate_run(schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / g0["run_id"])
        assert g0["outcome"] == "angle_not_approved" and calls == [] and "is for athlete" in g0["counts"]["reasons"][0], g0
        store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)", angle, refs, r3["run_id"])
        print("ok  t04 0: no angle -> angle_not_approved; self-reviewer, non-verbatim passage and missing stakes are "
              "refused; an approved angle for another athlete does not match the selection; no model call in any case")

        # T04 on the stored selection (Karim) and its approved angle: FIXTURE chat provider.
        calls = []
        g1 = stages.generate_run(schema, chat_factory=fake_chat("good", calls))
        made.append(RUNS_DIR / g1["run_id"])
        with fresh() as f:
            pkg = f.execute("SELECT status, package, review_state, package_sha256 FROM story_packages WHERE run_id = %s",
                            (g1["run_id"],)).fetchone()
        assert g1["outcome"] == "awaiting_review" and pkg[0] == "awaiting_review" and pkg[2] == "awaiting_review", (g1, pkg and pkg[1]["development_review"])
        p, sha = pkg[1], pkg[3]
        assert len(p["slides"]) == 5 and [s["role"] for s in p["slides"]] == ["hook", "background", "turning_point", "outcome", "why_it_matters"]
        assert {f["id"] for f in p["facts"]} == {"F1", "F2", "F3", "F4"} and p["dropped_facts"][0]["reason"].startswith("evidence span")
        assert p["photo"]["status"] == "placeholder" and "Laya" in p["explanation"]["label"]
        assert calls == ["extract_facts", "compose", "verify"], calls  # the approved angle replaces the model angle step
        assert [x["url"] for x in p["sources_used"]] == [sources.canonical_url(report_url)], p["sources_used"]
        assert p["sources_used"][0]["text"] == "\n\n".join(paras) and p["angle"]["source"] == "angle_review"
        assert p["angle_review"]["refs"][0]["content_hash"] and "editorially chosen" in p["selection_basis"]["angle"]
        assert {b["id"] for b in p["acceptance_blockers"]} == {"rights_fifa_terms"} and "FIFA" in p["rights_handling"]
        assert "Ajel" not in p["rights_handling"], "unused providers' terms are not claimed"
        assert all(f["verification"]["verdict"] == "supported" and f["verification"]["passage"] for f in p["facts"])
        assert p["claim_verification"]["rounds"][0]["unresolved"] == [] and p["sources_used"][0]["content_hash"]
        assert "awaiting independent review" in (RUNS_DIR / g1["run_id"] / "carousel.md").read_text(encoding="utf-8")
        print(f"ok  t04 1: checked + verified draft saved as awaiting_review (not completed): 5 ordered slides, F9 "
              f"dropped, every fact verified with its passage; only the angle's cited report passages were sent (the "
              f"linked discovery article was not); angle review, selection basis and FIFA rights blocker in the package; "
              f"{len(calls)} chat calls")

        with fresh() as f:
            assert store.exportable_package(f, p["package_id"]) is None, "unreviewed draft must not be exportable"
            for who, ver, why in (("groq openai/gpt-oss-120b", sha, "self-review"), ("automatic checks", sha, "self-review"),
                                  ("codex", "0" * 64, "wrong version")):
                try:
                    store.record_review(f, p["package_id"], "accepted", who, ["FIXTURE"], ver)
                    raise AssertionError(f"review by {who} / {why} was recorded")
                except store.ReviewError:
                    pass
            store.record_review(f, p["package_id"], "accepted", "codex (FIXTURE reviewer)", ["claims traced"], sha,
                                resolves=["rights_fifa_terms"])
        with fresh() as f:
            assert store.exportable_package(f, p["package_id"])["package_id"] == p["package_id"]
            dup = {**p, "package_id": p["package_id"] + "-dup"}
            dsha = store.save_package(f, dup, "awaiting_review")
            try:
                store.record_review(f, dup["package_id"], "accepted", "codex (FIXTURE reviewer)", ["dup"], dsha,
                                    resolves=["rights_fifa_terms"])
                raise AssertionError("second accepted package in one run was accepted")
            except psycopg.errors.UniqueViolation:
                pass
            store.record_review(f, dup["package_id"], "rejected", "codex (FIXTURE reviewer)", ["duplicate"], dsha)
            assert store.exportable_package(f, dup["package_id"]) is None, "rejected package must not be exportable"
            n_rev = f.execute("SELECT count(*) FROM package_reviews").fetchone()[0]
        assert n_rev == 2, n_rev
        print("ok  t04 2: unreviewed and rejected packages are not exportable; generator/automatic-check reviewers and "
              "a wrong package version are refused; accepted only by an independent reviewer for that exact sha; "
              "a second accepted package in the run is rejected by the database")

        for mode, want, n_calls in (("malformed", "malformed_output", 4), ("unsupported", "insufficient_evidence", 4)):
            calls = []
            g = stages.generate_run(schema, chat_factory=fake_chat(mode, calls))
            made.append(RUNS_DIR / g["run_id"])
            assert g["outcome"] == want and len(calls) == n_calls, (mode, g, calls)
        with fresh() as f:
            st = dict(f.execute("SELECT status || '/' || review_state, count(*) FROM story_packages GROUP BY 1").fetchall())
        assert st == {"awaiting_review/accepted": 1, "awaiting_review/rejected": 1, "malformed_output/not_applicable": 1,
                      "insufficient_evidence/not_applicable": 1}, st
        print(f"ok  t04 3: malformed -> malformed_output, unsupported 2019 -> insufficient_evidence after 2 revisions; "
              f"earlier packages and review states intact {st}")

        # Reviewed facts carried by the angle record replace model extraction; ids and passages reach drafting intact.
        pids = [f"P{i}" for i in range(1, 5)]
        rrefs = [{"id": p, "url": report_url, "passage": x} for p, x in zip(pids, paras)]
        rfacts = [{"id": f["id"], "statement": f["statement"], "refs": [p]} for f, p in zip(FIX_FACTS["facts"][:4], pids)]
        rfacts[0]["refs"] = ["P1", "P4"]  # a fact may rest on several passages of one source
        for bad, why in (([{**rfacts[0], "refs": ["P9"]}] + rfacts[1:], "unknown passage"),
                         (rfacts[1:], "not cited by any reviewed fact")):
            try:
                store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)", {**angle, "facts": bad}, rrefs,
                                   r3["run_id"])
                raise AssertionError(f"angle recorded despite: {why}")
            except store.ReviewError as e:
                assert why in str(e), (why, e)
        store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)", {**angle, "facts": rfacts}, rrefs,
                           r3["run_id"])
        seen, calls = {}, []

        def capture(usage_log):
            inner = fake_chat("good", calls)(usage_log)
            return lambda step, m: (seen.__setitem__(step, m[-1]["content"]), inner(step, m))[1]
        g8 = stages.generate_run(schema, chat_factory=capture)
        made.append(RUNS_DIR / g8["run_id"])
        with fresh() as f:
            p8 = f.execute("SELECT package FROM story_packages WHERE run_id = %s", (g8["run_id"],)).fetchone()[0]
        assert g8["outcome"] == "awaiting_review" and calls == ["compose", "verify"], (g8, calls)  # no extraction call
        assert [(x["id"], x["statement"], x["passage_refs"], x["origin"]) for x in p8["facts"]] == \
            [(x["id"], x["statement"], x["refs"], "angle_review") for x in rfacts], p8["facts"]
        assert p8["facts"][0]["evidence_span"] == p8["facts"][0]["verification"]["passage"] == f"{paras[0]} {paras[3]}"
        assert all(x in seen[step] for step in ("compose", "verify") for x in paras + ["F1", "F2", "F3", "F4"])
        assert not p8["dropped_facts"] and "F9" not in seen["compose"]
        print("ok  t04 8: reviewed facts (ids, statements, passages) reach the writer and verifier intact; no extraction "
              "call; unknown or uncited passages are refused at review time")

        # A manual revision of g8's copy: same reviewed facts, one verifier call, linked to the unchanged original.
        with fresh() as f:
            sha8 = f.execute("SELECT package_sha256 FROM story_packages WHERE run_id = %s", (g8["run_id"],)).fetchone()[0]
        good = {"editor": "codex (FIXTURE editor)", "title": "Karim Benali’s comeback",
                "slides": [{"heading": h, "body": b, "fact_ids": fi} for _, h, b, fi in FIX_SLIDES],
                "explanation": {"text": "A documented injury, a comeback goal and mentoring.", "fact_ids": ["F1", "F3", "F4"]}}
        rev_file = RUNS_DIR / g8["run_id"] / "revision_fixture.json"
        rev_file.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        calls = []
        rv = stages.revise_run(g8["run_id"], rev_file, schema, chat_factory=fake_chat("good", calls))
        made.append(RUNS_DIR / rv["run_id"])
        with fresh() as f:
            pr, st_r = f.execute("SELECT package, review_state FROM story_packages WHERE run_id = %s", (rv["run_id"],)).fetchone()
            sha8_after = f.execute("SELECT package_sha256 FROM story_packages WHERE run_id = %s", (g8["run_id"],)).fetchone()[0]
        assert rv["outcome"] == "awaiting_review" and st_r == "awaiting_review" and calls == ["verify"], (rv, calls)
        assert pr["revision_of"] == {**pr["revision_of"], "package_id": p8["package_id"], "package_sha256": sha8} and sha8_after == sha8
        assert "manually revised by codex (FIXTURE editor)" in pr["selection_basis"]["copy"] and pr["slides"][0]["role"] == "hook"
        assert [x["passage_refs"] for x in pr["facts"]] == [x["passage_refs"] for x in p8["facts"]]
        assert {b["id"] for b in pr["acceptance_blockers"]} == {"rights_fifa_terms"}
        rev_file.write_text(json.dumps({**good, "explanation": None}), encoding="utf-8")
        calls = []
        rv2 = stages.revise_run(g8["run_id"], rev_file, schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / rv2["run_id"])
        assert rv2["outcome"] == "insufficient_evidence" and calls == [] and any(
            "explanation" in e for e in rv2["counts"]["structure_errors"]), rv2
        print("ok  t04 9: a manual revision is saved as a new package linked to the unchanged original, keeps the reviewed "
              "facts and FIFA blocker, and is verified with one call; a copy failing the structure checks makes no call")

        # A later angle review that only corrects citations (same fact ids/statements, supersedes_review) can carry a
        # revision of the revision; changing a statement cannot.
        with fresh() as f:
            prev_review = store.latest_angle(f)["id"]
        cfacts = [{**x, "refs": list(x["refs"])} for x in rfacts]
        cfacts[0]["refs"] = ["P1", "P4", "P2"]
        store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)",
                           {**angle, "facts": cfacts, "supersedes_review": prev_review}, rrefs, r3["run_id"])
        rev_file.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        calls = []
        rv4 = stages.revise_run(rv["run_id"], rev_file, schema, chat_factory=fake_chat("good", calls))
        made.append(RUNS_DIR / rv4["run_id"])
        with fresh() as f:
            p4 = f.execute("SELECT package FROM story_packages WHERE run_id = %s", (rv4["run_id"],)).fetchone()[0]
            new_review = store.latest_angle(f)["id"]
        assert rv4["outcome"] == "awaiting_review" and calls == ["verify"], (rv4, calls)
        assert p4["facts"][0]["passage_refs"] == ["P1", "P4", "P2"] and p4["angle_review"]["id"] == new_review
        assert p4["revision_of"]["package_id"] == pr["package_id"] and f"supersedes review {prev_review}" in p4["selection_basis"]["angle"]
        assert paras[1] in p4["facts"][0]["evidence_span"]
        store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)",
                           {**angle, "facts": [{**cfacts[0], "statement": "Changed."}] + cfacts[1:],
                            "supersedes_review": new_review}, rrefs, r3["run_id"])
        calls = []
        rv5 = stages.revise_run(rv4["run_id"], rev_file, schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / rv5["run_id"])
        assert rv5["outcome"] == "stale_selection" and calls == [], rv5
        # restore the citation-only review as the latest approved one for the checks below
        store.record_angle(conn, sel_now, "approved", "codex (FIXTURE reviewer)",
                           {**angle, "facts": cfacts, "supersedes_review": prev_review}, rrefs, r3["run_id"])
        print("ok  t04 10: a later angle review that only corrects citations (supersedes_review, same facts) carries a "
              "revision re-cited from its passages; one that changes a statement is refused with no call")

        # T05 export gate: refused while awaiting review, written only after acceptance resolving the FIFA blocker.
        from arabsports import export
        site = RUNS_DIR / f"fixture-{schema}-site"
        made.append(site)
        pres_file = RUNS_DIR / rv["run_id"] / "presentation_fixture.json"
        pres_file.write_text(json.dumps({"theme": {"panel": "#0e6b3b", "accent": "#c8102e", "strip": ["#000000", "#ffffff"]},
                                         "visuals": [{"kind": "figure", "text": s["heading"].split()[0]} for s in pr["slides"]]}),
                             encoding="utf-8")
        try:
            stages.export_run(pr["package_id"], pres_file, schema, site=site)
            raise AssertionError("export of a package awaiting review must be refused")
        except export.ExportError as e:
            assert "awaiting_review" in str(e) and not site.exists(), e
        with fresh() as f:
            rsha = f.execute("SELECT package_sha256 FROM story_packages WHERE package_id = %s", (pr["package_id"],)).fetchone()[0]
            store.record_review(f, pr["package_id"], "accepted", "codex (FIXTURE reviewer)", ["checked"], rsha,
                                resolves=["rights_fifa_terms"])
        out = stages.export_run(pr["package_id"], pres_file, schema, site=site)
        blob = (site / out["file"]).read_text(encoding="utf-8")
        pub, idx = json.loads(blob), json.loads((site / "data" / "index.json").read_text(encoding="utf-8"))
        assert pub["review"]["state"] == "accepted" and pub["review"]["resolved_blockers"] == ["rights_fifa_terms"]
        assert pub["fixture"] is False and len(pub["slides"]) == 5 and [p["package_id"] for p in idx["packages"]] == [pr["package_id"]]
        assert not any(r["passage"][:40] in blob for r in pr["angle_review"]["refs"]), "source passages leaked"
        print("ok  t05 1: export is refused while a package awaits review and, after acceptance that resolves the FIFA "
              "blocker, writes a whitelisted public file and index entry without source passages")

        # The cited report changes after the angle review -> generation refuses (no model call).
        store.upsert_articles(conn, [{**lead, "content": lead["content"].replace("eight months", "nine months")}], r3["run_id"])
        calls = []
        g6 = stages.generate_run(schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / g6["run_id"])
        assert g6["outcome"] == "angle_evidence_changed" and calls == [] and "changed since the angle review" in g6["counts"]["reasons"][0], g6
        rev_file.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        rv3 = stages.revise_run(g8["run_id"], rev_file, schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / rv3["run_id"])
        assert rv3["outcome"] == "angle_evidence_changed" and calls == [], rv3
        print("ok  t04 7: a cited source changed after the angle review -> angle_evidence_changed for generate and "
              "revise, no model call")

        # Evidence changes after selection -> generation refuses the stale selection (no model call).
        replay("t4", raw_input(stories=[(A(6), "FIXTURE update", "Moroccan forward Karim Benali signed a new contract in football.")]),
               schema, made)
        calls = []
        g5 = stages.generate_run(schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / g5["run_id"])
        with fresh() as f:
            st5 = f.execute("SELECT status, review_state, package->'development_review'->'notes' FROM story_packages "
                            "WHERE run_id = %s", (g5["run_id"],)).fetchone()
        assert g5["outcome"] == "stale_selection" and calls == [] and st5[:2] == ("stale_selection", "not_applicable"), (g5, st5)
        assert any("evidence changed since screening" in n for n in st5[2]), st5
        print("ok  t04 5: evidence changed after selection -> stale_selection, rescreen required, no model call")

        # Legacy 'completed' rows (saved before review state existed) are never treated as accepted.
        with fresh() as f:
            f.execute("INSERT INTO story_packages (package_id, run_id, schema_version, athlete_id, screening_id, status, "
                      "package) VALUES ('pkg-legacy', %s, 'story-package/1', %s, %s, 'completed', %s)",
                      (g1["run_id"], p["athlete"]["id"], p["screening"]["screening_id"],
                       psycopg.types.json.Jsonb({"legacy": True})))
        with fresh() as f:  # schema re-applied on connect: backfill marks it legacy_unreviewed
            lst = f.execute("SELECT review_state FROM story_packages WHERE package_id = 'pkg-legacy'").fetchone()[0]
            lsha = store.package_sha({"legacy": True})
            try:
                store.record_review(f, "pkg-legacy", "accepted", "codex (FIXTURE reviewer)", ["x"], lsha)
                raise AssertionError("legacy package without verification was accepted")
            except store.ReviewError:
                pass
            store.record_review(f, "pkg-legacy", "rejected", "codex (FIXTURE reviewer)", ["no verification record"], lsha)
            assert store.exportable_package(f, "pkg-legacy") is None
        assert lst == "legacy_unreviewed", lst
        with fresh() as f:  # an open source-use blocker prevents acceptance until explicitly resolved
            blk = {**p, "package_id": "pkg-blocked", "run_id": g5["run_id"],
                   "acceptance_blockers": [{"id": "rights_fixture", "issue": "FIXTURE source-use question"}]}
            bsha = store.save_package(f, blk, "awaiting_review")
            try:
                store.record_review(f, "pkg-blocked", "accepted", "codex (FIXTURE reviewer)", ["claims traced"], bsha)
                raise AssertionError("accepted with an open rights blocker")
            except store.ReviewError as e:
                assert "rights_fixture" in str(e), e
            store.record_review(f, "pkg-blocked", "accepted", "codex (FIXTURE reviewer)", ["claims traced",
                                "FIXTURE rights decision"], bsha, resolves=["rights_fixture"])
            rb = f.execute("SELECT resolved_blockers FROM package_reviews WHERE package_id = 'pkg-blocked'").fetchone()[0]
        assert rb == ["rights_fixture"], rb
        print("ok  t04 6: legacy completed package -> legacy_unreviewed; cannot be accepted, can be rejected; not "
              "exportable; an open rights blocker refuses acceptance until the reviewer explicitly resolves it")

        # No selection -> no chat call at all.
        conn.execute("UPDATE athletes SET external = external - 'wikidata_roles' || '{\"wikidata_roles\": {\"occupations\": [\"coach\"], \"national_teams\": []}}'::jsonb")
        r4 = stages.screen_run(schema)
        made.append(RUNS_DIR / r4["run_id"])
        calls = []
        g4 = stages.generate_run(schema, chat_factory=fake_chat("forbidden", calls))
        made.append(RUNS_DIR / g4["run_id"])
        assert r4["outcome"] == "no_selection" and g4["outcome"] == "no_selection" and calls == [], (r4, g4)
        print("ok  t04 4: no eligible candidate -> no_selection, and generation makes no model call")
    finally:
        screen.screen_packet = real_screen
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


class VClock:
    """Virtual clock: sleep() advances time instantly and is recorded."""
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 2))
        self.t += s


def check_pacing():
    from arabsports import compose
    from arabsports.compose import Pacer, ProviderError, groq_chat, parse_reset
    base = {"groq_tpm": 8000, "groq_rpm": 30, "groq_max_wait_s": 300, "groq_max_tokens": 2000, "groq_retries": 1,
            "groq_max_calls": 6}
    ok = lambda hdr=None: (200, hdr or {}, json.dumps({"model": "FIXTURE", "usage": {"total_tokens": 1},
                                                       "choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode())

    def run_calls(responses, msgs, limits=base, n=1):
        vc, log, sent = VClock(), [], []
        seq = iter(responses)

        def transport(url, data, headers):
            sent.append(json.loads(data))
            return next(seq)
        chat = groq_chat("FIXTURE-KEY", "openai/gpt-oss-120b", limits, log, transport,
                         Pacer(limits, clock=vc.clock, sleep=vc.sleep))
        err = None
        try:
            for _ in range(n):
                chat("step", msgs)
        except ProviderError as e:
            err = e.kind
        return vc, log, sent, err

    assert parse_reset("2m52.8s") == 172.8 and parse_reset("3.81s") == 3.81 and parse_reset("450ms") == 0.45
    big = [{"role": "user", "content": "x" * 9000}]          # ~3020 input + 2000 reserved = 5040 tokens
    small = [{"role": "user", "content": "x" * 300}]         # ~2120 tokens
    vc, log, sent, err = run_calls([ok(), ok()], big, n=2)
    assert err is None and vc.sleeps == [60.0] and sent[0]["reasoning_effort"] == "low", (vc.sleeps, err)
    vc, log, _, err = run_calls([ok({"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "7.66s"}), ok()], small, n=2)
    assert err is None and vc.sleeps == [7.66], vc.sleeps
    vc, log, _, err = run_calls([(429, {"retry-after": "12"}, b"{}"), ok()], small)
    assert err is None and vc.sleeps == [12.0] and [e["status"] for e in log] == [429, 200], (vc.sleeps, log)
    vc, log, _, err = run_calls([(429, {}, b"{}"), ok()], small)
    assert vc.sleeps == [5.0], vc.sleeps
    vc, log, _, err = run_calls([(429, {"retry-after": "3"}, b"{}"), (429, {"retry-after": "3"}, b"{}")], small)
    assert err == "quota_or_rate_limited" and len(log) == 2 and vc.sleeps == [3.0], (err, log, vc.sleeps)
    vc, log, _, err = run_calls([(429, {"retry-after": "30"}, b"{}")], small, {**base, "groq_max_wait_s": 10})
    assert err == "quota_or_rate_limited" and vc.sleeps == [], (err, vc.sleeps)
    vc, log, sent, err = run_calls([ok()], [{"role": "user", "content": "x" * 30000}])
    assert err == "request_too_large" and sent == [] and log == [], (err, sent)
    # a 429 on the last call the cap allows keeps the rate-limit error (no room to retry, so no backoff wait)
    vc, log, _, err = run_calls([(429, {"retry-after": "1"}, b"{}"), ok()], small, {**base, "groq_max_calls": 1})
    assert err == "quota_or_rate_limited" and len(log) == 1 and vc.sleeps == [], (err, log, vc.sleeps)
    # a call blocked by our own cap before it is sent is still call_limit
    vc, log, sent, err = run_calls([ok(), ok()], small, {**base, "groq_max_calls": 1}, n=2)
    assert err == "call_limit" and len(log) == 1 and len(sent) == 1, (err, log)
    vc, log, _, err = run_calls([ok({"x-ratelimit-remaining-requests": "0"}), ok()], small, n=2)
    assert err == "daily_limit" and len(log) == 1, (err, log)
    vc, log, _, err = run_calls([(400, {}, b'{"error":{"code":"json_validate_failed"}}')], small)
    assert err == "malformed_output", err
    # AR-03: one pacer shared by two runs (separate logs and caps): the second run waits for the first run's requests
    vc, logs, seq = VClock(), ([], []), iter([ok()] * 3)
    shared = Pacer({**base, "groq_rpm": 2}, clock=vc.clock, sleep=vc.sleep)
    run_a, run_b = (groq_chat("FIXTURE-KEY", "openai/gpt-oss-120b", {**base, "groq_max_calls": 2}, log,
                              lambda *a: next(seq), shared) for log in logs)
    run_a("step", small); run_a("step", small)
    try:
        run_a("step", small)
        raise AssertionError("run A's own call cap must hold")
    except ProviderError as e:
        assert e.kind == "call_limit"
    assert vc.sleeps == [] and run_b("step", small) == {"ok": True} and vc.sleeps == [60.0], vc.sleeps
    assert [len(x) for x in logs] == [2, 1] and logs[1][0]["pacing_wait_total_s"] == 60.0, logs
    # token pacing carries across players: player 1's ~5040-token call fills the 8000/minute window, so player 2's
    # call waits 60s on a shared pacer; with a pacer per player it would not wait
    for share, want in ((True, [60.0]), (False, [])):
        vc, seq = VClock(), iter([ok()] * 2)
        mk = lambda: Pacer(base, clock=vc.clock, sleep=vc.sleep)
        one = mk()
        players = [groq_chat("FIXTURE-KEY", "openai/gpt-oss-120b", base, [], lambda *a: next(seq), one if share else mk())
                   for _ in range(2)]
        for p in players:
            p("angle_proposal", big)
        assert vc.sleeps == want, (share, vc.sleeps)
    srcs = [{"url": f"u{i}", "text": "y" * 9000, "sid": f"S{i}", "publisher": "F"} for i in range(3)]
    res = compose.compose({"athlete": {"name": "N", "profile": {}}, "sources": srcs, "coverage": []},
                          lambda *a: {"facts": []}, {**base, "max_revisions": 0})
    assert res["omitted_sources"] == ["u1", "u2"] and res["status"] == "insufficient_evidence", res  # 5200-token input budget
    res = compose.compose({"athlete": {"name": "N", "profile": {}}, "sources": [{**srcs[0], "text": "y" * 30000}],
                           "coverage": []}, lambda *a: {}, {**base, "max_revisions": 0})
    assert res["status"] == "request_too_large", res
    print("ok  groq pacing (virtual clock, FIXTURE responses): window wait 60s; header reset 7.66s; retry-after 12s; "
          "fallback 5s; retry exhaustion; wait-budget refusal; oversized rejected unsent; attempts count toward the "
          "call cap; daily limit; json_validate_failed -> malformed; sources omitted visibly; none fits -> request_too_large; "
          "a pacer shared by two runs makes the second wait for the first's requests and tokens while each keeps its own "
          "log and cap; a 429 on the last allowed call stays quota_or_rate_limited, a pre-send cap block is call_limit")


def check_discovery_db():
    """Pagination through run('live') against a FIXTURE Wikidata/Ajel router (no network), temp schema."""
    from urllib.parse import parse_qs, urlsplit
    from arabsports.http import FetchError
    url = settings()["database_url"]
    if not url:
        print("SKIP discovery db checks: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    index = ["QD1", "QD2", "QD3", "QD4", "QD5"]
    names = {q: f"Fixture Athlete {q[-1]}" for q in index + ["QD6"]}
    state = {"fail_offsets": set(), "fail_entities": False, "wire_q": []}

    def entity(q):
        return {"labels": {"en": {"value": names[q]}}, "aliases": {}, "sitelinks": {},
                "claims": {"P641": [], "P27": [], "P569": [], "P1532": [{"id": f"{q}$r", "rank": "normal",
                           "mainsnak": {"datavalue": {"value": {"id": "Q262"}}}, "references": []}]}}

    def router(u, **kw):
        p = {k: v[0] for k, v in parse_qs(urlsplit(u).query).items()}
        if "wikidata.org" in u and p.get("list") == "search":
            off, lim = int(p.get("sroffset", 0)), int(p["srlimit"])
            if off in state["fail_offsets"]:
                raise FetchError("HTTP 503")
            body = {"query": {"search": [{"title": q} for q in index[off:off + lim]]}}
            if off + lim < len(index):
                body["continue"] = {"sroffset": off + lim, "continue": "-||"}
            return json.dumps(body)
        if "wikidata.org" in u and p.get("action") == "wbgetentities":
            ids = p["ids"].split("|")
            if p["props"] == "labels":
                return json.dumps({"entities": {i: {"labels": {"en": {"value": i}}} for i in ids}})
            if state["fail_entities"]:
                raise FetchError("HTTP 503")
            return json.dumps({"entities": {i: entity(i) for i in ids}})
        if "ai-feed.json" in u:
            return json.dumps({"source": "FIXTURE", "language": "en", "items": []})
        if "/v1/search" in u:
            state["wire_q"].append(p["q"])
            return json.dumps({"data": []})
        if "thesportsdb" in u:
            return json.dumps({"player": []})
        raise AssertionError(f"unexpected FIXTURE request {u}")

    env = {"GDELT_ENABLED": "0", "WIKIDATA_PER_NATION": "2", "WIKIDATA_MAX_NATIONS": "1", "WIKIDATA_MAX_PAGES": "3",
           "WIKIDATA_REFRESH": "0", "AJEL_NAME_SEARCHES": "10"}
    saved = {k: os.environ.get(k) for k in env}
    real_get, real_sleep = sources.get, sources._sleep
    sources.get, sources._sleep = router, (lambda s: None)
    os.environ.update(env)
    key = sources.query_key("Q262")

    def live():
        r = run("live", schema=schema)
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"].startswith("completed"), r
        with store.connect(url, schema) as f:
            cur = f.execute("SELECT next_offset, exhausted FROM discovery_cursors WHERE query_key = %s", (key,)).fetchone()
            n = f.execute("SELECT count(*) FROM athletes WHERE source = 'wikidata'").fetchone()[0]
        return r["counts"]["discovery"], cur, n
    try:
        d, cur, n = live()
        assert d["new_ids"] == ["QD1", "QD2"] and cur == (2, False) and n == 2, (d, cur, n)
        assert {"Fixture Athlete 1", "Fixture Athlete 2"} <= set(state["wire_q"]), state["wire_q"]
        state["fail_offsets"] = {2}
        d, cur, n = live()
        assert d["new_ids"] == [] and d["failed_queries"] == [key] and cur == (2, False) and n == 2, (d, cur, n)
        state["fail_offsets"] = set()
        d, cur, n = live()
        assert d["new_ids"] == ["QD3", "QD4"] and cur == (4, False) and n == 4, (d, cur, n)
        d, cur, n = live()
        assert d["new_ids"] == ["QD5"] and cur == (5, True) and d["exhausted_queries"] == 1 and n == 5, (d, cur, n)
        print("ok  discovery 1: pages 0->2->4 resume from the saved cursor; a failed page (503 @2) holds the cursor; "
              "exhausted at 5; new athletes fed to the Wire rotation")

        index.append("QD6")
        state["fail_entities"] = True
        d, cur, n = live()
        assert cur == (5, True) and n == 5 and d["cursors_held"] == [key], (d, cur, n)
        state["fail_entities"] = False
        d, cur, n = live()
        assert d["new_ids"] == ["QD6"] and cur == (6, True) and n == 6, (d, cur, n)
        print("ok  discovery 2: exhausted query re-probed and found a new result; entity-fetch failure did not advance "
              "past unstored ids; next run stored QD6")

        with store.connect(url, schema) as f:
            store.save_cursors(f, {key: {"next_offset": 0, "exhausted": False}}, None)
        names["QD1"] = "Fixture Athlete One Renamed"
        os.environ["WIKIDATA_REFRESH"] = "6"
        d, cur, n = live()
        with store.connect(url, schema) as f:
            renamed = f.execute("SELECT name FROM athletes WHERE source_id = 'QD1'").fetchone()[0]
        assert d["new_ids"] == [] and d["repeats"] == 6 and n == 6 and d["refreshed"] == 6, (d, n)
        assert renamed == "Fixture Athlete One Renamed", renamed
        print("ok  discovery 3: forced re-read of pages 0-4 -> 6 repeats, no duplicate rows; refresh updated an "
              "existing profile after its query was exhausted")
    finally:
        sources.get, sources._sleep = real_get, real_sleep
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        with store.connect(url) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for dd in made:
            shutil.rmtree(dd, ignore_errors=True)


# ---------- Wikipedia biographies (FIXTURE pages, no network) ----------

def bio_html(name, nation, extra=""):
    """FIXTURE Parsoid-like HTML: infobox, hatnote, lead, sections with h2/h3, citation markers, reference list."""
    ref = lambda i: f'<sup class="mw-ref reference"><a href="./P#cite_note-{i}">[{i}]</a></sup>'
    return (f'<html><body><section data-mw-section-id="0"><div class="hatnote">For others, see HATNOTE.</div>'
            f'<table class="infobox"><tr><td>{name} INFOBOX</td></tr></table>'
            f'<p>{name} (born 1995) is a FIXTURE athlete.{ref(1)} {extra}</p></section>'
            f'<section><h2 id="Early_life">Early life</h2><p>{name} grew up in a FIXTURE town and trained at dawn.</p></section>'
            f'<section><h2>International career</h2><h3>Senior</h3><p>{name} represented {nation} at the 2024 '
            f'Summer Olympics.{ref(2)}</p><p>Singles Mixed doubles</p><ul><li>LIST ITEM</li></ul></section>'
            f'<section><h2>References</h2><p>REFERENCES PROSE</p><div class="mw-references-wrap"><ol class="mw-references references">'
            f'<li id="cite_note-1"><span class="mw-cite-backlink"><a href="./P#cite_ref-1">↑</a></span> '
            f'<span class="mw-reference-text">FIXTURE ref one <a rel="mw:ExtLink" href="https://fixture.test/ref1">x</a></span></li>'
            f'<li id="cite_note-2"><span class="mw-reference-text">FIXTURE ref two <a rel="mw:ExtLink" '
            f'href="https://fixture.test/ref2">y</a></span></li></ol></div></section></body></html>')


def check_wikipedia_parse():
    from arabsports import screen, wikipedia as wp
    secs, cites = wp.parse(bio_html("Karim Fixture", "Morocco"))
    text = wp.content_text(secs)
    assert [s["heading"] for s in secs] == ["Lead", "Early life", "International career > Senior"], secs
    assert not any(x in text for x in ("INFOBOX", "HATNOTE", "LIST ITEM", "REFERENCES PROSE", "[1]", "Mixed doubles")), text
    assert secs[2]["paragraphs"][0]["cites"] == ["cite_note-2"] and cites["cite_note-2"]["urls"] == ["https://fixture.test/ref2"]
    assert cites["cite_note-1"]["text"].startswith("FIXTURE ref one"), cites
    page = lambda title, q, **kw: {"pageid": 1, "title": title, "pageprops": {"wikibase_item": q, **kw},
                                   "revisions": [{"revid": 5, "timestamp": "2026-01-01T00:00:00Z"}]}
    ident = lambda pg, hops=(): {"requested": "t", "normalized": None, "redirects": list(hops), "page": pg}
    assert wp.classify("Q1", ident(page("A", "Q1"))) == "verified"
    assert wp.classify("Q1", ident(page("A", "Q1"), [{"from": "a", "to": "A"}])) == "verified"
    assert wp.classify("Q1", ident(page("A", "Q1"), [{"from": "a", "to": "A", "tofragment": "Squad"}])) == "entity_mismatch"
    assert wp.classify("Q1", ident(page("A", "Q2"))) == "entity_mismatch"
    assert wp.classify("Q1", ident(page("A", "Q1", disambiguation=""))) == "disambiguation"
    assert wp.classify("Q1", ident({"title": "A", "missing": True})) == "missing" == wp.classify("Q1", ident(None))
    real = wp._get
    wp._get = lambda u, **kw: json.dumps({"query": {"normalized": [{"from": "mo salah", "to": "Mo salah"}],
                                                    "redirects": [{"from": "Mo salah", "to": "A"}],
                                                    "pages": [page("A", "Q1")], "rightsinfo": {"text": "CC"}}})
    try:
        out, rights = wp.identify(["mo salah"], 5, 0)
    finally:
        wp._get = real
    assert out["mo salah"]["page"]["title"] == "A" and out["mo salah"]["redirects"] == [{"from": "Mo salah", "to": "A"}]
    # Long page: 10 sections -> 6 evenly spaced (first and last included), the rest counted by section.
    long_text = "\n\n".join(f"== S{i} ==\n\nKarim Fixture paragraph {i} about the season." for i in range(10))
    wins, red = screen.Windows().build_bio({"name": "Karim Fixture"}, {"id": 1, "content": long_text, "canonical_url": "u",
                                                                       "publisher": "p"})
    assert [w["section"] for w in wins] == ["S0", "S2", "S4", "S5", "S7", "S9"], [w["section"] for w in wins]
    assert red["windows_total"] == 10 and sum(red["not_screened_by_section"].values()) == 4 and "Section: S9" in wins[-1]["text"]
    print("ok  wikipedia 1: prose + headings kept, infobox/lists/labels/refs dropped, citations mapped; verified/redirect/"
          "section-redirect/other-entity/disambiguation/missing; long page windows spread start->end with the rest counted")


def check_biography_db():
    """biography_run + screen_run on FIXTURE Wikipedia responses (no network) in a temp schema."""
    from arabsports import screen, stages, wikipedia as wp
    url = settings()["database_url"]
    if not url:
        print("SKIP biography db checks: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    fresh = lambda: store.connect(url, schema)
    cache = RUNS_DIR / f"fixture-{schema}-cache"
    made.append(cache)
    # title -> (final title, qid, extra pageprops, redirect-from) ; revid/html per final title are mutable state
    pages = {"Karim Fixture": ("Karim Fixture", "QB1", {}, None), "Laila (athlete)": ("Laila Fixture", "QB2", {}, "Laila (athlete)"),
             "Sami Fixture": ("Sami Fixture", "QSAMI", {"disambiguation": ""}, None), "Nadia Fixture": None,
             "Omar Fixture": ("Fixture FC", "QFC", {}, "Omar Fixture")}
    rev = {"Karim Fixture": 101, "Laila Fixture": 201}
    html = {101: bio_html("Karim Fixture", "Morocco"), 201: bio_html("Laila Fixture", "Egypt")}
    state = {"fail_identity": False, "fail_html": set(), "requests": []}

    def router(u, **kw):
        state["requests"].append(u)
        if "/rest.php/v1/revision/" in u:
            rid = int(u.split("/revision/")[1].split("/")[0])
            if rid in state["fail_html"]:
                raise FetchError("HTTP 503")
            return html[rid]
        if state["fail_identity"]:
            raise FetchError("HTTP 503")
        from urllib.parse import parse_qs, urlsplit
        titles = parse_qs(urlsplit(u).query)["titles"][0].split("|")
        out, redirects = [], []
        for t in titles:
            p = pages.get(t)
            if p is None:
                out.append({"title": t, "missing": True})
                continue
            final, q, props, frm = p
            if frm:
                redirects.append({"from": frm, "to": final})
            out.append({"pageid": abs(hash(final)) % 10 ** 6, "title": final, "pageprops": {"wikibase_item": q, **props},
                        "revisions": [{"revid": rev.get(final, 999), "timestamp": "2026-11-01T00:00:00Z"}]})
        return json.dumps({"query": {"redirects": redirects, "pages": out, "rightsinfo": {
            "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en", "text": "Creative Commons Attribution-Share Alike 4.0"}}})

    calls = []

    def fixture_screen(pkt):
        calls.append(pkt["name"])
        return [{"fixture": True, "windows": len(pkt["windows"])}], "routine", None, []
    real = (wp._get, wp.CACHE, screen.screen_packet)
    wp._get, wp.CACHE, screen.screen_packet = router, cache, fixture_screen
    A = lambda n: f"https://fixture.test/sports/b{n}"

    def bio():
        r = stages.biography_run(schema)
        made.append(RUNS_DIR / r["run_id"])
        with fresh() as f:
            rows = f.execute("SELECT integrations->'wikipedia'->'rows' FROM runs WHERE id = %s", (r["run_id"],)).fetchone()[0]
        return r, {x["name"]: x["outcome"] for x in rows}

    def scr():
        calls.clear()
        r = stages.screen_run(schema)
        made.append(RUNS_DIR / r["run_id"])
        with fresh() as f:
            sel = store.latest_selection(f)
        return sorted(calls), {c["name"]: c for c in sel["candidates"]}

    def evidence(f):
        return {n: (v, st) for n, v, st in f.execute("SELECT name, evidence_version, evidence_status FROM athletes "
                                                     "WHERE source_id LIKE 'QB%%'")}
    try:
        ents = [("QB1", "Karim Fixture", "Q2736", "Q1028"), ("QB2", "Laila Fixture", "Q542", "Q79"),
                ("QB3", "Sami Fixture", "Q542", "Q810"), ("QB4", "Nadia Fixture", "Q542", "Q79"),
                ("QB5", "Omar Fixture", "Q2736", "Q79"), ("QB6", "Hadi Fixture", "Q2736", "Q1028")]
        replay("b1", raw_input(entities=ents, stories=[
            (A(1), "FIXTURE derby", "Moroccan striker Karim Fixture scored twice in the football derby."),
            (A(2), "FIXTURE final", "Moroccan winger Hadi Fixture set up the winner in the football final.")]), schema, made)
        sitelinks = {"QB1": "Karim Fixture", "QB2": "Laila (athlete)", "QB3": "Sami Fixture", "QB4": "Nadia Fixture",
                     "QB5": "Omar Fixture"}
        for aid, qid in conn.execute("SELECT id, source_id FROM athletes WHERE source_id LIKE 'QB%%'").fetchall():
            ext = {"wikidata_roles": {"occupations": ["association football player"], "national_teams": [], "fixture": True}}
            if qid in sitelinks:
                ext["enwiki"] = sitelinks[qid]
            conn.execute("UPDATE athletes SET external = external || %s WHERE id = %s", (psycopg.types.json.Jsonb(ext), aid))
        c0, _ = scr()
        assert c0 == ["Hadi Fixture", "Karim Fixture"], c0
        with fresh() as f:
            ev0 = evidence(f)

        r1, out = bio()
        want = {"Karim Fixture": "imported_new", "Laila Fixture": "imported_new", "Sami Fixture": "disambiguation",
                "Nadia Fixture": "missing", "Omar Fixture": "entity_mismatch", "Hadi Fixture": "no_sitelink"}
        assert out == want and r1["counts"]["requests"] == 3, (out, r1["counts"])
        with fresh() as f:
            arts = f.execute("SELECT title, provider, acquisition, published_at, extra, usage_terms, content FROM articles "
                             "WHERE provider = 'wikipedia' ORDER BY title").fetchall()
            ment = f.execute("SELECT m.method, m.status FROM mentions m JOIN articles ar ON ar.id = m.article_id "
                             "WHERE ar.provider = 'wikipedia'").fetchall()
            ev1 = evidence(f)
        assert [a[0] for a in arts] == ["Karim Fixture", "Laila Fixture"] and all(a[3] is None for a in arts)
        x = arts[1][4]
        assert x["license"]["text"].startswith("Creative Commons Attribution-Share Alike 4.0") and x["revision"]["id"] == 201
        assert "oldid=201" in x["attribution"]["text"] and x["identity"]["redirects"] == [{"from": "Laila (athlete)", "to": "Laila Fixture"}]
        assert x["citations"]["cite_note-2"]["urls"] == ["https://fixture.test/ref2"] and "INFOBOX" not in arts[1][6]
        assert arts[1][5]["flags"] == {"attribution_required": True, "licence_notice_required": True, "share_alike_required": True}
        assert set(ment) == {("wikidata_sitelink_verified", "linked")}, ment
        assert ev1["Laila Fixture"] == (ev0["Laila Fixture"][0] + 1, "biography_only"), (ev0, ev1)
        assert ev1["Karim Fixture"] == (ev0["Karim Fixture"][0] + 1, "has_full_text_news") and ev1["Hadi Fixture"] == ev0["Hadi Fixture"], ev1
        print("ok  biography 1: verified + redirect imported; disambiguation/missing/other-entity/no-sitelink recorded, "
              "nothing stored for them; licence, revision link, attribution, citations kept; no publication date")

        c1, cands = scr()
        assert c1 == ["Karim Fixture", "Laila Fixture"], c1  # Hadi's packet unchanged -> cached screening reused
        el = cands["Laila Fixture"]["eligibility"]
        bio_ev = [e for e in el["representation_evidence"] if e["route"] == "wikipedia_biography"]
        assert bio_ev and bio_ev[0]["section"] == "International career > Senior" and "represented Egypt" in bio_ev[0]["passage"]
        assert bio_ev[0]["cited_references"][0]["urls"] == ["https://fixture.test/ref2"] and "not independent" in bio_ev[0]["uncertainty"]
        assert el["independent_of_wikimedia"] == [], el["representation_routes"]
        with fresh() as f:
            pk = f.execute("SELECT content FROM packets p JOIN athletes a ON a.id = p.athlete_id WHERE a.name = 'Laila Fixture'").fetchone()[0]
        assert {w["section"] for w in pk["windows"]} == {"Lead", "Early life", "International career > Senior"}
        assert pk["biography_reduction"][0]["windows_total"] == 3 and pk["sources"][0]["kind"] == "biography"
        print("ok  biography 2: biography windows enter packets with section headers; only the 2 athletes with new "
              "evidence are rescreened (Hadi reused); representation passage, citations and uncertainty kept; "
              "Wikipedia+Wikidata not counted as independent")

        state["requests"].clear()
        r2, out = bio()
        rest = [u for u in state["requests"] if "/revision/" in u]
        with fresh() as f:
            n_art, ev2 = f.execute("SELECT count(*) FROM articles WHERE provider = 'wikipedia'").fetchone()[0], evidence(f)
        assert out == {"Karim Fixture": "unchanged_revision_reused", "Laila Fixture": "unchanged_revision_reused"}, out
        assert rest == [] and n_art == 2 and ev2 == ev1, (rest, n_art, ev2)
        rev["Karim Fixture"], html[102] = 102, bio_html("Karim Fixture", "Morocco").replace("trained at dawn.", "trained at dawn .")
        _, out = bio()
        with fresh() as f:
            ev3 = evidence(f)
        assert out["Karim Fixture"] == "imported_existing" and ev3 == ev1, (out, ev3)
        rev["Karim Fixture"], html[103] = 103, bio_html("Karim Fixture", "Morocco", "He returned from a knee injury in 2025.")
        _, out = bio()
        with fresh() as f:
            ev4 = evidence(f)
            x = f.execute("SELECT extra FROM articles WHERE provider = 'wikipedia' AND title = 'Karim Fixture'").fetchone()[0]
        assert out["Karim Fixture"] == "imported_changed" and ev4["Karim Fixture"][0] == ev1["Karim Fixture"][0] + 1
        assert x["revision"]["id"] == 103 and x["previous"]["replaced_by_run"] and ev4["Laila Fixture"] == ev1["Laila Fixture"]
        c2, _ = scr()
        assert c2 == ["Karim Fixture"], c2
        print("ok  biography 3: same revision reused with no fetch; new revision with only punctuation change keeps "
              "the evidence version; meaningful change bumps it, updates revision metadata, rescreens only that athlete")

        with fresh() as f:
            before = f.execute("SELECT id, content_hash, extra->'revision' FROM articles WHERE provider = 'wikipedia' ORDER BY id").fetchall()
        state["fail_identity"] = True
        rf, _ = bio()
        state["fail_identity"] = False
        rev["Karim Fixture"], html[104] = 104, bio_html("Karim Fixture", "Morocco", "Different text.")
        state["fail_html"] = {104}
        _, out = bio()
        pages["Laila (athlete)"] = None  # page deleted since import
        _, out2 = bio()
        with fresh() as f:
            after = f.execute("SELECT id, content_hash, extra->'revision' FROM articles WHERE provider = 'wikipedia' ORDER BY id").fetchall()
            ev5 = evidence(f)
            lc = f.execute("SELECT external->'wikipedia_bio' FROM athletes WHERE source_id = 'QB2'").fetchone()[0]
        assert rf["outcome"] == "completed_with_source_failures" and out["Karim Fixture"].startswith("fetch_failed")
        assert out2["Laila Fixture"] == "missing" and lc["status"] == "imported" and lc["latest_check"]["status"] == "missing"
        assert after == before and ev5 == ev4, (after, before)
        print("ok  biography 4: identity failure, revision fetch failure and a now-missing page leave stored biographies "
              "and evidence versions intact (missing page flagged for review)")
    finally:
        wp._get, wp.CACHE, screen.screen_packet = real
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def _fix_proposal(ids=("F1", "F2", "F3", "F4"), quote_ok=True):
    """FIXTURE angle-proposal output over FIX_BODY as passage P1."""
    stmts = [f["statement"] for f in FIX_FACTS["facts"][:4]]
    # FIX_BODY excerpts: P1.1 cup-final winner, P1.2 Fes academy, P1.3 knee injury, P1.4 mentoring
    field = lambda t, ex: {"text": t, "excerpts": [ex if quote_ok else "P1.9"]}
    return {"verdict": "angle", "reason": "FIXTURE", "stakes": field("A long injury threatened his career.", "P1.3"),
            "turning_point": field("He came back and scored the cup-final winner.", "P1.1"),
            "significance": field("He now mentors young players in Fes.", "P1.4"),
            "facts": [{"id": i, "statement": s, "passages": ["P1"]} for i, s in zip(ids, stmts)], "omit": ""}


def check_angles():
    """Automated angle validation and automatic graphics, without a database or model."""
    from arabsports import angles, export
    row = {"id": 1, "canonical_url": "https://fixture.test/a1", "publisher": "FIXTURE", "content_hash": "h",
           "content": FIX_BODY + "\n\nshort\n\nA routine paragraph with no story cues about the weather in Fes city today.",
           "usage_terms": None}
    passages, found = angles.select_passages("Karim Benali", [row, {**row, "id": 2, "usage_terms": {
        "flags": {"republication_allowed": False}}}])
    assert [p["id"] for p in passages] == ["P1"] and passages[0]["passage"] == FIX_BODY and found == 1, passages
    ok = angles.validate(_fix_proposal(), passages)
    assert ok[0] == "advanced" and [f["id"] for f in ok[2]["facts"]] == ["F1", "F2", "F3", "F4"], ok
    assert ok[3][0]["passage"] == FIX_BODY and ok[3][0]["content_hash"] == "h"
    # excerpt ids: code attaches the exact stored sentence; missing or unknown ids still block the proposal
    assert "[P1.3] After a knee injury in 2022 he spent eight months in rehabilitation." in angles.ask(
        "Karim Benali", ["Morocco"], passages)[1]["content"]
    assert ok[2]["quotes"] == {"stakes": "After a knee injury in 2022 he spent eight months in rehabilitation.",
                               "turning_point": "Moroccan forward Karim Benali scored the winner in the 2024 cup final.",
                               "significance": "He returned to captain the youth side and now mentors young players in Fes."}
    assert ok[2]["excerpts"]["stakes"] == ["P1.3"]
    bad_quote = angles.validate(_fix_proposal(quote_ok=False), passages)
    assert bad_quote[0] == "invalid_references" and any("unknown or no excerpt ids ['P1.9']" in r for r in bad_quote[1])
    for mutate in (lambda o: o["stakes"].pop("excerpts"), lambda o: o["stakes"].update(excerpts=[]),
                   lambda o: o["significance"].update(excerpts=["P2.1"]), lambda o: o["turning_point"].update(excerpts=["P1"])):
        o = _fix_proposal(); mutate(o)
        r = angles.validate(o, passages)
        assert r[0] == "invalid_references" and any("excerpt ids" in x for x in r[1]), r
    unknown = _fix_proposal(); unknown["stakes"]["excerpts"] = ["P9.1"]
    assert angles.validate(unknown, passages)[0] == "invalid_references"
    invented = _fix_proposal()
    for f in invented["facts"][1:]:
        f["statement"] += " He won 9 caps for Spain."
    r = angles.validate(invented, passages)
    assert r[0] == "invalid_references" and any("number 9" in x for x in r[1]) and any("only 1 fact" in x for x in r[1]), r
    # AR-04: a valid excerpt id in a fact resolves to its stored passage and the cited excerpt is kept
    ex_ids = _fix_proposal()
    ex_ids["facts"][0]["passages"], ex_ids["facts"][2]["passages"] = ["P1.1"], ["P1.3", "P1"]
    r = angles.validate(ex_ids, passages)
    assert r[0] == "advanced" and [f["refs"] for f in r[2]["facts"]] == [["P1"]] * 4, r
    assert r[2]["facts"][0]["excerpts"] == ["P1.1"] and r[2]["facts"][2]["excerpts"] == ["P1.3"] and "excerpts" not in r[2]["facts"][1]
    unknown_ex = _fix_proposal(); unknown_ex["facts"][0]["passages"] = ["P1.9"]
    r = angles.validate(unknown_ex, passages)
    assert r[0] == "advanced" and "fact 'F1': unknown or no passages" in r[1] and len(r[2]["facts"]) == 3, r
    unsupported_ex = _fix_proposal(); unsupported_ex["facts"][0].update(passages=["P1.1"], statement="Benali scored 9 goals.")
    assert "number 9 not in passages" in angles.validate(unsupported_ex, passages)[1][0]
    other = {**passages[0], "id": "P2", "article_id": 2, "passage": "Karim Benali scored the winner in the 2024 cup final again."}
    cross = _fix_proposal(); cross["facts"][0]["passages"] = ["P1.1", "P2.1"]
    r = angles.validate(cross, passages + [other])
    assert r[0] == "invalid_references" and any("more than one source" in x for x in r[1]), r
    import pathlib
    tr = json.loads((pathlib.Path(__file__).parent / "fixtures" / "angle_trabelsi_b166.json").read_text(encoding="utf-8"))
    r = angles.validate(tr["model_output"], tr["passages_sent"])
    assert tr["outcome_then"] == "invalid_references" and r[0] == "advanced" and r[1] == [], r
    assert [(f["id"], f["refs"], f.get("excerpts")) for f in r[2]["facts"]] == [
        ("A1", ["P8"], None), ("A2", ["P8"], ["P8.3"]), ("A3", ["P8"], ["P8.3"]), ("A4", ["P8"], ["P8.4"])], r[2]["facts"]
    assert angles.validate({"verdict": "insufficient", "reason": "match recap"}, passages)[:2] == (
        "insufficient_evidence", ["model: match recap"])
    # valid JSON, wrong types: recorded as malformed output, never a crash
    for mutate, why in [(lambda o: o["stakes"].update(text=42), "stakes.text is not text"),
                        (lambda o: o["stakes"].update(excerpts=42), "stakes.excerpts is not a list"),
                        (lambda o: o["stakes"].update(excerpts="P1.1"), "stakes.excerpts is not a list"),
                        (lambda o: o.update(turning_point="P1"), "turning_point is not an object"),
                        (lambda o: o["facts"][0].update(statement=42), "fact 1.statement is not text"),
                        (lambda o: o.update(facts=42), "facts is not a list"),
                        (lambda o: o.update(omit=42), "omit is not text")]:
        o = _fix_proposal(); mutate(o)
        r = angles.validate(o, passages)
        assert r[0] == "malformed_output" and why in r[1][0], (why, r)
    from arabsports.stages import candidates_for
    sel = {"candidates": [{"athlete_id": 1, "rank": 2, "eligible": True}, {"athlete_id": 2, "rank": 1, "eligible": True},
                          {"athlete_id": 3, "eligible": True, "score": 0.2, "n_articles": 1},
                          {"athlete_id": 4, "eligible": False, "score": 0.9, "n_articles": 9},
                          {"athlete_id": 5, "eligible": True, "score": 0.5, "n_articles": 1}]}
    ids = lambda **k: [c["athlete_id"] for c in candidates_for(sel, **k)]
    assert ids() == [2, 1] and ids(wider=True) == [2, 1, 5, 3] and ids(wider=True, exclude=[2, 5]) == [1, 3]
    pkg = {"title": "T", "athlete": {"name": "Mohamed Salah", "represents": ["Egypt"], "sport": ["association football"]},
           "slides": [{"heading": "A long wait", "body": "Egypt had been absent for 28 years since 1990."},
                      {"heading": "Late drama", "body": "Congo equalized in the 88th minute."},
                      {"heading": "The penalty", "body": "Egypt won 2–1 against Congo in stoppage time."},
                      {"heading": "Back at last", "body": "Egypt qualified for 2018 after 28 years."},
                      {"heading": "Why it matters", "body": "A generation saw its team return."}]}
    pres = export.auto_presentation(pkg)
    v = pres["visuals"]
    assert v[0] == {"kind": "figure", "text": "28 years", "label": "A long wait"}, v[0]
    assert v[1]["kind"] == "pitch" and v[1]["text"] == "88′" and v[2] == {"kind": "score", "home": "Egypt", "score": "2–1",
                                                                          "away": "Congo", "label": "The penalty"}, v
    assert v[3]["text"] == "2018" and v[4] == {"kind": "figure", "text": "Why it matters"}, v  # no repeat; typographic fallback
    assert pres["theme"]["strip"] == export.FLAGS["Egypt"] and pres["theme"]["accent"] == "#C8102E"
    print("ok  angles: passage selection skips refused sources and cue-less paragraphs; a proposal advances only when "
          "its excerpt ids resolve to exact stored text, passages exist and 3+ facts pass code checks; graphics come from the copy without repeats")


def check_auto_db():
    """auto_run end to end on FIXTURE data (Laya and Groq stand-ins): call reserve, invalid proposal, then a
    validated proposal drafted and saved in the same run."""
    import os
    from arabsports import screen, stages, export
    url = settings()["database_url"]
    if not url:
        print("SKIP auto db check: DATABASE_URL not set")
        return
    schema, made = "check_" + secrets.token_hex(4), []
    conn = store.connect(url, schema)
    real_screen = screen.screen_packet
    scores = {"Karim Benali": 0.6, "Omar Zidane": 0.5}
    screen.screen_packet = lambda pkt: ([{"fixture": True}], "promising", scores[pkt["name"]], [])
    A = lambda n: f"https://fixture.test/auto/a{n}"
    old_cap = os.environ.get("GROQ_MAX_CALLS")
    try:
        replay("a1", raw_input(entities=[("QAK", "Karim Benali", "Q2736", "Q1028"), ("QAO", "Omar Zidane", "Q2736", "Q79")],
                               stories=[(A(1), "FIXTURE comeback", FIX_BODY),
                                        (A(2), "FIXTURE season", "Egyptian forward Omar Zidane scored twice in football "
                                         "after a hard season and returned to captain the side in the final.")]),
               schema, made)
        for aid, in conn.execute("SELECT id FROM athletes WHERE source_id LIKE 'QA%%'").fetchall():
            store.set_enrichment(conn, aid, "wikidata_roles", {"occupations": ["association football player"],
                                                               "national_teams": [], "fixture": True})
        good_screen = stages.screen_run(schema)
        made.append(RUNS_DIR / good_screen["run_id"])
        assert good_screen["outcome"] == "selected", good_screen
        calls = []

        def chat_with(proposals, draft_fix=None, verifier=None, mode="good", on_step=None):
            def factory(usage_log):
                inner = fake_chat(mode, calls)(usage_log)
                def chat(step, m):
                    if on_step:
                        on_step(step)
                    if step == "angle_proposal":
                        calls.append(step); usage_log.append({"step": step})
                        return proposals.pop(0)
                    out = inner(step, m)
                    if verifier and step.startswith("verify"):
                        return verifier
                    if draft_fix and step.startswith(("compose", "revise", "repair")):
                        draft_fix(out)
                    return out
                return chat
            return factory

        def run_row(run_id):
            return conn.execute("SELECT outcome, finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)).fetchone()

        # 0) the current screen fails (an older valid shortlist exists): zero model calls, nothing saved
        real_cands, real_order = store.screening_candidates, screen.order_candidates
        store.screening_candidates = lambda *a: (_ for _ in ()).throw(RuntimeError("FIXTURE screening failure"))
        try:
            failed = stages.screen_run(schema)
        finally:
            store.screening_candidates = real_cands
        made.append(RUNS_DIR / failed["run_id"])
        assert failed["outcome"] == "failed" and store.latest_selection(conn)["run_id"] == good_screen["run_id"]
        r = stages.auto_run(failed["run_id"], schema, chat_factory=chat_with([_fix_proposal()]))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "screening_failed" and calls == [] and run_row(r["run_id"]) == ("screening_failed", True), (r, calls)
        # the current screen succeeds with no candidates: reported as such, no fallback to the older shortlist
        screen.order_candidates = lambda recs: ([], None)
        try:
            empty = stages.screen_run(schema)
        finally:
            screen.order_candidates = real_order
        made.append(RUNS_DIR / empty["run_id"])
        assert empty["outcome"] == "no_selection", empty
        r = stages.auto_run(empty["run_id"], schema, chat_factory=chat_with([_fix_proposal()]))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "no_selection" and calls == [] and r["counts"]["selection_run"] == empty["run_id"], r
        assert conn.execute("SELECT count(*) FROM story_packages").fetchone()[0] == 0
        # wrong-type proposal for Karim: recorded malformed_output, Omar is tried next within the same budget
        wrong = _fix_proposal(); wrong["stakes"]["text"] = 42
        r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with(
            [wrong, {"verdict": "insufficient", "reason": "FIXTURE routine season summary"}]))
        made.append(RUNS_DIR / r["run_id"])
        rows = conn.execute("SELECT outcome FROM angle_proposals WHERE run_id = %s ORDER BY id", (r["run_id"],)).fetchall()
        assert [x[0] for x in rows] == ["malformed_output", "insufficient_evidence"] and calls == ["angle_proposal"] * 2, (rows, calls)
        assert r["outcome"] == "no_supported_angle" and run_row(r["run_id"]) == ("no_supported_angle", True), r
        calls.clear()

        # 1) cap 5 = one angle call + the 4 reserved: Karim's proposal cites an unknown excerpt, Omar is not attempted
        os.environ["GROQ_MAX_CALLS"] = "5"
        r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with([_fix_proposal(quote_ok=False)]))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "no_supported_angle" and calls == ["angle_proposal"], (r, calls)
        assert r["counts"]["angle_attempts"] == ["Karim Benali: invalid_references", "Omar Zidane: skipped_call_reserve"]
        rows = conn.execute("SELECT outcome FROM angle_proposals WHERE run_id = %s ORDER BY id", (r["run_id"],)).fetchall()
        assert [x[0] for x in rows] == ["invalid_references", "skipped_call_reserve"]
        assert conn.execute("SELECT count(*) FROM story_packages").fetchone()[0] == 0
        # 2) default cap: Karim's valid proposal advances and is drafted, checked and saved in the same run
        os.environ.pop("GROQ_MAX_CALLS")
        calls.clear()
        r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with([_fix_proposal()]),
                            prior={"screen_run": "FIXTURE"})
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "awaiting_review" and calls == ["angle_proposal", "compose", "verify"], (r, calls)
        p = conn.execute("SELECT package FROM story_packages WHERE run_id = %s", (r["run_id"],)).fetchone()[0]
        assert p["angle_proposal"]["decision"] == "system_proposed" and "angle_review" not in p
        assert [a["outcome"] for a in p["angle_search"]] == ["advanced"] and p["run_chain"] == {"screen_run": "FIXTURE"}
        assert {f["origin"] for f in p["facts"]} == {"angle_proposal"} and p["acceptance_blockers"] == []
        assert "system-proposed" in p["selection_basis"]["angle"] and len(p["presentation"]["visuals"]) == 5
        pub = export.public_package(p, p["presentation"], None, None, draft=True)
        assert pub["how_chosen"]["mode"] == "automated" and pub["draft"] and pub["how_chosen"]["search"][0]["outcome"] == "advanced"
        try:
            export.write(pub, export.SITE)
            raise AssertionError("an unreviewed draft must not reach the deployable site")
        except export.ExportError:
            pass
        # a saved package whose cited fact failed verification cannot be accepted (stored before this rule existed)
        pid = p["package_id"]
        conn.execute("UPDATE story_packages SET package = jsonb_set(package, '{facts,0,verification,verdict}', "
                     "'\"contradicted\"') WHERE package_id = %s", (pid,))
        stored = conn.execute("SELECT package FROM story_packages WHERE package_id = %s", (pid,)).fetchone()[0]
        try:
            store.record_review(conn, pid, "accepted", "FIXTURE reviewer", ["FIXTURE"], store.package_sha(stored))
            raise AssertionError("a package citing a failed fact was accepted")
        except store.ReviewError as e:
            assert "did not pass verification" in str(e), e
        # wrong-type slide body from compose and every revision: bounded revisions, then malformed_output, run closed
        calls.clear()
        r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with(
            [_fix_proposal()], draft_fix=lambda d: d["slides"][1].update(body=42)))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "malformed_output" and run_row(r["run_id"]) == ("malformed_output", True), r
        assert calls[:2] == ["angle_proposal", "compose"] and all(c.startswith("revise") for c in calls[2:]), calls
        st, slides = conn.execute("SELECT status, package->'slides' FROM story_packages WHERE run_id = %s",
                                  (r["run_id"],)).fetchone()
        assert st == "malformed_output" and slides is None, (st, slides)
        # malformed verifier response: draft kept unapproved, package saved as malformed_output, run closed
        calls.clear()
        r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with([_fix_proposal()], verifier={"facts": 42}))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "malformed_output" and run_row(r["run_id"]) == ("malformed_output", True), r
        assert calls == ["angle_proposal", "compose", "verify"], calls

        # batch: Karim's draft fails -> the next run skips him and tries a different player (Omar)
        def proposers(conn_):
            return [x[0] for x in conn_.execute("SELECT a.name FROM angle_proposals p JOIN athletes a ON a.id = "
                                                "p.athlete_id WHERE p.run_id = ANY(%s) AND p.outcome <> 'skipped_call_reserve' ORDER BY p.id", (runs,)).fetchall()]
        insufficient = {"verdict": "insufficient", "reason": "FIXTURE routine season summary"}
        calls.clear()
        b = stages.batch_run(good_screen["run_id"], schema, chat_with([_fix_proposal(), insufficient], mode="unsupported"),
                             previews=False)
        runs = [x.split(":")[0] for x in b["counts"]["runs"]]
        made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
        assert b["counts"]["runs"][0].endswith("insufficient_evidence") and len(runs) == 3, b
        assert proposers(conn) == ["Karim Benali", "Omar Zidane"], proposers(conn)  # never Karim twice
        assert b["outcome"] == "no_candidates" and b["counts"]["ready"] == [] and b["counts"]["players_called"] == 2, b
        assert b["counts"]["groq_calls"] == len(calls) <= stages.BATCH["calls"], (b, calls)
        # batch: Karim's draft is ready; Omar has no angle; nothing left -> one ready draft
        b = stages.batch_run(good_screen["run_id"], schema, chat_with([_fix_proposal(), insufficient]), previews=False)
        runs = [x.split(":")[0] for x in b["counts"]["runs"]]
        made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
        assert b["outcome"] == "no_candidates" and len(b["counts"]["ready"]) == 1 and proposers(conn) == [
            "Karim Benali", "Omar Zidane"], (b, proposers(conn))
        st = conn.execute("SELECT review_state FROM story_packages WHERE package_id = %s", (b["counts"]["ready"][0],)).fetchone()
        assert st == ("awaiting_review",) and "Karim Benali" in open(b["report"], encoding="utf-8").read()
        # batch call cap: 6 calls -> Karim's ready draft uses 3, too few remain for another attempt (1 + 4 reserved)
        b = stages.batch_run(good_screen["run_id"], schema, chat_with([_fix_proposal()]), previews=False, calls=6)
        runs = [x.split(":")[0] for x in b["counts"]["runs"]]
        made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
        assert b["outcome"] == "call_limit" and b["counts"]["groq_calls"] == 3 and len(b["counts"]["ready"]) == 1, b
        assert proposers(conn) == ["Karim Benali"], proposers(conn)
        # dropped Neon connection after generation: model output saved locally, then written on a fresh connection
        real_connect, conns, down = store.connect, [], []
        def spy(*a, **k):
            if down:
                raise psycopg.OperationalError("FIXTURE: Neon unreachable")
            conns.append(real_connect(*a, **k))
            return conns[-1]
        def drop(step, unreachable=False):
            if step == "verify":   # the drafting calls are done; the server ends the idle connection
                conns[-1].close()
                down.extend([True] if unreachable else [])
        store.connect = spy
        try:
            calls.clear()
            r = stages.auto_run(good_screen["run_id"], schema, chat_factory=chat_with([_fix_proposal()], on_step=drop))
            made.append(RUNS_DIR / r["run_id"])
            local = json.loads((RUNS_DIR / r["run_id"] / "draft_result.json").read_text(encoding="utf-8"))
            assert r["outcome"] == "awaiting_review" and run_row(r["run_id"]) == ("awaiting_review", True), r
            assert local["status"] == "awaiting_review" and local["package"]["claim_verification"]["rounds"]
            assert [c["step"] for c in local["call_log"]] == ["angle_proposal", "compose", "verify"], local["call_log"]
            assert conn.execute("SELECT count(*) FROM story_packages WHERE run_id = %s", (r["run_id"],)).fetchone()[0] == 1
            # Neon still unreachable: nothing lost, the batch report names the failed step and the saved files
            calls.clear()
            b = stages.batch_run(good_screen["run_id"], schema, chat_with(
                [_fix_proposal()], on_step=lambda st: drop(st, unreachable=True)), previews=False)
        finally:
            store.connect = real_connect
        rid = b["counts"]["runs"][0].split(":")[0]
        made += [RUNS_DIR / rid, RUNS_DIR / b["run_id"]]
        assert b["outcome"] == "db_write_failed" and len(b["counts"]["runs"]) == 1, b
        report = open(b["report"], encoding="utf-8").read()
        assert "Failed step: reconnect to Neon after the model calls" in report and "draft_result.json" in report, report
        local = json.loads((RUNS_DIR / rid / "draft_result.json").read_text(encoding="utf-8"))
        assert local["package"]["slides"] and len(local["call_log"]) == 3
        assert (RUNS_DIR / rid / "db_write_failed.md").exists()

        # AR-03 shared pacing: the batch's child runs go through the real Groq client (FIXTURE transport, virtual
        # clock) with one pacer. 3 requests/minute: Karim's run makes 3 calls, so Omar's run waits 60s first.
        from arabsports import compose
        real_groq, real_models, old_key = compose.groq_chat, compose.list_models, os.environ.get("GROQ_API_KEY")
        pacers = []

        def fixture_groq(key, model, L, log, transport=None, pacer=None):
            pacers.append(pacer)
            inner, cur = chat_with([proposals.pop(0)])([]), {}
            def transport(url, data, headers):
                body = inner(cur["step"], cur["msgs"])
                return 200, {}, json.dumps({"model": "FIXTURE", "choices": [{"message": {"content": json.dumps(body)}}]}).encode()
            real = real_groq(key, model, L, log, transport, pacer)
            def chat(step, msgs):
                cur.update(step=step, msgs=msgs)
                return real(step, msgs)
            return chat

        vc = VClock()
        rpm3 = {**settings()["limits"], "groq_rpm": 3, "groq_tpm": 10 ** 6}  # only the request limit can cause a wait
        compose.groq_chat, compose.list_models = fixture_groq, lambda key: [settings()["groq_model"]]
        os.environ["GROQ_API_KEY"] = "FIXTURE-not-a-key"  # never sent: the transport is a FIXTURE
        try:
            proposals = [_fix_proposal(), insufficient]  # one per child run: Karim, then Omar
            b = stages.batch_run(good_screen["run_id"], schema, None, previews=False,
                                 pacer=compose.Pacer(rpm3, clock=vc.clock, sleep=vc.sleep))
            runs = [x.split(":")[0] for x in b["counts"]["runs"]]
            made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
            assert len(pacers) == 2 and pacers[0] is pacers[1] and vc.sleeps == [60.0], (pacers, vc.sleeps)
            assert len(b["counts"]["ready"]) == 1 and b["counts"]["groq_calls"] == 4, b
            report = open(b["report"], encoding="utf-8").read()
            assert "awaiting_review**, 3 call(s)" in report and "no_supported_angle**, 1 call(s)" in report, report
            # control: without the shared pacer each child run starts with an empty window and nothing waits
            pacers.clear(); vc.sleeps.clear(); proposals = [_fix_proposal(), insufficient]
            real_pacer = compose.Pacer
            compose.Pacer = lambda limits, **k: real_pacer(rpm3, clock=vc.clock, sleep=vc.sleep)
            try:
                r2 = stages.auto_run(good_screen["run_id"], schema)
                r3 = stages.auto_run(good_screen["run_id"], schema, exclude=[r2["counts"]["tried_athlete_ids"][0]])
            finally:
                compose.Pacer = real_pacer
            made += [RUNS_DIR / r2["run_id"], RUNS_DIR / r3["run_id"]]
            assert (r2["outcome"], r3["outcome"]) == ("awaiting_review", "no_supported_angle"), (r2, r3)
            assert pacers == [None, None] and vc.sleeps == [], (pacers, vc.sleeps)
        finally:
            compose.groq_chat, compose.list_models = real_groq, real_models
            if old_key is None:
                os.environ.pop("GROQ_API_KEY", None)
            else:
                os.environ["GROQ_API_KEY"] = old_key

        # AR-03 provider failure during verification: the draft is kept and the batch stops with the provider's
        # error; Omar is not tried. (An unsupported story moving on to the next player is the first batch check.)
        def fail_verify(step):
            if step == "verify":
                raise compose.ProviderError("provider_failed", "HTTP 503: FIXTURE upstream unavailable")
        calls.clear()
        b = stages.batch_run(good_screen["run_id"], schema, chat_with([_fix_proposal(), insufficient], on_step=fail_verify),
                             previews=False)
        runs = [x.split(":")[0] for x in b["counts"]["runs"]]
        made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
        assert b["outcome"] == "provider_failed" and len(runs) == 1 and calls == ["angle_proposal", "compose"], (b, calls)
        assert b["counts"]["provider_error"] == "provider_failed: HTTP 503: FIXTURE upstream unavailable", b
        assert "**Provider error:** provider_failed: HTTP 503: FIXTURE upstream unavailable" in open(b["report"], encoding="utf-8").read()
        st, kept = conn.execute("SELECT status, package->'draft_not_ready' FROM story_packages WHERE run_id = %s",
                                (runs[0],)).fetchone()
        assert st == "verification_budget_exhausted" and len(kept["slides"]) == 5, (st, kept)
        local = json.loads((RUNS_DIR / runs[0] / "draft_result.json").read_text(encoding="utf-8"))
        assert local["package"]["draft_not_ready"]["slides"] and local["counts"]["provider_error"].startswith("provider_failed")

        # AR-02: Hani has no usable passage (skipped, no player slot), three rejected angles, then Karim (4th
        # player) still has room to revise a copied phrase and verify. Standalone auto keeps its old shared budget.
        scores.update({"Hani Saber": 1.0, "Sami Haddad": 0.9, "Yassin Amrani": 0.8, "Nabil Fares": 0.7})
        replay("a2", raw_input(entities=[("QAH", "Hani Saber", "Q2736", "Q79"), ("QAS", "Sami Haddad", "Q2736", "Q79"),
                                         ("QAY", "Yassin Amrani", "Q2736", "Q1028"), ("QAN", "Nabil Fares", "Q2736", "Q1028")],
                               stories=[(A(3), "FIXTURE brief", "Egyptian forward Hani Saber played football."),
                                        (A(4), "FIXTURE Sami", "Egyptian forward Sami Haddad played football in a final "
                                         "after an injury and returned to captain his side in the cup."),
                                        (A(5), "FIXTURE Yassin", "Moroccan forward Yassin Amrani played football in a final "
                                         "after an injury and returned to captain his side in the cup."),
                                        (A(6), "FIXTURE Nabil", "Moroccan forward Nabil Fares played football in a final "
                                         "after an injury and returned to captain his side in the cup.")]),
               schema, made)
        for aid, in conn.execute("SELECT id FROM athletes WHERE source_id IN ('QAH', 'QAS', 'QAY', 'QAN')").fetchall():
            store.set_enrichment(conn, aid, "wikidata_roles", {"occupations": ["association football player"],
                                                               "national_teams": [], "fixture": True})
        wide = stages.screen_run(schema)
        made.append(RUNS_DIR / wide["run_id"])
        order = [c["name"] for c in sorted(store.latest_selection(conn, wide["run_id"])["candidates"],
                                           key=lambda c: c.get("rank") or 99)]
        assert order[:6] == ["Hani Saber", "Sami Haddad", "Yassin Amrani", "Nabil Fares", "Karim Benali", "Omar Zidane"], order
        copied = [False]
        def copy_once(d):  # the first draft reuses an 8-word run from the source; the revision does not
            if not copied[0]:
                copied[0] = True
                d["slides"][1]["body"] = "Benali grew up in Fes and started playing football at a local academy."
        rejected = lambda: [wrong, insufficient, _fix_proposal(quote_ok=False)]
        calls.clear()
        b = stages.batch_run(wide["run_id"], schema, chat_with(rejected() + [_fix_proposal(), insufficient],
                                                               draft_fix=copy_once), previews=False)
        runs = [x.split(":")[0] for x in b["counts"]["runs"]]
        made += [RUNS_DIR / x for x in runs] + [RUNS_DIR / b["run_id"]]
        assert calls == ["angle_proposal"] * 4 + ["compose", "revise_1", "verify", "angle_proposal"], calls
        assert b["outcome"] == "player_limit" and b["counts"]["players_called"] == 5 and len(b["counts"]["ready"]) == 1, b
        assert b["counts"]["groq_calls"] == 8 and [r.split(": ")[1] for r in b["counts"]["runs"]] == [
            "no_supported_angle", "no_supported_angle", "no_supported_angle", "awaiting_review", "no_supported_angle"], b
        rows = conn.execute("SELECT a.name, p.outcome FROM angle_proposals p JOIN athletes a ON a.id = p.athlete_id "
                            "WHERE p.run_id = ANY(%s) ORDER BY p.id", (runs,)).fetchall()
        assert rows[:2] == [("Hani Saber", "no_usable_passages"), ("Sami Haddad", "malformed_output")], rows
        p = conn.execute("SELECT package FROM story_packages WHERE package_id = %s", (b["counts"]["ready"][0],)).fetchone()[0]
        assert p["athlete"]["name"] == "Karim Benali" and p["generation"]["calls"][-1]["step"] == "verify", p["generation"]
        # standalone auto (unchanged): one shared 8-call run leaves Karim 4 calls, so the copied phrase cannot be revised
        copied[0] = False; calls.clear()
        r = stages.auto_run(wide["run_id"], schema, chat_factory=chat_with(rejected() + [_fix_proposal()], draft_fix=copy_once))
        made.append(RUNS_DIR / r["run_id"])
        assert r["outcome"] == "insufficient_evidence" and calls == ["angle_proposal"] * 4 + ["compose"], (r, calls)
        assert "revision skipped" in (RUNS_DIR / r["run_id"] / "draft_result.json").read_text(encoding="utf-8")
        print("ok  batch budget: a player with no usable passage is skipped without a slot; after three rejected angles "
              "the 4th player's child run still revises a copied phrase and verifies (8 batch calls, 5 players); "
              "standalone auto keeps one shared 8-call run (revision skipped)")
        print("ok  batch pacing and provider errors: child runs share one Groq pacer through the real client (the "
              "second run waits 60s at 3 requests/minute; per-run pacers would not) with per-run call logs; a "
              "provider failure during verification keeps the draft and stops the batch with the provider's error")
        print("ok  db handoff: a connection dropped after the model calls -> draft, verification and call log saved "
              "locally first, package saved on a fresh connection; Neon unreachable -> db_write_failed, batch report "
              "names the failed step and the saved files")
        print("ok  batch: a failed draft moves on to a different player (never the same one twice in a batch); a "
              "ready draft is kept awaiting review; stops at no candidates or the batch call cap")
        print("ok  auto: a failed current screen or an empty current shortlist -> zero model calls, never an older "
              "shortlist; wrong-type proposal -> malformed_output, next player tried; with 5 calls only one angle attempt "
              "fits beside the 4 reserved; with 8 a validated proposal is drafted, verified and saved awaiting review; "
              "a failed cited fact blocks acceptance; a wrong-type slide body -> malformed_output, run closed")
    finally:
        if old_cap is None:
            os.environ.pop("GROQ_MAX_CALLS", None)
        else:
            os.environ["GROQ_MAX_CALLS"] = old_cap
        screen.screen_packet = real_screen
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def check_showcase():
    """T05/T06 contract without a database: public whitelist refusals, fixture isolation, deployable site contents."""
    import copy
    from arabsports import export, photos
    assert photos.licence_decision("CC BY 4.0")[0] == "shortlist" and photos.licence_decision("CC BY-SA 4.0")[0] == "review"
    assert photos.licence_decision("CC BY-NC-SA 2.0")[0] == "reject" and photos.licence_decision(None)[0] == "reject"
    pkg = json.loads((export.FIXTURE / "layout_fixture.json").read_text(encoding="utf-8"))
    pres = json.loads((export.FIXTURE / "layout_presentation.json").read_text(encoding="utf-8"))
    pub = export.public_package(pkg, pres, None, None, fixture=True)
    assert pub["fixture"] and pub["review"]["state"].startswith("layout fixture")

    def refused(p=pkg, pr=pres, fixture=True, why=""):
        try:
            export.public_package(p, pr, None, None, fixture=fixture)
        except export.ExportError as e:
            assert why in str(e), e
            return
        raise AssertionError(f"not refused: {why}")
    bad = copy.deepcopy(pres); bad["visuals"][0]["text"] = "99 years"
    refused(pr=bad, why="'99'")
    real = copy.deepcopy(pkg)  # as a real saved package: every fact carries its verification verdict
    for f in real["facts"]:
        f["verification"] = {"verdict": "supported"}
    refused(p=real, fixture=False, why="photo")
    no_photo = copy.deepcopy(pres)
    for v in no_photo["visuals"]:
        v.pop("photo", None)
    assert export.public_package(real, no_photo, None, None, draft=True)["facts"]
    # failed evidence is never public, also for a package saved before this rule (verdict stored, but not enforced)
    for verdict in ("contradicted", "insufficient", None):
        p = copy.deepcopy(real)
        p["facts"][1]["verification"] = {"verdict": verdict} if verdict else {}
        refused(p=p, pr=no_photo, fixture=False, why="F2 did not pass verification")
    p = copy.deepcopy(pkg); p["slides"] = p["slides"][:4]
    refused(p=p, why="exactly 5")
    p = copy.deepcopy(pkg); p["facts"][0]["source"]["url"] = "javascript:alert(1)"
    refused(p=p, why="https")
    p = copy.deepcopy(pkg); p["rights_handling"] = "see postgresql://user@host/db"
    refused(p=p, why="secret")
    try:
        export.write(pub, export.SITE)
        raise AssertionError("fixture written to the deployable site")
    except export.ExportError as e:
        assert "fixture" in str(e)
    # deployable site: app files + exported data only, no fixture entries, no HTML injection, same-origin reads only
    assert {x.name for x in export.SITE.iterdir()} == {"index.html", "style.css", "app.js", "data"}
    for entry in json.loads((export.SITE / "data" / "index.json").read_text(encoding="utf-8"))["packages"]:
        assert json.loads((export.SITE / entry["path"]).read_text(encoding="utf-8"))["fixture"] is False, entry
    js, page = (export.SITE / "app.js").read_text(encoding="utf-8"), (export.SITE / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "connect-src 'self'" in page and "script-src 'self'" in page
    print("ok  showcase: licence policy, whitelist refusals (graphic not in copy, photo without review, slide count, "
          "non-https link, secret-like text, cited fact contradicted/insufficient/unverified), fixture kept out of the "
          "deployable site, site holds no fixture data")


if __name__ == "__main__":
    check_angles()
    check_showcase()
    check_auto_db()
    check_wikipedia_parse()
    check_biography_db()
    check_matching()
    check_fingerprint()
    check_urls()
    check_wire_parse()
    check_windows()
    check_pacing()
    check_claim_verification()
    check_failed_facts_and_types()
    check_repair_and_wording()
    check_correction_pass()
    check_bounded_failure()
    check_missing_db()
    check_pipeline_db()
    check_followup_db()
    check_t03_t04_db()
    check_discovery_db()
    check_revalidation_db()
    print("all checks passed")
