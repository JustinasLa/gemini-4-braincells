"""The lookup's swarm step (lookup/swarm_step.py): which posts count, what goes into the scorer,
the live-vs-sample choice, and that nothing about it can fail or change the lookup (no network;
fictional accounts)."""

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lookup import swarm_step
from lookup.fxtwitter import FxTwitterClient
from lookup.models import Account, Edge, ImageRef, LookupInput, Metrics, Post
from lookup.service import Deps, Lookup
from swarm.model import AccountInfo, Sighting, SwarmDetail, SwarmReport

SUBJ = "2121360054330986967"
COPY = "2121370648207671396"
QUOTE = "2121364193509277805"
REPLY = "2121385772448096586"
X_IMG = "https://pbs.twimg.com/media/HXAAAA?format=jpg&name=large"
DID = "did:plc:abcdefghijklmnopqrstuvwx"
CID = "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku"
BSKY_IMG = f"https://cdn.bsky.app/img/feed_fullsize/plain/{DID}/{CID}@jpeg"
BSKY_URI = f"at://{DID}/app.bsky.feed.post/3kabc"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
T0 = datetime(2026, 11, 13, 21, 50, tzinfo=UTC)


def _x(sid, role, handle, *, text="the comets have landed", media=(), **kw) -> Post:
    kw.setdefault("time_source", "snowflake")
    return Post(
        id=f"x:{sid}", platform="x", url=f"https://x.com/{handle}/status/{sid}",
        author_handle=handle, text=text, created_at=T0, role=role,
        media_urls=list(media), **{"verified": True, **kw},
    )  # fmt: skip


def _bsky(role="bluesky", **kw) -> Post:
    return Post(
        id=BSKY_URI, platform="bluesky", url="https://bsky.app/profile/eve.example/post/3kabc",
        author_handle="eve.example", text="the comets have landed", created_at=T0,
        time_source="api", role=role, media_urls=[BSKY_IMG], verified=True,
        metrics=Metrics(likes=3, reposts=1, replies=0), **kw,
    )  # fmt: skip


def _edge(frm, to, rel) -> Edge:
    return Edge(**{"from": frm, "to": to, "rel": rel})


