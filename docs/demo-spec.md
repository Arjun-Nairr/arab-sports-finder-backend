---
title: Arab sports carousel demo
status: ready-for-agent
label: ready-for-agent
scope: minimal functional demonstration
---

# Arab sports carousel demo: build specification

## 1. Purpose and authority

Build a minimal working demonstration that discovers a football player representing an Arab nation, selects a supported inspiring story about that player, and generates one short English carousel. A temporary website shows previously generated carousels and the evidence behind them to Emile. It does not run the generation process.

The end-to-end acceptance run is automated from a local start command through a saved five-slide carousel, including player discovery, story-angle selection, fact selection, writing and visual layout. The operator supplies configuration. Independent review may approve or reject the generated result for sharing. If a person supplies or rewrites the angle, evidence, copy or slide visuals, that run counts as an editorially assisted example. The current Salah carousel is such an example.

This specification synthesizes the latest demo interview. It takes precedence over older planning notes and PDFs where they conflict. In particular, the demo now includes carousel presentation, uses Neon rather than a local database, and uses Groq rather than Gemini or a mandatory Hermes harness. There is no delivery-duration commitment.

The purpose is to demonstrate a real workflow, discover which integrations work, and identify caveats and fallback needs. It is not a production service or a study of model performance. No implementation or successful integration is implied by the document's ready-for-agent status.

## 2. Settled requirements

- Run generation locally through a small Python workflow, use Groq for model calls, and store research in Neon PostgreSQL. Keep local Laya behind an optional switch, off by default for the first automated demonstration.
- Use free access only. Do not activate paid services, billing upgrades, paid batch APIs, or paid photo licences to complete the demo. Missing access or exhausted quotas must be reported.
- Discover the athlete through the algorithm. No manually selected athlete is the starting input for an accepted discovery demonstration. There are no user-facing sport, country, or athlete filters in this demo.
- Discover the story through the algorithm as well. The run must find and record a specific angle with evidence for the stakes, turning point and significance, or report that it found no qualifying story. An athlete ranking alone is an intermediate result.
- Attempt athlete-first and news-first discovery. Sources under investigation are Wikidata, Ajel Wire and GDELT, with TheSportsDB as optional enrichment.
- Produce at most one completed carousel per generation run. Inspect up to five eligible players with evidence by default, using at most one story-angle call per player. Try the next player when an angle lacks support. Pace requests within the account limits and record a no-story outcome when the shortlist or budget is exhausted.
- A carousel tells one specific inspiring story about a football player in five slides, approximately 100-150 words overall. The evidence must support a human stake, a response or turning point, and why it matters. Perseverance, contribution, community, identity, setbacks and achievement are possible angles. Scores, transfers and statistics can support the narrative. A routine match recap or news roundup fails selection and editorial acceptance.
- Keep all presentation text English: slides, interface, explanations, evidence summaries, status descriptions and photo captions. Arabic output and bilingual layout are deferred. English output does not establish a requirement to build comprehensive Arabic-source retrieval or translation support.
- Follow the supplied Barshim example's presentation direction: portrait slides, a strong opening, short text, prominent visual/statistical elements and photo or graphic areas. Adapt the structure to the selected narrative. Do not reuse its athlete claims as verified facts or require Barshim to be the selected athlete.
- Deliver a swipeable web preview and a separate five-page PDF for each accepted carousel. Both render the same saved slides. A designed graphic is acceptable when a suitable photo is unavailable.
- Deploy only the static showcase to Vercel. It displays saved generation results and proof of concept. It has no live backend controls or connection to the research database.
- Show facts used, supporting sources, the recorded story-selection result, and a brief model-generated explanation grounded in the selected evidence. When Laya is enabled, show its actual screening result separately. When disabled, record it as skipped.
- Use the coding assistant as a development reviewer of functional behaviour and evidence. Formal quality benchmarks, performance evaluation and classifier training are deferred.

## 3. Actors and user stories

### Local operator / developer

