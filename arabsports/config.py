"""Local settings. Secrets come from demo/.env or the environment and are never recorded."""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"

# Represented-nation reference list (Arab League members), Wikidata QID -> name, demonyms.
# Eligibility is representation of one of these in sport (Wikidata P1532), not citizenship.
NATIONS = {
    "Q262": ("Algeria", ["algerian"]), "Q398": ("Bahrain", ["bahraini"]),
    "Q970": ("Comoros", ["comorian"]), "Q977": ("Djibouti", ["djiboutian"]),
    "Q79": ("Egypt", ["egyptian"]), "Q796": ("Iraq", ["iraqi"]),
    "Q810": ("Jordan", ["jordanian"]), "Q817": ("Kuwait", ["kuwaiti"]),
    "Q822": ("Lebanon", ["lebanese"]), "Q1016": ("Libya", ["libyan"]),
    "Q1025": ("Mauritania", ["mauritanian"]), "Q1028": ("Morocco", ["moroccan"]),
    "Q842": ("Oman", ["omani"]), "Q219060": ("Palestine", ["palestinian"]),
    "Q846": ("Qatar", ["qatari"]), "Q851": ("Saudi Arabia", ["saudi"]),
    "Q1045": ("Somalia", ["somali"]), "Q1049": ("Sudan", ["sudanese"]),
    "Q858": ("Syria", ["syrian"]), "Q948": ("Tunisia", ["tunisian"]),
    "Q878": ("United Arab Emirates", ["emirati", "uae"]), "Q805": ("Yemen", ["yemeni"]),
}


def _load_env_file(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def settings():
    _load_env_file()
    num = lambda k, d: int(os.environ.get(k) or d)
    return {
        "database_url": os.environ.get("DATABASE_URL") or None,
        "ajel_key": os.environ.get("AJEL_WIRE_API_KEY") or None,
        "groq_key": os.environ.get("GROQ_API_KEY") or None,
        # llama-3.3-70b-versatile is in Groq's docs but returned 404 model_not_found for this account (2026-11-06).
        "groq_model": os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b",
        "limits": {
            "wikidata_per_nation": num("WIKIDATA_PER_NATION", 2),
            "wikidata_max_nations": num("WIKIDATA_MAX_NATIONS", 22),
            "wikidata_max_pages": num("WIKIDATA_MAX_PAGES", 3),   # pages per nation query per run
            "wikidata_refresh": num("WIKIDATA_REFRESH", 10),      # existing profiles re-fetched per run
            "ajel_max_articles": num("AJEL_MAX_ARTICLES", 10),
            "ajel_name_searches": num("AJEL_NAME_SEARCHES", 10),
            "ajel_wire_articles": num("AJEL_WIRE_ARTICLES", 10),
            "gdelt_enabled": num("GDELT_ENABLED", 1),
            "gdelt_max_records": num("GDELT_MAX_RECORDS", 25),
            "gdelt_name_queries": num("GDELT_NAME_QUERIES", 3),
            "thesportsdb_lookups": num("THESPORTSDB_LOOKUPS", 3),
            "screen_candidates": num("SCREEN_CANDIDATES", 20),
            "groq_max_calls": num("GROQ_MAX_CALLS", 8),  # ceiling incl. verify + repair + re-verify
            "groq_max_tokens": num("GROQ_MAX_TOKENS", 2000),  # includes gpt-oss reasoning tokens; account TPM is 8000
            "groq_retries": num("GROQ_RETRIES", 1),
            "groq_tpm": num("GROQ_TPM", 8000),            # observed account allowance (x-ratelimit-limit-tokens)
            "groq_rpm": num("GROQ_RPM", 30),              # docs base table; RPD comes from response headers
            "groq_max_wait_s": num("GROQ_MAX_WAIT_S", 480),  # total pacing + backoff wait per generation run
            "max_revisions": num("MAX_REVISIONS", 2),
            "representation_searches": num("REPRESENTATION_SEARCHES", 3),
            "wikipedia_biographies": num("WIKIPEDIA_BIOGRAPHIES", 10),  # unprocessed athletes per run (discovery order)
            "wikipedia_refresh": num("WIKIPEDIA_REFRESH", 10),        # imported biographies re-checked per run
            "wikipedia_max_requests": num("WIKIPEDIA_MAX_REQUESTS", 12),  # identity + revision requests per run
            "wikipedia_retries": num("WIKIPEDIA_RETRIES", 1),
            "recheck_identities": num("RECHECK_IDENTITIES", 50),
            "recheck_articles": num("RECHECK_ARTICLES", 200),
            "http_timeout_s": num("HTTP_TIMEOUT_S", 30),
            "http_retries": num("HTTP_RETRIES", 2),
        },
    }


def public_config(s):
    """Effective configuration safe to store: limits plus which credentials are present."""
    return {
        "limits": s["limits"],
        "credentials_present": {"DATABASE_URL": bool(s["database_url"]), "AJEL_WIRE_API_KEY": bool(s["ajel_key"]),
                                "GROQ_API_KEY": bool(s.get("groq_key"))},
        "groq_model": s.get("groq_model"),
        "nations": sorted(NATIONS),
    }
