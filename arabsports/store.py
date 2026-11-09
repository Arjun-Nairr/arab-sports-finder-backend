"""Neon/PostgreSQL storage: schema, upserts and evidence versioning. Uniqueness lives in constraints."""
import hashlib
import json
import pathlib
import re

import psycopg
from psycopg.types.json import Jsonb

from .matching import content_hash, evidence_fingerprint, match_fingerprint, norm, search_text

SCHEMA = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
RANK = {"title_only": 1, "summary": 2, "full_text": 3}


def connect(url, schema=None):
    # autocommit: each `with conn.transaction()` block is a real commit (not a savepoint inside an
    # implicit transaction opened by an earlier read).
    conn = psycopg.connect(url, connect_timeout=10, autocommit=True)
    if schema:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
    conn.execute(SCHEMA)
    return conn


def start_run(conn, run_id, mode, config, replay_of=None):
    with conn.transaction():
        conn.execute("INSERT INTO runs (id, mode, replay_of, config) VALUES (%s, %s, %s, %s)",
                     (run_id, mode, replay_of, Jsonb(config)))


def finish_run(conn, run_id, integrations, counts, outcome, notes):
    with conn.transaction():
        conn.execute("UPDATE runs SET finished_at = now(), integrations = %s, counts = %s, outcome = %s, notes = %s "
                     "WHERE id = %s", (Jsonb(integrations), Jsonb(counts), outcome, Jsonb(notes), run_id))


_REPLACED = ("name", "aliases", "sports", "representation", "citizenship", "eligibility", "resolution", "gaps")


def upsert_athletes(conn, leads, run_id):
    """Returns [(athlete_id, 'new'|'existing', lead)]. Source identity is the key; names never merge rows.
    Structured refreshes replace source-owned fields and merge `external` (other providers' keys survive).
    Provisional news-only rows are never overwritten."""
    out = []
    with conn.transaction():
        for a in leads:
            vals = [Jsonb(a[c]) if isinstance(a[c], (list, dict)) else a[c] for c in _REPLACED]
            # A changed match_fp (names or nation/sport context) restarts the stored-article recheck.
            update = ("".join(f"{c} = EXCLUDED.{c}, " for c in _REPLACED) +
                      "norm_name = EXCLUDED.norm_name, match_fp = EXCLUDED.match_fp, "
                      "recheck_cursor = CASE WHEN athletes.match_fp IS DISTINCT FROM EXCLUDED.match_fp "
                      "THEN NULL ELSE athletes.recheck_cursor END, "
                      "external = athletes.external || EXCLUDED.external, "
                      if a["source"] != "news_mention" else "")
            row = conn.execute(
                f"INSERT INTO athletes (source, source_id, norm_name, match_fp, {', '.join(_REPLACED)}, external, "
                f"first_run, last_run) VALUES (%s, %s, %s, %s, {', '.join(['%s'] * len(_REPLACED))}, %s, %s, %s) "
                f"ON CONFLICT (source, source_id) DO UPDATE SET {update}last_run = EXCLUDED.last_run, updated_at = now() "
                f"RETURNING id, (xmax = 0)",
                (a["source"], a["source_id"], norm(a["name"]), match_fingerprint(a), *vals,
                 Jsonb(a["external"]), run_id, run_id)).fetchone()
            out.append((row[0], "new" if row[1] else "existing", a))
    return out


def load_cursors(conn):
    return {k: {"next_offset": o, "exhausted": e} for k, o, e in
            conn.execute("SELECT query_key, next_offset, exhausted FROM discovery_cursors")}


def save_cursors(conn, cursors, run_id):
    """Called only after the athletes on those pages were persisted."""
    with conn.transaction():
        for k, c in cursors.items():
            conn.execute("INSERT INTO discovery_cursors (query_key, next_offset, exhausted, last_run) VALUES (%s,%s,%s,%s) "
                         "ON CONFLICT (query_key) DO UPDATE SET next_offset = EXCLUDED.next_offset, "
                         "exhausted = EXCLUDED.exhausted, last_run = EXCLUDED.last_run, updated_at = now()",
                         (k, c["next_offset"], c["exhausted"], run_id))


def known_source_ids(conn, source):
    return {r[0] for r in conn.execute("SELECT source_id FROM athletes WHERE source = %s", (source,))}


def stale_profiles(conn, source, limit):
    """Least recently refreshed structured profiles, so evidence updates continue after pagination moves on."""
    return [r[0] for r in conn.execute("SELECT source_id FROM athletes WHERE source = %s ORDER BY updated_at, id LIMIT %s",
                                       (source, limit))]