def _x_job(deps: Deps, posts=None, edges=None, accounts=None) -> Lookup:
    lk = Lookup("lk_sw", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    subj = f"x:{SUBJ}"
    lk.result.subject_id = subj
    lk.result.posts = posts if posts is not None else _x_posts()
    lk.result.edges = (
        edges
        if edges is not None
        else [_edge(subj, f"x:{QUOTE}", "quote"), _edge(subj, f"x:{REPLY}", "reply")]
    )
    lk.result.accounts = accounts or []
    return lk


def _x_posts() -> list[Post]:
    return [
        _x(SUBJ, "submitted", "Ada_Example", text="look\n\n> quoted words", media=[X_IMG],
           metrics=Metrics(likes=10, reposts=2, replies=4, views=900)),
        _x(COPY, "copy", "cy_example", media=[X_IMG.replace("name=large", "name=small")]),
        _x(QUOTE, "quote", "qu_example", text="lol\n\n> the comets have landed", media=[X_IMG]),
        _x(REPLY, "reply", "re_example", text="source?"),
        _x("2121360054330986001", "mention", "mo_example"),
        _x("2121360054330986002", "author_recent", "Ada_Example"),
        _x("2121360054330986003", "screenshotted", "sh_example"),
        _x("2121360054330986004", "copy", "hint_example", time_source="search_hint"),
        _bsky(),
    ]  # fmt: skip


class FakeScorer:
    def __init__(self, n_accounts=None, error=None, sleep=0.0):
        self.calls = []
        self.n_accounts, self.error, self.sleep = n_accounts, error, sleep

    def __call__(self, sightings, accounts=None, *, basis="sample", basis_note="", **kw):
        self.calls.append((sightings, accounts, basis, basis_note))
        if self.sleep:
            time.sleep(self.sleep)
        if self.error:
            raise self.error
        n = len({s.account for s in sightings if s.kind == "post"})
        return SwarmReport(
            verdict="unclear", n_posts=len(sightings), basis=basis, basis_note=basis_note,
            n_accounts=self.n_accounts if self.n_accounts is not None else n,
            n_accounts_known=len(accounts or {}),
        )  # fmt: skip


def _detail(n_accounts: int) -> SwarmDetail:
    return SwarmDetail(
        id="grp1", thumb="https://cdn.bsky.app/img/feed_thumbnail/x.jpg", verdict="swarm",
        score=0.8, n_posts=40, n_accounts=n_accounts, picture="https://cdn.bsky.app/x.jpg",
        report=SwarmReport(verdict="swarm", n_accounts=n_accounts, basis="live",
                           basis_note="40 posts our Bluesky feed saw in 24 h"),
    )  # fmt: skip


class FakeLive:
    def __init__(self, by_cids=None, by_picture=None, error=None):
        self.by_cids, self.by_picture, self.error = by_cids, by_picture, error
        self.cids, self.pictures = [], []

    def find_by_cids(self, cids):
        self.cids.append(list(cids))
        if self.error:
            raise self.error
        return self.by_cids

    def find_by_picture(self, image):
        self.pictures.append(image)
        if self.error:
            raise self.error
        return self.by_picture


# ------------------------------------------------------------------ sightings
def test_sightings_keep_carrying_roles_with_targets_times_and_files():
    lk = _x_job(Deps(oxylabs=None))
    s = {x.id: x for x in swarm_step.build_sightings(lk.result)}
    # mention, author_recent and screenshotted never count
    assert set(s) == {
        f"x:{SUBJ}",
        f"x:{COPY}",
        f"x:{QUOTE}",
        f"x:{REPLY}",
        "x:2121360054330986004",
        BSKY_URI,
    }
    subj = s[f"x:{SUBJ}"]
    assert subj.kind == "post" and subj.target is None and subj.account == "x:ada_example"
    assert subj.text == "look"  # the quoted post's words are someone else's
    assert (subj.likes, subj.reposts, subj.replies) == (10, 2, 4)
    assert subj.media_key == "HXAAAA" and s[f"x:{COPY}"].media_key == "HXAAAA"  # any size
    q = s[f"x:{QUOTE}"]
    assert q.kind == "quote" and q.target == f"x:{SUBJ}" and q.text == "lol"
    assert q.media_key is None  # the quoted post's file, not this account's
    r = s[f"x:{REPLY}"]
    assert r.kind == "reply" and r.target == f"x:{SUBJ}" and r.media_key is None
    assert s[f"x:{COPY}"].time_exact and not s["x:2121360054330986004"].time_exact
    b = s[BSKY_URI]
    assert b.platform == "bluesky" and b.account == DID and b.kind == "post"
    assert b.media_key == CID and b.likes == 3


def test_media_keys():
    assert swarm_step.media_key(X_IMG) == "HXAAAA"
    assert swarm_step.media_key("https://pbs.twimg.com/media/HXAB.jpg?name=orig") == "HXAB"
    assert swarm_step.media_key(BSKY_IMG) == CID
    assert swarm_step.media_key("https://pbs.twimg.com/card_img/123/abc?format=jpg") is None
    assert swarm_step.media_key("https://pbs.twimg.com/profile_images/1/a_normal.jpg") is None
    assert swarm_step.media_key("/api/lookup/lk/image") is None
    assert swarm_step.media_key("https://example.com/media/abc.jpg") is None


def _image_job(deps: Deps) -> Lookup:
    ref = ImageRef(sha256=hashlib.sha256(JPEG).hexdigest(), mime="image/jpeg", bytes=len(JPEG))
    lk = Lookup("lk_img", LookupInput(image=ref), deps, image=JPEG)
    lk._add_upload_post()
    run = ["the comets have landed"]
    lk.result.posts += [
        _x(COPY, "origin", "cy_example", caption_match=run),
        _x(QUOTE, "quote", "qu_example", text="ha\n\n> the comets have landed", caption_match=run),
        _x(REPLY, "copy", "no_run", caption_match=[]),  # kept for nothing: not counted
        _bsky(caption_match=run),
    ]
    # an upload's quote quotes another copy: that copy is its target (the upload is no post)
    lk.result.edges = [_edge(f"x:{COPY}", f"x:{QUOTE}", "quote")]
    return lk


def test_upload_counts_only_posts_with_a_caption_run_never_the_placeholder():
    lk = _image_job(Deps(oxylabs=None))
    s = {x.id: x for x in swarm_step.build_sightings(lk.result)}
    assert set(s) == {f"x:{COPY}", f"x:{QUOTE}", BSKY_URI}
    assert s[f"x:{QUOTE}"].target == f"x:{COPY}"
    assert s[f"x:{COPY}"].kind == "post"  # origin is a post


def test_upload_counts_a_read_mention_that_repeats_the_caption():
    lk = _image_job(Deps(oxylabs=None))
    run = ["the comets have landed"]
    lk.result.posts += [
        _x("2121360054330986010", "mention", "read_example", caption_match=run),
        _x("2121360054330986011", "mention", "hit_example", caption_match=run, verified=False),
        _x("2121360054330986012", "mention", "other_example"),
    ]
    s = {x.id: x for x in swarm_step.build_sightings(lk.result)}
    assert "x:2121360054330986010" in s and s["x:2121360054330986010"].kind == "post"
    # an unread search hit and a mention without the caption still do not count
    assert "x:2121360054330986011" not in s and "x:2121360054330986012" not in s


def test_x_post_asks_the_live_index_with_the_picture_it_downloaded():
    live = FakeLive(by_picture=_detail(12))
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=FakeScorer(), live_swarms=live))
    lk._pictures[X_IMG] = JPEG  # what _read_image keeps of the submitted post's picture
    lk._pictures["https://pbs.twimg.com/media/other.jpg"] = b"not the subject's"
    lk._swarm()
    assert live.pictures == [JPEG]
    assert lk.result.swarm_live.id == "grp1"


