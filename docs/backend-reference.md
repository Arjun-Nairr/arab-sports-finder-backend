# Arab sports demo — local discovery and news library (T01–T02)

Local Python discovers athletes who represent an Arab nation (Wikidata P1532, "country for sport"),
pulls bounded sports news (Ajel AI feed and, with a key, Ajel Wire name search with permitted full text;
GDELT metadata), links mentions
conservatively and stores everything in Neon. No athlete is named as input.

## Setup (Windows, as used)

```powershell
cd demo
py -3.12 -m venv .venv            # Python 3.12.5
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # then set DATABASE_URL (Neon); AJEL_WIRE_API_KEY optional
```

`truststore` makes TLS verification use the Windows certificate store (needed here because an
antivirus re-signs some HTTPS traffic). Verification is never disabled.

Local Laya (T03): the pinned English base checkpoint is downloaded once into `models/` (ignored, ~840 MB) and
loaded from there, because `laya.load` cannot pin a revision itself:

```powershell
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download as s; s('convaiinnovations/laya', revision='7b928d828b7b0e022f929d9bd2e44165aa270148', allow_patterns=['config.json','model.safetensors','rl_agent_config.json','encoder/*','tokenizer/*','*.py'], local_dir='models/laya-7b928d82')"
```

## Run

```powershell
.venv\Scripts\python.exe -m arabsports run              # ingestion only, live: Wikidata -> Neon -> Ajel/GDELT -> mentions
.venv\Scripts\python.exe -m arabsports replay <RUN_ID>  # ingestion only: re-ingest that run's saved raw input; labelled replay, no network
.venv\Scripts\python.exe -m arabsports biographies      # bounded Wikipedia biography import (verified sitelinks)
.venv\Scripts\python.exe -m arabsports screen           # T03 on the stored library: packets -> local Laya -> one selection or none
.venv\Scripts\python.exe -m arabsports generate         # T04 on the latest stored selection: Groq -> checked 5-slide package
.venv\Scripts\python.exe -m arabsports workflow         # screen, then generate if something was selected (no discovery)
.venv\Scripts\python.exe checks.py                      # focused checks (fixtures + temporary Neon schema)
```

`screen` and `generate` never re-fetch discovery; they use what is stored. Reports: `runs/<RUN_ID>/screening.md`
and `runs/<RUN_ID>/carousel.md`.

## Discovery pagination