def _observation(a, run_id):
    """What one acquisition route reported, independent of whether the text changed."""
    return {"provider": a["provider"], "provider_id": a["provider_id"], "acquisition": a["acquisition"],
            "content_kind": a["content_kind"], "title": a["title"], "seen_at": a["seen_at"],
            "access_note": a["access_note"], "usage": a.get("usage"), "source_status": a.get("source_status"),
            "attribution": (a.get("extra") or {}).get("attribution"),
            "revision": ((a.get("extra") or {}).get("revision") or {}).get("id"), "run": run_id}


def usage_terms(observations):
    """Most restrictive explicit usage flags across acquisition routes. '*_required' flags combine with OR,
    '*_allowed' (and other) flags with AND. A flag no route stated is absent = unknown, never permission."""
    flags, sources = {}, []
    for channel, o in sorted(observations.items()):
        usage = o.get("usage") or {}
        if usage:
            sources.append(channel)
        for k, v in usage.items():
            if isinstance(v, bool):
                flags[k] = (flags.get(k, False) or v) if k.endswith("_required") else (flags.get(k, True) and v)
    attribution = [o["attribution"] for _, o in sorted(observations.items()) if o.get("attribution")]
    return {"flags": flags, "flag_sources": sources, "attribution": attribution,
            "note": "Explicit provider flags only. Missing flags are unknown. Permission to retrieve or "
                    "republish does not imply permission to modify or rewrite text."}


def upsert_articles(conn, leads, run_id):
    """Returns [(article_id, status, lead)] keyed on canonical URL, status one of
    new | existing | changed (same-provider revision or richer content) | poorer_observation | withdrawn.
    Every import records extra.observations[channel] (usage, attribution, status, acquisition) even when the
    text is unchanged, and recomputes usage_terms. A poorer or other-provider observation never replaces
    retained content. Only the owning provider can revise or withdraw content."""
    out = []
    with conn.transaction():
        for a in leads:
            kind = a["content_kind"]
            channel = a.get("channel") or a["provider"]
            h = content_hash(a["content"] or a["title"] or a["url"]) if kind != "withdrawn" else content_hash("withdrawn " + a["url"])
            cur = conn.execute("SELECT id, content_hash, content_kind, provider, provider_id, acquisition, access_note, extra "
                               "FROM articles WHERE canonical_url = %s FOR UPDATE", (a["canonical_url"],)).fetchone()
            if not cur:
                if kind == "withdrawn":
                    continue  # nothing retained to withdraw
                obs = {channel: _observation(a, run_id)}
                aid = conn.execute(
                    "INSERT INTO articles (provider, provider_id, canonical_url, url, publisher, title, language, "
                    "published_at, updated_at_source, seen_at, content, content_kind, content_hash, acquisition, "
                    "access_note, extra, usage_terms, search_text, first_run, last_run) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (a["provider"], a["provider_id"], a["canonical_url"], a["url"], a["publisher"], a["title"],
                     a["language"], a["published_at"], a["updated_at_source"], a["seen_at"], a["content"], kind, h,
                     a["acquisition"], a["access_note"], Jsonb({**a["extra"], "observations": obs}),
                     Jsonb(usage_terms(obs)), search_text(a["title"], a["content"]), run_id, run_id)).fetchone()[0]
                out.append((aid, "new", a))
                continue
            aid, old_hash, old_kind, old_provider = cur[:4]
            obs = {**(cur[7].get("observations") or {}), channel: _observation(a, run_id)}
            conn.execute("UPDATE articles SET extra = jsonb_set(extra, '{observations}', %s), usage_terms = %s, "
                         "last_run = %s WHERE id = %s", (Jsonb(obs), Jsonb(usage_terms(obs)), run_id, aid))
            same_provider = a["provider"] == old_provider
            if old_hash == h:
                status = "existing"
            elif kind == "withdrawn" and same_provider:
                status = "withdrawn"
            elif old_kind == "withdrawn" or kind == "withdrawn":
                status = "poorer_observation"  # withdrawn stays withdrawn; others cannot withdraw
            elif RANK[kind] > RANK[old_kind] or (RANK[kind] == RANK[old_kind] and same_provider):
                status = "changed"
            else:
                status = "poorer_observation"
            if status in ("changed", "withdrawn"):
                prev = {"provider": old_provider, "provider_id": cur[4], "acquisition": cur[5], "content_kind": old_kind,
                        "access_note": cur[6], "replaced_by_run": run_id}
                content = None if status == "withdrawn" else a["content"]
                conn.execute(
                    "UPDATE articles SET provider = %s, provider_id = %s, content = %s, content_kind = %s, content_hash = %s, "
                    "title = coalesce(%s, title), updated_at_source = coalesce(%s, updated_at_source), acquisition = %s, "
                    "access_note = %s, search_text = %s, last_run = %s, retrieved_at = now(), "
                    "extra = jsonb_set(extra || %s, '{previous}', %s) WHERE id = %s",
                    (a["provider"], a["provider_id"], content, kind, h, a["title"], a["updated_at_source"],
                     a["acquisition"], a["access_note"], search_text(a["title"], content), run_id,
                     # the revision's own metadata (e.g. biography sections/revision) replaces the old; observations stay
                     Jsonb({k: v for k, v in a["extra"].items() if k != "observations"} if status == "changed" else {}),
                     Jsonb(prev), aid))
            elif status == "existing" and same_provider:
                conn.execute("UPDATE articles SET retrieved_at = now() WHERE id = %s", (aid,))
            out.append((aid, status, a))
    return out


