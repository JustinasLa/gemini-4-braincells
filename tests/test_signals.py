"""Detector tests: unit tests per signal on tiny fixtures, and property tests on synthetic
campaigns injected into a synthetic organic background (no DB, no network)."""

import dataclasses

import numpy as np
import pytest

from signals.config import DEFAULT
from signals.detect import detect
from signals.graph import rarity
from signals.keys import build_keys
from signals.score import _max_in_window, content_signals, handle_shape, shared_keys
from signals.synth import SYNTH_PREFIX, campaign, empty_window, hard_negative, inject
from signals.textnorm import text_hash
from signals.window import WAction, WindowData, WPost


def run(wd, accounts=None, cfg=DEFAULT):
    info = (lambda dids: {d: accounts[d] for d in dids if d in accounts}) if accounts else None
    return detect(wd, info, cfg)


def best_cluster(res, dids):
    hits = [c for c in res.clusters if set(c.members) & dids]
    return max(hits, key=lambda c: len(set(c.members) & dids), default=None)


# ------------------------------------------------------------------ unit ---------------------
def test_rarity_weights_rare_keys_more():
    assert rarity(3) > rarity(10) > rarity(150)
    assert rarity(150) < 0.25


def test_max_in_window():
    t = np.array([[0.0, 1.0, 2.0, 100.0, 200.0]])
    assert _max_in_window(t, 5.0).tolist() == [3]
    assert _max_in_window(t, 0.5).tolist() == [1]


def test_handle_shape():
    assert handle_shape("amber4821.bsky.social") == "a9.bsky.social"
    assert handle_shape("wdusakenosha.bsky.social") == "a.bsky.social"


def test_duplicate_text_signal_on_tiny_fixture():
    text = "this exact sentence is posted by every single account here"
    wd = WindowData(start=0, end=600)
    for i in range(5):
        wd.posts.append(
            WPost(
                uri=f"at://did:plc:a{i}/app.bsky.feed.post/1",
                did=f"did:plc:a{i}",
                t=10.0 * i,
                text=text,
                text_hash=text_hash(text),
            )
        )
    wd.posts.append(
        WPost(
            uri="at://did:plc:z/app.bsky.feed.post/1",
            did="did:plc:z",
            t=5.0,
            text="something else entirely for this one account",
            text_hash="x",
        )
    )
    keys = build_keys(wd, DEFAULT)
    members = [f"did:plc:a{i}" for i in range(5)]
    sigs = {s.name: s for s in content_signals(5, shared_keys(members, keys), DEFAULT)}
    assert sigs["duplicate_text"].score == 1.0
    assert sigs["duplicate_text"].detail["accounts"] == 5
    assert len(sigs["duplicate_text"].uris) == 5


def test_short_texts_are_not_keys():
    wd = WindowData(start=0, end=600)
    for i in range(10):
        wd.posts.append(
            WPost(
                uri=f"at://did:plc:s{i}/app.bsky.feed.post/1",
                did=f"did:plc:s{i}",
                t=float(i),
                text="LOL",
                text_hash=text_hash("LOL"),
            )
        )
    assert not [k for k in build_keys(wd, DEFAULT) if k[0] == "text"]


def test_viral_keys_are_dropped():
    wd = WindowData(start=0, end=600)
    subj = "at://did:plc:star/app.bsky.feed.post/v"
    for i in range(DEFAULT.max_key_accounts + 1):
        wd.actions.append(
            WAction(
                "like",
                f"at://did:plc:f{i}/app.bsky.feed.like/1",
                f"did:plc:f{i}",
                float(i),
                subj,
                "did:plc:star",
            )
        )
    assert ("like", subj) not in build_keys(wd, DEFAULT)


# ------------------------------------------------------------------ properties ---------------
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_l1_campaign_is_coordinated(seed):
    base = empty_window(seed=seed)
    s = campaign("L1", base, seed=seed)
    c = best_cluster(run(inject(base, s), s.accounts), s.dids)
    assert c is not None and c.status == "coordinated"
    assert len(set(c.members) & s.dids) / len(s.dids) >= 0.8
    assert not [d for d in c.members if not d.startswith(SYNTH_PREFIX)]


