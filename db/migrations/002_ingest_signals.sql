-- 002: ingest + signals schema (owner: kirill). Additive to db/schema.sql, with one exception:
-- the raw event tables (posts, reposts, follows; likes is new) become day-partitioned by
-- `seen_at` (Jetstream server time, derived from time_us). Record `createdAt` is client-supplied
-- and can be backdated or in the future, so it can't be the partition key; it stays in `created_at`.
-- The conversion only runs while those tables are empty and refuses otherwise.
-- Every statement is idempotent: re-running this file (or db/schema.sql after it) is a no-op.

-- 1. Convert empty, unpartitioned raw tables into partitioned ones -------------------------------
DO $$
DECLARE
    t text;
    has_rows boolean;
BEGIN
    FOREACH t IN ARRAY ARRAY['posts', 'reposts', 'follows'] LOOP
        IF to_regclass(t) IS NOT NULL
           AND (SELECT relkind FROM pg_class WHERE oid = to_regclass(t)) = 'r' THEN
            EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)', t) INTO has_rows;
            IF has_rows THEN
                RAISE EXCEPTION 'migration 002: table % has rows; refusing to convert it', t;
            END IF;
            EXECUTE format('DROP TABLE %I', t);
        END IF;
    END LOOP;
END $$;

-- 2. Accounts: identity enrichment (PLC, AppView) and moderation status -------------------------
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS did_created_at     TIMESTAMPTZ;  -- first PLC op
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS pds_host           TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS handle_changes     INTEGER;      -- from PLC audit log
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status             TEXT;         -- active|takendown|suspended|deleted|deactivated|...
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status_at          TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS posts_count        INTEGER;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS description_len    INTEGER;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS has_avatar         BOOLEAN;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS profile_fetched_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS plc_fetched_at     TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS accounts_status_idx ON accounts (status) WHERE status IS NOT NULL;

-- Account / identity events from Jetstream (takedowns = free external ground truth; can't be backfilled).
CREATE TABLE IF NOT EXISTS account_events (
    id           BIGSERIAL PRIMARY KEY,
    did          TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('account', 'identity')),
    active       BOOLEAN,
    status       TEXT,
    handle       TEXT,
    seq          BIGINT,
    time_us      BIGINT NOT NULL,
    event_time   TIMESTAMPTZ,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS account_events_dedupe_idx ON account_events (did, kind, seq);
CREATE INDEX IF NOT EXISTS account_events_did_idx ON account_events (did, time_us);
CREATE INDEX IF NOT EXISTS account_events_status_idx ON account_events (status, time_us)
    WHERE status IS NOT NULL;

-- 3. Raw event tables, partitioned by day on seen_at ----------------------------------------------
CREATE TABLE IF NOT EXISTS posts (
    uri           TEXT NOT NULL,
    cid           TEXT,
    did           TEXT NOT NULL REFERENCES accounts(did),
    text          TEXT,
    langs         TEXT[],
    created_at    TIMESTAMPTZ,              -- record createdAt (client clock)
    seen_at       TIMESTAMPTZ NOT NULL,     -- Jetstream time_us (server clock)
    reply_to      TEXT,                     -- reply.parent.uri
    reply_root    TEXT,                     -- reply.root.uri
    quote_uri     TEXT,                     -- embed.record uri
    embed_kind    TEXT,                     -- images|video|external|record|recordWithMedia
    embed_urls    TEXT[],                   -- external card + link facets, normalised
    media_hashes  TEXT[],                   -- image/video blob CIDs (identical upload => same CID)
    mentions      TEXT[],                   -- mentioned DIDs
    tags          TEXT[],                   -- hashtags, lowercased
    text_hash     TEXT,                     -- hash of normalised text (signals.textnorm)
    deleted_at    TIMESTAMPTZ,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (uri, seen_at)
) PARTITION BY RANGE (seen_at);
-- Names match db/schema.sql so re-running it doesn't add duplicate indexes.
CREATE INDEX IF NOT EXISTS posts_created_at_idx  ON posts (created_at);
CREATE INDEX IF NOT EXISTS posts_ingested_at_idx ON posts (ingested_at);
CREATE INDEX IF NOT EXISTS posts_did_idx         ON posts (did, seen_at);
CREATE INDEX IF NOT EXISTS posts_seen_at_idx     ON posts (seen_at);
CREATE INDEX IF NOT EXISTS posts_reply_to_idx    ON posts (reply_to, seen_at) WHERE reply_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS posts_quote_idx       ON posts (quote_uri, seen_at) WHERE quote_uri IS NOT NULL;
CREATE INDEX IF NOT EXISTS posts_text_hash_idx   ON posts (text_hash, seen_at) WHERE text_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS posts_embed_urls_idx  ON posts USING GIN (embed_urls);
CREATE INDEX IF NOT EXISTS posts_media_idx       ON posts USING GIN (media_hashes);

CREATE TABLE IF NOT EXISTS reposts (
    uri           TEXT NOT NULL,
    did           TEXT NOT NULL,
    subject_uri   TEXT NOT NULL,
    subject_did   TEXT,
    created_at    TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL,
    deleted_at    TIMESTAMPTZ,
    PRIMARY KEY (uri, seen_at)
) PARTITION BY RANGE (seen_at);
CREATE INDEX IF NOT EXISTS reposts_subject_idx ON reposts (subject_uri, seen_at);
CREATE INDEX IF NOT EXISTS reposts_did_idx     ON reposts (did, seen_at);
CREATE INDEX IF NOT EXISTS reposts_seen_at_idx ON reposts (seen_at);

CREATE TABLE IF NOT EXISTS likes (
    uri           TEXT NOT NULL,
    did           TEXT NOT NULL,
    subject_uri   TEXT NOT NULL,
    subject_did   TEXT,
    created_at    TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL,
    deleted_at    TIMESTAMPTZ,
    PRIMARY KEY (uri, seen_at)
) PARTITION BY RANGE (seen_at);
CREATE INDEX IF NOT EXISTS likes_subject_idx ON likes (subject_uri, seen_at);
CREATE INDEX IF NOT EXISTS likes_did_idx     ON likes (did, seen_at);
CREATE INDEX IF NOT EXISTS likes_seen_at_idx ON likes (seen_at);

CREATE TABLE IF NOT EXISTS follows (
    uri           TEXT NOT NULL,
    did           TEXT NOT NULL,
    subject_did   TEXT NOT NULL,
    created_at    TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL,
    deleted_at    TIMESTAMPTZ,
    PRIMARY KEY (uri, seen_at)
) PARTITION BY RANGE (seen_at);
CREATE INDEX IF NOT EXISTS follows_subject_idx ON follows (subject_did, seen_at);
CREATE INDEX IF NOT EXISTS follows_did_idx     ON follows (did, seen_at);
CREATE INDEX IF NOT EXISTS follows_seen_at_idx ON follows (seen_at);

-- Partition helpers. Day boundaries are UTC.
CREATE OR REPLACE FUNCTION noise_ensure_day_partitions(d date) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    t text;
    part text;
BEGIN
    FOREACH t IN ARRAY ARRAY['posts', 'reposts', 'likes', 'follows'] LOOP
        part := t || '_p' || to_char(d, 'YYYYMMDD');
        IF to_regclass(part) IS NULL THEN
            BEGIN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                    part, t,
                    (d::timestamp AT TIME ZONE 'UTC'),
                    ((d + 1)::timestamp AT TIME ZONE 'UTC'));
            EXCEPTION WHEN duplicate_table THEN NULL;
            END;
        END IF;
    END LOOP;