def load_articles(conn, ids):
    """Retained (authoritative) article content, used for resolution instead of an incoming observation."""
    cols = ("id", "title", "content", "canonical_url", "extra", "content_kind")
    return {r[0]: dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM articles WHERE id = ANY(%s)", (list(ids),))}


def backfill_search_text(conn):
    rows = conn.execute("SELECT id, title, content FROM articles WHERE search_text IS NULL LIMIT 500").fetchall()
    with conn.transaction():
        for aid, title, content in rows:
            conn.execute("UPDATE articles SET search_text = %s WHERE id = %s", (search_text(title, content), aid))
    return len(rows)


def load_identities(conn):
    """Structured identities only: the targets a text mention may resolve to."""
    cols = ("id", "name", "norm_name", "aliases", "sports", "representation", "source", "source_id", "occupations")
    return [dict(zip(cols, r)) for r in conn.execute(
        "SELECT id, name, norm_name, aliases, sports, representation, source, source_id, "
        "coalesce(external->'wikidata_roles'->'occupations', '[]'::jsonb) FROM athletes WHERE resolution = 'source_identity'")]


def articles_with_links(conn, exclude_ids, limit):
    """Stored articles holding active links, for bounded revalidation under the current matching rules."""
    cols = ("id", "title", "content", "canonical_url", "extra", "content_kind")
    return [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join('ar.' + c for c in cols)} FROM articles ar WHERE ar.content_kind <> 'withdrawn' "
        "AND ar.provider <> 'wikipedia' AND EXISTS (SELECT 1 FROM mentions m WHERE m.article_id = ar.id AND m.status = 'linked') "
        "AND NOT (ar.id = ANY(%s)) ORDER BY ar.id LIMIT %s", (list(exclude_ids), limit))]


def athletes_not_searched(conn, key, limit):
    """Structured athletes not yet searched on a provider, in discovery (id) order: a rotation, not a pick."""
    return conn.execute("SELECT id, source_id, name FROM athletes WHERE resolution = 'source_identity' "
                        "AND NOT (external ? %s) ORDER BY id LIMIT %s", (key, limit)).fetchall()


_PENDING = "resolution = 'source_identity' AND match_checked_fp IS DISTINCT FROM match_fp"


def identities_to_recheck(conn, limit):
    """Structured athletes whose names or matching context changed since their last finished recheck,
    with the saved cursor (last stored article id already processed for the current match_fp)."""
    rows = conn.execute(f"SELECT id, name, aliases, recheck_cursor FROM athletes WHERE {_PENDING} "
                        f"ORDER BY id LIMIT %s", (limit,)).fetchall()
    pending = conn.execute(f"SELECT count(*) FROM athletes WHERE {_PENDING}").fetchone()[0]
    return rows, pending


def stored_articles_matching(conn, names, after_id, limit):
    patterns = [f"% {n} %" for n in names]
    cols = ("id", "title", "content", "canonical_url", "extra")
    return [dict(zip(cols, r)) for r in conn.execute(
        # Biographies are linked by verified sitelink only; name matching never attaches them to other athletes.
        f"SELECT {', '.join(cols)} FROM articles WHERE content_kind <> 'withdrawn' AND provider <> 'wikipedia' "
        f"AND search_text LIKE ANY(%s) "
        f"AND id > %s ORDER BY id LIMIT %s", (patterns, after_id or 0, limit))]


