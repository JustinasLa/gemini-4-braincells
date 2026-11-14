"""The live picture index (swarm/live.py, picture.py, safety.py, store.py).

Most tests use fakes (no network, no database). The tests at the bottom run the SQL against a
disposable Postgres and are skipped unless TEST_DATABASE_URL is set (see tests/test_backend_db.py).
"""

import io
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import psycopg
import pytest
from PIL import Image

from swarm import live, picture, safety
from swarm.live import Group, LiveConfig, LiveSwarmIndex, assign_ids, build_sightings, run_pass
from swarm.model import AccountInfo, SwarmReport, SwarmSignal
from swarm.store import MemoryHashCache, PriorState, SwarmStore, stable_id

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


# --- fake pictures -------------------------------------------------------------------------------


def picture_bytes(seed: int, size=(480, 360), quality=90, fmt="JPEG") -> bytes:
    """A smooth random picture (low-frequency structure, like a photo or meme)."""
    rng = np.random.default_rng(seed)
    small = Image.fromarray(rng.integers(0, 255, (6, 8, 3), dtype=np.uint8), "RGB")
    img = small.resize((480, 360), Image.Resampling.BICUBIC).resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def flat_bytes(color="white", size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def cid_of(url: str) -> str:
    return re.search(r"/([^/@]+)@jpeg$", url).group(1)


def fake_fetch(images: dict[str, bytes]):
    calls = []

    def fetch(url: str):
        calls.append(url)
        return images.get(cid_of(url))

    fetch.calls = calls
    return fetch


def all_clean(uris):
    """An AppView that returned every asked post, without labels."""
    return {u: set() for u in uris}


def clean_vision(data):
    return ("meme", None, False)


# --- grouping by perceptual hash -----------------------------------------------------------------


def test_resized_recompressed_copies_group_and_other_pictures_do_not():
    images = {
        "a1": picture_bytes(1),
        "a2": picture_bytes(1, size=(240, 180), quality=55),  # same picture, smaller, worse
        "b1": picture_bytes(2),
        "c1": picture_bytes(3, size=(300, 600)),  # other aspect
        "w1": flat_bytes("white"),
        "w2": flat_bytes("white", size=(200, 150)),  # flat: never merged by hash
    }
    hashes = picture.hash_files([(c, "did:plc:x") for c in images], fake_fetch(images))
    groups = picture.group_files(list(images), hashes)
    assert ["a1", "a2"] in groups
    assert ["b1"] in groups and ["c1"] in groups
    assert ["w1"] in groups and ["w2"] in groups


def test_same_template_with_other_words_is_not_the_same_picture():
    """Hash-close but a region differs (another product name, another face): kept apart."""
    from PIL import ImageDraw

    def with_label(seed, text_box):
        img = Image.open(io.BytesIO(picture_bytes(seed))).convert("RGB")
        ImageDraw.Draw(img).rectangle(text_box, fill="black")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    images = {
        "t1": with_label(5, (200, 150, 260, 175)),
        "t2": with_label(5, (200, 190, 260, 215)),  # the label moved: a different picture
        "t3": picture_bytes(5),
    }
    hashes = picture.hash_files([(c, "d") for c in images], fake_fetch(images))
    a, b = hashes["t1"], hashes["t2"]
    assert picture.hamming(a.phash, b.phash) <= picture.GROUP_PHASH  # the prefilter lets it in
    assert picture.pixel_distance(a, b)[0] > picture.PIXEL_BLOCK_LIMIT
    assert picture.group_files(list(images), hashes) == [["t1"], ["t2"], ["t3"]]


def test_missing_thumbnail_stays_alone():
    images = {"a1": picture_bytes(1), "a2": picture_bytes(1, quality=60)}
    hashes = picture.hash_files([("a1", "d"), ("a2", "d"), ("gone", "d")], fake_fetch(images))
    assert hashes["gone"] is None
    assert picture.group_files(["a1", "gone", "a2"], hashes) == [["a1", "a2"], ["gone"]]


def test_thumbnail_urls():
    assert picture.thumb_url("did:plc:abc", "bafkx") == (
        "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:abc/bafkx@jpeg"
    )
    assert "feed_fullsize" in picture.full_url("did:plc:abc", "bafkx")


# --- sightings -----------------------------------------------------------------------------------


def post_row(i, did, cid="a1", text="look at this", tags=None, at=None):
    return {
        "uri": f"at://{did}/app.bsky.feed.post/p{i}",
        "did": did,
        "text": text,
        "langs": ["en"],
        "seen_at": at or T0 + timedelta(seconds=10 * i),
        "media_hashes": [cid],
        "tags": tags or [],
        "embed_kind": "images",
    }


def test_build_sightings_posts_and_boosts():
    posts = [post_row(i, f"did:plc:p{i}") for i in range(3)]
    target = posts[0]["uri"]
    quote_of_itself = {**post_row(9, "did:plc:q"), "quote_uri": target}
    g = Group(files=["a1"], posts=posts + [quote_of_itself])
    g.boosts = {
        "repost": [
            {
                "uri": "at://did:plc:r/app.bsky.feed.repost/1",
                "did": "did:plc:r",
                "subject_uri": target,
                "seen_at": T0 + timedelta(minutes=1),
            }
        ],
        "like": [
            {
                "uri": "at://did:plc:l/app.bsky.feed.like/1",
                "did": "did:plc:l",
                "subject_uri": target,
                "seen_at": T0 + timedelta(minutes=2),
            }
        ],
        "quote": [quote_of_itself],  # also carries the picture: counted once, as a post
        "reply": [
            {
                "uri": "at://did:plc:y/app.bsky.feed.post/r1",
                "did": "did:plc:y",
                "text": "lol",
                "langs": [],
                "seen_at": T0 + timedelta(minutes=3),
                "reply_to": target,
            }
        ],
    }
    s = build_sightings(g)
    kinds = [x.kind for x in s]
    assert kinds == ["post"] * 4 + ["repost", "like", "reply"]
    first = s[0]
    assert first.id == target and first.account == "did:plc:p0" and first.media_key == "a1"
    assert first.url == "https://bsky.app/profile/did:plc:p0/post/p0"
    assert first.at == T0 and first.langs == ["en"] and first.platform == "bluesky"
    assert all(x.target == target for x in s if x.kind != "post")
    reply = s[-1]
    assert reply.text == "lol" and reply.url.endswith("/post/r1")
    assert s[4].url is None and s[4].text == ""


def test_pictures_of_one_multi_image_post_are_one_group():
    posts = [post_row(i, f"did:plc:p{i}") for i in range(5)]
    for p in posts:
        p["media_hashes"] = ["x1", "x2"]
    a = Group(files=["x1"], posts=posts)
    b = Group(files=["x2"], posts=posts)
    other = Group(files=["y"], posts=posts[:3])
    merged = live.merge_same_posts([a, b, other])
    assert [g.files for g in merged] == [["x1", "x2"], ["y"]]


def test_account_info_prefers_plc_date_and_drops_bridged_age():
    row = {
        "did": "did:plc:a",
        "handle": "a.bsky.social",
        "did_created_at": T0,
        "created_at": T0 - timedelta(days=9),
        "followers": 3,
        "follows": 9,
        "posts_count": 1,
        "description": "hi",
        "has_avatar": False,
        "pds_host": "morel.us-east.host.bsky.network",
        "display_name": "A",
    }
    info = live.account_info(row)
    assert info.created_at == T0 and info.bio == "hi" and info.host.startswith("morel")
    assert info.following == 9 and info.has_avatar is False
    bridged = live.account_info({**row, "handle": "x.mastodon.social.ap.brid.gy"})
    assert bridged.created_at is None


def test_basis_note():
    assert live.basis_note(43, 24.0) == "43 posts our Bluesky feed saw in 24 h"
    assert live.basis_note(1, 0.5) == "1 post our Bluesky feed saw in 0.5 h"


# --- safety --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tags", "unsafe"),
    [
        ("new video on my page", ["nsfw"], True),
        ("hot pics 18+ t.me/xyz", [], True),
        ("link in bio #OnlyFans", [], True),
        ("Free porn here", [], True),
        ("when the cat sees the cucumber", ["cats", "meme"], False),
        ("I was escorted out lol", [], False),
        ("Sussex by the sea", [], False),
        ("", ["porn"], True),
    ],
)
def test_text_markers(text, tags, unsafe):
    assert (safety.text_marker(text, tags) is not None) is unsafe


