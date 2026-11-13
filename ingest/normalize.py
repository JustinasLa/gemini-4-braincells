"""Pure Jetstream event -> row normalisation. No I/O, so it's easy to test and to replay.

Jetstream event shapes (verified live 2026-11-13 on jetstream{1,2}.us-{east,west}.bsky.network):
  {"did", "time_us", "kind": "commit", "commit": {"operation": create|update|delete,
     "collection", "rkey", "record"?, "cid"?}}
  {"did", "time_us", "kind": "account", "account": {"active", "did", "seq", "status"?, "time"}}
  {"did", "time_us", "kind": "identity", "identity": {"did", "seq", "time", "handle"?}}
Account and identity events arrive even when wantedCollections is set.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from signals.textnorm import normalize_url, text_hash

POST = "app.bsky.feed.post"
REPOST = "app.bsky.feed.repost"
LIKE = "app.bsky.feed.like"
FOLLOW = "app.bsky.graph.follow"
COLLECTIONS = (POST, REPOST, LIKE, FOLLOW)
TABLE_FOR = {POST: "posts", REPOST: "reposts", LIKE: "likes", FOLLOW: "follows"}

_MAX_TEXT = 10_000  # posts are capped at 3000 graphemes; anything bigger is malformed


def clean(s: object) -> str | None:
    """Text safe for Postgres (no NUL bytes), or None."""
    if not isinstance(s, str):
        return None
    return s.replace("\x00", "")[:_MAX_TEXT]


def us_to_dt(time_us: int) -> datetime:
    return datetime.fromtimestamp(time_us / 1_000_000, tz=UTC)


def parse_ts(value: object) -> datetime | None:
    """Parse a record timestamp. Client-supplied, so be lenient and return None on garbage."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if not 1990 <= dt.year <= 2100:
        return None
    return dt


def did_of(at_uri: str | None) -> str | None:
    """'at://did:plc:x/app.bsky.feed.post/abc' -> 'did:plc:x'."""
    if not at_uri or not at_uri.startswith("at://"):
        return None
    authority = at_uri[5:].split("/", 1)[0]
    return authority if authority.startswith("did:") else None


def _blob_cid(blob: object) -> str | None:
    if not isinstance(blob, dict):
        return None
    ref = blob.get("ref")
    if isinstance(ref, dict) and isinstance(ref.get("$link"), str):
        return ref["$link"]
    if isinstance(blob.get("cid"), str):  # legacy blob format
        return blob["cid"]
    return None


def _strong_ref_uri(ref: object) -> str | None:
    if isinstance(ref, dict) and isinstance(ref.get("uri"), str):
        return clean(ref["uri"])
    return None


@dataclass(slots=True)
class Post:
    uri: str
    cid: str | None
    did: str
    text: str | None
    langs: list[str] | None
    created_at: datetime | None
    seen_at: datetime
    reply_to: str | None = None
    reply_root: str | None = None
    quote_uri: str | None = None
    embed_kind: str | None = None
    embed_urls: list[str] = field(default_factory=list)
    media_hashes: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    text_hash: str | None = None


@dataclass(slots=True)
class Action:
    """A repost, like or follow. subject_uri is None for follows."""

    table: str
    uri: str
    did: str
    subject_uri: str | None
    subject_did: str | None
    created_at: datetime | None
    seen_at: datetime


@dataclass(slots=True)
class Delete:
    table: str
    uri: str
    did: str
    seen_at: datetime


@dataclass(slots=True)
class AccountEvent:
    did: str
    kind: str  # 'account' | 'identity'
    active: bool | None
    status: str | None
    handle: str | None
    seq: int | None
    time_us: int
    event_time: datetime | None


Row = Post | Action | Delete | AccountEvent