def save_recheck_progress(conn, completed, cursors):
    """completed: finished for their current match_fp. cursors: {athlete_id: last processed article id}."""
    with conn.transaction():
        conn.execute("UPDATE athletes SET match_checked_fp = match_fp, recheck_cursor = NULL WHERE id = ANY(%s)",
                     (list(completed),))
        for aid, cursor in cursors.items():
            conn.execute("UPDATE athletes SET recheck_cursor = %s WHERE id = %s", (cursor, aid))


ACTIVE = ("linked", "provisional", "no_context", "ambiguous", "role_conflict")


def supersede_stale(conn, article_id, mentions, run_id, reason):
    """After an authoritative revision or withdrawal, deactivate stored mentions the retained text no longer
    supports (name gone, or no longer linked to the same athlete). The old state moves to evidence.history;
    the row stays for provenance. Returns athlete ids whose evidence must be refreshed."""
    new = {norm(m["surface"]): m for m in mentions}
    affected = set()
    with conn.transaction():
        for n, status, athlete, ev in conn.execute(
                "SELECT norm, status, athlete_id, evidence FROM mentions WHERE article_id = %s AND status = ANY(%s) "
                "FOR UPDATE", (article_id, list(ACTIVE))).fetchall():
            m = new.get(n)
            if m and not (status == "linked" and (m["status"], m["athlete_id"]) != ("linked", athlete)):
                continue
            hist = {"superseded_in_run": run_id, "reason": reason, "status": status, "athlete_id": athlete, "evidence": ev}
            conn.execute("UPDATE mentions SET status = 'superseded', athlete_id = NULL, "
                         "evidence = jsonb_build_object('history', coalesce(evidence->'history', '[]'::jsonb) || %s) "
                         "WHERE article_id = %s AND norm = %s", (Jsonb([hist]), article_id, n))
            if athlete:
                affected.add(athlete)
    return affected


def upsert_mentions(conn, article_id, mentions, run_id):
    """Insert new mentions. An existing unresolved mention (no_context / ambiguous / provisional) is only
    upgraded to 'linked'. Upgrading a provisional mention is the explicit reconciliation path: the
    provisional athlete row is kept for provenance, marked 'reconciled' and pointed at the structured id.
    Returns (new_count, upgraded_count, [(provisional_id, structured_id)])."""
    new = upgraded = 0
    reconciled = []
    with conn.transaction():
        for m in mentions:
            n = norm(m["surface"])
            old = conn.execute("SELECT status, athlete_id, evidence FROM mentions WHERE article_id = %s AND norm = %s "
                               "FOR UPDATE", (article_id, n)).fetchone()
            if not old:
                conn.execute(
                    "INSERT INTO mentions (article_id, surface, norm, status, athlete_id, candidates, evidence, method, first_run) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (article_id, m["surface"], n, m["status"], m["athlete_id"], Jsonb(m["candidates"]),
                     Jsonb(m["evidence"]), m["method"], run_id))
                new += 1
                continue
            status, old_athlete, old_ev = old
            if status == "superseded":
                # The name is back in authoritative text: the current resolution becomes active again.
                if m["athlete_id"] and conn.execute("SELECT 1 FROM mentions WHERE article_id = %s AND athlete_id = %s",
                                                    (article_id, m["athlete_id"])).fetchone():
                    continue
                conn.execute("UPDATE mentions SET status = %s, athlete_id = %s, candidates = %s, method = %s, "
                             "evidence = %s WHERE article_id = %s AND norm = %s",
                             (m["status"], m["athlete_id"], Jsonb(m["candidates"]), m["method"],
                              Jsonb({**m["evidence"], "history": old_ev.get("history", [])}), article_id, n))
                upgraded += m["status"] == "linked"
                continue
            if status == "provisional" and m["status"] in ("ambiguous", "no_context"):
                # Not enough to reconcile: keep the provisional person, record the structured candidates seen.
                conn.execute("UPDATE mentions SET candidates = %s, evidence = evidence || %s "
                             "WHERE article_id = %s AND norm = %s",
                             (Jsonb(m["candidates"]), Jsonb({"structured_match": m["status"],
                                                             "structured_candidates": m["candidates"]}), article_id, n))
                continue
            if m["status"] != "linked" or status not in ("no_context", "ambiguous", "provisional"):
                continue
            if conn.execute("SELECT 1 FROM mentions WHERE article_id = %s AND athlete_id = %s",
                            (article_id, m["athlete_id"])).fetchone():
                continue  # already linked in this article under another name form
            ev = {**m["evidence"], "previous_status": status, "previous_evidence": old_ev, "upgraded_in_run": run_id}
            conn.execute("UPDATE mentions SET status = 'linked', athlete_id = %s, candidates = %s, evidence = %s "
                         "WHERE article_id = %s AND norm = %s",
                         (m["athlete_id"], Jsonb(m["candidates"]), Jsonb(ev), article_id, n))
            upgraded += 1
            if status == "provisional" and old_athlete:
                conn.execute("UPDATE athletes SET resolution = 'reconciled', reconciled_to = %s, "
                             "gaps = gaps || %s, updated_at = now() WHERE id = %s AND resolution = 'provisional'",
                             (m["athlete_id"], Jsonb([f"reconciled to athlete {m['athlete_id']} via single-candidate "
                                                      f"name match with article context"]), old_athlete))
                reconciled.append((old_athlete, m["athlete_id"]))
    return new, upgraded, reconciled