# ------------------------------------------------------------------ accounts
def test_account_info_from_profiles_fxtwitter_and_get_profiles():
    lk = _x_job(
        Deps(oxylabs=None),
        accounts=[
            # an X profile page read by Oxylabs: joined to the month only
            Account(platform="x", handle="ada_example", joined="2009-06", followers=252_600,
                    following=10, posts_count=64_800, bio="hi", age_days=6000),
            Account(platform="bluesky", handle="eve.example", joined="2024-02-03",
                    followers=5, following=900, posts_count=2, avatar_url=None),
        ],
    )  # fmt: skip
    fx = {
        "cy_example": {
            "handle": "cy_example", "name": "Cy", "followers": 3, "following": 400,
            "posts_count": 12, "bio": "", "joined": datetime(2026, 11, 1, 6, tzinfo=UTC),
            "avatar_url": "https://abs.twimg.com/sticky/default_profile_images/x_normal.png",
        },
        "ada_example": {"handle": "Ada_Example", "followers": 252_611, "joined": None},
    }  # fmt: skip
    sightings = swarm_step.build_sightings(lk.result)
    acc = swarm_step.build_accounts(lk.result, sightings, fx)
    cy = acc["x:cy_example"]
    assert cy.platform == "x" and cy.followers == 3 and cy.posts_count == 12
    assert cy.created_at == datetime(2026, 11, 1, 6, tzinfo=UTC) and cy.has_avatar is False
    ada = acc["x:ada_example"]
    assert ada.followers == 252_611  # FxTwitter's exact count first, the page fills the rest
    assert ada.posts_count == 64_800 and ada.bio == "hi"
    assert ada.created_at is None  # "2009-06" is too coarse to compare join days
    eve = acc[DID]  # a Bluesky profile is keyed by the DID of the posts' author
    assert eve.platform == "bluesky" and eve.created_at == datetime(2024, 2, 3, tzinfo=UTC)
    assert eve.has_avatar is False and eve.following == 900
    assert "x:qu_example" not in acc  # nothing known: left out
    more = swarm_step.build_accounts(
        lk.result, sightings, fx,
        [{"did": DID, "handle": "eve.example", "createdAt": "2024-02-03T10:00:00Z",
          "followersCount": 6, "avatar": "https://cdn.bsky.app/a.jpg"}],
    )  # fmt: skip
    assert more[DID].followers == 5  # the lookup's own read was there first


