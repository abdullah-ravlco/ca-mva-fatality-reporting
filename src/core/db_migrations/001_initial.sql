-- Tables for the pipeline's results. Filled after each stage from its JSON output (src/core/db_sync.py);
-- the JSON files stay the working files. Every statement is IF NOT EXISTS so it is safe on a database
-- that already has the documents table.

-- A raw copy of every JSON output, key = the file's path relative to data/.
CREATE TABLE IF NOT EXISTS documents (
    key        TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per stage of each run.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    ok          BOOLEAN,
    PRIMARY KEY (run_id, stage)
);

-- Stage 1: every item that passed the filters (finalized_raw.json), across runs.
CREATE TABLE IF NOT EXISTS collected_items (
    url            TEXT PRIMARY KEY,
    title          TEXT,
    description    TEXT,
    source_name    TEXT,
    published_at   TIMESTAMPTZ,
    first_seen_run TEXT,
    last_seen_run  TEXT,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publishers (
    publisher_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE
);

-- Stages 3-5: one row per crash.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id              TEXT PRIMARY KEY,                       -- 'inc-…', from incident grouping
    -- incident grouping
    article_count            INT,
    candidate_dates          JSONB,
    grouping_locations       JSONB,
    grouping_conflicts       JSONB,
    grouping_rationale       JSONB,
    grouped_at               TIMESTAMPTZ,
    -- severity screening
    screening_status         TEXT CHECK (screening_status IN ('qualified', 'victim_identity_unidentified',
                                 'fault_unidentified', 'dropped', 'screening_failed')),
    screening_tags           TEXT[],
    screening_reasons        TEXT[],
    drop_reason_codes        TEXT[],
    drop_reason              TEXT,
    screening_facts          JSONB,
    screened_at              TIMESTAMPTZ,
    -- lead verification
    verification_verdict     TEXT CHECK (verification_verdict IN ('qualifies', 'fault_unidentified',
                                 'does_not_qualify', 'needs_review')),
    verification_reasons     TEXT[],
    verification_explanation TEXT,
    corrections              TEXT[],
    missing_information      TEXT[],
    crash_date               DATE,
    crash_time               TEXT,
    crash_location           TEXT,
    city                     TEXT,
    county                   TEXT,
    road                     TEXT,
    crash_type               TEXT,
    other_party_involved     TEXT,
    other_party_description  TEXT,
    other_party_at_fault     TEXT,
    summary                  TEXT,
    verified_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stage 2: one row per article.
CREATE TABLE IF NOT EXISTS articles (
    article_id            TEXT PRIMARY KEY,                          -- 'art-…'
    incident_id           TEXT REFERENCES incidents (incident_id) ON DELETE SET NULL,
    publisher_id          BIGINT REFERENCES publishers (publisher_id) ON DELETE SET NULL,
    url                   TEXT NOT NULL,
    canonical_url         TEXT,
    final_url             TEXT,
    title                 TEXT,
    description           TEXT,
    published_at          TIMESTAMPTZ,
    discovered_by_sources TEXT[],
    discovered_by_queries TEXT[],
    fetch_status          TEXT,
    extraction_method     TEXT,
    extraction_error      TEXT,
    text_available        BOOLEAN,
    text                  TEXT,
    filter_decision       TEXT,
    filter_reasons        TEXT[],
    metadata              JSONB,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS articles_incident_idx ON articles (incident_id);
CREATE INDEX IF NOT EXISTS articles_canonical_url_idx ON articles (canonical_url);
CREATE INDEX IF NOT EXISTS articles_publisher_idx ON articles (publisher_id);

-- Stages 5-6: everyone reported in an incident (killed, injured, uninjured).
CREATE TABLE IF NOT EXISTS people (
    person_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id         TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    person_key          TEXT NOT NULL,                               -- normalized name, or 'unnamed-N'
    full_name           TEXT,
    first_name          TEXT,
    last_name           TEXT,
    aliases             TEXT[],
    age                 INT CHECK (age IS NULL OR age BETWEEN 0 AND 130),
    dob                 DATE,
    date_of_death       DATE,
    gender              TEXT,
    role                TEXT,                                        -- driver, passenger, pedestrian...
    outcome             TEXT CHECK (outcome IS NULL OR outcome IN ('killed', 'injured', 'uninjured', 'unknown')),
    vehicle             TEXT,
    at_fault            TEXT CHECK (at_fault IS NULL OR at_fault IN ('yes', 'no', 'unknown')),
    fault_basis         TEXT,
    fault_explanation   TEXT,
    impaired            TEXT CHECK (impaired IS NULL OR impaired IN ('yes', 'no', 'unknown')),
    residence           TEXT,                                        -- as the news states it
    -- contact enrichment (SearchBug)
    street              TEXT,
    town                TEXT,
    address_county      TEXT,
    state               TEXT,
    zip                 TEXT,
    other_locations     TEXT[],
    match_status        TEXT,                                        -- matched, possible_match, ambiguous...
    searchbug_token     TEXT,
    searchbug_record    JSONB,
    relatives_on_record INT,
    enriched_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (incident_id, person_key)
);
CREATE INDEX IF NOT EXISTS people_outcome_idx ON people (outcome);

-- Stage 6: up to N closest relatives per deceased person, rank 1 = closest.
CREATE TABLE IF NOT EXISTS next_of_kin (
    kin_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id           BIGINT NOT NULL REFERENCES people (person_id) ON DELETE CASCADE,
    rank                SMALLINT NOT NULL CHECK (rank >= 1),
    full_name           TEXT,
    relationship        TEXT,
    relationship_source TEXT,                                        -- searchbug or article
    relationship_quote  TEXT,
    address             TEXT,
    searchbug_token     TEXT,
    lookup_status       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_id, rank)
);

-- Phones and emails, of a person or of a next of kin (exactly one of the two).
CREATE TABLE IF NOT EXISTS contact_points (
    contact_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id  BIGINT REFERENCES people (person_id) ON DELETE CASCADE,
    kin_id     BIGINT REFERENCES next_of_kin (kin_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('phone', 'email')),
    value      TEXT NOT NULL,
    phone_type TEXT,
    last_seen  DATE,
    rank       SMALLINT,
    CHECK ((person_id IS NULL) <> (kin_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS contact_points_person_value_idx ON contact_points (person_id, kind, value) WHERE person_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS contact_points_kin_value_idx ON contact_points (kin_id, kind, value) WHERE kin_id IS NOT NULL;

-- The quote behind each fact (the audit trail).
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id    TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    person_id      BIGINT REFERENCES people (person_id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    field          TEXT NOT NULL,
    value          TEXT,
    quote          TEXT,
    source_url     TEXT,
    quote_verified BOOLEAN
);
CREATE INDEX IF NOT EXISTS evidence_incident_idx ON evidence (incident_id);

-- Filled by the team, never by the pipeline (the mailer sheet's Mailing Date / Been Verified).
CREATE TABLE IF NOT EXISTS mailings (
    mailing_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id     BIGINT NOT NULL REFERENCES people (person_id) ON DELETE CASCADE,
    mailing_date  DATE,
    been_verified BOOLEAN,
    verified_by   TEXT,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
