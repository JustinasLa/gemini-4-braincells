-- 005: meme swarms (owner: swarm/). Additive.
-- swarm/ (python -m swarm run) groups the image files that many accounts posted into pictures,
-- scores each picture's posts for swarm behaviour and stores one row per picture here. The API
-- serves the wall (GET /api/swarms) and the detail (GET /api/swarms/{id}) from this table.

-- Per file (blob CID): perceptual hashes of its thumbnail, so a file is fetched from the CDN once,
-- and whether the file was ever judged unsafe for the public wall. phash/dhash are NULL when the
-- thumbnail could not be fetched or decoded (error says why; retried after a while), or when the
-- row only records unsafe_reason (error NULL: the hash is still fetched). unsafe_reason is sticky:
-- it survives group merges, splits, id changes and stale drops, and such rows are never pruned.
CREATE TABLE IF NOT EXISTS picture_hashes (
    cid         TEXT PRIMARY KEY,           -- Bluesky blob CID (equal CID = same file)
    did         TEXT NOT NULL,              -- an account that posted it (CDN address)
    phash       TEXT,                       -- 64-bit imagehash hex
    dhash       TEXT,
    detail      REAL,                       -- grayscale std: low = too plain to hash-match
    width       INTEGER,
    height      INTEGER,
    tiny        BYTEA,                      -- 64x64 grayscale pixels: verifies a hash match
    error       TEXT,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    unsafe_reason TEXT,                     -- set once, never cleared ("label porn", "vision: adult")
    unsafe_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS picture_hashes_fetched_idx ON picture_hashes (fetched_at);

-- One row per picture group. `id` is stable across passes: a group keeps the id of the stored
-- group it overlaps (by file), else a short hash of its earliest file's CID.
CREATE TABLE IF NOT EXISTS meme_swarms (
    id                 TEXT PRIMARY KEY,
    files              TEXT[] NOT NULL,     -- blob CIDs, earliest first
    phash              TEXT,                -- representative file's hashes
    dhash              TEXT,
    rep_did            TEXT NOT NULL,       -- representative file, for the thumbnail URL
    rep_cid            TEXT NOT NULL,
    verdict            TEXT NOT NULL,       -- swarm | organic | unclear (the group, never an account)
    score              REAL NOT NULL,
    n_posts            INTEGER NOT NULL,
    n_accounts         INTEGER NOT NULL,
    first_at           TIMESTAMPTZ,
    last_at            TIMESTAMPTZ,
    report             JSONB NOT NULL,      -- swarm.model.SwarmReport
    posts              JSONB NOT NULL DEFAULT '[]',  -- <= 30 swarm.model.SwarmPost, earliest first
    safe               BOOLEAN NOT NULL DEFAULT false,  -- shown on the public wall
    unsafe_reason      TEXT,
    labels_checked_at  TIMESTAMPTZ,
    kind               TEXT,                -- meme | ad | photo | screenshot | other (vision of vision_cid)
    caption            TEXT,                -- text on the picture (vision of vision_cid)
    vision_at          TIMESTAMPTZ,
    vision_cid         TEXT,                -- the file the vision read saw: counts only while = rep_cid
    window_h           REAL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS meme_swarms_wall_idx ON meme_swarms (safe, verdict, score DESC);
CREATE INDEX IF NOT EXISTS meme_swarms_files_idx ON meme_swarms USING GIN (files);
CREATE INDEX IF NOT EXISTS meme_swarms_updated_idx ON meme_swarms (updated_at);