def test_fxtwitter_answer_keeps_its_authors_profiles():
    path = Path(__file__).parent / "fixtures/fxtwitter"
    data = json.loads((path / "conversation_visegrad24_2121220535044464871.json").read_text())
    conv = FxTwitterClient._conversation(data, with_replies=True)
    a = conv.authors["visegrad24"]
    assert a["followers"] == 2127338 and a["following"] == 2768 and a["posts_count"] == 109614
    assert a["joined"] == datetime(2020, 1, 30, 6, 48, 4, tzinfo=UTC)
    assert a["bio"].startswith("Curating news") and a["avatar_url"].startswith("https://pbs")
    assert "_pixiara_" in conv.authors  # the replies' authors too


# ------------------------------------------------------------------ the step
def test_step_scores_the_sample_and_never_touches_status_or_origin():
    scorer = FakeScorer()
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=scorer))
    lk.result.origin_id = f"x:{COPY}"
    lk._swarm()
    r = lk.result
    st = next(s for s in r.steps if s.id == "swarm")
    assert st.state == "done" and st.label == "Swarm check" and st.detail == "4 accounts"
    sightings, accounts, basis, note = scorer.calls[0]
    assert basis == "sample" and note == "6 posts our search found"
    assert all(isinstance(x, Sighting) for x in sightings)
    assert r.swarm is not None and r.swarm.basis == "sample"
    assert r.swarm_live is None and r.swarm_sample is None
    assert r.status == "insufficient" and r.origin_id == f"x:{COPY}"


def test_choose_live_when_it_has_more_accounts():
    sample = SwarmReport(n_accounts=5)
    shown, kept = swarm_step.choose(sample, _detail(31))
    assert shown.basis == "live" and kept is sample
    shown, kept = swarm_step.choose(sample, _detail(5))
    assert shown is sample and kept is None
    assert swarm_step.choose(None, _detail(2))[0].n_accounts == 2
    assert swarm_step.choose(sample, None) == (sample, None)


def test_live_group_by_bluesky_cids_is_shown_and_the_sample_kept():
    live = FakeLive(by_cids=_detail(31))
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=FakeScorer(), live_swarms=live))
    lk._swarm()
    r = lk.result
    assert live.cids == [[CID]] and live.pictures == []  # an X post: no picture bytes held
    assert r.swarm.basis == "live" and r.swarm.n_accounts == 31
    assert r.swarm_sample.basis == "sample" and r.swarm_live.id == "grp1"
    assert next(s for s in r.steps if s.id == "swarm").detail == "31 accounts · live Bluesky feed"


def test_live_group_with_fewer_accounts_is_kept_beside_the_sample():
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=FakeScorer(), live_swarms=FakeLive(_detail(2))))
    lk._swarm()
    assert lk.result.swarm.basis == "sample" and lk.result.swarm_live.n_accounts == 2
    assert lk.result.swarm_sample is None


def test_upload_asks_the_live_index_by_picture():
    live = FakeLive(by_picture=_detail(12))
    lk = _image_job(Deps(oxylabs=None, swarm_scorer=FakeScorer(), live_swarms=live))
    lk._swarm()
    assert live.cids == [[CID]] and live.pictures == [JPEG]  # CIDs first, then the picture
    assert lk.result.swarm.n_accounts == 12


@pytest.mark.parametrize(
    "deps",
    [
        dict(swarm_scorer=FakeScorer(error=RuntimeError("bug"))),
        dict(swarm_scorer=FakeScorer(), live_swarms=FakeLive(error=RuntimeError("db down"))),
        dict(swarm_scorer=FakeScorer(sleep=1.0)),
    ],
    ids=["scorer fails", "live index fails", "scorer too slow"],
)
def test_failures_leave_the_lookup_intact(deps, monkeypatch):
    monkeypatch.setattr(swarm_step, "SWARM_CAP_S", 0.3)
    lk = _x_job(Deps(oxylabs=None, **deps))
    before = lk.result.model_copy(deep=True)
    lk._swarm()
    r = lk.result
    st = next(s for s in r.steps if s.id == "swarm")
    live_failed = "live_swarms" in deps
    if live_failed:  # the sample still stands
        assert st.state == "done" and r.swarm.basis == "sample" and r.swarm_live is None
    else:
        assert r.swarm is None and r.swarm_live is None and r.swarm_sample is None
        assert st.state in ("failed", "skipped") and st.detail in ("check failed", "took too long")
    assert r.posts == before.posts and r.edges == before.edges and r.accounts == before.accounts
    assert r.status == before.status and r.origin_id == before.origin_id
    assert r.receipts == before.receipts