1. As a developer, I want to run discovery without naming an athlete, so that the demo proves the system can find its own subject.
2. As a developer, I want leads from athlete records and news, so that both discovery routes can be exercised.
3. As a developer, I want to retain incomplete athlete records and attach later evidence, so that sparse information does not permanently exclude a person.
4. As a developer, I want repeated imports to match existing records, so that the library does not grow through duplicate copies of the same source item.
5. As a developer, I want uncertain identity matches kept separate, so that two different athletes are not silently merged.
6. As a developer, I want screening results tied to the exact evidence assessed, so that I can inspect how the selected lead advanced.
7. As a developer, I want a bounded shortlist of eligible players researched per run and one carousel produced, so I can control cost while giving the system alternatives.
8. As a developer, I want source and tool outcomes reported honestly, so that I can distinguish useful integrations, empty results and failures.
9. As a developer, I want unsupported story attempts to stop with a reason, so that attractive writing does not hide missing evidence.
10. As a developer, I want completed results saved independently of the website, so that they remain reviewable without rerunning the backend.
11. As a developer, I want free-tier limits respected, so that the demo does not incur unapproved charges.
12. As a developer, I want a functional inspection record, so that I can demonstrate what actually worked without claiming measured model performance.

### Viewer / content head

13. As a viewer, I want to open a simple link and browse saved carousels, so that I can assess the idea without running software.
14. As a viewer, I want to swipe or click through five readable slides on a phone or desktop, so that I can experience the intended social format.
15. As a viewer, I want a complete short narrative, so that I understand why the athlete's story matters.
16. As a viewer, I want a separate evidence section, so that I can check the facts without cluttering the slides.
17. As a viewer, I want to see the recorded screening result and a short explanation of the angle, so that I can understand the selection.
18. As a viewer, I want a clear indication of missing integrations and unresolved facts, so that I can assess the demonstration's limits.
19. As a viewer, I want photo credit and licence information or an honest placeholder, so that an uncertain image is not presented as cleared.
20. As a viewer, I want clear empty, missing-photo and unavailable-source states, so that the showcase remains understandable when supporting material is incomplete.

## 4. Architecture and boundaries

### A. Local generation workflow

Python owns fetching, normalization, database access, evidence preparation, Laya execution, selection, controlled Groq calls, photo licence screening, and saved-package export. The operator starts it locally. It is not deployed as a Vercel function.

Keep source adapters, storage operations, model access and workflow coordination separate. Use ordinary functions and data contracts; do not introduce a plugin framework, message queue or general agent runtime. A later harness should be able to reuse these functions as tools rather than replace the database and source integrations.

### B. Persistent research library

Neon is the selected PostgreSQL service. It stores research and run history. The local process is its client. The public showcase never receives a database connection string or queries Neon.

Use ordinary indexed queries by identity, represented nation, source identifier, status and evidence version. Fetch candidate-specific evidence rather than sending the entire library into Laya or Groq. A vector database is not required.

### C. Saved publication package

The local process exports a versioned, validated package containing slides, the athlete profile, facts and sources, screening results, explanation, photo status, and an integration summary. This package is a snapshot of a completed run; it is not a live query of mutable database state.

Preserve full permitted research locally/in Neon as appropriate. The public export contains only the information intended for the demo: concise English evidence summaries, necessary source references, and permitted media. Exclude keys, connection strings, private source material and raw diagnostic dumps. Public source links need not be copied or cached as full article bodies.

### D. Static showcase

Use a minimal frontend capable of being built or served statically on Vercel. A particular JavaScript framework is not a product requirement; choose the smallest suitable implementation at scaffolding and record the choice. Do not add a server component, account system or dashboard framework merely to display saved packages.

Read only the exported files and approved local assets. An index lists available carousel packages. A successful new local run can be deliberately added to that index and deployed; it does not automatically publish itself.

## 5. Local workflow

### 5.1 Start and collect

Create a run record and record effective configuration: enabled sources, model/checkpoint identifiers, rubric/prompt versions, retrieval limits, retry limits and input/output caps. Limits must be finite and configurable; they are not throughput targets. Account quotas determine safe initial values during setup. No specific retrieval count has been agreed.

Request bounded lead sets through both discovery routes. Athlete-first starts with Wikidata representation/sport information and seeks relevant reports. News-first starts with sports reports and identifies athlete mentions. Optional TheSportsDB information enriches a profile but is never required for eligibility or selection.

Retain origin and publication/retrieval times. For each integration record `worked`, `empty`, `failed` or `not_attempted`, with a concise reason. `Worked` requires an actual successful operation, not merely configured credentials. Record optional or missing services without inventing results.