def set_enrichment(conn, athlete_id, key, data):
    with conn.transaction():
        conn.execute("UPDATE athletes SET external = jsonb_set(external, %s, %s), updated_at = now() WHERE id = %s",
                     ([key], Jsonb(data), athlete_id))


# ---------- T03 screening ----------

_ATH_COLS = ("id", "source", "source_id", "name", "aliases", "sports", "representation", "eligibility",
             "resolution", "gaps", "external", "evidence_version", "evidence_fingerprint")


def article_rows(conn, ids):
    cols = ("id", "canonical_url", "publisher", "title", "content", "content_kind", "usage_terms", "published_at")
    return [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM articles WHERE id = ANY(%s) AND content_kind IN ('full_text', 'summary') "
        f"ORDER BY id", (list(ids),))]


# Ordering only (nothing is excluded, eligibility is still decided in screen.eligibility): athletes whose structured
# record says association football and a listed nation come before the rest, so out-of-scope athletes do not take
# the bounded biography and screening slots first.
IN_SCOPE_FIRST = ("(sports @> '[{\"qid\": \"Q2736\"}]'::jsonb) DESC, "
                  "(representation @> '[{\"listed\": true}]'::jsonb) DESC")


def screening_candidates(conn, limit):
    """Athletes with current evidence: active linked/provisional mentions to non-withdrawn articles with text.
    Superseded mentions and withdrawn articles are excluded. Bounded; in-scope athletes first, then athlete id."""
    rows = conn.execute(
        f"SELECT {', '.join('a.' + c for c in _ATH_COLS)} FROM athletes a WHERE a.resolution IN ('source_identity', 'provisional') "
        "AND EXISTS (SELECT 1 FROM mentions m JOIN articles ar ON ar.id = m.article_id WHERE m.athlete_id = a.id "
        "AND m.status IN ('linked', 'provisional') AND ar.content_kind IN ('full_text', 'summary')) "
        f"ORDER BY {IN_SCOPE_FIRST.replace('sports', 'a.sports').replace('representation', 'a.representation')}, a.id "
        "LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        a = dict(zip(_ATH_COLS, r))
        a["articles"] = [dict(zip(("id", "canonical_url", "publisher", "title", "content", "content_kind", "usage_terms",
                                   "published_at", "provider", "extra"), x)) for x in conn.execute(
            "SELECT ar.id, ar.canonical_url, ar.publisher, ar.title, ar.content, ar.content_kind, ar.usage_terms, "
            "ar.published_at, ar.provider, ar.extra "
            "FROM mentions m JOIN articles ar ON ar.id = m.article_id WHERE m.athlete_id = %s "
            "AND m.status IN ('linked', 'provisional') AND ar.content_kind IN ('full_text', 'summary') ORDER BY ar.id",
            (a["id"],))]
        out.append(a)
    without = conn.execute("SELECT count(*) FROM athletes WHERE resolution IN ('source_identity', 'provisional') "
                           "AND evidence_status = 'needs_evidence'").fetchone()[0]
    return out, without


def upsert_packet(conn, packet, run_id):
    with conn.transaction():
        row = conn.execute("INSERT INTO packets (athlete_id, packet_fp, evidence_version, content, created_run) "
                           "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (packet_fp) DO UPDATE SET packet_fp = EXCLUDED.packet_fp "
                           "RETURNING id, (xmax = 0)", (packet["athlete_id"], packet["packet_fp"],
                                                       packet["evidence_version"], Jsonb(packet), run_id)).fetchone()
    return row[0], row[1]


def cached_screening(conn, packet_id, config_fp):
    r = conn.execute("SELECT id, mapped, score, raw, warnings, created_run FROM screenings WHERE packet_id = %s "
                     "AND config_fp = %s", (packet_id, config_fp)).fetchone()
    return dict(zip(("id", "mapped", "score", "raw", "warnings", "created_run"), r)) if r else None


def insert_screening(conn, packet_id, config_fp, model, rubric_version, raw, mapped, score, warnings, run_id):
    with conn.transaction():
        return conn.execute("INSERT INTO screenings (packet_id, config_fp, model, rubric_version, raw, mapped, score, "
                            "warnings, created_run) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (packet_id, config_fp, Jsonb(model), rubric_version, Jsonb(raw), mapped, score,
                             Jsonb(warnings), run_id)).fetchone()[0]


