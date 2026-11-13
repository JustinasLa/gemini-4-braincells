# Media analysis

`media.nemotron.NemotronClient` calls Nemotron 3.5 Super VL through NVIDIA's OpenAI-compatible
API. The NVIDIA key is intentionally left blank in `.env.example`; provide `NVIDIA_API_KEY` only
in the ignored runtime `.env` when a team key is available.

The worker path is:

1. `BlobFetcher` retrieves a post's media CID from the author's PDS with the read-only
   `com.atproto.sync.getBlob` endpoint.
2. `process_cluster_media` deduplicates CIDs, fetches bounded media, and sends each item to
   Nemotron. Video can be represented as extracted image frames.
3. `MediaRun.evidence_rows()` or `persist_evidence()` produces idempotent `kind='media'` rows for
   the shared `evidence` table. Failures are retained in `MediaRun.failures` and logged rather
   than silently discarded.

```python
from media import BlobFetcher, NemotronClient, process_cluster_media

with BlobFetcher() as fetcher:
    run = process_cluster_media(cluster_posts, NemotronClient.from_settings(settings), fetcher)
    persist_evidence(conn, cluster_id, run)
```

Successful results are cached in memory by `media_hash`, include model/latency/token metadata,
and expose `evidence_detail()` for the shared JSON field. The model describes visible evidence;
it does not infer political intent or make a coordination verdict.