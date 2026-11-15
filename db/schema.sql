-- Noise Radar schema v1. Shared contract: change only with a note in docs/ARCHITECTURE.md (Shared interfaces).
-- Applied automatically by the Postgres container on first start.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS accounts (
    did           TEXT PRIMARY KEY,
    handle        TEXT,
    display_name  TEXT,
    created_at    TIMESTAMPTZ,
    followers     INTEGER,
    follows       INTEGER,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS posts (
    uri           TEXT PRIMARY KEY,
    did           TEXT NOT NULL REFERENCES accounts(did),
    text          TEXT,
    langs         TEXT[],
    created_at    TIMESTAMPTZ,
    reply_to      TEXT,
    embed_kind    TEXT,
    embed_urls    TEXT[],
    media_hashes  TEXT[],
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS posts_created_at_idx ON posts (created_at);
CREATE INDEX IF NOT EXISTS posts_ingested_at_idx ON posts (ingested_at);
CREATE INDEX IF NOT EXISTS posts_did_idx ON posts (did);

CREATE TABLE IF NOT EXISTS reposts (
    uri           TEXT PRIMARY KEY,
    did           TEXT NOT NULL,
    subject_uri   TEXT NOT NULL,
    created_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reposts_subject_idx ON reposts (subject_uri);

CREATE TABLE IF NOT EXISTS follows (
    uri           TEXT PRIMARY KEY,
    did           TEXT NOT NULL,
    subject_did   TEXT NOT NULL,
    created_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS follows_subject_idx ON follows (subject_did);

CREATE TABLE IF NOT EXISTS clusters (
    id            BIGSERIAL PRIMARY KEY,
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    score         REAL NOT NULL,
    confidence    REAL,
    status        TEXT NOT NULL CHECK (status IN ('coordinated', 'likely_organic', 'insufficient')),
    signals       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id    BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    did           TEXT NOT NULL,
    role          TEXT,
    PRIMARY KEY (cluster_id, did)
);

CREATE TABLE IF NOT EXISTS cluster_posts (
    cluster_id    BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    post_uri      TEXT NOT NULL,
    PRIMARY KEY (cluster_id, post_uri)
);

CREATE TABLE IF NOT EXISTS evidence (
    id            BIGSERIAL PRIMARY KEY,
    cluster_id    BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('signal', 'media', 'source', 'analyst')),
    summary       TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_url    TEXT,
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_cluster_idx ON evidence (cluster_id);

CREATE TABLE IF NOT EXISTS eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    dataset       TEXT NOT NULL,
    split         TEXT NOT NULL,
    precision     REAL,
    recall        REAL,
    f1            REAL,
    fpr           REAL,
    n             INTEGER,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