def save_selection(conn, run_id, candidates, selected, reason):
    with conn.transaction():
        conn.execute("INSERT INTO selections (run_id, candidates, selected_athlete_id, selected_screening_id, reason) "
                     "VALUES (%s,%s,%s,%s,%s)", (run_id, Jsonb(candidates), selected and selected["athlete_id"],
                                                 selected and selected["screening_id"], reason))


def screening_candidates_for(conn, athlete_id):
    """Current active evidence for one athlete (same exclusions as screening), with usage terms and observations."""
    cols = ("id", "canonical_url", "publisher", "title", "content", "content_kind", "usage_terms", "extra", "provider",
            "content_hash")
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join('ar.' + c for c in cols)} FROM mentions m JOIN articles ar ON ar.id = m.article_id "
        "WHERE m.athlete_id = %s AND m.status IN ('linked', 'provisional') AND ar.content_kind IN ('full_text', 'summary') "
        "ORDER BY ar.id", (athlete_id,))]
    return rows, len(rows)


def package_sha(package):
    return hashlib.sha256(json.dumps(package, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save_package(conn, package, status):
    """Every attempt is stored. A draft that passed checks is 'awaiting_review'; nothing is saved as accepted.
    The sha is computed from the stored (read-back) jsonb so a later review can name the exact version."""
    review = "awaiting_review" if status == "awaiting_review" else "not_applicable"
    with conn.transaction():
        conn.execute("INSERT INTO story_packages (package_id, run_id, schema_version, athlete_id, screening_id, status, "
                     "package, review_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (package["package_id"], package["run_id"], package["schema_version"], package["athlete"]["id"],
                      package["screening"]["screening_id"], status, Jsonb(package), review))
        stored = conn.execute("SELECT package FROM story_packages WHERE package_id = %s", (package["package_id"],)).fetchone()[0]
        sha = package_sha(stored)
        conn.execute("UPDATE story_packages SET package_sha256 = %s WHERE package_id = %s", (sha, package["package_id"]))
    return sha


REVIEWABLE = {"accepted": ("awaiting_review",), "rejected": ("awaiting_review", "legacy_unreviewed")}
# The generator and automatic checks cannot review their own output.
SELF_REVIEWERS = re.compile(r"groq|gpt|llama|laya|auto|check|pipeline|generator|system", re.I)


class ReviewError(Exception):
    pass


def record_review(conn, package_id, decision, reviewer, reasons, package_sha256, resolves=()):
    """Independent review decision for one exact package version. Accepting needs a draft awaiting review; a legacy
    unreviewed package can only be rejected (it has no verification record). History stays in package_reviews."""
    if decision not in REVIEWABLE:
        raise ReviewError(f"decision must be one of {sorted(REVIEWABLE)}")
    if not reviewer or SELF_REVIEWERS.search(reviewer):
        raise ReviewError(f"reviewer '{reviewer}' is not an independent reviewer")
    if not reasons:
        raise ReviewError("a review needs at least one reason")
    with conn.transaction():
        row = conn.execute("SELECT review_state, package FROM story_packages WHERE package_id = %s FOR UPDATE",
                           (package_id,)).fetchone()
        if not row:
            raise ReviewError(f"no package {package_id}")
        state, pkg = row
        current = package_sha(pkg)
        if package_sha256 != current:
            raise ReviewError(f"reviewed version {package_sha256[:12]} is not the stored version {current[:12]}")
        if state not in REVIEWABLE[decision]:
            raise ReviewError(f"cannot record '{decision}' for a package in state '{state}'")
        open_ = [b["id"] for b in pkg.get("acceptance_blockers") or [] if b["id"] not in set(resolves)]
        if decision == "accepted" and open_:
            raise ReviewError(f"acceptance blocked until explicitly resolved: {', '.join(open_)}")
        from .compose import failed_cited_facts
        if decision == "accepted" and (bad := failed_cited_facts(pkg)):
            raise ReviewError(f"cannot accept: cited fact(s) {', '.join(bad)} did not pass verification")
        conn.execute("INSERT INTO package_reviews (package_id, package_sha256, from_state, decision, reviewer, reasons, "
                     "resolved_blockers) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (package_id, current, state, decision, reviewer, Jsonb(list(reasons)), Jsonb(list(resolves))))
        conn.execute("UPDATE story_packages SET review_state = %s, package_sha256 = %s WHERE package_id = %s",
                     (decision, current, package_id))


def exportable_package(conn, package_id):
    """The package only if an independent review accepted exactly this stored version; otherwise None."""
    row = conn.execute("SELECT p.package, p.package_sha256 FROM story_packages p WHERE p.package_id = %s "
                       "AND p.review_state = 'accepted' AND EXISTS (SELECT 1 FROM package_reviews r WHERE "
                       "r.package_id = p.package_id AND r.decision = 'accepted' AND r.package_sha256 = p.package_sha256)",
                       (package_id,)).fetchone()
    return row[0] if row and package_sha(row[0]) == row[1] else None


ANGLE_FIELDS = ("stakes", "turning_point", "significance")


def record_angle(conn, selection, decision, reviewer, angle, refs, run_id):
    """Independent angle review for the current selection. Each ref names a stored article (by URL) and a passage
    copied verbatim from it; the article's content_hash is frozen with the passage."""
    if decision not in ("approved", "rejected"):
        raise ReviewError("angle decision must be approved or rejected")
    if not reviewer or SELF_REVIEWERS.search(reviewer):
        raise ReviewError(f"reviewer '{reviewer}' is not an independent reviewer")
    if not selection or not selection["athlete_id"]:
        raise ReviewError("no current selection to review an angle for")
    missing = [k for k in ANGLE_FIELDS if not (angle.get(k) or "").strip()]
    if missing or not refs:
        raise ReviewError(f"angle needs {', '.join(ANGLE_FIELDS)} and at least one source reference; missing: "
                          f"{', '.join(missing) or 'refs'}")
    frozen = []
    for r in refs:
        row = conn.execute("SELECT id, publisher, content, content_hash, content_kind FROM articles WHERE canonical_url = %s",
                           (r["url"],)).fetchone()
        if not row or row[4] == "withdrawn":
            raise ReviewError(f"referenced source not stored (or withdrawn): {r['url']}")
        if r["passage"] not in (row[2] or ""):
            raise ReviewError(f"passage not found verbatim in {r['url']}: {r['passage'][:60]}")
        frozen.append({"article_id": row[0], "url": r["url"], "publisher": row[1], "content_hash": row[3],
                       "passage": r["passage"], "supports": r.get("supports", []),
                       **({"id": r["id"]} if r.get("id") else {})})
    check_reviewed_facts(angle.get("facts"), frozen)
    return conn.execute("INSERT INTO angle_reviews (athlete_id, screening_id, selection_run, decision, reviewer, angle, refs, "
                        "recorded_run) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (selection["athlete_id"], selection["screening_id"], selection["run_id"], decision, reviewer,
                         Jsonb(angle), Jsonb(frozen), run_id)).fetchone()[0]


def check_reviewed_facts(facts, refs):
    """Optional reviewed facts: [{id, statement, refs: [passage ids]}]. Each fact cites passages of one article, and
    every passage is cited by some fact, so drafting sees exactly the passages behind the reviewed facts."""
    if facts is None:
        return
    by_id = {r.get("id"): r for r in refs if r.get("id")}
    if not isinstance(facts, list) or not facts:
        raise ReviewError("reviewed facts must be a non-empty list")
    ids, cited = set(), set()
    for f in facts:
        fid = f.get("id")
        if not fid or fid in ids or not (f.get("statement") or "").strip() or not f.get("refs"):
            raise ReviewError(f"reviewed fact {fid!r} needs a unique id, a statement and passage refs")
        ids.add(fid)
        unknown = [x for x in f["refs"] if x not in by_id]
        if unknown:
            raise ReviewError(f"reviewed fact {fid} cites unknown passage(s): {', '.join(map(str, unknown))}")
        if len({by_id[x]["article_id"] for x in f["refs"]}) > 1:
            raise ReviewError(f"reviewed fact {fid} cites passages from more than one source")
        cited |= set(f["refs"])
    if uncited := [r.get("id") or r["passage"][:40] for r in refs if r.get("id") not in cited]:
        raise ReviewError(f"passage(s) not cited by any reviewed fact: {', '.join(uncited)}")


def latest_angle(conn):
    r = conn.execute("SELECT id, athlete_id, screening_id, selection_run, decision, reviewer, angle, refs, created_at "
                     "FROM angle_reviews ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
    return dict(zip(("id", "athlete_id", "screening_id", "selection_run", "decision", "reviewer", "angle", "refs",
                     "created_at"), r)) if r else None


def latest_selection(conn, run_id=None):
    """The newest saved selection, or (run_id given) only the selection that screening run saved."""
    cols = "SELECT run_id, selected_athlete_id, selected_screening_id, reason, candidates FROM selections "
    r = (conn.execute(cols + "WHERE run_id = %s", (run_id,)) if run_id
         else conn.execute(cols + "ORDER BY created_at DESC LIMIT 1")).fetchone()
    return dict(zip(("run_id", "athlete_id", "screening_id", "reason", "candidates"), r)) if r else None


def refresh_evidence(conn, athlete_ids):
    """Recompute fingerprints; bump evidence_version only when meaningful evidence changed.
    Withdrawn articles do not count as evidence."""
    changes = []
    with conn.transaction():
        rows = conn.execute(
            "SELECT a.id, a.name, a.representation, a.evidence_fingerprint, a.evidence_version, a.resolution, "
            "coalesce(array_agg(DISTINCT ar.content_hash) FILTER (WHERE ar.id IS NOT NULL), '{}'), "
            "coalesce(bool_or(ar.content_kind = 'full_text' AND ar.provider <> 'wikipedia'), false), "
            "coalesce(bool_or(ar.provider <> 'wikipedia'), false) "
            "FROM athletes a LEFT JOIN mentions m ON m.athlete_id = a.id AND m.status IN ('linked', 'provisional') "
            "LEFT JOIN articles ar ON ar.id = m.article_id AND ar.content_kind <> 'withdrawn' "
            "WHERE a.id = ANY(%s) GROUP BY a.id", (list(athlete_ids),)).fetchall()
        for aid, name, rep, old_fp, ver, resolution, hashes, full, news in rows:
            fp = evidence_fingerprint(rep, hashes)
            status = ("reconciled" if resolution == "reconciled" else "has_full_text_news" if full
                      else "title_or_summary_only" if news else "biography_only" if hashes else "needs_evidence")
            if fp == old_fp:
                conn.execute("UPDATE athletes SET evidence_status = %s WHERE id = %s AND evidence_status <> %s",
                             (status, aid, status))
                continue
            conn.execute("UPDATE athletes SET evidence_fingerprint = %s, evidence_version = %s, evidence_status = %s, "
                         "updated_at = now() WHERE id = %s", (fp, ver + 1, status, aid))
            changes.append({"athlete_id": aid, "name": name, "from_version": ver, "to_version": ver + 1,
                            "articles": len(hashes), "evidence_status": status})
    return changes


# ---------- Wikipedia biographies ----------

def bio_targets(conn, limit, refresh):
    """Unprocessed structured athletes, in-scope first (IN_SCOPE_FIRST), then discovery (id) order: athletes without
    an enwiki sitelink are returned separately (no request needed); `limit` with a sitelink are taken. Plus `refresh`
    already-imported biographies, least recently checked first. Returns (new, no_sitelink, refresh, pending_after)."""
    rows = conn.execute("SELECT id, source_id, name, external->>'enwiki' FROM athletes WHERE source = 'wikidata' "
                        f"AND NOT (external ? 'wikipedia_bio') ORDER BY {IN_SCOPE_FIRST}, id").fetchall()
    new, none = [], []
    for r in rows:
        if r[3] is None:
            none.append(r)
        elif len(new) < limit:
            new.append(r)
        else:
            break
    pending = sum(1 for r in rows if r[3]) - len(new)
    old = conn.execute("SELECT id, source_id, name, external->>'enwiki', external->'wikipedia_bio' FROM athletes "
                       "WHERE source = 'wikidata' AND external->'wikipedia_bio'->>'status' = 'imported' "
                       "ORDER BY external->'wikipedia_bio'->>'checked_at', id LIMIT %s", (refresh,)).fetchall()
    return new, none, old, pending