END $$;

-- Drops raw partitions whose whole day ended more than keep_hours before ref. Returns dropped names.
CREATE OR REPLACE FUNCTION noise_drop_old_partitions(keep_hours integer, ref timestamptz DEFAULT now())
RETURNS SETOF text LANGUAGE plpgsql AS $$
DECLARE
    r record;
    day_end timestamptz;
BEGIN
    FOR r IN
        SELECT c.relname AS part
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname IN ('posts', 'reposts', 'likes', 'follows')
          AND c.relname ~ '_p[0-9]{8}$'
    LOOP
        day_end := ((to_date(right(r.part, 8), 'YYYYMMDD') + 1)::timestamp AT TIME ZONE 'UTC');
        IF day_end < ref - make_interval(hours => keep_hours) THEN
            EXECUTE format('DROP TABLE %I', r.part);
            RETURN NEXT r.part;
        END IF;
    END LOOP;
END $$;

SELECT noise_ensure_day_partitions(current_date);
SELECT noise_ensure_day_partitions(current_date + 1);

-- 4. Ingest cursor / health -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_state (
    stream          TEXT PRIMARY KEY,         -- 'jetstream' live, 'replay' for replays
    cursor_time_us  BIGINT,
    host            TEXT,
    last_event_at   TIMESTAMPTZ,              -- server time of the newest stored event
    events_per_s    REAL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Clusters: detector metadata and stable campaign ids --------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id               BIGSERIAL PRIMARY KEY,
    first_seen       TIMESTAMPTZ NOT NULL,
    last_seen        TIMESTAMPTZ NOT NULL,
    latest_cluster_id BIGINT,
    max_score        REAL,
    status           TEXT,                     -- latest cluster status
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS campaign_id      BIGINT REFERENCES campaigns(id);
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS p_value          REAL;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS families_fired   INTEGER;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS detector_version TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS window_kind      TEXT;      -- e.g. '10m', '1h'
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS size             INTEGER;   -- distinct member accounts
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS synthetic        BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS clusters_status_idx   ON clusters (status, window_end DESC);
CREATE INDEX IF NOT EXISTS clusters_campaign_idx ON clusters (campaign_id);
CREATE INDEX IF NOT EXISTS clusters_window_idx   ON clusters (window_end DESC);
CREATE INDEX IF NOT EXISTS cluster_members_did_idx ON cluster_members (did);

-- Posts a cluster references, copied out of the raw partitions so they outlive retention.
CREATE TABLE IF NOT EXISTS cluster_posts_snapshot (
    uri           TEXT PRIMARY KEY,
    did           TEXT NOT NULL,
    text          TEXT,
    langs         TEXT[],
    created_at    TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ,
    reply_to      TEXT,
    reply_root    TEXT,
    quote_uri     TEXT,
    embed_kind    TEXT,
    embed_urls    TEXT[],
    media_hashes  TEXT[],
    text_hash     TEXT,
    deleted_at    TIMESTAMPTZ,
    snapshot_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. Migration bookkeeping (used by common/migrate.py) ---------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
