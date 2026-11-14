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
    assert c.p_value == float(f"{(1 + nd['worlds_at_least_observed']) / (1 + nd['worlds']):.3g}")
    ts = [e["t"] for e in c.timeline]
    assert ts == sorted(ts) and c.timeline
    nodes = c.member_graph["nodes"]
    assert nodes == c.members
    assert {nodes[e["n"]] for e in c.timeline} <= set(c.members)
    edges = c.member_graph["edges"]
    assert edges and all(0 <= a < len(nodes) and 0 <= b < len(nodes) for a, b, _, _ in edges)


# ------------------------------------------------------------------ semantic (no network) ----
def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_semantic_core_counts_density_not_chains():
    from signals.semantic import semantic_core

    rng = np.random.default_rng(0)
    base = rng.normal(size=32)
    # campaign: 6 accounts, all close to one message
    camp = np.stack([_unit(base + 0.15 * rng.normal(size=32)) for _ in range(6)])
    core, density = semantic_core([f"c{i}" for i in range(6)], camp, 0.85)
    assert len(core) == 6 and density > 0.9
    # a chain: rotate in a plane so neighbours match (cos 0.88) but two steps apart don't (0.55):
    # nobody is similar to a third of the others, so nobody is in the core
    theta = float(np.arccos(0.88))
    chain = np.zeros((9, 32), dtype=np.float32)
    for k in range(9):
        chain[k, 0], chain[k, 1] = np.cos(k * theta), np.sin(k * theta)
    core, density = semantic_core([f"o{i}" for i in range(9)], chain, 0.85)
    assert core == set() and density < 0.3


def test_semantic_signal_uses_the_embedder_only_when_given():
    from signals.score import semantic_signal

    base = empty_window(seed=10)
    texts = [
        f"the same long message number {i} about the fictional parking fee in velmora town"
        for i in range(6)
    ]
    s = from_texts_helper(texts, base)
    wd = inject(base, s)
    calls = []

    def fake_embed(uris, texts):
        calls.append(len(uris))
        return np.stack([_unit(np.ones(8) + 0.01 * i) for i in range(len(uris))])

    sig = semantic_signal(sorted(s.dids), wd, fake_embed, DEFAULT)
    assert calls and sig is not None and sig.score == 1.0
    res = run(wd, s.accounts)  # no embedder passed -> no semantic signal anywhere
    assert not [x for c in res.clusters for x in c.signals if x.name == "semantic_duplicate"]


def from_texts_helper(texts, base):
    from signals.synth import from_texts

    return from_texts(texts, base, coordinated=True, timing="spread", accounts="fresh", seed=1)


# ------------------------------------------------------------------ identity base-rate test ---
def _rates(fresh=0.0125, empty=0.0, n=400):
    from signals.baseline import BaseRates

    return BaseRates(n_age=n, fresh=fresh, n_profile=n, empty=empty)


def test_identity_test_flags_overrepresented_fresh_accounts():
    from signals.score import AccountInfo, identity_tests

    members = [f"did:plc:m{i}" for i in range(8)]
    fresh = {
        d: AccountInfo(did=d, age_days=3.0, has_avatar=True, description_len=10, followers=50)
        for d in members
    }
    old = {
        d: AccountInfo(did=d, age_days=900.0, has_avatar=True, description_len=10, followers=50)
        for d in members
    }
    t_fresh = {t["test"]: t for t in identity_tests(members, fresh, _rates(), DEFAULT)}
    t_old = {t["test"]: t for t in identity_tests(members, old, _rates(), DEFAULT)}
    assert t_fresh["fresh_accounts"]["p_value"] < 1e-9
    assert t_old["fresh_accounts"]["p_value"] == 1.0
    # no base rates, or too small a sample -> no test at all
    assert identity_tests(members, fresh, None, DEFAULT) == []
    assert identity_tests(members, fresh, _rates(n=10), DEFAULT) == []
    # a high base rate (e.g. an onboarding wave) makes the same cluster unremarkable
    assert {t["test"]: t for t in identity_tests(members, fresh, _rates(fresh=0.9), DEFAULT)}[
        "fresh_accounts"
    ]["p_value"] > 0.1


def test_paraphrase_campaign_without_burst_needs_the_identity_test():
    """Spread-out campaign of fresh accounts sharing a link: no timing evidence, so without base
    rates we abstain; with measured base rates the identity test makes it coordinated."""
    from signals.synth import from_texts

    base = empty_window(seed=11)
    texts = [
        f"honest review number {i}: this fictional gadget changed my whole week, worth it"
        for i in range(10)
    ]
    s = from_texts(texts, base, coordinated=True, timing="spread", accounts="fresh", seed=11)
    wd = inject(base, s)

    def info(dids):
        return {d: s.accounts[d] for d in dids if d in s.accounts}

    without = best_cluster(detect(wd, info, DEFAULT), s.dids)
    with_rates = best_cluster(detect(wd, info, DEFAULT, base_rates=_rates()), s.dids)
    assert without.status != "coordinated"
    assert with_rates.status == "coordinated"
    assert with_rates.p_detail["combined"]["decisive_test"] in ("fresh_accounts", "empty_profiles")


