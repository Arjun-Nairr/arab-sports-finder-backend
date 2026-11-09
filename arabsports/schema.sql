CREATE TABLE IF NOT EXISTS runs (
  id text PRIMARY KEY,
  mode text NOT NULL,                       -- live | replay
  replay_of text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  config jsonb NOT NULL,
  integrations jsonb NOT NULL DEFAULT '{}',
  counts jsonb NOT NULL DEFAULT '{}',
  outcome text,
  notes jsonb NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS athletes (
  id bigserial PRIMARY KEY,
  source text NOT NULL,                     -- wikidata | news_mention
  source_id text NOT NULL,                  -- QID, or provisional key for news-only leads
  name text NOT NULL,
  norm_name text NOT NULL,
  aliases jsonb NOT NULL DEFAULT '[]',
  sports jsonb NOT NULL DEFAULT '[]',
  representation jsonb NOT NULL DEFAULT '[]',
  citizenship jsonb NOT NULL DEFAULT '[]',  -- recorded, never used for eligibility
  eligibility text NOT NULL,                -- supported | claimed | unresolved
  resolution text NOT NULL,                 -- source_identity | provisional
  gaps jsonb NOT NULL DEFAULT '[]',
  external jsonb NOT NULL DEFAULT '{}',
  evidence_status text NOT NULL DEFAULT 'needs_evidence',
  evidence_fingerprint text,
  evidence_version int NOT NULL DEFAULT 0,
  first_run text REFERENCES runs(id),
  last_run text REFERENCES runs(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS athletes_norm_name ON athletes (norm_name);
CREATE INDEX IF NOT EXISTS athletes_status ON athletes (eligibility, evidence_status);

CREATE TABLE IF NOT EXISTS articles (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  provider_id text,
  canonical_url text NOT NULL UNIQUE,
  url text NOT NULL,
  publisher text,
  title text,
  language text,
  published_at timestamptz,
  updated_at_source timestamptz,
  seen_at timestamptz,
  retrieved_at timestamptz NOT NULL DEFAULT now(),
  content text,
  content_kind text NOT NULL,               -- full_text | summary | title_only
  content_hash text NOT NULL,
  acquisition text NOT NULL,
  access_note text,
  extra jsonb NOT NULL DEFAULT '{}',
  first_run text REFERENCES runs(id),
  last_run text REFERENCES runs(id),
  UNIQUE (provider, provider_id)
);
CREATE INDEX IF NOT EXISTS articles_content_hash ON articles (content_hash);

-- Every detected mention; a link is a mention with athlete_id set.
CREATE TABLE IF NOT EXISTS mentions (
  id bigserial PRIMARY KEY,
  article_id bigint NOT NULL REFERENCES articles(id),
  surface text NOT NULL,
  norm text NOT NULL,
  status text NOT NULL,                     -- linked | ambiguous | no_context | provisional
  athlete_id bigint REFERENCES athletes(id),
  candidates jsonb NOT NULL DEFAULT '[]',
  evidence jsonb NOT NULL DEFAULT '{}',
  method text NOT NULL,
  first_run text REFERENCES runs(id),
  UNIQUE (article_id, norm)
);
-- Batch 1 corrections (additive; existing rows are kept).
ALTER TABLE articles ADD COLUMN IF NOT EXISTS search_text text;        -- padded normalized title+content
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS names_fp text;           -- fingerprint of name + aliases
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS names_checked_fp text;   -- names_fp last matched against stored articles
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS reconciled_to bigint REFERENCES athletes(id);

-- Batch 1 follow-up corrections (additive). names_fp / names_checked_fp above are superseded by
-- match_fp / match_checked_fp and kept only to avoid a destructive migration.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS usage_terms jsonb NOT NULL DEFAULT '{}';  -- most restrictive explicit flags
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS match_fp text;          -- names + matching context (nation/sport terms)
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS match_checked_fp text;  -- match_fp whose stored-article recheck finished
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS recheck_cursor bigint;  -- last stored article id processed for match_fp

-- Batch 2 continuation: resumable discovery pagination, additive.
CREATE TABLE IF NOT EXISTS discovery_cursors (
  query_key text PRIMARY KEY,               -- e.g. wikidata_search:P1532=Q1028
  next_offset int NOT NULL DEFAULT 0,       -- sroffset of the first unread result
  exhausted boolean NOT NULL DEFAULT false, -- last page had no `continue`; re-probed cheaply each run
  last_run text REFERENCES runs(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Batch 2 (T03-T04), additive.
CREATE TABLE IF NOT EXISTS packets (
  id bigserial PRIMARY KEY,
  athlete_id bigint NOT NULL REFERENCES athletes(id),
  packet_fp text NOT NULL UNIQUE,
  evidence_version int NOT NULL,
  content jsonb NOT NULL,                   -- windows text is private evidence (never exported as-is)
  created_run text REFERENCES runs(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS packets_athlete ON packets (athlete_id);
CREATE TABLE IF NOT EXISTS screenings (
  id bigserial PRIMARY KEY,
  packet_id bigint NOT NULL REFERENCES packets(id),
  config_fp text NOT NULL,                  -- model revision + runtime + rubric + input limits
  model jsonb NOT NULL,
  rubric_version text NOT NULL,
  raw jsonb NOT NULL,                       -- Laya typed answers per window, unmodified
  mapped text NOT NULL,                     -- promising | routine | insufficient
  score double precision,                   -- raw uncalibrated P(narrative=A); not an approval probability
  warnings jsonb NOT NULL DEFAULT '[]',
  created_run text REFERENCES runs(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (packet_id, config_fp)
);
CREATE TABLE IF NOT EXISTS selections (
  run_id text PRIMARY KEY REFERENCES runs(id),
  candidates jsonb NOT NULL,                -- every screened candidate with eligibility, outcome, order
  selected_athlete_id bigint REFERENCES athletes(id),
  selected_screening_id bigint REFERENCES screenings(id),
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS story_packages (
  id bigserial PRIMARY KEY,
  package_id text NOT NULL UNIQUE,
  run_id text NOT NULL REFERENCES runs(id),
  schema_version text NOT NULL,
  athlete_id bigint REFERENCES athletes(id),
  screening_id bigint REFERENCES screenings(id),
  status text NOT NULL,                     -- completed | insufficient_evidence | malformed_output | provider_failed | quota_exhausted | blocked
  package jsonb NOT NULL,                   -- frozen snapshot, not a live view
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS story_one_completed_per_run ON story_packages (run_id) WHERE status = 'completed';

CREATE UNIQUE INDEX IF NOT EXISTS mentions_one_link ON mentions (article_id, athlete_id) WHERE athlete_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS mentions_athlete ON mentions (athlete_id);

-- T04 correction: generated drafts are not successful stories until independently reviewed (additive).
-- review_state: awaiting_review | accepted | rejected | not_applicable (failed attempt) | legacy_unreviewed.
-- The package jsonb stays frozen; package_sha256 identifies the exact version a review decision applies to.
ALTER TABLE story_packages ADD COLUMN IF NOT EXISTS review_state text;
ALTER TABLE story_packages ADD COLUMN IF NOT EXISTS package_sha256 text;
-- Legacy rows saved as 'completed' before review state existed have no review evidence: never treated as accepted.
UPDATE story_packages SET review_state = CASE WHEN status = 'completed' THEN 'legacy_unreviewed' ELSE 'not_applicable' END
  WHERE review_state IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS story_one_accepted_per_run ON story_packages (run_id) WHERE review_state = 'accepted';
CREATE TABLE IF NOT EXISTS package_reviews (
  id bigserial PRIMARY KEY,
  package_id text NOT NULL REFERENCES story_packages(package_id),
  package_sha256 text NOT NULL,             -- exact frozen package version reviewed
  from_state text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('accepted', 'rejected')),
  reviewer text NOT NULL,
  reasons jsonb NOT NULL DEFAULT '[]',
  reviewed_at timestamptz NOT NULL DEFAULT now()
);
-- Source-use blockers (package.acceptance_blockers) the reviewer explicitly resolved when accepting (additive).
ALTER TABLE package_reviews ADD COLUMN IF NOT EXISTS resolved_blockers jsonb NOT NULL DEFAULT '[]';

-- T04 story gate: generation needs a reviewed angle for the exact selected screening (additive).
-- refs freeze each cited passage with the article's content_hash at review time; a changed hash blocks generation.
CREATE TABLE IF NOT EXISTS angle_reviews (
  id bigserial PRIMARY KEY,
  athlete_id bigint NOT NULL REFERENCES athletes(id),
  screening_id bigint NOT NULL REFERENCES screenings(id),
  selection_run text NOT NULL REFERENCES runs(id),
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
  reviewer text NOT NULL,
  angle jsonb NOT NULL,                     -- stakes, turning_point, significance, selection_basis, notes
  refs jsonb NOT NULL,                      -- [{article_id, url, publisher, content_hash, passage, supports}]
  recorded_run text REFERENCES runs(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS angle_reviews_athlete ON angle_reviews (athlete_id, created_at);

-- Automated angle search (additive): one row per attempted player in an `auto` run, advanced or not. Not an
-- independent review; the resulting package still needs one before export.
CREATE TABLE IF NOT EXISTS angle_proposals (
  id bigserial PRIMARY KEY,
  run_id text NOT NULL REFERENCES runs(id),
  athlete_id bigint NOT NULL REFERENCES athletes(id),
  screening_id bigint REFERENCES screenings(id),
  selection_run text,
  rank int,
  outcome text NOT NULL,                    -- advanced | insufficient_evidence | invalid_references | malformed_output | provider_failed | skipped_call_reserve | no_usable_passages
  reasons jsonb NOT NULL DEFAULT '[]',
  angle jsonb,                              -- stakes, turning_point, significance, omit, facts [{id, statement, refs}]
  refs jsonb,                               -- frozen cited passages [{id, article_id, url, publisher, content_hash, passage}]
  passages_sent jsonb NOT NULL DEFAULT '[]',
  model_output jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