def test_judge_labels_and_vision():
    clean = [("a meme", ["meme"])]
    assert safety.judge(clean, [set()]).safe
    assert safety.judge(clean, [{"spam"}]).safe  # spam is the point of the wall
    s = safety.judge(clean, [{"porn"}])
    assert not s.safe and s.reason == "label porn"
    assert not safety.judge(clean, [], vision_adult=True).safe
    assert not safety.judge([("x", ["nsfw"])], []).safe


def test_fetch_labels_reads_post_author_and_self_labels():
    def handler(request: httpx.Request):
        uris = request.url.params.get_list("uris")
        posts = [
            {"uri": uris[0], "labels": [{"val": "porn"}], "author": {"labels": []}},
            {"uri": uris[1], "labels": [], "author": {"labels": [{"val": "sexual"}]}},
            {
                "uri": uris[2],
                "labels": [],
                "author": {},
                "record": {"labels": {"values": [{"val": "nudity"}]}},
            },
            {"uri": uris[3], "labels": [], "author": {}},
        ]
        return httpx.Response(200, json={"posts": posts})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = safety.fetch_labels(["u1", "u2", "u3", "u4"], client)
    assert found == {"u1": {"porn"}, "u2": {"sexual"}, "u3": {"nudity"}, "u4": set()}


# --- stable ids ----------------------------------------------------------------------------------


def test_stable_id_is_short_and_deterministic():
    assert stable_id("bafkreia") == stable_id("bafkreia")
    assert stable_id("bafkreia") != stable_id("bafkreib")
    assert re.fullmatch(r"[0-9a-f]{12}", stable_id("bafkreia"))


def _prior(gid, files_created):
    return PriorState(gid, True, None, None, None, None, files_created)


def test_assign_ids_reuses_stored_group_and_retires_merged_one():
    old = _prior("old", T0 - timedelta(hours=3))
    newer = _prior("newer", T0 - timedelta(hours=1))
    g = Group(files=["x2", "x1"], posts=[post_row(0, "did:plc:a")])
    fresh = Group(files=["y1"], posts=[post_row(1, "did:plc:b")])
    retired = assign_ids([g, fresh], {"x1": [old], "x2": [newer]})
    assert g.id == "old" and g.prior is old
    assert retired == ["newer"]
    assert fresh.id == stable_id("y1")


def test_assign_ids_without_history_uses_earliest_file():
    g = Group(files=["first", "second"], posts=[post_row(0, "did:plc:a")])
    assert assign_ids([g], {}) == [] and g.id == stable_id("first")


# --- a whole pass on a fake read-only connection -------------------------------------------------


