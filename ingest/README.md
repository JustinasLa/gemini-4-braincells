# ingest/

Bluesky Jetstream → Postgres. Read-only: it never posts or creates accounts.

```bash
uv run python -m common.migrate                       # schema.sql + db/migrations/*.sql
uv run python -m ingest run [--record DIR]            # live; resumes from ingest_state cursor
uv run python -m ingest replay DIR --database-url URL # recorded files -> another DB (eval)
uv run python -m ingest maintain                      # partitions + retention, once
```

## What it stores

| Table | From | Notes |
|---|---|---|
| `posts` | `app.bsky.feed.post` | text, langs, reply parent/root, quote, `embed_urls` (external card + link facets, normalised), `media_hashes` (image/video blob CIDs: identical uploads share a CID), mentions, tags, `text_hash` (hash of `signals.textnorm.normalize_text`) |
| `reposts`, `likes` | `app.bsky.feed.repost/like` | `subject_uri` and `subject_did` (author of the subject) |
| `follows` | `app.bsky.graph.follow` | `subject_did` |
| `account_events` | Jetstream `account`/`identity` events | takedowns, deactivations, handle changes; also updates `accounts.status/handle` |
| `accounts` | every DID seen | `first_seen`; enrichment columns are filled later, only for DIDs in clusters |
| `ingest_state` | consumer | `cursor_time_us`, `last_event_at`, `events_per_s`, host |

Deletes set `deleted_at` (rows are never hard-deleted by ingest; campaigns often delete after
amplifying, which is itself evidence).

## Time columns

- `seen_at`: Jetstream `time_us` (server clock). Partition key and what windows use.
- `created_at`: the record's `createdAt` (client clock, can be backdated). Kept for comparison.

## Partitions and retention

Raw tables are partitioned by hour on `seen_at` (`posts_p2026111319`, ...). The consumer creates
upcoming partitions and drops old ones every 10 minutes: `INGEST_RETENTION_HOURS` (default 48),
`INGEST_LIKES_RETENTION_HOURS` (likes are ~2/3 of the volume). At ~500 events/s with indexes that's
about 1 GB per hour of raw data. Anything a cluster references is copied to
`cluster_posts_snapshot` / `evidence` by `signals/`, so it outlives retention.

## Reliability

- Cursor saved in the same transaction as the rows; on reconnect it rewinds 5 s. Inserts are
  `ON CONFLICT DO NOTHING`, and a recent-URI set drops duplicates from a different host (another
  host stamps another `time_us`).
- Fails over across `jetstream{1,2}.us-{east,west}.bsky.network` with exponential backoff.
  Override with `JETSTREAM_HOSTS`; `JETSTREAM_URL`'s host is tried first.
- Lag watchdog: if the stream stays more than 60 s behind real time for 2 minutes while our own
  queue is nearly empty (the host is late, not us), the reader drops that host and moves to the
  next one, resuming from the cursor (`INGEST_FAILOVER_LAG_S`, `INGEST_FAILOVER_AFTER_S`).
- Logs `events/s`, `lag_s`, queue size, rows and duplicates every 30 s.

## Recording and replay

`--record DIR` (or `INGEST_RECORD_DIR`) tees raw messages to `DIR/YYYYMMDD/HH-<start>.jsonl.gz`,
pruned after `INGEST_RECORD_KEEP_HOURS`. `replay` runs the same normaliser and writer over those
files (as fast as possible, or `--speed 1` for real time), so a recorded slice always produces the
same rows. Use a separate database for replays.