def test_viral_post_with_5000_organic_likers_is_not_coordinated():
    base = empty_window(seed=3)
    s = hard_negative("viral", base)
    res = run(inject(base, s), s.accounts)
    assert not [c for c in res.clusters if c.status == "coordinated"]


@pytest.mark.parametrize("kind", ["news_link", "live_event", "fandom"])
def test_organic_look_alikes_are_not_coordinated(kind):
    base = empty_window(seed=4)
    s = hard_negative(kind, base)
    res = run(inject(base, s), s.accounts)
    assert not [c for c in res.clusters if c.status == "coordinated"]


def test_background_alone_has_no_coordinated_clusters():
    res = run(empty_window(seed=5))
    assert not [c for c in res.clusters if c.status == "coordinated"]


def _spread(s, wd, seed=0):
    """Same campaign, but every event at an independent uniform random time."""
    rng = np.random.default_rng(seed)
    span = wd.end - wd.start
    posts = [dataclasses.replace(p, t=float(wd.start + rng.uniform(0, span))) for p in s.posts]
    acts = [dataclasses.replace(a, t=float(wd.start + rng.uniform(0, span))) for a in s.actions]
    return dataclasses.replace(s, posts=posts, actions=acts)


def test_shuffling_timestamps_lowers_timing_score():
    base = empty_window(seed=6)
    s = campaign("L1", base, seed=6)
    before = best_cluster(run(inject(base, s), s.accounts), s.dids)
    after = best_cluster(run(inject(base, _spread(s, base)), s.accounts), s.dids)
    assert after.family_scores["timing"] < before.family_scores["timing"]
    assert before.family_scores["timing"] >= DEFAULT.family_fire


def test_removing_a_family_drops_coordinated():
    """Content + timing only (no likes -> no network, no account info -> no identity) is
    coordinated; spreading the timestamps removes timing and the status must drop."""
    base = empty_window(seed=7)
    s = campaign("L1", base, seed=7)
    s = dataclasses.replace(s, actions=[])
    two = best_cluster(run(inject(base, s)), s.dids)
    assert sorted(two.families_fired) == ["content", "timing"]
    assert two.status == "coordinated"
    one = best_cluster(run(inject(base, _spread(s, base))), s.dids)
    assert one.families_fired == ["content"]
    assert one.status != "coordinated"


def test_same_input_same_output():
    base = empty_window(seed=8)
    s = campaign("L2", base, seed=8)
    wd = inject(base, s)
    a, b = run(wd, s.accounts), run(wd, s.accounts)

    def key(r):
        return [(c.members, c.status, c.score, c.p_value, c.family_scores) for c in r.clusters]

    assert key(a) == key(b)


# ------------------------------------------------------------------ demo visuals -------------
def test_visual_data_for_the_dashboard():
    base = empty_window(seed=9)
    s = campaign("L1", base, seed=9)
    c = best_cluster(run(inject(base, s), s.accounts), s.dids)
    nd = c.p_detail["null_distribution"]
    assert sum(nd["counts"]) == nd["worlds"] == c.p_detail["permutations"]
    assert len(nd["bin_edges"]) == len(nd["counts"]) + 1
    assert nd["observed"] == c.p_detail["observed"]
    # p-value is consistent with the histogram's tail
    assert c.p_value == round((1 + nd["worlds_at_least_observed"]) / (1 + nd["worlds"]), 4)
    ts = [e["t"] for e in c.timeline]
    assert ts == sorted(ts) and c.timeline
    nodes = c.member_graph["nodes"]
    assert nodes == c.members
    assert {nodes[e["n"]] for e in c.timeline} <= set(c.members)
    edges = c.member_graph["edges"]
    assert edges and all(0 <= a < len(nodes) and 0 <= b < len(nodes) for a, b, _, _ in edges)
