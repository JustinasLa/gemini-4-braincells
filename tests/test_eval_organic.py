"""Organic false-positive eval: window building from recordings, the live-case selection rule,
case scoring and the summary. Offline: no network, no database."""

import gzip
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eval import organic
from eval.organic import (
    LIVE_RULE,
    WINDOW_S,
    CaseResult,
    HourData,
    Profiles,
    account_info_fn,
    build_window,
    candidate_keys,
    case_outcome,
    read_rows,
    select_live_cases,
    split_of,
    summarize,
)
from eval.store import write_eval_run
from signals.synth import empty_window, hard_negative
from signals.window import WPost

W = 1_794_600_000  # 2026-11-13T20:40:00Z, a clock-aligned 10-minute window
AT = W + WINDOW_S
OLD = datetime.fromtimestamp(W - 800 * 86400, UTC).isoformat()
NEW = datetime.fromtimestamp(W - 5 * 86400, UTC).isoformat()


def _profiles(n: int, created: str = OLD, followers: int = 200, posts: int = 500) -> Profiles:
    p = Profiles(None, fetch=False)
    for i in range(n):
        p.data[f"did:plc:u{i}"] = {
            "handle": f"u{i}.bsky.social",
            "created": created,
            "followers": followers,
            "posts": posts,
            "plc": None,
        }
    return p


def _post(i: int, t: float, text: str, urls=(), uri_n: int = 0) -> WPost:
    return WPost(
        uri=f"at://did:plc:u{i}/app.bsky.feed.post/p{uri_n}",
        did=f"did:plc:u{i}",
        t=t,
        text=text,
        embed_urls=list(urls),
    )


def _hour(posts: list[WPost], tags: dict | None = None) -> HourData:
    d = HourData()
    for p in posts:
        d.posts[W][p.uri] = p
    d.tags.update(tags or {})
    return d


# ------------------------------------------------------------------ split / windows -----------
def test_split_is_by_utc_hour_parity():
    assert split_of(datetime(2026, 11, 13, 19, 50, tzinfo=UTC).timestamp()) == "heldout"
    assert split_of(datetime(2026, 11, 13, 20, 0, tzinfo=UTC).timestamp()) == "dev"
    assert split_of(datetime(2026, 11, 13, 21, 59, tzinfo=UTC).timestamp()) == "heldout"


def _event(did, t, collection, rkey, record=None, op="create"):
    commit = {"operation": op, "collection": collection, "rkey": rkey}
    if record is not None:
        commit["record"] = record
    return {"did": did, "time_us": int(t * 1e6), "kind": "commit", "commit": commit}