def _embed(record: dict, post: Post) -> None:
    embed = record.get("embed")
    if not isinstance(embed, dict):
        return
    etype = embed.get("$type") or ""
    kind = etype.rsplit(".", 1)[-1] if etype.startswith("app.bsky.embed.") else None
    post.embed_kind = kind
    media = embed
    if kind == "recordWithMedia":
        rec = embed.get("record")
        post.quote_uri = _strong_ref_uri(rec.get("record") if isinstance(rec, dict) else None)
        media = embed.get("media") if isinstance(embed.get("media"), dict) else {}
    elif kind == "record":
        post.quote_uri = _strong_ref_uri(embed.get("record"))
    mtype = media.get("$type") or ""
    if mtype.endswith(".images"):
        for img in media.get("images") or []:
            cid = _blob_cid(img.get("image")) if isinstance(img, dict) else None
            if cid:
                post.media_hashes.append(cid)
    elif mtype.endswith(".video"):
        cid = _blob_cid(media.get("video"))
        if cid:
            post.media_hashes.append(cid)
    elif mtype.endswith(".external"):
        ext = media.get("external")
        if isinstance(ext, dict):
            u = normalize_url(clean(ext.get("uri")))
            if u:
                post.embed_urls.append(u)


def _facets(record: dict, post: Post) -> None:
    for facet in record.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        for feat in facet.get("features") or []:
            if not isinstance(feat, dict):
                continue
            ftype = feat.get("$type") or ""
            if ftype.endswith("#link"):
                u = normalize_url(clean(feat.get("uri")))
                if u and u not in post.embed_urls:
                    post.embed_urls.append(u)
            elif ftype.endswith("#mention"):
                d = clean(feat.get("did"))
                if d and d not in post.mentions:
                    post.mentions.append(d)
            elif ftype.endswith("#tag"):
                tag = clean(feat.get("tag"))
                if tag and tag.lower() not in post.tags:
                    post.tags.append(tag.lower()[:200])


def normalize(event: dict) -> Row | None:
    """One Jetstream event -> one row, or None for events we don't store."""
    did = event.get("did")
    time_us = event.get("time_us")
    if not isinstance(did, str) or not isinstance(time_us, int):
        return None
    seen_at = us_to_dt(time_us)
    kind = event.get("kind")

    if kind == "account":
        acc = event.get("account") or {}
        return AccountEvent(
            did=did,
            kind="account",
            active=acc.get("active") if isinstance(acc.get("active"), bool) else None,
            status=clean(acc.get("status")) or ("active" if acc.get("active") else None),
            handle=None,
            seq=acc.get("seq") if isinstance(acc.get("seq"), int) else None,
            time_us=time_us,
            event_time=parse_ts(acc.get("time")),
        )
    if kind == "identity":
        ident = event.get("identity") or {}
        return AccountEvent(
            did=did,
            kind="identity",
            active=None,
            status=None,
            handle=clean(ident.get("handle")),
            seq=ident.get("seq") if isinstance(ident.get("seq"), int) else None,
            time_us=time_us,
            event_time=parse_ts(ident.get("time")),
        )
    if kind != "commit":
        return None

    commit = event.get("commit") or {}
    collection = commit.get("collection")
    rkey = commit.get("rkey")
    op = commit.get("operation")
    if collection not in TABLE_FOR or not isinstance(rkey, str):
        return None
    uri = f"at://{did}/{collection}/{rkey}"
    table = TABLE_FOR[collection]

    if op == "delete":
        return Delete(table=table, uri=uri, did=did, seen_at=seen_at)
    if op != "create":  # updates are rare for these collections; ignore them
        return None
    record = commit.get("record")
    if not isinstance(record, dict):
        return None
    created_at = parse_ts(record.get("createdAt"))

    if collection == POST:
        text = clean(record.get("text"))
        langs = [clean(x) for x in record.get("langs") or [] if isinstance(x, str)][:5] or None
        post = Post(
            uri=uri,
            cid=clean(commit.get("cid")),
            did=did,
            text=text,
            langs=langs,
            created_at=created_at,
            seen_at=seen_at,
            text_hash=text_hash(text),
        )
        reply = record.get("reply")
        if isinstance(reply, dict):
            post.reply_to = _strong_ref_uri(reply.get("parent"))
            post.reply_root = _strong_ref_uri(reply.get("root"))
        _embed(record, post)
        _facets(record, post)
        return post

    if collection == FOLLOW:
        subject = clean(record.get("subject"))
        if not subject:
            return None
        return Action(table, uri, did, None, subject, created_at, seen_at)

    subject_uri = _strong_ref_uri(record.get("subject"))
    if not subject_uri:
        return None
    return Action(table, uri, did, subject_uri, did_of(subject_uri), created_at, seen_at)
