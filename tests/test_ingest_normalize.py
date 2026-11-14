from datetime import UTC, datetime

from ingest.normalize import AccountEvent, Action, Delete, Post, did_of, normalize, parse_ts
from signals.textnorm import normalize_text, normalize_url, text_hash

DID = "did:plc:abc123"
T_US = 1794598323302197


def commit(collection, record=None, op="create", rkey="3n27pxszzdz2w"):
    c = {"rev": "r", "operation": op, "collection": collection, "rkey": rkey}
    if record is not None:
        c["record"] = record
        c["cid"] = "bafyrecordcid"
    return {"did": DID, "time_us": T_US, "kind": "commit", "commit": c}


def blob(cid):
    return {"$type": "blob", "ref": {"$link": cid}, "mimeType": "image/jpeg", "size": 1}


def test_plain_post():
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "$type": "app.bsky.feed.post",
                "text": "Hello  World!",
                "langs": ["en"],
                "createdAt": "2026-11-13T19:32:02.801Z",
            },
        )
    )
    assert isinstance(row, Post)
    assert row.uri == f"at://{DID}/app.bsky.feed.post/3n27pxszzdz2w"
    assert row.text == "Hello  World!"
    assert row.langs == ["en"]
    assert row.created_at == datetime(2026, 11, 13, 19, 32, 2, 801000, tzinfo=UTC)
    assert row.seen_at == datetime.fromtimestamp(T_US / 1e6, tz=UTC)
    assert row.text_hash == text_hash("hello world")
    assert row.embed_kind is None and row.embed_urls == [] and row.media_hashes == []


def test_post_with_images():
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "look",
                "createdAt": "2026-11-13T19:32:02Z",
                "embed": {
                    "$type": "app.bsky.embed.images",
                    "images": [
                        {"alt": "", "image": blob("bafkimg1")},
                        {"alt": "", "image": {"cid": "bafklegacy", "mimeType": "image/png"}},
                    ],
                },
            },
        )
    )
    assert row.embed_kind == "images"
    assert row.media_hashes == ["bafkimg1", "bafklegacy"]


def test_post_with_video():
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "",
                "createdAt": "2026-11-13T19:32:02Z",
                "embed": {"$type": "app.bsky.embed.video", "video": blob("bafkvid")},
            },
        )
    )
    assert row.embed_kind == "video"
    assert row.media_hashes == ["bafkvid"]
    assert row.text_hash is None


def test_external_link_and_facets():
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "read this #News @bob.bsky.social example.org/x",
                "createdAt": "2026-11-13T19:32:02Z",
                "embed": {
                    "$type": "app.bsky.embed.external",
                    "external": {
                        "uri": "https://WWW.Example.com/a/?utm_source=x&id=2#frag",
                        "title": "t",
                        "description": "d",
                    },
                },
                "facets": [
                    {
                        "index": {"byteStart": 0, "byteEnd": 1},
                        "features": [
                            {
                                "$type": "app.bsky.richtext.facet#link",
                                "uri": "https://example.org/x?fbclid=zzz",
                            }
                        ],
                    },
                    {
                        "index": {"byteStart": 0, "byteEnd": 1},
                        "features": [
                            {"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:bob"}
                        ],
                    },
                    {
                        "index": {"byteStart": 0, "byteEnd": 1},
                        "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "News"}],
                    },
                    {
                        "index": {"byteStart": 0, "byteEnd": 1},
                        "features": [
                            {
                                "$type": "app.bsky.richtext.facet#link",
                                "uri": "https://example.com/a?id=2",
                            }
                        ],
                    },
                ],
            },
        )
    )
    assert row.embed_kind == "external"
    assert row.embed_urls == ["https://example.com/a?id=2", "https://example.org/x"]
    assert row.mentions == ["did:plc:bob"]
    assert row.tags == ["news"]


def test_reply():
    parent = "at://did:plc:p/app.bsky.feed.post/1"
    root = "at://did:plc:r/app.bsky.feed.post/0"
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "I love this show, such a hidden gem.",
                "createdAt": "2026-11-13T19:32:02Z",
                "reply": {
                    "parent": {"uri": parent, "cid": "c1"},
                    "root": {"uri": root, "cid": "c0"},
                },
            },
        )
    )
    assert row.reply_to == parent and row.reply_root == root