def test_identity_test_does_not_flag_aged_look_alikes():
    for kind in ("news_link", "fandom"):
        base = empty_window(seed=12)
        s = hard_negative(kind, base)
        res = detect(
            inject(base, s),
            lambda d, s=s: {x: s.accounts[x] for x in d if x in s.accounts},
            DEFAULT,
            base_rates=_rates(),
        )
        assert not [c for c in res.clusters if c.status == "coordinated"]


# ------------------------------------------------------------------ v1.6 guards --------------
def _fresh_campaign(seed=13):
    from signals.synth import from_texts

    base = empty_window(seed=seed)
    texts = [
        f"amazing deal number {i}: this fictional gadget changed my whole week, link below"
        for i in range(8)
    ]
    s = from_texts(texts, base, coordinated=True, timing="burst", accounts="fresh", seed=seed)
    return base, s


def test_members_converse_guard_separates_friend_groups_from_rings():
    import dataclasses as dc

    base, s = _fresh_campaign()

    def info(dids):
        return {d: s.accounts[d] for d in dids if d in s.accounts}

    ring = best_cluster(detect(inject(base, s), info, DEFAULT, base_rates=_rates()), s.dids)
    assert ring.status == "coordinated"
    assert not [g for g in ring.signals if g.name == "members_converse"]
    # Same accounts, but they also reply to each other's posts: a conversation, not a broadcast.
    dids = sorted(s.dids)
    posts = {p.did: p for p in s.posts}
    chat = []
    for i, d in enumerate(dids):
        target = posts[dids[(i + 1) % len(dids)]]
        for k in range(2):
            chat.append(
                WPost(
                    uri=f"at://{d}/app.bsky.feed.post/chat{k}",
                    did=d,
                    t=target.t + 30 + k,
                    text=f"haha yes {k}",
                    reply_to=target.uri,
                    reply_root=target.uri,
                )
            )
    talky = dc.replace(s, posts=s.posts + chat)
    c = best_cluster(detect(inject(base, talky), info, DEFAULT, base_rates=_rates()), s.dids)
    guard = next(g for g in c.signals if g.name == "members_converse")
    assert guard.score >= DEFAULT.organic_guard_strong
    assert c.status != "coordinated"


def test_declared_automation_guard_needs_old_accounts_that_say_so():
    from signals.score import AccountInfo, organic_guards

    members = [f"did:plc:wv{i}" for i in range(5)]
    old_bots = {
        d: AccountInfo(
            did=d,
            age_days=300.0,
            display_name="511 Northern WV",
            description="Automated traffic alerts for West Virginia roads",
        )
        for d in members
    }
    fresh_bots = {
        d: AccountInfo(
            did=d, age_days=5.0, display_name="Deals bot", description="automated deals feed"
        )
        for d in members
    }
    humans = {
        d: AccountInfo(
            did=d, age_days=300.0, display_name="Sam", description="I like hiking and robotics"
        )
        for d in members
    }
    wd = WindowData(start=0, end=600)

    def guard(info):
        return next(
            (
                g
                for g in organic_guards(members, [], info, wd, DEFAULT)
                if g.name == "declared_automation"
            ),
            None,
        )

    assert guard(old_bots).score == 1.0
    assert guard(fresh_bots) is None  # new accounts calling themselves bots don't get a pass
    assert guard(humans) is None  # "robotics" doesn't match the word "bot"


def test_declared_automation_ignores_bios_not_loaded_yet():
    from signals.score import AccountInfo, organic_guards

    members = [f"did:plc:q{i}" for i in range(6)]
    info = {
        d: AccountInfo(
            did=d,
            age_days=700.0,
            display_name="Quakes",
            description="Bot posting new quakes in near-real time",
        )
        for d in members[:3]
    }
    info.update(
        {
            d: AccountInfo(did=d, age_days=700.0, display_name="Quakes", description=None)
            for d in members[3:]
        }
    )  # bios not fetched yet
    g = next(
        x
        for x in organic_guards(members, [], info, WindowData(start=0, end=600), DEFAULT)
        if x.name == "declared_automation"
    )
    assert g.score == 1.0 and g.detail["with_known_bio"] == 3