class FakeCursor:
    def __init__(self, conn, dict_rows):
        self.conn, self.dict_rows, self._rows = conn, dict_rows, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._rows = self.conn.answer(sql, params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Answers the pass's read queries from in-memory rows; any write fails the test."""

    autocommit = True
    closed = False

    def __init__(self, posts, boosts=None, accounts=None):
        self.posts, self.boosts, self.accounts = posts, boosts or {}, accounts or {}
        self.sql: list[str] = []

    def cursor(self, row_factory=None):
        return FakeCursor(self, row_factory is not None and "dict" in row_factory.__name__)

    def execute(self, sql, params=None):
        return FakeCursor(self, False).execute(sql, params)

    def transaction(self):
        raise AssertionError("a dry run must not open a write transaction")

    def answer(self, sql, params):
        self.sql.append(sql)
        s = " ".join(sql.split())
        if re.match(r"(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", s.strip(), re.I):
            raise AssertionError(f"write in a dry run: {s[:80]}")
        if "ingest_state" in s or "pg_inherits" in s:
            return [(None,)]
        if "baseline_accounts" in s:
            return [(0, 0, 0, 0)]
        if "unnest(p.media_hashes)" in s:
            by = {}
            for p in self.posts:
                for h in p["media_hashes"]:
                    by.setdefault(h, []).append(p)
            out = []
            for h, ps in by.items():
                dids = {p["did"] for p in ps}
                if len(dids) >= params["min"]:
                    first = min(ps, key=lambda p: p["seen_at"])
                    out.append(
                        {
                            "cid": h,
                            "n_accounts": len(dids),
                            "n_posts": len(ps),
                            "first_at": first["seen_at"],
                            "did": first["did"],
                        }
                    )
            return sorted(out, key=lambda r: -r["n_accounts"])[: params["limit"]]
        if "media_hashes && " in s:
            want = set(params["cids"])
            return [p for p in self.posts if want & set(p["media_hashes"])]
        for kind, table in (
            ("repost", "FROM reposts"),
            ("like", "FROM likes"),
            ("quote", "quote_uri = ANY"),
            ("reply", "reply_to = ANY"),
        ):
            if table in s:
                return self.boosts.get(kind, [])
        if "FROM accounts" in s:
            return [self.accounts[d] for d in params[0] if d in self.accounts]
        if "FROM meme_swarms" in s or "FROM picture_hashes" in s:
            return []  # nothing stored yet
        raise AssertionError(f"unexpected query: {s[:120]}")


def fake_scorer(calls=None):
    def score(
        sightings,
        accounts=None,
        *,
        basis="sample",
        basis_note="",
        baseline=None,
        cfg=None,
        window=None,
        selected_by_file=False,
    ):
        if calls is not None:
            calls.append(
                {
                    "sightings": sightings,
                    "accounts": accounts,
                    "basis": basis,
                    "basis_note": basis_note,
                    "window": window,
                    "selected_by_file": selected_by_file,
                }
            )
        posts = [s for s in sightings if s.kind == "post"]
        n_acc = len({s.account for s in posts})
        texts = {s.text for s in posts}
        swarm = len(texts) == 1 and n_acc >= 5
        return SwarmReport(
            verdict="swarm" if swarm else "organic",
            score=0.9 if swarm else 0.1,
            families={"copy": 1.0 if swarm else 0.0},
            signals=[
                SwarmSignal(
                    family="copy",
                    name="same_text",
                    score=1.0,
                    summary=f"{n_acc} accounts · same words",
                )
            ]
            if swarm
            else [],
            n_posts=len(posts),
            n_accounts=n_acc,
            basis=basis,
            basis_note=basis_note,
            first_at=min(s.at for s in posts),
            last_at=max(s.at for s in posts),
        )

    return score


def world():
    """A spam picture (6 accounts, same words, two files of the same picture), an organic one
    (6 accounts, own words) and an adult one (5 accounts, #nsfw)."""
    posts = []
    for i in range(4):
        posts.append(post_row(i, f"did:plc:s{i}", "spamA", "Buy now at shop.example"))
    for i in range(4, 6):
        posts.append(post_row(i, f"did:plc:s{i}", "spamB", "Buy now at shop.example"))
    for i in range(6):
        posts.append(
            post_row(
                10 + i,
                f"did:plc:o{i}",
                "meme",
                f"my own words number {i}",
                at=T0 + timedelta(hours=i),
            )
        )
    for i in range(5):
        posts.append(post_row(20 + i, f"did:plc:n{i}", "adult", "new pics", tags=["nsfw"]))
    images = {
        "spamA": picture_bytes(11),
        "spamB": picture_bytes(11, size=(300, 225), quality=60),
        "meme": picture_bytes(12),
        "adult": picture_bytes(13),
    }
    return posts, images


def test_dry_run_writes_nothing_and_groups_files(capsys):
    posts, images = world()
    conn = FakeConn(posts)
    calls = []
    res = run_pass(
        conn,
        LiveConfig(min_accounts=5, file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(calls),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    by_files = {tuple(g.files): g for g in res.groups}
    assert set(by_files) == {("spamA", "spamB"), ("meme",), ("adult",)}
    spam = by_files[("spamA", "spamB")]
    assert spam.report.verdict == "swarm" and len(spam.dids) == 6
    assert spam.id == stable_id("spamA") and spam.safe
    assert spam.rep_cid == "spamA"  # most accounts
    assert not by_files[("adult",)].safe and by_files[("adult",)].unsafe_reason == "tag #nsfw"
    assert by_files[("meme",)].report.verdict == "organic"
    assert all(c["basis"] == "live" for c in calls)
    assert all(
        c["selected_by_file"] is True and c["window"][1] == T0 + timedelta(hours=12) for c in calls
    )
    assert any(c["basis_note"] == "6 posts our Bluesky feed saw in 24 h" for c in calls)
    assert res.stats["unsafe"] == 1 and res.stats["groups"] == 3
    assert "candidates_s" in res.stats["timings"] and "total_s" in res.stats["timings"]
    line = live.describe(spam)
    assert "swarm" in line and "acc=6" in line and "feed_thumbnail" in line and "Buy now" in line


def test_dry_run_with_labels_hides_labelled_group():
    posts, images = world()
    meme_uris = {p["uri"] for p in posts if p["media_hashes"] == ["meme"]}
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=lambda uris: {u: ({"porn"} if u in meme_uris else set()) for u in uris},
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    meme = next(g for g in res.groups if g.files == ["meme"])
    assert not meme.safe and meme.unsafe_reason == "label porn"


def test_failed_label_check_is_not_safe_for_new_groups():
    posts, images = world()

    def broken(uris):
        raise httpx.ConnectError("down")

    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=broken,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    assert not any(g.safe for g in res.groups)


def by_bytes(images, answers):
    """A fake vision reader: answer by which test picture it was given; records the reads."""
    reads = []

    def vision(data):
        cid = next(c for c, b in images.items() if b == data)
        reads.append(cid)
        return answers(cid)

    vision.reads = reads
    return vision


def test_vision_reads_round_robin_and_holds_unread_groups():
    posts, images = world()
    vision = by_bytes(
        images, lambda cid: ("ad", "BUY NOW", False) if cid == "spamA" else ("photo", None, True)
    )
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2, vision_budget=1),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    by = {g.files[0]: g for g in res.groups}
    assert vision.reads == ["spamA"]  # round-robin starts with the swarm verdicts
    assert by["spamA"].safe and by["spamA"].kind == "ad" and by["spamA"].caption == "BUY NOW"
    assert by["spamA"].vision_cid == "spamA"
    assert not by["meme"].safe and by["meme"].hold == "vision pending"
    assert by["meme"].unsafe_reason is None  # not sticky: it is read on a later pass
    assert res.stats["wall"] == 1 and res.stats["pending"] == 1 and res.stats["unsafe"] == 1

    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2, vision_budget=5),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    meme = next(g for g in res.groups if g.files == ["meme"])
    assert not meme.safe and meme.unsafe_reason == "vision: adult" and meme.caption is None


def test_vision_order_is_round_robin_over_verdicts_largest_first():
    def grp(name, verdict, n):
        g = Group(files=[name], posts=[post_row(i, f"did:plc:{name}{i}") for i in range(n)])
        g.id, g.report = name, SwarmReport(verdict=verdict, score=1.0)
        return g

    gs = [grp("s1", "swarm", 9), grp("s2", "swarm", 20), grp("s3", "swarm", 5)]
    gs += [grp("o1", "organic", 6), grp("o2", "organic", 7), grp("u1", "unclear", 8)]
    assert [g.id for g in live.vision_order(gs)] == ["s2", "o2", "u1", "s1", "o1", "s3"]


def test_no_vision_reader_holds_everything_unless_opted_in():
    posts, images = world()
    kw = dict(
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    res = run_pass(FakeConn(posts), LiveConfig(file_min_accounts=2), **kw)
    assert not any(g.safe for g in res.groups) and res.stats["wall"] == 0
    assert {g.hold for g in res.groups if not g.unsafe_reason} == {"vision pending"}
    res = run_pass(FakeConn(posts), LiveConfig(file_min_accounts=2, wall_without_vision=True), **kw)
    assert res.stats["wall"] == 2  # the adult one stays unsafe


def test_vision_stops_for_the_pass_after_a_timeout():
    posts, images = world()
    calls = []

    def slow(data):
        calls.append(1)
        raise TimeoutError("read timed out")

    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2, vision_budget=5),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=slow,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    assert len(calls) == 1 and res.stats["wall"] == 0


def test_caption_with_adult_words_makes_the_group_unsafe():
    posts, images = world()
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=lambda data: ("ad", "SUBSCRIBE ON ONLYFANS", False),
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    spam = next(g for g in res.groups if g.files[0] == "spamA")
    assert not spam.safe and spam.unsafe_reason.startswith("caption") and spam.caption is None


def test_deadline_stops_hashing_labels_and_vision():
    posts, images = world()
    fetch = fake_fetch(images)
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2, deadline_s=0),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fetch,
        labels=all_clean,
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    assert fetch.calls == [] and res.stats["wall"] == 0
    # nothing hashed or label-checked: nothing safe, so no vision read was even queued
    assert res.stats["late"] == ["hashes", "labels"]