def test_quote_and_record_with_media():
    q = "at://did:plc:q/app.bsky.feed.post/9"
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "lol",
                "createdAt": "2026-11-13T19:32:02Z",
                "embed": {"$type": "app.bsky.embed.record", "record": {"uri": q, "cid": "c"}},
            },
        )
    )
    assert row.embed_kind == "record" and row.quote_uri == q
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {
                "text": "lol",
                "createdAt": "2026-11-13T19:32:02Z",
                "embed": {
                    "$type": "app.bsky.embed.recordWithMedia",
                    "record": {"$type": "app.bsky.embed.record", "record": {"uri": q, "cid": "c"}},
                    "media": {"$type": "app.bsky.embed.images", "images": [{"image": blob("m")}]},
                },
            },
        )
    )
    assert row.embed_kind == "recordWithMedia"
    assert row.quote_uri == q and row.media_hashes == ["m"]


def test_repost_like_follow():
    subj = "at://did:plc:author/app.bsky.feed.post/xyz"
    for coll, table in (("app.bsky.feed.repost", "reposts"), ("app.bsky.feed.like", "likes")):
        row = normalize(
            commit(
                coll,
                {
                    "subject": {"uri": subj, "cid": "c"},
                    "createdAt": "2026-11-13T19:32:02.908768+00:00",
                },
            )
        )
        assert isinstance(row, Action) and row.table == table
        assert row.subject_uri == subj and row.subject_did == "did:plc:author"
        assert row.created_at is not None
    row = normalize(
        commit(
            "app.bsky.graph.follow",
            {"subject": "did:plc:target", "createdAt": "2026-11-13T19:32:03Z"},
        )
    )
    assert row.table == "follows" and row.subject_did == "did:plc:target"
    assert row.subject_uri is None


def test_deletes():
    for coll, table in (
        ("app.bsky.feed.post", "posts"),
        ("app.bsky.feed.like", "likes"),
        ("app.bsky.feed.repost", "reposts"),
        ("app.bsky.graph.follow", "follows"),
    ):
        row = normalize(commit(coll, op="delete", rkey="k1"))
        assert isinstance(row, Delete)
        assert row.table == table and row.uri == f"at://{DID}/{coll}/k1"


def test_account_and_identity_events():
    row = normalize(
        {
            "did": DID,
            "time_us": T_US,
            "kind": "account",
            "account": {
                "active": False,
                "did": DID,
                "seq": 5,
                "status": "takendown",
                "time": "2026-11-13T19:32:06.412Z",
            },
        }
    )
    assert isinstance(row, AccountEvent)
    assert (row.kind, row.active, row.status, row.seq) == ("account", False, "takendown", 5)
    row = normalize(
        {
            "did": DID,
            "time_us": T_US,
            "kind": "account",
            "account": {"active": True, "did": DID, "seq": 6, "time": "x"},
        }
    )
    assert row.status == "active" and row.event_time is None
    row = normalize(
        {
            "did": DID,
            "time_us": T_US,
            "kind": "identity",
            "identity": {
                "did": DID,
                "seq": 7,
                "time": "2026-11-13T19:32:09Z",
                "handle": "new.bsky.social",
            },
        }
    )
    assert row.kind == "identity" and row.handle == "new.bsky.social"


def test_garbage_is_ignored():
    assert normalize({}) is None
    assert normalize({"did": DID, "time_us": "x", "kind": "commit"}) is None
    assert normalize(commit("app.bsky.actor.profile", {"x": 1})) is None
    assert normalize(commit("app.bsky.feed.like", {"subject": "not-a-ref"})) is None
    assert normalize(commit("app.bsky.feed.post", "not-a-dict")) is None
    row = normalize(commit("app.bsky.feed.post", {"text": "a\x00b", "createdAt": "99999-01-01"}))
    assert row.text == "ab" and row.created_at is None