def test_wider_crowd_guard_text_matches_its_score():
    """#18: the guard row is shown at every score; its text must match what the score says."""
    from signals.keys import Key
    from signals.score import organic_guards

    members = [f"did:plc:c{i}" for i in range(5)]

    def crowd(*totals: int):
        keys = []
        for i, total in enumerate(totals):
            k = Key("link", f"https://example.com/{i}", accounts=set(members))
            k.total_accounts = total  # accounts sharing it in the whole window
            keys.append(k)
        g = next(
            x
            for x in organic_guards(members, keys, {}, WindowData(start=0, end=600), DEFAULT)
            if x.name == "part_of_wider_crowd"
        )
        return g.score, g.summary

    wider = "a wider crowd is doing the same thing"
    score, text = crowd(5)  # nobody outside the cluster (cluster 682)
    assert score == 0.0
    assert "no wider crowd" in text and "only" not in text and "100%" not in text
    score, text = crowd(*[5] * 19, 6)  # one item of 20 has an outsider: not "all"
    assert 0 < score < 0.05 and text.startswith("on average 99%") and "too few" in text
    score, text = crowd(6)  # 83% exclusive
    assert score < DEFAULT.organic_guard_strong and wider not in text and "83%" in text
    score, text = crowd(11)  # 45%: below the strong-guard bar, so no "wider crowd" claim
    assert score < DEFAULT.organic_guard_strong and wider not in text and "too few" in text
    score, text = crowd(13)  # 38%: the guard holds
    assert score >= DEFAULT.organic_guard_strong and text.startswith("on average only 38%")
    assert wider in text
    score, text = crowd(50)  # 10% exclusive: a real crowd, the original wording
    assert score == 0.9 and text == (
        "on average only 10% of the accounts sharing these items in the window are in this "
        "cluster (a wider crowd is doing the same thing)"
    )


def test_tiny_p_values_fit_the_real_column():
    from signals.store import P_VALUE_FLOOR, _real_p

    assert _real_p(8.82e-61) == P_VALUE_FLOOR > 1.2e-38  # float4 can hold it
    assert _real_p(0.004) == 0.004
    assert _real_p(None) is None


def test_publisher_feed_accounts_are_not_coordinated_once_bios_are_known():
    """#19 (clusters 682/859): five JD Supra topic-feed accounts on bsky.social post the same
    jdsupra.com article about 1 s apart. With their profiles loaded, declared_automation (v1.6)
    keeps them out of `coordinated`; brand-new accounts with the same bios get no pass."""
    members = [  # handle, display name, bio, age (days), followers: the live accounts
        (
            "techlawjdsupra.bsky.social",
            "Tech Law News",
            "A JD Supra feed www.jdsupra.com",
            627,
            206,
        ),
        (
            "m-and-a-jdsupra.bsky.social",
            "M&A Watch",
            "A JD Supra news feed www.jdsupra.com",
            627,
            96,
        ),
        ("european-union-law.bsky.social", "EU Legal Watch", "A JD Supra news feed", 627, 36),
        ("ai-law-news.bsky.social", "AI Watch", "A JD Supra law feed www.jdsupra.com", 627, 160),
        ("frank-jdsupra.bsky.social", "Frank", "Lead Engineer at JD Supra.", 723, 156),
    ]

    def run(age_override=None):
        wd = empty_window(seed=0)
        for j, slug in enumerate(["be-kind-rewind-8542956", "the-bug-october-2026-4316556"]):
            for i in range(len(members)):
                t = wd.start + 100 + 400 * j + i
                wd.posts.append(
                    WPost(
                        uri=f"at://did:plc:jd{i}/app.bsky.feed.post/{j}",
                        did=f"did:plc:jd{i}",
                        t=t,
                        created_t=t,
                        text=f"title {slug}",
                        text_hash=text_hash(f"title {slug}"),
                        embed_urls=[f"https://jdsupra.com/legalnews/{slug}"],
                        langs=["en"],
                    )
                )
        from signals.score import AccountInfo

        info = {
            f"did:plc:jd{i}": AccountInfo(
                did=f"did:plc:jd{i}",
                handle=h,
                display_name=name,
                description=bio,
                age_days=float(age_override or age),
                followers=followers,
                follows=50,
                posts_count=5000,
                description_len=len(bio),
                has_avatar=True,
            )
            for i, (h, name, bio, age, followers) in enumerate(members)
        }
        res = detect(wd, lambda dids: {d: info[d] for d in dids if d in info}, DEFAULT)
        return [c for c in res.clusters if any(m.startswith("did:plc:jd") for m in c.members)]

    feeds = run()
    assert feeds and all(c.status != "coordinated" for c in feeds)
    guard = {s.name: s.score for c in feeds for s in c.signals if s.polarity == "against"}
    assert guard.get("declared_automation", 0) >= DEFAULT.organic_guard_strong
    fresh = run(age_override=3)  # same bios on brand-new accounts: no "openly automated" pass
    assert all(
        s.name != "declared_automation" for c in fresh for s in c.signals if s.polarity == "against"
    )
    assert any(c.status == "coordinated" for c in fresh)  # and the same burst is still flagged