def test_an_unexpected_error_in_the_step_is_recorded_not_raised(monkeypatch):
    def boom(_lk):
        raise RuntimeError("bug")

    monkeypatch.setattr(swarm_step, "_skip_reason", boom)
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=FakeScorer()))
    lk._swarm()
    st = next(s for s in lk.result.steps if s.id == "swarm")
    assert st.state == "failed" and lk.result.swarm is None


class FakeBsky:
    def __init__(self, error=None):
        self.calls, self.error = [], error
        self.last_call = {"endpoint": "app.bsky.actor.getProfiles", "status": 200, "latency_ms": 9}

    def profiles(self, actors, deadline=None):
        self.calls.append((list(actors), deadline))
        if self.error:
            raise self.error
        return [{"did": a, "handle": "eve.example", "followersCount": 7, "postsCount": 2,
                 "createdAt": "2026-11-12T08:00:00Z"} for a in actors]  # fmt: skip


def test_bluesky_profiles_are_read_in_one_batched_call_with_a_receipt():
    bsky, scorer = FakeBsky(), FakeScorer()
    lk = _x_job(Deps(oxylabs=None, bsky=bsky, swarm_scorer=scorer))
    lk._swarm()
    assert [c[0] for c in bsky.calls] == [[DID]]
    accounts = scorer.calls[0][1]
    assert accounts[DID].followers == 7
    assert accounts[DID].created_at == datetime(2026, 11, 12, 8, tzinfo=UTC)
    rc = [r for r in lk.result.receipts if r.endpoint == "getProfiles"]
    assert len(rc) == 1 and rc[0].tool == "bluesky" and rc[0].ok


def test_bluesky_profiles_are_skipped_when_the_budget_is_short():
    bsky = FakeBsky()
    lk = _x_job(Deps(oxylabs=None, bsky=bsky, swarm_scorer=FakeScorer()))
    lk.deadline = time.monotonic() + 5  # inside the reading's reserve
    lk._swarm()
    assert bsky.calls == [] and lk.result.swarm is not None
    assert not any(r.endpoint == "getProfiles" for r in lk.result.receipts)


def test_failed_profiles_read_gets_a_failed_receipt_and_the_step_goes_on():
    lk = _x_job(
        Deps(oxylabs=None, bsky=FakeBsky(error=RuntimeError("502")), swarm_scorer=FakeScorer())
    )
    lk._swarm()
    rc = [r for r in lk.result.receipts if r.endpoint == "getProfiles"]
    assert len(rc) == 1 and not rc[0].ok and lk.result.swarm is not None


@pytest.mark.parametrize(
    "inp, why",
    [
        (LookupInput(text="the comets have landed and nobody says why"), "no picture"),
        (LookupInput(url="https://example.com/story"), "no picture"),
    ],
)
def test_skipped_without_a_picture(inp, why):
    scorer = FakeScorer()
    lk = Lookup("lk_t", inp, Deps(oxylabs=None, swarm_scorer=scorer))
    lk._swarm()
    st = next(s for s in lk.result.steps if s.id == "swarm")
    assert (st.state, st.detail) == ("skipped", why) and scorer.calls == []


def test_x_post_without_a_picture_is_skipped():
    posts = [_x(SUBJ, "submitted", "ada_example"), _x(COPY, "copy", "cy_example")]
    scorer = FakeScorer()
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=scorer), posts=posts, edges=[])
    lk._swarm()
    assert next(s for s in lk.result.steps if s.id == "swarm").state == "skipped"
    assert scorer.calls == []