### 5.2 Resolve identities and eligibility

For this demo, eligible players compete in football and represent an Arab nation. Citizenship, surname, birthplace, publisher country or club affiliation alone are insufficient evidence of representation. Keep a configurable represented-nation reference list and evidence of the affiliation. Discovery must stay open across eligible nations and players.

Source IDs and existing verified aliases are primary matching signals. Name alone is insufficient for a cross-source merge; use available sport and representation/context evidence. Preserve ambiguous records or mentions with an unresolved status until supported. A news-only athlete may proceed once identity and eligibility have adequate evidence even without structured player data.

Use structured fields and text matching first. If news mention extraction requires a model, group independent article extraction work into bounded requests rather than create a research call for every athlete. Record this model use separately from deeper story research. Never silently insert manually selected athletes as algorithm output.

### 5.3 Upsert and prepare evidence

Upsert known athletes and articles using source identifiers and canonical links. Preserve links between athletes and articles. Do not overwrite a previously sourced fact with an unsupported assertion. Repeated articles and syndicated copies are not independent corroboration.

Prepare a bounded candidate packet from relevant retrieved passages and profile facts, preserving source references and uncertainty. Evidence sufficiency is distinct from editorial potential. A long statistics record is not automatically an inspiring story; short material is not automatically a bad one.

Retain athletes and leads without usable narrative evidence as `needs_evidence`. Later imports can attach reports and change that status. Re-screen when meaningful evidence or the screening configuration changes. Do not build an endless retry loop or fetch the entire library on each run.

### 5.4 Shortlist and select a story

Start with football eligibility, Arab national-team representation and usable evidence. The default path proceeds from those checks to angle discovery. If local Laya is enabled, record its checkpoint, runtime, input sizes and scores as optional ranking signals. Preserve relevant evidence and make sampling visible. Training and RLCD are excluded.

Use an explicit screening rubric with three domain outcomes: promising narrative lead, routine sporting information, insufficient information. Preserve the underlying typed answers/scores and the mapping into these outcomes. Laya supplies classification, not a written narrative or proof of originality.

Persist each result against the evidence fingerprint, checkpoint and rubric version. Retain candidates that do not advance. Use a documented, deterministic ordering rule over eligible promising candidates; apply the same rubric and comparable score fields, then evidence availability and a stable tie-break. Do not describe an uncalibrated score as the probability that an editor will approve a story. Exact thresholds are integration settings to inspect, not a deferred performance study.

Automatically examine relevant passages for up to five eligible players by default. Use at most one paced model call per player to propose a specific angle with cited evidence for the human stakes, turning point and significance. Move on when a player lacks a supported angle. Select at most one supported angle for drafting and record a no-selection outcome if the bounded search finds none. Retain the evidence and reason for each decision.

### 5.5 Research, explain and compose with Groq

Ordinary Python controls a small sequence of Groq calls and source fetches. Generation consumes the system's selected angle and supporting evidence directly. Retrieve and verify any additional material within the configured limits, record the facts the writer can use, inspect existing coverage of the angle, and identify unresolved consequential claims. The automated path proceeds to drafting when its evidence checks pass. Independent editorial approval happens on the resulting package before public export.

Separate the source-supported fact set from interpretation. Store claim-to-source references. An LLM may help extract and compare claims but cannot certify its own assertions. Do not invent quotes, feelings, scenes, causal explanations or claims of being first/unique. Describe distinctiveness only relative to coverage actually checked.

Generate one concise English carousel and a short editorial explanation of why the angle may be worthwhile. The explanation summarizes evidence and criteria; it is not hidden chain-of-thought, not Laya's explanation, and not proof that alternatives were exhaustively considered.

Check factual assertions in the slides and explanation against the evidence. Repair or remove unsupported assertions within a bounded revision pass; unresolved major claims cause an insufficient-evidence outcome. Stop after this candidate if the story fails rather than automatically selecting another or expanding discovery.

### 5.6 Compose five slides

Use the agreed narrative progression:

1. Hook: the specific surprising or meaningful idea.
2. Background: only what the reader needs to understand the situation.
3. Challenge or turning point.
4. Outcome or contribution, without forcing a triumphant ending.
5. Why it matters: a grounded closing thought.