def test_wrong_field_types_do_not_raise():
    # Records can skip lexicon validation; one malformed post must not crash the writer loop
    # (it would crash-loop: the restart resumes from a cursor before the same event).
    for record in (
        {"text": "x", "embed": {"$type": 5}},
        {"embed": {"$type": "app.bsky.embed.recordWithMedia", "media": {"$type": 7}}},
        {"embed": {"$type": "app.bsky.embed.images", "images": 5}},
        {"facets": 5},
        {"facets": [{"features": 3}]},
        {"facets": [{"features": [{"$type": 1}]}]},
        {"langs": 3},
        {"langs": "en"},
    ):
        row = normalize(commit("app.bsky.feed.post", record))
        assert isinstance(row, Post)
        assert row.embed_urls == [] and row.media_hashes == [] and row.langs is None


def test_helpers():
    assert parse_ts("2026-11-13T19:32:02.908768+00:00").tzinfo is not None
    assert parse_ts("1970-01-01T00:00:00Z") is None
    assert did_of("at://did:plc:x/app.bsky.feed.post/1") == "did:plc:x"
    assert did_of("at://handle.example/app.bsky.feed.post/1") is None
    assert normalize_text("🔥 BUY NOW!!! https://x.co/a @bob.bsky.social #Deal") == "buy now #deal"
    assert (
        normalize_url("HTTP://www.Example.COM:443/?b=2&a=1&utm_x=3")
        == "https://example.com?a=1&b=2"
    )
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("") is None


def test_lag_watchdog_fails_over_only_on_sustained_upstream_lag():
    from ingest.consumer import LagWatchdog

    w = LagWatchdog(max_lag_s=60, for_s=120, max_queue=5000)
    assert not w.update(90, 10, now=0)
    assert not w.update(90, 10, now=60)
    assert w.update(90, 10, now=120)  # 2 minutes behind with an empty queue -> fail over
    assert not w.update(90, 10, now=130)  # timer restarts after a failover
    assert not w.update(10, 10, now=400)  # caught up -> reset
    assert not w.update(90, 10, now=410)
    assert not w.update(90, 50_000, now=600)  # our own backlog: not the host's fault, reset
    assert not w.update(None, 0, now=700)


def test_huge_urls_and_lists_are_bounded():
    long = "https://example.com/" + "a" * 5000
    facets = [
        {
            "index": {"byteStart": 0, "byteEnd": 1},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": f"{long}{i}"}],
        }
        for i in range(40)
    ]
    row = normalize(
        commit(
            "app.bsky.feed.post",
            {"text": "x", "createdAt": "2026-11-13T19:32:02Z", "facets": facets},
        )
    )
    assert len(row.embed_urls) <= 20
    assert all(len(u) <= 1000 for u in row.embed_urls)


class _FakeWs:
    def __init__(self, frames, stop_event):
        self.frames, self.stop_event = list(frames), stop_event

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        if not self.frames:
            self.stop_event.set()
            raise TimeoutError
        return self.frames.pop(0)


def _reader(monkeypatch, frames_per_host):
    import queue

    from ingest import consumer

    q = queue.Queue()
    reader = consumer.Reader(["h1", "h2"], None, q, None)
    hosts = []

    def fake_connect(url, **kw):
        hosts.append(url.split("/")[2])
        return _FakeWs(frames_per_host.pop(0), reader.stop_event)

    monkeypatch.setattr(consumer, "ws_connect", fake_connect)
    monkeypatch.setattr(consumer.time, "sleep", lambda s: None)
    return consumer, q, reader, hosts


def test_reader_always_ends_the_queue_with_stop(monkeypatch):
    # A frame the reader can't handle kills its thread; the writer must still get STOP
    # instead of waiting on an empty queue forever.
    consumer, q, reader, _ = _reader(monkeypatch, [["[]"]])
    monkeypatch.setattr("threading.excepthook", lambda args: None)
    reader.start()
    reader.join(5)
    assert not reader.is_alive()
    assert q.get(timeout=1) is consumer.STOP


def test_failover_requested_while_disconnected_does_not_drop_the_next_host(monkeypatch):
    frame = '{"did": "did:plc:x", "time_us": 1794598323302197, "kind": "account"}'
    consumer, q, reader, hosts = _reader(monkeypatch, [[frame], [frame]])
    reader.request_failover()  # the watchdog fired during a reconnect
    reader.run()
    assert hosts == ["h1"]
    assert q.get_nowait()[1]["did"] == "did:plc:x"
