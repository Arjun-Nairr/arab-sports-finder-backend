# Arab football carousel: backend

This is the backend for a prototype that finds footballers who represent Arab nations, picks a sourced story about one of them, and writes a five-slide English carousel draft. Every draft waits for editorial review. It runs locally in Python with a Neon PostgreSQL database, a local Laya screening model and Groq.

This repository is a **backend-only snapshot** of the working project, taken on 2026-11-09 and including uncommitted changes. The static website (`showcase/`), private previews, generated PDFs, run logs, model weights and credentials are not included. See [What is not included](#what-is-not-included).

## How it works

1. **Discover.** Wikidata supplies athlete profiles. English Wikipedia biographies and available Ajel and GDELT news supply background evidence. Python links evidence to athletes and stores it in Neon.
2. **Shortlist.** Code checks sport and national representation. Local Laya ranks eligible players with promising passages. Batch mode can also try other eligible players.
3. **Choose a story.** For one player at a time, Groq proposes the stakes, turning point and significance from numbered source excerpts. Code attaches the exact excerpt text and checks every fact against its stored passage.
4. **Write and verify.** Groq writes five slides from the checked facts. Code checks structure, word count and copied wording. Code guards and a Groq verifier then check each claim against its cited passages, and the stricter verdict wins. A failed claim can be repaired and checked again.
5. **Review.** A passing draft is saved as `awaiting_review`. Only an independent review of that exact package version can accept it.

In batch mode, each player gets a separate run with up to 8 Groq calls. The batch tries up to 5 players, makes at most 24 calls and stops at 3 ready drafts. All runs share one Groq rate-limit tracker. A provider failure stops the batch and keeps any draft.

## Setup (Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env    # set DATABASE_URL (Neon) and GROQ_API_KEY; AJEL_WIRE_API_KEY is optional
```

Download the pinned Laya checkpoint once (about 840 MB, saved in the ignored `models/` folder):

```powershell
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download as s; s('convaiinnovations/laya', revision='7b928d828b7b0e022f929d9bd2e44165aa270148', allow_patterns=['config.json','model.safetensors','rl_agent_config.json','encoder/*','tokenizer/*','*.py'], local_dir='models/laya-7b928d82')"
```

`truststore` makes TLS verification use the Windows certificate store. Verification is never disabled. Never commit `.env`.

## Run

```powershell
.venv\Scripts\python.exe -m arabsports batch     # discovery -> biographies -> Laya screen -> up to 3 drafts (5 players, 24 Groq calls)
.venv\Scripts\python.exe -m arabsports auto      # the same chain, one run, one draft at most
.venv\Scripts\python.exe -m arabsports review PACKAGE_ID accepted|rejected SHA256 --reviewer NAME --reason TEXT
.venv\Scripts\python.exe checks.py               # regression checks (fixtures; DB checks use a temporary Neon schema)
```

Each run writes its report to `runs/<RUN_ID>/`, which is ignored. The full command list is in [`arabsports/__main__.py`](arabsports/__main__.py). Detailed design notes are in [docs/backend-reference.md](docs/backend-reference.md), [docs/demo-spec.md](docs/demo-spec.md) and [docs/review-tickets.md](docs/review-tickets.md). Those documents were written for the original layout, where this code lived in `demo/` beside `demo/showcase/`.

## Latest run result (2026-11-09)

The live batch `batch-20261109T063505Z-bf8d` produced the first fully automated draft awaiting review. It stopped at the 5-player limit after 13 of 24 Groq calls.

| Player | Outcome |
|---|---|
| Mohamed Salah, Pione Sisto, Akram Afif | No story: the model judged their excerpts routine (1 call each). |
| **Younis Mahmoud** | **Awaiting review** (`pkg-20261109T062539Z-7eed`). Story: Iraq's first AFC Asian Cup in 2007, his winning goal in the final, and his top-scorer and MVP awards. Verification flagged 3 claims, one repair fixed them, and re-verification passed every claim. |
| Hatem Trabelsi | Story found, but the repaired draft copied an 8-word phrase from the source, so the code check stopped it. |

The private preview rendered all five slides with facts and source links. Still open: independent acceptance, a decision on Wikipedia's CC BY-SA 4.0 terms for adapted slides, near-duplicate slides 1 and 5, and text-only graphics on slides 2–5.

## What is not included

| Left out | Effect in this snapshot |
|---|---|
| `showcase/` (static site, preview copies, layout fixture, PDF helper) | `export`, `preview`, `pdf` and `fixture-preview` fail because they copy or read `showcase/site` or `showcase/fixture`. `batch` still runs, but its preview step reports an error instead of building `showcase/review/<id>/`. `check_showcase` fails because it reads `showcase/fixture/layout_fixture.json` and `showcase/site`. |
| `models/` (Laya weights) | Download them with the setup step above. Until then, `screen`, `auto`, `batch`, `check_windows` and `check_wikipedia_parse` fail because they need the Laya tokenizer. |
| `.env`, `runs/`, `cache/`, previews, PDFs, run logs, database exports, integration ledger | Local only. Without `DATABASE_URL`, the DB checks print `SKIP`. |

In this snapshot, with no `.env` and no `models/`, these checks pass offline: `check_angles`, `check_matching`, `check_fingerprint`, `check_urls`, `check_wire_parse`, `check_pacing`, `check_claim_verification`, `check_failed_facts_and_types`, `check_repair_and_wording`, `check_correction_pass` and `check_missing_db`. In the full project with Neon, the whole suite passed (59 checks). `check_t03_t04_db` writes its export test to a temporary folder, so it probably doesn't need `showcase/`, but it hasn't been run in this snapshot.

## Licences and source use

The test fixtures include text from the English Wikipedia biographies of [Mohamed Salah](https://en.wikipedia.org/w/index.php?oldid=1384220918), [Younis Mahmoud](https://en.wikipedia.org/w/index.php?oldid=1382744198) and [Hatem Trabelsi](https://en.wikipedia.org/w/index.php?oldid=1381395079), attributed to Wikipedia contributors under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The fixtures pair those excerpts with test data and earlier model output. Each source keeps its own terms. The pipeline records them, and drafts list unresolved source-use questions as acceptance blockers.