Each nation query (`wikidata_search:P1532=<QID>`) keeps a cursor in `discovery_cursors`. A run reads pages from the
cursor (`sroffset`, the API's `continue` value) until it finds `WIKIDATA_PER_NATION` new ids, the query is exhausted,
or `WIKIDATA_MAX_PAGES` pages are read. The cursor is saved only after the athletes on those pages are stored; replays
never move it. Exhausted queries are re-probed cheaply each run, and `WIKIDATA_REFRESH` least-recently-updated
profiles are re-fetched every run, so pagination never stops evidence updates. Search offsets are not snapshot-stable:
repeats are expected and idempotent. `GDELT_ENABLED=0` skips GDELT while it is throttled.

Every run also re-resolves stored articles that hold links, under the current matching rules. A link no longer
supported (e.g. "sports analyst <name>" when the athlete's occupation does not confirm that role) is superseded with
history, and the athlete's evidence is refreshed.

## Wikipedia biographies (background evidence)

`python -m arabsports biographies` imports English Wikipedia biographies for discovered athletes. It handles up to
`WIKIPEDIA_BIOGRAPHIES` unprocessed athletes in discovery (id) order, plus a re-check of up to `WIKIPEDIA_REFRESH`
imported ones, within `WIKIPEDIA_MAX_REQUESTS` requests (`WIKIPEDIA_RETRIES` each; provider `Retry-After` honoured).
Report: `runs/<RUN_ID>/biographies.md`.
- **Identity.** The title comes only from the athlete's Wikidata `enwiki` sitelink, never from a name search. One
  Action API `query` (pageprops, revisions, siteinfo rightsinfo) resolves normalisation and redirects. The resulting
  page must carry `wikibase_item` = the athlete's QID. Outcomes:
  - `verified` (imported)
  - `missing`
  - `disambiguation`
  - `entity_mismatch`, for example a redirect to an event, a team or a section
  - `no_sitelink`

  Only `verified` pages are stored.
- **Content.** Core REST `revision/{id}/html` (Parsoid HTML pinned to the checked revision), cached in `cache/wikipedia/`
  by revision id. What is kept:
  - prose paragraphs, under their headings
  - citation markers, mapped to the page's references (text plus links, not retrieved)

  Infobox, tables, lists, images, navboxes and the References/Notes/External links sections are dropped. No images
  and no crawling.
- **Storage.**
  - **Row:** an `articles` row with provider `wikipedia`, acquisition `wikipedia_core_rest_revision_html`, keyed by
    page id (`?curid=`).
  - **No dates:** `published_at` and `updated_at_source` stay empty. A biography is not news.
  - **Provenance in `extra`:** revision id, timestamp and link; licence from siteinfo; attribution; redirect chain; the
    sections; the citations; the reduction rule.
  - **Usage:** flags are attribution, licence notice and share-alike required.
  - **Link:** a `linked` mention with method `wikidata_sitelink_verified`. Name matching and revalidation never touch
    biographies.
  - **Evidence status:** `biography_only` when an athlete has no news.
- **Repeats.**
  - Same revision: reused without a fetch.
  - New revision with the same normalized text: kept as is, evidence version unchanged.
  - Changed text: the article is updated (previous state kept), and the evidence version is bumped.
  - A failed request changes nothing; the athlete is retried next run.
  - A page that later goes missing keeps its stored biography, flagged in `latest_check`.
- **Licence.** CC BY-SA 4.0 (siteinfo `rightsinfo`; see Wikipedia:Reusing_Wikipedia_content). Reuse needs attribution
  with a link, a licence notice and share-alike for adapted text. Media and cited sources have separate rights. T04
  adds this to a package's rights handling when a biography is a source.
- **Screening.**
  - Biography windows follow the news windows in the packet, each headed `Section: <heading>`.
  - A page with more sections than `BIO_MAX_WINDOWS` (6) gets the first window of 6 evenly spaced sections, from first
    to last; otherwise windows are taken round-robin across sections.
  - Every window not screened is counted by section in `biography_reduction` and in the screening warnings.
- **Eligibility.**
  - A biography sentence naming the athlete with "<nation> national … team" or "represent… <nation>" is route
    `wikipedia_biography`. It keeps its section, paragraph, cited references and an uncertainty note.
  - `independent_of_wikimedia` lists only non-Wikimedia routes. Agreement between Wikipedia and Wikidata is not
    corroboration.
- **User-Agent.** `WIKIMEDIA_CONTACT` (optional) adds operator contact to the User-Agent, as Wikimedia's policy asks.
  Without it the tool identifies itself but gives no contact.

## Groq pacing

`compose.Pacer` budgets each request as estimated input plus the reserved `max_tokens`. Groq was observed to reserve
`max_tokens` against the per-minute limit. A request is rejected unsent if it alone exceeds `GROQ_TPM`. Before each call it waits on
the stricter of a local 60 s token/request window (`GROQ_TPM`, `GROQ_RPM`) and the `x-ratelimit-remaining-tokens` /
`x-ratelimit-reset-tokens` headers. A 429 waits for `retry-after` (5 s × 2^n fallback). Total waiting is capped
(`GROQ_MAX_WAIT_S`), every attempt counts toward `GROQ_MAX_CALLS`, and `x-ratelimit-remaining-requests` = 0 (per day)
stops the run. Sources that do not fit the input budget are omitted visibly, never cut mid-text.

## T03 screening (what is decided where)

- **Eligibility is rules, not Laya.** The athlete must be a structured identity, have a competitor occupation
  (Wikidata P106; referees and coaches are excluded, chess players count), and have a sourced P1532, a sourced
  national-team P54, or a stored or searched report saying they represent the nation. For promising athletes whose
  only gap is representation, a bounded Ajel Wire search (`<name> national team`) is tried.
- **Laya** (base English checkpoint, CPU) answers two `choice` questions per evidence window (rubric `r1` in
  `screen.py`). Windows hold the paragraphs that mention the athlete, are kept to 280 tokens or fewer, and are checked
  against Laya's reported tokens (per-question 512 limit), so truncation is visible. The raw answers are stored, and the
  mapping and ordering rules are written into every report.
- Screenings are cached on packet fingerprint (evidence + windows) × config fingerprint (revision, runtime,
  rubric, limits, truncation check).

## T04 composition

`compose.py` runs a fixed sequence: extract facts → angle against existing coverage → compose → code checks →
at most 2 revisions. The checks:
- every fact's evidence span must appear verbatim in its cited source
- slide numbers and proper names must come from the cited facts
- no quotation marks, and no 8-word run copied from a source
- 5 slides in order, about 100–150 words, English only

After the structural checks, **claim verification** runs:
- Every fact is judged against the passage around its evidence span. Every title, heading, body and explanation
  sentence is judged against the passages of the facts it cites.
- **Code guards:** negation mismatches, and debut/first, order, cause and feeling words that are absent from the source
  words. A Groq verifier call also judges every item.
- The stricter verdict wins. It is a fallible aid, not certification.
- The sequence stays within `GROQ_MAX_CALLS` / `GROQ_MAX_WAIT_S`. Verification is reserved, and a repair is made only if
  a re-verification call remains.

Outcomes:
- `awaiting_review`: passed everything, still not a successful story
- `insufficient_evidence`
- `verification_budget_exhausted`
- `stale_selection`: the selected screening no longer matches current evidence or config; no Groq call
- `malformed_output`, `quota_or_rate_limited`, `provider_failed`, `blocked`

Every attempt is stored with its verification record and the exact source text, hashes and revisions used.

**Review.** Only an independent reviewer makes a package successful:

```powershell
.venv\Scripts\python.exe -m arabsports review PACKAGE_ID accepted|rejected SHA256 --reviewer NAME --reason "..."
```

The SHA256 is printed in `carousel.md` and stored as `story_packages.package_sha256`.
- **Refused:** generator or check names as reviewer, a wrong sha, and accepting anything not `awaiting_review`.
- **Export:** `store.exportable_package` returns only accepted packages, with at most one per run.
- **Legacy `completed` rows:** shown as `legacy_unreviewed`. They can be rejected, never accepted.

Source-use questions are recorded as `acceptance_blockers`, for example `rights_ajel_modification` (Wire
`modification_allowed=false` vs the Ajel policy) and `rights_wikipedia_share_alike`. Acceptance is refused until the
reviewer resolves each one explicitly with `--resolves BLOCKER_ID`. Attribution or paraphrase does not resolve them.
Each new package links its `previous_attempt` for the same selection; earlier packages stay unchanged.
Defaults: `GROQ_MAX_CALLS=8`, `GROQ_MAX_WAIT_S=480`. These are ceilings. A structural revision is made only if verify +
repair + re-verify still fit.

Each run writes `runs/<RUN_ID>/report.md` (readable report, read back from Neon) and
`runs/<RUN_ID>/raw.json` (private raw source payloads for replay; gitignored, may contain Ajel text).

## Showcase (T05–T07): photo lookup, export, preview, deploy

The showcase is plain static files in `showcase/site/` (`index.html`, `style.css`, `app.js`, `data/`). No build step and no
dependencies. The page reads only `data/index.json` and one package file, and its CSP allows same-origin reads only. It
never contacts Neon, Groq or Laya, and it has no generation controls.

```
python -m arabsports photos PACKAGE_ID showcase/stories/salah_photo_query.json   # T06: one bounded Commons lookup, recorded as a run
python -m arabsports review PACKAGE_ID accepted SHA --reviewer NAME --reason TEXT --resolves rights_fifa_terms   # independent reviewer only
python -m arabsports export PACKAGE_ID showcase/stories/salah_presentation.json  # refused unless that exact version was accepted
python -m arabsports fixture-preview   # labelled layout fixture -> showcase/preview/ (never deployed)
python -m arabsports auto              # one automated attempt: discovery -> biographies -> screen -> angle search -> draft
python -m arabsports preview PACKAGE_ID [FILE.json]  # draft-labelled review preview + draft PDF -> showcase/review/<id>
python -m arabsports pdf PACKAGE_ID    # five-page PDF of an exported (accepted) package -> showcase/exports/
```

- **Export** maps the package through a whitelist (`arabsports/export.py`):
  - Kept: copy, facts with source links, the selection explanation, screening, review, photo outcome, rights and caveats.
  - Left out: source passages, verification transcripts, diagnostics and local paths.
  - Refused: secret-like text, non-https links, a slide count other than 5, and any graphic word or number that isn't in the story copy or its cited facts.
- **Presentation files** (`showcase/stories/*_presentation.json`) hold the theme and one designed graphic per slide: `figure`, `pitch` or `score`.
- **Preview locally:** serve the folder as static files, for example `python -m http.server 8472 --directory showcase/site`, or use `showcase/preview` for the fixture.
- **Deploy:** publish `showcase/site/` as a static Vercel project on the free tier, with no build command and the output directory set to the folder root. Do this only after a real export.
- **Update:** export again, check locally, then redeploy the same folder.

### Photo screening and approval

The separate `photos` command uses [`photos.py`](arabsports/photos.py) to search Wikimedia Commons and read file metadata. It filters to image MIME types, checks for the athlete and event in the metadata, and requires a creator credit. Its licence rules use the stated short name. Public domain, CC0 and CC BY can be shortlisted. CC BY-SA and custom terms go to review. Missing terms, noncommercial, no-derivatives and all-rights-reserved files are rejected by this workflow. The lookup records the file page, declared licence and licence URL. It does not download the file, verify that the image loads or establish that the stated terms cover the intended use.

The live Salah event search returned eight PDFs and a public-domain map. The map showed neither Salah nor the match. No photo was shortlisted. The automated batch skips photo lookup, and [`public_package()`](arabsports/export.py) currently rejects photo fields on all real packages. The viewer's photo path has only been used with fixture data.

A future approval step should open the original file page, confirm the image and its context, check the exact terms for the intended use and any edits, and save the approved asset with its source, creator, licence and required credit. Use a local asset served with the showcase. The metadata screen can narrow the list for that review.

### Proposed image flow

Groq continues writing the five slides and includes a visual brief with each slide in the same response. Each brief requests an athlete photo, a contextual stock image, or a text-only slide. The last checked draft supplies the briefs used for sourcing.

Once the copy passes verification, a separate Python step routes athlete requests to Wikimedia Commons and contextual requests to Pexels. Reuse searches when multiple slides request the same portrait or scene. Save candidate metadata and the approval result with the package. Label stock photos as illustrative and identify athlete portraits as portraits when they are from a different event. The renderer reads saved copy and approved assets from the package so the web view and PDF use the same images. A slide can use a text-only layout when there is no approved asset.

### Temporary graphics and where to remove them

Retirement target: the Python rules that pick a score, minute, year, count or heading as the visual, plus the matching renderer branches and tests. Keep story fact verification, image screening and approval, and the shared web/PDF layout. Remove the temporary rules after the approved-image and text-only paths work in both formats.

| Layer | Current fallback code | What to revisit when replacing it |
| --- | --- | --- |
| Package generation | [`stages.py`](arabsports/stages.py), `_auto_fields()` sets the photo placeholder and builds `presentation` when the draft is ready for review. | Change this handoff to use the replacement visual source. Preserve the five slides and their cited facts. |
| Template selection | [`export.py`](arabsports/export.py), `auto_presentation()`, `_options()`, and the score, minute, count, and year patterns. They pull values from checked slide copy and fall back to the slide heading. The theme uses a flag-colour strip. | Retire the patterns and template picker once the replacement covers each slide. |
| Export and saved data | [`export.py`](arabsports/export.py), `KINDS`, `_check_graphic()`, and `public_package()`. The current contract requires five graphics and emits a `visual` for each slide. Preview and export use the same contract. | Update the contract, validation, and old-package handling together. Keep fact verification and the public export allowlist. |
| Rendering and tests | [`app.js`](showcase/site/app.js), `graphic()` and `panel()`, plus graphic styles in [`style.css`](showcase/site/style.css). Web and PDF share the renderer. [`checks.py`](checks.py), `check_angles()` and `check_showcase()`, exercise the templates and export guard. | Remove unused figure, score, and pitch styles and assertions after the new web and PDF paths pass. |

## Layout

- `arabsports/sources.py` — Wikidata, Ajel, GDELT, TheSportsDB adapters (fetch raw / parse leads)
- `arabsports/wikipedia.py` — English Wikipedia identity check, revision HTML fetch/cache, biography parser
- `arabsports/matching.py` — normalization, mention extraction, conservative resolution, evidence fingerprint
- `arabsports/store.py`, `schema.sql` — Neon tables `runs`, `athletes`, `articles`, `mentions`
- `arabsports/run.py` — coordination and report
- `arabsports/config.py` — limits, nation reference list (Arab League members)

## Rules implemented

- Eligibility: `supported` (referenced P1532 to a listed nation), `claimed` (unreferenced P1532),
  `unresolved` (no such claim). Citizenship is stored but never used.
- Identity: `(source, source_id)` is unique. Names never merge records. A text mention links only when
  exactly one **structured** athlete has that full name **and** the article mentions their nation or sport;
  otherwise it is kept as `ambiguous` / `no_context`. Names introduced by a role word ("striker X Y")
  that match no structured athlete become provisional news-only athletes, one per (article, name)
  (`source_id = <canonical_url>#<name>`), so same-name reports never merge.
- Reconciliation: when a stored provisional mention later meets exactly one structured athlete with
  article context, the mention is relinked, and the provisional row is kept with `resolution = reconciled`
  and `reconciled_to`. Ambiguous matches stay provisional, and the candidates are recorded on the mention.
- Stored-article recheck: structured athletes whose names or matching context (nation/sport terms) changed
  (`match_fp` ≠ `match_checked_fp`) are matched against stored articles, paging by article id from `recheck_cursor`
  (`RECHECK_IDENTITIES` / `RECHECK_ARTICLES` per run). An athlete is complete only when its matches are exhausted.
- Revisions: an authoritative revision or withdrawal re-resolves the retained text. Links it no longer supports
  become `superseded` (history kept), and every affected athlete's evidence is refreshed.
- Usage: each acquisition route's usage, attribution and status are kept in `extra.observations`, even for unchanged
  text. `usage_terms` holds the most restrictive explicit flags; a missing flag is unknown, not permission.
- Articles: unique canonical URL (fragment and tracking parameters dropped, semantic query kept). Only the
  owning provider can revise or withdraw content (Ajel markdown HTTP 410). A poorer or other-provider sighting
  (e.g. GDELT title-only) is kept under `extra.observations` and never replaces full text.
- Evidence version: fingerprint of normalized representation claims (value, rank, dates, reference contents)
  plus distinct non-withdrawn article contents. Reordering and syndicated copies don't bump it.
- TheSportsDB results are stored as unverified candidates only. Wikidata refreshes merge `external`.
- Limits come from `.env` (see `.env.example`); retries are finite and a long `Retry-After` stops the call.