def test_label_outage_holds_groups_with_the_real_fetch_labels():
    """fetch_labels never raises on a network error: what it did not check must not be safe."""
    posts, images = world()

    def down(request):
        raise httpx.ConnectError("AppView down")

    client = httpx.Client(transport=httpx.MockTransport(down))
    got = safety.fetch_labels(["at://x/app.bsky.feed.post/1"], client)
    assert got == {} and got.failed == {"at://x/app.bsky.feed.post/1"}
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=lambda uris: safety.fetch_labels(uris, client),
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    assert not any(g.safe for g in res.groups)
    assert {g.hold for g in res.groups if not g.unsafe_reason} == {"labels unchecked"}
    assert not any(g.labels_checked for g in res.groups)


def test_posts_missing_on_the_appview_hold_the_group():
    posts, images = world()
    gone = posts[0]["uri"]  # a spam post the AppView no longer returns (taken down)
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=lambda uris: {u: set() for u in uris if u != gone},
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    by = {g.files[0]: g for g in res.groups}
    assert not by["spamA"].safe and by["spamA"].hold == "posts missing on AppView"
    assert by["meme"].safe


def test_many_taken_down_accounts_hold_the_group_and_their_posts_are_not_listed():
    posts, images = world()
    accounts = {
        f"did:plc:s{i}": {"did": f"did:plc:s{i}", "status": "takendown" if i < 2 else None}
        for i in range(6)
    }
    res = run_pass(
        FakeConn(posts, accounts=accounts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    spam = next(g for g in res.groups if g.files[0] == "spamA")
    assert not spam.safe and spam.hold == "accounts taken down" and not spam.unsafe_reason
    listed = live.swarm_posts(spam, {}, {"did:plc:s0", "did:plc:s1"})
    assert len(listed) == 4 and not any("did:plc:s0/" in p.url for p in listed)


def test_boosts_are_capped_per_group():
    posts, images = world()
    target = posts[0]["uri"]
    likes = [
        {
            "uri": f"at://did:plc:f{i}/app.bsky.feed.like/{i}",
            "did": f"did:plc:f{i}",
            "subject_uri": target,
            "seen_at": T0 + timedelta(seconds=100 + i),
        }
        for i in range(30)
    ]
    res = run_pass(
        FakeConn(posts, boosts={"like": likes}),
        LiveConfig(file_min_accounts=2, likes_per_group=10),
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fake_fetch(images),
        labels=all_clean,
        vision=clean_vision,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    spam = next(g for g in res.groups if g.files[0] == "spamA")
    assert [r["uri"] for r in spam.boosts["like"]] == [r["uri"] for r in likes[:10]]
    assert res.stats["boosts"]["like"] == 30


def test_split_never_reuses_an_id_in_the_same_pass():
    """A stored 2-picture group splits; the larger part keeps its id, the other gets a new one."""
    old = PriorState(stable_id("x1"), True, None, None, None, T0, T0 - timedelta(hours=1))
    x2 = Group(files=["x2"], posts=[post_row(i, f"did:plc:b{i}") for i in range(8)])
    x1 = Group(files=["x1"], posts=[post_row(i, f"did:plc:a{i}") for i in range(5)])
    retired = assign_ids([x2, x1], {"x1": [old], "x2": [old]})
    assert x2.id == stable_id("x1") and x2.prior is old
    assert x1.id not in (x2.id, "") and x1.priors == [old] and retired == []


def test_examples_of_account_and_boost_signals_are_never_served():
    from swarm.store import detail_from_row, public_report

    sig = lambda fam: SwarmSignal(  # noqa: E731
        family=fam, name="n", score=1.0, summary="s", examples=["at://did:plc:a/x/1"]
    )
    report = SwarmReport(
        verdict="swarm", signals=[sig("accounts"), sig("boost"), sig("timing"), sig("copy")]
    )
    clean = public_report(report)
    assert [bool(s.examples) for s in clean.signals] == [False, False, True, True]
    row = {
        "id": "g",
        "rep_did": "did:plc:x",
        "rep_cid": "c",
        "verdict": "swarm",
        "score": 1.0,
        "n_posts": 1,
        "n_accounts": 1,
        "report": report.model_dump(mode="json"),
    }
    served = detail_from_row(row)
    assert [bool(s.examples) for s in served.report.signals] == [False, False, True, True]


def test_wall_reader_parses_the_reply_and_rejects_other_models():
    from types import SimpleNamespace

    from media.nemotron import NotVisionModelError

    def client(model, content):
        msg = SimpleNamespace(content=content)
        resp = SimpleNamespace(
            model=model, choices=[SimpleNamespace(message=msg, finish_reason="stop")]
        )
        create = lambda **kw: resp  # noqa: E731
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    ok = '{"kind": "meme", "caption": "when the code compiles", "adult": false, "graphic": false}'
    reader = safety.WallReader(
        "k", base_url="u", model="m", client=client("nemotron-3.5-super-vl", ok)
    )
    assert reader(picture_bytes(1)) == ("meme", "when the code compiles", False)
    sent = []
    spy = client("nemotron-3.5-super-vl", ok)
    real_create = spy.chat.completions.create
    spy.chat.completions.create = lambda **kw: sent.append(kw) or real_create(**kw)
    safety.WallReader("k", base_url="u", model="m", client=spy)(picture_bytes(1))
    texts = [
        p["text"]
        for m in sent[0]["messages"]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p["type"] == "text"
    ]
    assert texts == ["Read this picture and return the JSON object."]  # no post text
    words = '{"kind": "ad", "caption": "xxx hot milfs", "adult": false, "graphic": false}'
    reader = safety.WallReader(
        "k", base_url="u", model="m", client=client("nemotron-3.5-super-vl", words)
    )
    assert reader(picture_bytes(1)) == ("ad", None, True)
    bait = '{"kind": "photo", "caption": "", "adult": true}'
    reader = safety.WallReader(
        "k", base_url="u", model="m", client=client("nemotron-3.5-super-vl", bait)
    )
    assert reader(picture_bytes(1)) == ("photo", None, True)
    unsure = '{"kind": "photo", "caption": "x"}'  # no adult answer: not safe
    reader = safety.WallReader(
        "k", base_url="u", model="m", client=client("nemotron-3.5-super-vl", unsure)
    )
    assert reader(picture_bytes(1))[2] is True
    reader = safety.WallReader("k", base_url="u", model="m", client=client("llama-3-70b", ok))
    with pytest.raises(NotVisionModelError):
        reader(picture_bytes(1))


def test_hash_cache_fetches_each_file_once():
    posts, images = world()
    fetch = fake_fetch(images)
    cache = MemoryHashCache()
    kw = dict(
        dry_run=True,
        scorer=fake_scorer(),
        fetch=fetch,
        labels=all_clean,
        enrich=False,
        hash_cache=cache,
        end=T0 + timedelta(hours=12),
    )
    run_pass(FakeConn(posts), LiveConfig(file_min_accounts=2), **kw)
    n = len(fetch.calls)
    run_pass(FakeConn(posts), LiveConfig(file_min_accounts=2), **kw)
    assert n == 4 and len(fetch.calls) == n


def test_accounts_reach_the_scorer():
    posts, images = world()
    accounts = {
        f"did:plc:s{i}": {
            "did": f"did:plc:s{i}",
            "handle": f"s{i}.bsky.social",
            "did_created_at": T0 - timedelta(days=2),
            "created_at": None,
            "display_name": None,
            "followers": 0,
            "follows": 0,
            "posts_count": 1,
            "description": "",
            "has_avatar": False,
            "pds_host": None,
            "enriched": i < 5,
        }
        for i in range(6)
    }
    calls = []
    run_pass(
        FakeConn(posts, accounts=accounts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=fake_scorer(calls),
        fetch=fake_fetch(images),
        labels=all_clean,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    spam_call = next(c for c in calls if len(c["accounts"]) > 0)
    assert len(spam_call["accounts"]) == 5  # the unenriched one is unknown, not empty
    assert isinstance(next(iter(spam_call["accounts"].values())), AccountInfo)


def test_cli_dry_run_opens_read_only(monkeypatch, capsys):
    from swarm import __main__ as cli

    seen = {}

    def fake_open(url, *, read_only):
        seen["read_only"] = read_only
        posts, _ = world()
        return FakeConn(posts)

    FakeConn.__enter__ = lambda self: self
    FakeConn.__exit__ = lambda self, *a: False
    monkeypatch.setattr(cli, "open_connection", fake_open)
    monkeypatch.setattr(cli, "nemotron_reader", lambda: None)
    posts, images = world()
    real = cli.run_pass

    def run(conn, cfg, **kw):
        kw.update(
            scorer=fake_scorer(),
            fetch=fake_fetch(images),
            enrich=False,
            end=T0 + timedelta(hours=12),
        )
        return real(conn, cfg, **kw)

    monkeypatch.setattr(cli, "run_pass", run)
    cli.main(
        [
            "once",
            "--dry-run",
            "--no-labels",
            "--file-min-accounts",
            "2",
            "--database-url",
            "postgresql:///x",
        ]
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert seen["read_only"] is True
    assert len(out) == 4 and out[-1].startswith("{")  # 3 groups + the stats line


def test_open_connection_sets_read_only_option(monkeypatch):
    from swarm import __main__ as cli

    got = {}
    monkeypatch.setattr(cli.psycopg, "connect", lambda url, **kw: got.update(kw) or "conn")
    cli.open_connection("postgresql:///x", read_only=True)
    assert "-c default_transaction_read_only=on" in got["options"]
    assert "-c statement_timeout=30s" in got["options"] and "-c lock_timeout=3s" in got["options"]
    got.clear()
    cli.open_connection("postgresql:///x", read_only=False)
    assert "read_only" not in got["options"] and "statement_timeout" in got["options"]


def test_duration_parsing():
    from swarm.__main__ import duration_h

    assert duration_h("24h") == 24 and duration_h("90m") == 1.5 and duration_h("2d") == 48
    assert duration_h("6") == 6


# --- the real scorer, when it is there -----------------------------------------------------------


def test_real_scorer_scores_a_live_group():
    score_mod = pytest.importorskip("swarm.score")
    posts, images = world()
    res = run_pass(
        FakeConn(posts),
        LiveConfig(file_min_accounts=2),
        dry_run=True,
        scorer=score_mod.score_swarm,
        fetch=fake_fetch(images),
        labels=all_clean,
        enrich=False,
        end=T0 + timedelta(hours=12),
    )
    assert len(res.groups) == 3
    for g in res.groups:
        assert g.report.basis == "live" and g.report.n_accounts == len(g.dids)
        assert g.report.verdict in ("swarm", "organic", "unclear")


# --- SQL against a real database -----------------------------------------------------------------

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def db():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for f in [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]:
            c.execute(f.read_text())
        now = datetime.now(UTC).replace(microsecond=0)
        c.execute("SELECT noise_ensure_partitions(%s, %s)", (now - timedelta(hours=3), now))
        posts, images = world()
        base = now - timedelta(minutes=50)
        for p in posts:
            p["seen_at"] = base + (p["seen_at"] - T0) / 12  # squeeze 6 h into 30 min
            c.execute("INSERT INTO accounts (did) VALUES (%s) ON CONFLICT DO NOTHING", (p["did"],))
            c.execute(
                """INSERT INTO posts (uri, did, text, langs, seen_at, embed_kind, media_hashes,
                       tags) VALUES (%s, %s, %s, %s, %s, 'images', %s, %s)""",
                (
                    p["uri"],
                    p["did"],
                    p["text"],
                    p["langs"],
                    p["seen_at"],
                    p["media_hashes"],
                    p["tags"],
                ),
            )
        target = posts[0]["uri"]
        c.execute("INSERT INTO accounts (did) VALUES ('did:plc:fan') ON CONFLICT DO NOTHING")
        c.execute(
            "INSERT INTO reposts (uri, did, subject_uri, seen_at) VALUES (%s, %s, %s, %s)",
            (
                "at://did:plc:fan/app.bsky.feed.repost/1",
                "did:plc:fan",
                target,
                posts[0]["seen_at"] + timedelta(seconds=5),
            ),
        )
        c.execute(
            "INSERT INTO likes (uri, did, subject_uri, seen_at) VALUES (%s, %s, %s, %s)",
            (
                "at://did:plc:fan/app.bsky.feed.like/1",
                "did:plc:fan",
                target,
                posts[0]["seen_at"] + timedelta(seconds=6),
            ),
        )
        # a deleted post and a video don't count
        c.execute(
            """INSERT INTO posts (uri, did, seen_at, embed_kind, media_hashes, deleted_at)
               VALUES ('at://did:plc:s0/app.bsky.feed.post/del', 'did:plc:s0', %s, 'images',
                       '{deleted}', %s)""",
            (base, base),
        )
        yield c, images, posts


def _count(c, table):
    return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@needs_db
def test_db_dry_run_on_read_only_connection_writes_nothing(db):
    c, images, _ = db
    before = (_count(c, "meme_swarms"), _count(c, "picture_hashes"))
    from swarm.__main__ import open_connection

    with open_connection(DSN, read_only=True) as ro:
        res = run_pass(
            ro,
            LiveConfig(file_min_accounts=2),
            dry_run=True,
            scorer=fake_scorer(),
            fetch=fake_fetch(images),
            labels=all_clean,
            enrich=False,
        )
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("INSERT INTO picture_hashes (cid, did) VALUES ('x', 'y')")
    assert len(res.groups) == 3
    spam = next(g for g in res.groups if g.files == ["spamA", "spamB"])
    assert len(spam.boosts["repost"]) == 1 and len(spam.boosts["like"]) == 1
    assert (_count(c, "meme_swarms"), _count(c, "picture_hashes")) == before == (0, 0)


def kind_vision(images):
    """spam picture -> an ad, the meme -> a meme, anything else -> adult."""
    kinds = {"spamA": "ad", "spamB": "ad", "meme": "meme"}
    return by_bytes(images, lambda cid: (kinds.get(cid, "photo"), None, cid not in kinds))


@needs_db
def test_db_pass_stores_groups_with_stable_ids_and_serves_them(db):
    c, images, posts = db
    fetch = fake_fetch(images)
    vision = kind_vision(images)
    kw = dict(scorer=fake_scorer(), fetch=fetch, labels=all_clean, vision=vision, enrich=False)
    first = run_pass(c, LiveConfig(file_min_accounts=2), **kw)
    assert first.stats["stored"] == 3 and _count(c, "picture_hashes") == 4
    ids = {tuple(g.files): g.id for g in first.groups}
    second = run_pass(c, LiveConfig(file_min_accounts=2), **kw)
    assert {tuple(g.files): g.id for g in second.groups} == ids
    assert len(fetch.calls) == 4 + 2  # hashes from the table; 2 vision reads in pass 1 only
    assert sorted(vision.reads) == ["meme", "spamA"]

    store = SwarmStore(c)
    wall = store.wall()
    assert [i.kind for i in wall.items] == ["meme", "ad"]  # memes first; adult one not listed
    spam_id = ids[("spamA", "spamB")]
    assert wall.items[1].id == spam_id and wall.items[1].headline == "6 accounts · same words"
    assert wall.items[1].thumb.endswith("/spamA@jpeg") and wall.window_h
    detail = store.detail(spam_id)
    assert detail.picture.startswith("https://cdn.bsky.app/img/feed_fullsize/")
    assert len(detail.posts) == 6 and detail.posts[0].url.startswith("https://bsky.app/profile/")
    adult_id = ids[("adult",)]
    assert store.detail(adult_id) is None and store.detail(adult_id, include_unsafe=True)
    assert store.wall(verdict="organic").items[0].verdict == "organic"
    assert store.picture_source(spam_id) == (posts[0]["did"], "spamA")
    assert store.picture_source(adult_id) is None

    # the lookup's read side: no network (fetch is not called again)
    index = LiveSwarmIndex(lambda: psycopg.connect(DSN))
    n_fetch = len(fetch.calls)
    assert index.find_by_cids(["spamB"]).id == spam_id
    assert index.find_by_cids(["adult"]) is None
    upload = picture_bytes(11, size=(640, 480), quality=70)
    t0 = time.monotonic()
    assert index.find_by_picture(upload).id == spam_id
    assert index.find_by_picture(picture_bytes(11, size=(320, 240), quality=40)).id == spam_id
    assert time.monotonic() - t0 < 0.5
    assert index.find_by_picture(picture_bytes(99)) is None
    assert index.find_by_picture(b"not an image") is None
    assert len(fetch.calls) == n_fetch


@needs_db
def test_db_unsafe_stays_unsafe_and_stale_groups_are_dropped(db):
    c, images, posts = db
    meme_uris = {p["uri"] for p in posts if p["media_hashes"] == ["meme"]}
    kw = dict(scorer=fake_scorer(), fetch=fake_fetch(images), enrich=False)
    kw["vision"] = clean_vision
    run_pass(
        c,
        LiveConfig(file_min_accounts=2),
        labels=lambda us: {u: ({"porn"} if u in meme_uris else set()) for u in us},
        **kw,
    )
    run_pass(c, LiveConfig(file_min_accounts=2), labels=all_clean, **kw)
    row = c.execute("SELECT safe, unsafe_reason FROM meme_swarms WHERE files = '{meme}'").fetchone()
    assert row == (False, "label porn")
    c.execute("UPDATE meme_swarms SET updated_at = now() - interval '7 hours'")
    assert SwarmStore(c).drop_stale(6) == 3 and _count(c, "meme_swarms") == 0
    # the file keeps its mark: the picture coming back is unsafe at once
    res = run_pass(c, LiveConfig(file_min_accounts=2), labels=all_clean, **kw)
    meme = next(g for g in res.groups if g.files == ["meme"])
    assert not meme.safe and meme.unsafe_reason == "label porn"


# --- merges, splits, rep changes, freshness: each on its own empty schema -------------------------


@pytest.fixture
def empty_db():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for f in [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]:
            c.execute(f.read_text())
        now = datetime.now(UTC).replace(microsecond=0)
        c.execute("SELECT noise_ensure_partitions(%s, %s)", (now - timedelta(hours=3), now))
        yield c, now - timedelta(minutes=50)


def add_post(c, did, rkey, at, files, text="look at this"):
    c.execute("INSERT INTO accounts (did) VALUES (%s) ON CONFLICT DO NOTHING", (did,))
    uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    c.execute(
        """INSERT INTO posts (uri, did, text, langs, seen_at, embed_kind, media_hashes, tags)
           VALUES (%s, %s, %s, '{en}', %s, 'images', %s, '{}')""",
        (uri, did, text, at, list(files)),
    )
    return uri


@needs_db
def test_db_merge_keeps_unsafe_of_the_group_it_retires(empty_db):
    """S (file A, safe, read) and U (file C, same picture, labelled porn; its thumbnail failed
    once) merge: U's id is retired, but the merged group stays unsafe."""
    c, base = empty_db
    add_post(c, "did:plc:a0", "a", base, ["A"])
    for i in range(15):
        add_post(c, f"did:plc:b{i}", "b", base + timedelta(minutes=20, seconds=i), ["A"])
    img = {"A": picture_bytes(11), "C": picture_bytes(11, size=(300, 225), quality=60)}
    c_uris: set[str] = set()
    labels = lambda us: {u: ({"porn"} if u in c_uris else set()) for u in us}  # noqa: E731
    kw = dict(scorer=fake_scorer(), labels=labels, vision=clean_vision, enrich=False)
    cfg = LiveConfig(file_min_accounts=3)
    run_pass(c, cfg, fetch=fake_fetch(img), hash_cache=MemoryHashCache(), **kw)
    for i in range(5):
        at = base + timedelta(minutes=10, seconds=i)
        c_uris.add(add_post(c, f"did:plc:c{i}", "c", at, ["C"]))
    run_pass(c, cfg, fetch=fake_fetch({"A": img["A"]}), hash_cache=MemoryHashCache(), **kw)
    rows = c.execute("SELECT files, safe, unsafe_reason FROM meme_swarms ORDER BY created_at")
    assert rows.fetchall() == [(["A"], True, None), (["C"], False, "label porn")]
    res = run_pass(c, cfg, fetch=fake_fetch(img), hash_cache=MemoryHashCache(), **kw)
    assert res.stats["retired"] == 1
    rows = c.execute("SELECT files, safe, unsafe_reason FROM meme_swarms").fetchall()
    assert rows == [(["A", "C"], False, "label porn")]
    assert SwarmStore(c).wall().items == []
    marks = c.execute("SELECT cid, unsafe_reason FROM picture_hashes ORDER BY cid").fetchall()
    assert marks == [("A", "label porn"), ("C", "label porn")]


def _three_picture_post(c, base, files):
    for i in range(5):
        add_post(c, f"did:plc:m{i}", "m", base + timedelta(seconds=i), files)


@needs_db
def test_db_rep_change_after_a_vision_read_needs_a_new_read(empty_db):
    """A 3-picture post is one group, vision reads its rep X1. Then more accounts post X2 alone:
    the X2 group keeps the stored id, but X2 was never read, so it is read now (adult here)."""
    c, base = empty_db
    files = ["X1", "X2", "X3"]
    img = {f: picture_bytes(30 + i) for i, f in enumerate(files)}
    vision = by_bytes(img, lambda cid: ("photo", None, cid == "X2"))
    kw = dict(scorer=fake_scorer(), labels=all_clean, vision=vision, enrich=False)
    kw.update(fetch=fake_fetch(img), hash_cache=MemoryHashCache())
    _three_picture_post(c, base, files)
    run_pass(c, LiveConfig(file_min_accounts=3), **kw)
    assert vision.reads == ["X1"]
    for i in range(3):
        add_post(c, f"did:plc:x{i}", "x", base + timedelta(minutes=5, seconds=i), ["X2"])
    res = run_pass(c, LiveConfig(file_min_accounts=3), **kw)
    x2 = next(g for g in res.groups if g.files == ["X2"])
    assert vision.reads == ["X1", "X2"] and not x2.safe and x2.unsafe_reason == "vision: adult"
    thumbs = [i.thumb.rsplit("/", 1)[1] for i in SwarmStore(c).wall().items]
    assert thumbs == ["X1@jpeg"]


@needs_db
def test_db_rep_change_without_budget_is_held(empty_db):
    c, base = empty_db
    files = ["X1", "X2"]
    img = {f: picture_bytes(30 + i) for i, f in enumerate(files)}
    vision = by_bytes(img, lambda cid: ("photo", None, False))
    kw = dict(scorer=fake_scorer(), labels=all_clean, vision=vision, enrich=False)
    kw.update(fetch=fake_fetch(img), hash_cache=MemoryHashCache())
    _three_picture_post(c, base, files)
    run_pass(c, LiveConfig(file_min_accounts=3), **kw)
    for i in range(3):
        add_post(c, f"did:plc:x{i}", "x", base + timedelta(minutes=5, seconds=i), ["X2"])
    res = run_pass(c, LiveConfig(file_min_accounts=3, vision_budget=0), **kw)
    ids = [g.id for g in res.groups]
    assert len(set(ids)) == len(ids) == 2  # the split never stores two groups under one id
    x2 = next(g for g in res.groups if g.files == ["X2"])
    assert not x2.safe and x2.hold == "vision pending"
    assert _count(c, "meme_swarms") == 2


@needs_db
def test_db_wall_skips_near_duplicates_and_stale_groups(empty_db):
    c, base = empty_db
    img = {
        "ad1": picture_bytes(11),
        "ad2": picture_bytes(11, size=(480, 400)),  # another aspect: never merged, hashes alike
        "m1": picture_bytes(12),
    }
    for f in img:
        for i in range(5):
            add_post(c, f"did:plc:{f}{i}", f, base + timedelta(seconds=i), [f])
    kinds = {"ad1": "ad", "ad2": "ad", "m1": "meme"}
    vision = by_bytes(img, lambda cid: (kinds[cid], None, False))
    res = run_pass(
        c,
        LiveConfig(file_min_accounts=3),
        scorer=fake_scorer(),
        fetch=fake_fetch(img),
        labels=all_clean,
        vision=vision,
        enrich=False,
    )
    assert len(res.groups) == 3 and res.stats["wall"] == 3
    store = SwarmStore(c)
    items = store.wall().items
    assert [i.kind for i in items] == ["meme", "ad"]
    ad = next(g for g in res.groups if g.files == ["ad1"])
    c.execute(
        "UPDATE meme_swarms SET updated_at = now() - interval '30 minutes' WHERE id = %s", (ad.id,)
    )
    assert [i.kind for i in store.wall().items] == ["meme", "ad"]  # the other ad is listed now
    assert store.detail(ad.id) is None and store.picture_source(ad.id) is None
    assert SwarmStore(c, fresh_min=60).detail(ad.id) is not None


def test_store_reads_empty_without_the_table():
    class Missing:
        autocommit = False

        def __init__(self):
            self.rolled_back = False

        def cursor(self, row_factory=None):
            raise psycopg.errors.UndefinedTable('relation "meme_swarms" does not exist')

        def rollback(self):
            self.rolled_back = True

    conn = Missing()
    store = SwarmStore(conn)
    assert store.wall().items == [] and store.detail("abc") is None
    assert store.find_by_cids(["x"]) is None and conn.rolled_back