Keep approximately 100-150 words across the carousel, with brief headings and short body copy. Achievements or statistics belong where they support the story. Detailed biography and source explanation remain outside the slides. If this structure cannot be filled truthfully, revise the angle or stop rather than pad it.

Automatically build each slide's presentation data from the generated copy and cited facts using the reusable portrait template. Pick supported figures, score graphics or typographic panels as appropriate. The run produces complete, renderable slides even when a photo is unavailable.

### 5.7 Source a photo or use a placeholder

Search Wikimedia Commons for the verified athlete and appropriate context, with Openverse an optional fallback. Validate that the photo actually depicts the athlete; a generic stock subject must not stand in as their portrait. Archival photos need accurate context, not a caption implying a different event.

Read licence metadata and original file-page information. Apply a conservative rule-based policy: shortlist documented CC0/public-domain and CC BY candidates; route ShareAlike or other more involved terms to review; do not automatically select unknown, contradictory, noncommercial, no-derivatives or all-rights-reserved assets for an edited social carousel. These are selection defaults, not universal legal judgments. Preserve actual licence versions and required notices.

Store creator, original source/file page, licence and URL, credit, checked-at evidence, context, and review status. A metadata match is `licence_screened`, not `legally_cleared`. Respect provider delivery terms, image rights, and attribution in the public result. A suitable image is optional: unavailable, uncertain or unusable results produce a designed placeholder and a plain English explanation. Photo failure never invalidates an otherwise supported story.

### 5.8 Save and export

Save the generated package to Neon and an operator-readable local artifact. Export a public snapshot only after structural/language/evidence checks and development inspection. A partial or failed run cannot masquerade as a completed carousel. Previously completed packages remain intact if a later run fails.

One accepted development demonstration must use actual discovery, Neon persistence, automatic angle selection and Groq generation. Record whether the optional Laya step ran. Failure of an external source can coexist with a supported output from another route, but the export must identify that limitation. Do not claim all integrations worked because one story was produced.

## 6. Minimum data model

These are logical records and relationships, not prescribed SQL or a requirement for a generic data platform. Small nested payloads can remain JSON in PostgreSQL where appropriate.

| Record | Required information and relationships |
|---|---|
| Athlete | Internal ID; names/aliases; source IDs; sport; represented nation with evidence; available background/achievements; resolution and evidence status. Unknown fields remain unknown. |
| Article | Internal ID; provider/source ID; canonical URL; publisher; title/language; publication/retrieval/update times; available evidence or permitted content; acquisition/reuse limitations; fetch status. |
| Athlete-article link | Athlete and article IDs; evidence of the association; avoid duplicate links. Preserve uncertainty rather than forcing a match. |
| Candidate | Athlete reference; linked evidence; evidence fingerprint; proposed angle if available; state and selection/rejection reason. Initial screening does not require a prewritten angle. |
| Screening | Candidate/evidence version; Laya checkpoint and rubric version; original answers/scores; mapped result; time and truncation/input warnings. |
| Run | ID; configuration; stage outcomes; per-integration outcomes; selected candidate; usage/limits where available; terminal outcome and concise failure/caveat notes. |
| Photo asset | Provider/file ID; subject/context; source/image URLs; creator; licence and conditions; attribution; checked evidence; shortlist/review/placeholder status. |
| Story package | Stable package ID and schema version; run/candidate references; profile; five slides; fact/source map; screening snapshot; brief explanation; photo/placeholder data; integration summary and development review notes. |

Enforce uniqueness for stable source identifiers, repeated article imports and repeated links. Cross-source identity matching remains a conservative algorithm, not a database guarantee. Keep one completed package at most for a run. Historical packages and citations must not silently change when profile records update.

## 7. Application contracts

Specify domain contracts rather than provider-specific SDK calls:

- **Discover:** bounded configuration in; normalized athlete/article leads plus integration outcomes out.
- **Persist/resolve:** leads in; existing/new/unresolved identities, article links and meaningful-change signals out.
- **Prepare evidence:** candidate reference and size limits in; source-linked packet, fingerprint and sufficiency warnings out.
- **Screen:** eligibility and evidence checks produce the bounded shortlist. If Laya is enabled, its rubric, typed results and mapped outcomes are recorded separately.
- **Select:** stored screened candidates and retrieved passages in; one system-selected story angle with evidence for stakes, turning point and significance, or a no-selection reason out.
- **Research/compose:** selected angle and evidence in; facts, citations, five slides, visual presentation data, explanation and supported/insufficient/failed status out.
- **Photo search/screen:** verified identity and story context in; candidate rights record or placeholder reason out.
- **Export:** inspected completed package in; public package file, approved assets and index entry out.

The local entry point is the highest backend verification seam: run the workflow and inspect its database changes, artifacts and outcome. The static package is the frontend contract. No public generation API, Neon API proxy, or secret-bearing browser call is required.

Required public package fields: package/schema IDs; English title; brief athlete summary; exactly five ordered slides; source-linked facts used; screening result; brief explanation; photo/placeholder and credit; concise integration caveats. Original provider IDs and raw diagnostics can stay private. Escape/sanitize external text and metadata instead of rendering arbitrary provider HTML.

## 8. Showcase behaviour

- Load a static index of saved carousel packages. Initially one is enough; later successful runs may be added without changing the rendering contract.
- Export a separate five-page portrait PDF per accepted carousel, using the same copy and visual layout as the site. Include the necessary source credits and links. PDF export runs locally from saved content.
- Present portrait slides using a consistent template inspired by the supplied example, with clear heading hierarchy, large relevant figures when justified, image/graphic space, and short body text. All public copy is English.
- Support touch swipe plus visible previous/next controls, slide count, and keyboard operation. Keep text readable without clipping on phones and desktops. No autoplay is needed.
- Add a separate, plainly labelled evidence section with the profile, slide-supported facts, source links, actual screening result and short explanation. Display known integration caveats without raw logs.
- Show credits with selected photos, and distinguish a licence-screened candidate from a legally cleared asset. Render placeholders cleanly rather than broken image boxes.
- If there are no completed packages, say so. Do not seed the production showcase with a fabricated successful run. Development fixtures, if used for layout work, must be clearly labelled and excluded from accepted real-output evidence.
- If a package or photo fails to load, preserve navigation and show a readable error/placeholder. Never trigger the backend as a fallback.
- Publish only an inspected static build. No browser access to Groq, Neon, local Laya or source-service secrets.

## 9. Failure and free-budget policy

Retries must be bounded; obey provider backoff instructions. Record source failure, no results, missing credentials, quota exhaustion, unsupported evidence, malformed generated output and unavailable photo distinctly. Do not loop until a story appears.

Optional-source or optional-screening failures can yield a completed story with caveats if the remaining evidence supports it. Neon persistence, automatic story selection and actual Groq generation are necessary to claim the intended end-to-end demonstration. If these fail, preserve the diagnostic result and any safe intermediate work; do not publish a substitute as a complete run.

Group independent ingestion, database writes and Laya inference where useful. Dependent research and drafting remain sequential. Groq's separately offered asynchronous Batch API is not part of the free-only baseline; do not enable it merely because general batching was requested.

Read credentials through local configuration/environment and Vercel deployment tooling as appropriate, never through committed public files. Frontend hosting must fit the actual permitted free account tier; account/usage eligibility is an integration prerequisite, not a cost guarantee. If any chosen service cannot support this use without payment, record the blocker and discuss an alternative. Do not switch platforms or providers silently.

## 10. Functional verification and developer review

No formal performance, accuracy, recall, latency, throughput or cost-efficiency benchmark is required. No benchmark corpus, automated model judge service, telemetry dashboard or mock-heavy unit suite is in scope.

Use the highest available integration boundaries and concrete observations:

1. **Real inputs:** inspect actual source responses and normalized records. Show which discovery route contributed the selected athlete and evidence.
2. **Persistence:** verify corresponding Neon records exist. Re-import a small actual sample and confirm stable source records/links do not multiply; verify new evidence can attach to an existing athlete. This is functional correctness, not a performance evaluation.
3. **Screening and selection:** inspect eligibility, the passages sent for angle discovery and the recorded decisions across the bounded shortlist. Inspect local Laya input/output if enabled. Verify that the system can advance from a candidate with insufficient evidence, without claiming ranking quality statistics.
4. **Story support:** inspect the selected evidence, generated claims, citations and English slide copy. The coding assistant acts as a development reviewer and records discrepancies; a model's self-explanation is not independent validation.
5. **Photos:** inspect the selected original file/licence record or placeholder reason. Check caption context and attribution.
6. **Public package:** check five ordered slides, approximate word budget, source references, missing-data behaviour and absence of secrets/private content. A lightweight schema check is appropriate for this interface.
7. **Frontend:** open the static build at phone and desktop widths, operate swipe/buttons/keyboard, inspect the evidence section and verify it works without backend/database access. Check the deployed Vercel version when deployment is performed.
8. **Limitations:** retain a short integration ledger stating actual operations, outcomes, caveats, and possible fallback work. A missing integration is a finding, not a reason to invent proof.

Treat any automated checks as focused safeguards for the output contract and actual failure risks, not a requirement to write tests mirroring every implementation function. Do not broaden testing after checks pass without a new concern.

## 11. Milestones and completion criteria

### Milestone A: verify access and inspect real data

Check free-account access, credentials, local Laya/runtime compatibility, Neon connectivity, and real source responses. Pin working versions during implementation and record unavailable services. Do not assume older research notes prove present availability.

### Milestone B: demonstrate the library and screening

Persist actual athlete/news records, inspect identity matching and incremental updates, and show which eligible candidates and source passages led to the selected story. Record whether optional Laya screening ran.

### Milestone C: produce one carousel package

Research the chosen lead through bounded Groq calls and authorized retrieval. Produce five English slides, source-linked facts, a brief explanation, and a photo candidate/placeholder. Inspect the package for functional and factual mistakes.

### Milestone D: demonstrate the static showcase

Display the saved package in a temporary, responsive, swipeable frontend with an evidence section. Export and visually check a matching five-page PDF. Verify and deploy the static build to Vercel within permitted free access. Preserve reproducible instructions for running locally, exporting a package and updating the static showcase.

The minimal demo is complete when one genuine run starts without an athlete name, story angle, hand-picked facts, slide copy or presentation file and produces a cited, renderable five-slide package automatically. An independent reviewer then accepts the exact generated version and its source use. The saved carousel and evidence can be reviewed on the static showcase, with an honest record of integration limitations. An editorially assisted package is useful as a prototype example and cannot satisfy this end-to-end acceptance criterion.

## 12. Out of scope

- Live generation or research triggered by the public website.
- A deployed backend, scheduled ingestion, queues, production monitoring or resilience infrastructure.
- Formal model/performance evaluation, a trained editorial classifier, RLCD or other fine-tuning.
- Langfuse integration and a prebuilt harness such as Hermes; the module boundaries preserve later adoption.
- Vector retrieval or an advanced full-text/semantic search system; basic database querying and external evidence retrieval are included.
- Paid Groq usage, paid batch processing, paid data/photo plans or automatic billing upgrades.
- Arabic/bilingual output or a dedicated Arabic-support project.
- Downloadable slide images, reels, video and automatic social posting.
- AI image generation, automatic image-rights guarantees or a legal-clearance service.
- Airtable synchronization, a new editorial CMS, accounts, permissions, comments or approval dashboards. Airtable remains a later optional connection.
- A full public athlete website, networking/event/media/sponsor discovery, production-scale corpus building or exhaustive coverage of every Arab nation/sport.
- Automatic switching to another model/provider or fabricated/handpicked success data when a required component fails.

## 13. Integration settings to validate, not new product questions

The exact Groq model, Laya checkpoint/runtime, source response shapes, query budgets, score mapping, word-count enforcement details and Vercel frontend implementation are selected or confirmed during the first working milestone. Record them in implementation configuration and the integration ledger. None is permission to expand the product scope or spend money.

Existing files provide research context, not guaranteed vendor contracts or tested APIs: [source research](research-arab-sports-news.md), [photo sources](research-photo-api-shortlist.md), [earlier agent research](research-agent-feasibility.md). The older Hermes/Gemini execution recommendations and proposal-era timing assumptions are superseded.

There is no application manifest, established test seam, configured tracker or ADR structure in the inspected workspace. This local specification is the canonical deliverable. Tracker setup and ticket publication are optional subsequent workflow work; no external issue was created or labelled.