def test_window_from_recording_mirrors_load_window(tmp_path):
    like = "app.bsky.feed.like"
    subj = lambda k: {"subject": {"uri": f"at://did:plc:author/app.bsky.feed.post/{k}"}}  # noqa: E731
    events = []
    # one event per second so the window counts as fully recorded
    for s in range(WINDOW_S):
        events.append(_event(f"did:plc:bg{s}", W + s + 0.5, like, f"b{s}", subj(f"solo{s}")))
    # a subject liked by 3 accounts (kept), one liked by 1 account (dropped: < min_key_accounts)
    for i in range(3):
        events.append(_event(f"did:plc:l{i}", W + 10 + i, like, "x", subj("shared")))
    post = {"text": "hello world from a recorded slice #Tag", "createdAt": "2026-11-13T20:40:05Z"}
    post["facets"] = [{"features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "Tag"}]}]
    events.append(_event("did:plc:p", W + 5, "app.bsky.feed.post", "a", post))
    events.append(_event("did:plc:p", W + 6, "app.bsky.feed.post", "a", post))  # duplicate
    events.append(_event("did:plc:p", W + 7, "app.bsky.feed.post", "b", post))
    events.append(_event("did:plc:p", W + 8, "app.bsky.feed.post", "b", op="delete"))
    events.append(_event("did:plc:p", W + 9, "app.bsky.feed.post", "a", op="delete"))
    events[-1]["time_us"] = int((AT + 30) * 1e6)  # deleted only after the window ended
    path = tmp_path / "20261113" / "20-1.jsonl.gz"
    path.parent.mkdir()
    with gzip.open(path, "wt") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    data = read_rows([path])
    assert data.complete_windows() == [W]
    assert data.tags["at://did:plc:p/app.bsky.feed.post/a"] == ["tag"]
    wd = build_window(data, W)
    assert [p.uri.rsplit("/", 1)[1] for p in wd.posts] == ["a", "b"]
    assert [p.deleted for p in wd.posts] == [False, True]
    assert {a.subject.rsplit("/", 1)[1] for a in wd.actions} == {"shared"}
    assert len(wd.actions) == 3
    assert wd.background["like"].sum() == WINDOW_S + 3
    assert wd.background["post"].sum() == 2
    assert wd.active_accounts == 1


def test_partial_window_is_not_complete():
    d = HourData()
    d.seconds[W] = set(range(W, W + 300))
    assert d.complete_windows() == []


# ------------------------------------------------------------------ live-case rule -----------
LINK = "https://news.example/2026/11/13/story"


def test_link_shared_by_established_accounts_with_own_words_is_selected():
    posts = [
        _post(i, W + 30 * i, f"my own take number {i} on this {i * 7}", [LINK]) for i in range(6)
    ]
    cases = select_live_cases(_hour(posts), W, _profiles(6))
    assert [c["type"] for c in cases] == ["link_burst"]
    c = cases[0]
    assert c["key"] == LINK and c["accounts"] == 6 and c["established_share"] == 1.0
    assert c["split"] == "dev" and c["sample_urls"][0].startswith("https://bsky.app/profile/")
    assert "6 accounts posted the same link" in c["why"]


def test_copy_paste_text_is_not_an_organic_case():
    posts = [_post(i, W + i, "exact same words from everyone here", [LINK]) for i in range(6)]
    assert candidate_keys(_hour(posts), W) == []


def test_too_few_accounts_or_one_account_repeating_is_rejected():
    few = [
        _post(i, W + i, f"take {i} different", [LINK])
        for i in range(LIVE_RULE["link_min_accounts"] - 1)
    ]
    assert candidate_keys(_hour(few), W) == []
    spam = [_post(i, W + i, f"take {i} different", [LINK]) for i in range(6)]
    spam += [_post(0, W + 100 + k, f"again {k} and again", [LINK], uri_n=k + 1) for k in range(4)]
    assert candidate_keys(_hour(spam), W) == []


def test_fresh_or_unknown_accounts_are_not_established():
    posts = [_post(i, W + i, f"my own take number {i}", [LINK]) for i in range(6)]
    assert select_live_cases(_hour(posts), W, _profiles(6, created=NEW)) == []
    assert select_live_cases(_hour(posts), W, _profiles(6, followers=3)) == []
    assert select_live_cases(_hour(posts), W, Profiles(None, fetch=False)) == []


def test_hashtag_burst_and_popular_post():
    posts = [_post(i, W + i, f"what a match {i} wow {i * 3}") for i in range(8)]
    tags = {p.uri: ["final"] for p in posts}
    data = _hour(posts, tags)
    subject = "at://did:plc:star/app.bsky.feed.post/1"
    n = LIVE_RULE["post_min_engagers"]
    for i in range(n):
        data.actions[W]["likes"][f"at://did:plc:u{i}/app.bsky.feed.like/1"] = (
            f"did:plc:u{i}",
            W + i,
            subject,
            "did:plc:star",
        )
    cases = select_live_cases(data, W, _profiles(n))
    assert {(c["type"], c["key"]) for c in cases} == {
        ("hashtag_burst", "final"),
        ("popular_post", subject),
    }
    accounts, _ = organic.case_accounts(data, W, "popular_post", subject)
    assert len(accounts) == n


# ------------------------------------------------------------------ scoring ------------------
def _cluster(status, members, fired=("content", "timing")):
    sig = SimpleNamespace(name="duplicate_text", polarity="for", score=1.0)
    guard = SimpleNamespace(name="established_accounts", polarity="against", score=0.7)
    return SimpleNamespace(
        status=status,
        members=list(members),
        score=0.9,
        p_value=0.002,
        guard_score=0.1,
        families_fired=list(fired),
        signals=[sig, guard],
    )


def test_case_outcome_takes_the_worst_status_of_clusters_about_the_case():
    case = {f"c{i}" for i in range(10)}
    org = _cluster("likely_organic", ["c0", "c1", "c2"])
    assert case_outcome([org], case)[0] == "likely_organic"
    coord = _cluster("coordinated", ["c3", "c4", "x1"])
    outcome, hits = case_outcome([org, coord], case)
    assert outcome == "coordinated"
    assert hits[0]["status"] == "coordinated" and hits[0]["overlap"] == 2
    assert hits[0]["signals_for"] == ["duplicate_text"]
    assert hits[0]["guards"] == ["established_accounts"]
    assert case_outcome([_cluster("insufficient", ["c5", "c6"])], case)[0] == "insufficient"
    assert case_outcome([], case)[0] == "no_cluster"


def test_case_outcome_ignores_clusters_that_are_not_about_the_case():
    case = {f"c{i}" for i in range(10)}
    one = _cluster("coordinated", ["c0", "x1", "x2"])  # only 1 case account
    minority = _cluster("coordinated", ["c0", "c1"] + [f"x{i}" for i in range(8)])  # 20%
    assert case_outcome([one, minority], case)[0] == "no_cluster"


def test_summary_counts_fpr_and_lists_false_positives():
    _, hits = case_outcome([_cluster("coordinated", ["a", "b"])], {"a", "b"})
    rs = [
        CaseResult("fp1", "organic_live", "link_burst", W, 2, "coordinated", hits),
        CaseResult("ok1", "organic_live", "link_burst", W, 5, "likely_organic"),
        CaseResult("ok2", "organic_live", "hashtag_burst", W, 9, "insufficient"),
        CaseResult("ok3", "organic_live", "hashtag_burst", W, 9, "no_cluster"),
    ]
    s = summarize(rs)
    assert s["n"] == 4 and s["fpr"] == 0.25
    assert s["counts"] == {
        "coordinated": 1,
        "likely_organic": 1,
        "insufficient": 1,
        "no_cluster": 1,
    }
    assert s["clustered"] == 3 and s["fpr_given_clustered"] == pytest.approx(0.3333)
    assert s["by_type"]["link_burst"] == {
        "n": 2,
        "coordinated": 1,
        "likely_organic": 1,
        "insufficient": 0,
        "no_cluster": 0,
        "fpr": 0.5,
    }
    assert s["false_positives"][0]["case_id"] == "fp1"
    assert s["false_positives"][0]["families_fired"] == ["content", "timing"]
    lo, hi = s["fpr_ci95"]
    assert 0 < lo < 0.25 < hi < 1
    assert summarize([])["fpr"] is None


# ------------------------------------------------------------------ detector glue ------------
def test_account_info_budget_and_synthetic_accounts():
    p = _profiles(5)
    s = hard_negative("news_link", empty_window(), seed=1)
    synth = sorted(s.dids)[:2]
    fn = account_info_fn(p, AT, s, budget=3)
    out = fn(synth + [f"did:plc:u{i}" for i in range(5)])
    assert set(out) == set(synth) | {"did:plc:u0", "did:plc:u1", "did:plc:u2"}
    assert out["did:plc:u0"].age_days == pytest.approx(800 + WINDOW_S / 86400)  # at window end


def test_synthetic_hard_negatives_on_a_synthetic_background_are_not_coordinated():
    """End to end through the detector's entry point (no recording needed)."""
    from signals.detect import detect
    from signals.synth import inject

    base = empty_window(start=W, seed=11)
    for kind in ("news_link", "live_event", "fandom"):
        s = hard_negative(kind, base, seed=1000)
        res = detect(inject(base, s), account_info_fn(Profiles(None, fetch=False), AT, s))
        assert case_outcome(res.clusters, s.dids)[0] != "coordinated", kind


def test_write_eval_run_inserts_one_row():
    calls = []

    class Conn:
        def execute(self, sql, args):
            calls.append((sql, args))
            return SimpleNamespace(fetchone=lambda: (42,))

    rid = write_eval_run("organic_live", "heldout", n=19, fpr=0.0, details={"a": 1}, conn=Conn())
    assert rid == 42
    sql, args = calls[0]
    assert "INSERT INTO eval_runs" in sql
    assert args == ("organic_live", "heldout", None, None, None, 0.0, 19, '{"a": 1}')