def test_not_configured_is_skipped():
    lk = _x_job(Deps(oxylabs=None))
    lk._swarm()
    st = next(s for s in lk.result.steps if s.id == "swarm")
    assert (st.state, st.detail) == ("skipped", "not configured")


# ------------------------------------------------------------------ end to end (fakes)
def test_end_to_end_x_post_with_a_picture():
    from tests.test_lookup import COPY as T_COPY
    from tests.test_lookup import SUBJ as T_SUBJ
    from tests.test_lookup import FakeOxylabs

    scorer = FakeScorer()
    deps = Deps(oxylabs=FakeOxylabs(), swarm_scorer=scorer)
    job = Lookup("lk_e2e", LookupInput(url=f"https://x.com/ada_example/status/{T_SUBJ}"), deps)
    job.run()
    r = job.result
    assert r.state == "done"
    st = {s.id: s for s in r.steps}
    assert st["swarm"].state == "done"
    s = {x.id: x for x in scorer.calls[0][0]}
    assert s[f"x:{T_SUBJ}"].kind == "post" and s[f"x:{T_SUBJ}"].media_key == "HXAAAA"
    assert s[f"x:{T_COPY}"].kind == "quote" and s[f"x:{T_COPY}"].target == f"x:{T_SUBJ}"
    # the author's profile page read (Oxylabs) reaches the scorer
    assert scorer.calls[0][1]["x:ada_example"].followers == 252_600
    assert r.swarm is not None and r.status == "insufficient" and r.origin_id == f"x:{T_SUBJ}"
    assert job.snapshot()["swarm"]["basis"] == "sample"


def test_end_to_end_upload():
    from tests.test_lookup_image import COPY as I_COPY
    from tests.test_lookup_image import _run

    scorer, live = FakeScorer(), FakeLive()
    job = _run(swarm_scorer=scorer, live_swarms=live)
    r = job.result
    assert next(s for s in r.steps if s.id == "swarm").state == "done"
    ids = {x.id for x in scorer.calls[0][0]}
    assert f"x:{I_COPY}" in ids and not any(i.startswith("upload:") for i in ids)
    assert live.pictures == [job._upload]
    assert r.origin_id == f"x:{I_COPY}"  # unchanged by the step


def test_end_to_end_text_lookup_is_skipped():
    from tests.test_lookup import TEXT, FakeOxylabs

    scorer = FakeScorer()
    job = Lookup("lk_txt", LookupInput(text=TEXT), Deps(oxylabs=FakeOxylabs(), swarm_scorer=scorer))
    job.run()
    assert next(s for s in job.result.steps if s.id == "swarm").state == "skipped"
    assert scorer.calls == [] and job.result.swarm is None


# ------------------------------------------------------------------ the real scorer
def test_real_scorer_takes_the_lookups_input():
    score = pytest.importorskip("swarm.score")
    lk = _x_job(Deps(oxylabs=None, swarm_scorer=score.score_swarm))
    lk._swarm()
    r = lk.result
    st = next(s for s in r.steps if s.id == "swarm")
    assert st.state == "done", st.detail
    assert isinstance(r.swarm, SwarmReport) and r.swarm.basis == "sample"
    assert r.swarm.basis_note == "6 posts our search found"
    assert r.swarm.n_accounts >= 1
    # the JSON contract: it serializes with the result
    assert lk.result.model_dump(mode="json")["swarm"]["basis"] == "sample"


def test_real_scorer_end_to_end_upload():
    score = pytest.importorskip("swarm.score")
    from tests.test_lookup_image import _run

    job = _run(swarm_scorer=score.score_swarm)
    st = next(s for s in job.result.steps if s.id == "swarm")
    assert st.state == "done", st.detail
    assert job.result.swarm.basis == "sample"


def test_account_info_is_the_contract_model():
    lk = _x_job(Deps(oxylabs=None))
    acc = swarm_step.build_accounts(
        lk.result, swarm_step.build_sightings(lk.result), {"cy_example": {"handle": "cy_example"}}
    )
    assert all(isinstance(a, AccountInfo) and a.key == k for k, a in acc.items())
