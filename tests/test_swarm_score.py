"""Meme swarm scorer: synthetic swarms, organic spreads and the awkward inputs in between."""

import random
import time
from datetime import UTC, datetime, timedelta

from swarm.config import SwarmConfig
from swarm.model import FAMILIES, AccountInfo, Sighting
from swarm.score import Baseline, _binom_sf, _handle_shape, _wilson, score_swarm

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
# a live pass window around T0: everything in it was seen
WIN = (T0 - timedelta(hours=12), T0 + timedelta(hours=12))
WORDS = (
    "cat dog bird fish moon sun rain tree road car bus train shoe hat coat lamp door wall "
    "bread milk tea cake fork spoon cup plate chair desk book pen ink page song tune drum "
    "bell ring coin card game ball goal team club park lake hill rock sand wave wind snow"
).split()


def own_text(rng: random.Random, n: int = 8) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def post(i, acc, at, text="", media=None, **kw):
    sid = i if isinstance(i, str) else f"p{i}"
    return Sighting(
        id=sid, platform="bluesky", account=acc, at=at, text=text, media_key=media, **kw
    )


def swarm_case(n=30, minutes=3, with_info=True, seed=1):
    rng = random.Random(seed)
    sightings, info = [], {}
    for i in range(n):
        acc = f"did:plc:bot{i}"
        at = T0 + timedelta(seconds=rng.uniform(0, minutes * 60))
        sightings.append(
            post(
                f"s{i}",
                acc,
                at,
                "This coin is going to the moon buy now before it is too late",
                "cid1",
            )
        )
        info[acc] = AccountInfo(
            key=acc,
            handle=f"user{rng.randint(100000, 999999)}.bsky.social",
            created_at=T0 - timedelta(days=5, hours=rng.uniform(0, 48)),
            followers=rng.randint(0, 3),
            bio="",
            has_avatar=False,
        )
    return sightings, (info if with_info else None)


def organic_case(n=200, seed=2):
    rng = random.Random(seed)
    sightings, info = [], {}
    for i in range(n):
        acc = f"did:plc:person{i}"
        if rng.random() < 0.3:  # one natural peak, spread over hours
            offset = rng.gauss(20 * 3600, 2 * 3600)
        else:
            offset = rng.uniform(0, 48 * 3600)
        at = T0 + timedelta(seconds=max(0.0, offset))
        sightings.append(post(i, acc, at, own_text(rng, rng.randint(4, 12)), f"cid{i}"))
        info[acc] = AccountInfo(
            key=acc,
            handle="".join(rng.choice("abcdefghijklmnop") for _ in range(rng.randint(4, 10)))
            + ".bsky.social",
            created_at=T0 - timedelta(days=rng.uniform(400, 3000)),
            followers=rng.randint(100, 5000),
            bio=own_text(rng, 6),
            has_avatar=True,
            name=own_text(rng, 2),
        )
    return sightings, info


def test_obvious_swarm():
    s, info = swarm_case()
    r = score_swarm(s, info, window=WIN)
    assert r.verdict == "swarm", r
    assert set(r.fired) >= {"timing", "accounts", "copy"}
    assert r.score >= 0.9 and r.guard < 0.6
    assert r.p_value is not None and r.p_value <= 0.01
    names = {x.name: x for x in r.signals}
    assert {"burst", "same_text", "same_file", "same_cohort", "templated_handles"} <= set(names)
    # untested account shares (no baseline) never fire on their own
    assert names["same_cohort"].score <= 0.45 and names["empty_profiles"].score <= 0.45
    assert names["same_file"].summary == "same file x30"
    assert names["same_text"].summary == "same words x30"
    assert names["burst"].summary.startswith("30 accounts in ")
    assert r.burst is not None and r.burst.n_accounts == 30
    assert r.n_accounts == 30 and r.n_accounts_known == 30 and r.n_posts == 30


def test_organic_viral_meme_is_not_swarm():
    s, info = organic_case()
    r = score_swarm(s, info, basis="live")
    assert r.verdict in ("organic", "unclear")
    assert r.verdict != "swarm"
    assert r.guard >= 0.6
    assert r.families["copy"] < 0.5 and r.families["accounts"] < 0.5


def test_popular_but_nothing_else_is_not_swarm():
    rng = random.Random(3)
    s = [
        post(i, f"x:fan{i}", T0 + timedelta(seconds=rng.uniform(0, 86400)), likes=5000, reposts=300)
        for i in range(300)
    ]
    r = score_swarm(s)
    assert r.verdict != "swarm"
    assert not r.fired


def test_too_few_accounts_is_unclear():
    s, info = swarm_case(n=3)
    r = score_swarm(s, info)
    assert r.verdict == "unclear"
    assert r.why_unclear == "only 3 accounts"


def test_news_moment_timing_only_is_not_swarm():
    rng = random.Random(4)
    s, info = [], {}
    for i in range(40):
        acc = f"did:plc:reporter{i}"
        s.append(post(i, acc, T0 + timedelta(seconds=rng.uniform(0, 600)), own_text(rng), f"c{i}"))
        info[acc] = AccountInfo(
            key=acc,
            handle=f"{''.join(rng.choice('abcdefgh') for _ in range(7))}.bsky.social",
            created_at=T0 - timedelta(days=rng.uniform(500, 2000)),
            followers=rng.randint(200, 9000),
            has_avatar=True,
            bio=own_text(rng, 5),
        )
    r = score_swarm(s, info, window=WIN)
    assert r.families["timing"] >= 0.5  # the burst is real
    # a fired family is never "organic": with strong guards it is an honest "can't tell"
    assert r.verdict == "unclear" and r.why_unclear == "signs both ways"


def test_short_generic_text_is_not_reuse():
    rng = random.Random(5)
    s = [
        post(i, f"x:u{i}", T0 + timedelta(seconds=rng.uniform(0, 12 * 3600)), "lol", f"c{i}")
        for i in range(30)
    ]
    r = score_swarm(s)
    same = next(x for x in r.signals if x.name == "same_text")
    assert same.score < 0.1
    assert r.verdict != "swarm"


def test_long_text_weighs_more_than_lol():
    rng = random.Random(6)
    times = [T0 + timedelta(seconds=rng.uniform(0, 12 * 3600)) for _ in range(20)]
    long = [
        post(i, f"x:u{i}", t, "this is a long sentence anyone would not write twice")
        for i, t in enumerate(times)
    ]
    lol = [post(i, f"x:u{i}", t, "lol") for i, t in enumerate(times)]
    fam = lambda xs: score_swarm(xs).families["copy"]  # noqa: E731
    assert fam(long) > 0.8 > 0.1 > fam(lol)


def test_empty_text_is_not_reuse():
    s = [post(i, f"x:u{i}", T0 + timedelta(hours=i)) for i in range(10)]
    r = score_swarm(s)
    assert r.families["copy"] == 0.0
    assert not any(x.name == "same_text" for x in r.signals)


def test_missing_account_info_still_works():
    s, _ = swarm_case(with_info=False)
    r = score_swarm(s, None, window=WIN)
    assert r.families["accounts"] == 0.0
    assert r.n_accounts_known == 0
    assert r.verdict == "swarm"  # timing + copy still agree


def test_closed_loop_boosts_fire_boost():
    rng = random.Random(7)
    accs = [f"did:plc:ring{i}" for i in range(10)]
    s = [
        post(i, a, T0 + timedelta(hours=i * 1.3), own_text(rng), f"c{i}")
        for i, a in enumerate(accs)
    ]
    k = 0
    for i in range(len(accs)):
        for j in range(1, 5):
            b = accs[(i + j) % len(accs)]
            s.append(
                Sighting(
                    id=f"b{k}",
                    platform="bluesky",
                    account=b,
                    kind="repost" if j % 2 else "like",
                    target=f"p{i}",
                    at=T0 + timedelta(hours=i * 1.3, seconds=10 + 5 * j),
                )
            )
            k += 1
    r = score_swarm(s)
    assert r.families["boost"] >= 0.5
    names = {x.name for x in r.signals}
    assert {"closed_loop", "fast_boost"} <= names
    assert r.n_boosts == 40


def test_deterministic_and_order_free():
    s, info = organic_case(n=120)
    a = score_swarm(s, info).model_dump()
    b = score_swarm(s, info).model_dump()
    shuffled = list(s)
    random.Random(9).shuffle(shuffled)
    c = score_swarm(shuffled, info).model_dump()
    assert a == b == c


def test_timeline_cap_and_order():
    rng = random.Random(10)
    s = [
        post(i, f"x:u{i % 400}", T0 + timedelta(seconds=rng.uniform(0, 86400)), own_text(rng))
        for i in range(1000)
    ]
    r = score_swarm(s)
    assert 0 < len(r.timeline) <= 300
    ts = [p.t for p in r.timeline]
    assert ts == sorted(ts)
    assert abs(r.timeline[0].t - min(x.at.timestamp() for x in s)) < 0.001
    assert r.timeline[0].a == 0


def test_timeline_groups_same_words():
    s, info = swarm_case(n=10)
    s.append(post(99, "did:plc:other", T0 + timedelta(minutes=30), "my own caption here"))
    r = score_swarm(s, info)
    gs = {p.g for p in r.timeline}
    assert gs == {0, 1}
    assert sum(p.burst for p in r.timeline) >= 10


def test_all_family_keys_present():
    for s in ([], swarm_case()[0], organic_case(n=20)[0]):
        r = score_swarm(s)
        assert list(r.families) == list(FAMILIES)


def test_robust_inputs():
    # empty
    r = score_swarm([])
    assert r.verdict == "unclear" and r.n_accounts == 0 and r.why_unclear
    # no times at all
    r = score_swarm([post(i, f"x:u{i}", None, own_text(random.Random(i))) for i in range(8)])
    assert r.first_at is None and r.p_value is None and r.burst is None and r.timeline == []
    # one account posting 50 times
    r = score_swarm([post(i, "x:loud", T0 + timedelta(minutes=i)) for i in range(50)])
    assert r.verdict == "unclear" and r.why_unclear == "only 1 account"
    # all times identical
    r = score_swarm([post(i, f"x:u{i}", T0, own_text(random.Random(i))) for i in range(20)])
    assert r.burst is not None and r.burst.n_accounts == 20
    # naive and aware datetimes mixed; naive is UTC
    naive = [post(i, f"x:u{i}", (T0 + timedelta(hours=i)).replace(tzinfo=None)) for i in range(5)]
    aware = [post(10 + i, f"x:v{i}", T0 + timedelta(hours=i, minutes=30)) for i in range(5)]
    r = score_swarm(naive + aware)
    assert r.first_at == T0 and r.first_at.tzinfo is not None
    # inexact times are left out of timing
    r = score_swarm([post(i, f"x:u{i}", T0, time_exact=False) for i in range(20)])
    assert r.p_value is None and r.families["timing"] == 0.0 and r.timeline == []


def test_baseline_fresh_rate_test():
    rng = random.Random(11)
    s, info = [], {}
    for i in range(20):
        acc = f"did:plc:a{i}"
        s.append(post(i, acc, T0 + timedelta(hours=i), own_text(rng)))
        # created over a whole year, but 16 of 20 under 30 days old: no week bunch
        age = rng.uniform(1, 29) if i < 16 else rng.uniform(60, 400)
        info[acc] = AccountInfo(key=acc, created_at=T0 + timedelta(hours=i) - timedelta(days=age))
    high = score_swarm(s, info, baseline=Baseline(fresh_rate=0.05, n=500))
    common = score_swarm(s, info, baseline=Baseline(fresh_rate=0.6, n=500))
    assert high.families["accounts"] > 0.5
    assert common.families["accounts"] < high.families["accounts"]


def test_summaries_never_name_accounts():
    s, info = swarm_case()
    r = score_swarm(s, info)
    keys = set(info) | {i.handle for i in info.values()}
    for sig in r.signals:
        assert len(sig.summary) <= 40
        assert not any(k in sig.summary for k in keys)
        assert not any(k in str(sig.numbers) for k in keys)
        assert len(sig.examples) <= 12


def test_clockwork_scheduler():
    rng = random.Random(12)
    s = [
        post(i, f"x:sched{i}", T0 + timedelta(seconds=97 * i + rng.uniform(-2, 2)), own_text(rng))
        for i in range(15)
    ]
    r = score_swarm(s)
    clock = next(x for x in r.signals if x.name == "clockwork")
    assert clock.score >= 0.75  # Wilson bound of 15 of 15
    assert clock.summary == "15 posts every 97 s"


def test_near_same_text_templates():
    rng = random.Random(14)
    swaps = ["tonight", "today", "now", "soon", "again", "fast", "early", "later"]
    s = [
        post(
            i,
            f"x:t{i}",
            T0 + timedelta(seconds=rng.uniform(0, 20 * 3600)),
            f"everyone is buying this coin it will go to the moon {swaps[i % 8]} trust me {i}",
        )
        for i in range(16)
    ]
    r = score_swarm(s)
    near = next(x for x in r.signals if x.name == "near_same_text")
    assert near.summary == "near-same words x16"
    assert r.families["copy"] >= 0.5
    assert not any(x.name == "own_words" for x in r.signals)


def test_same_second_accounts():
    rng = random.Random(15)
    s = []
    for k in range(8):  # 8 moments over a day, 4 accounts within 2 s each time
        base = T0 + timedelta(hours=3 * k)
        for j in range(4):
            i = 4 * k + j
            s.append(post(i, f"x:q{i}", base + timedelta(seconds=rng.uniform(0, 2)), own_text(rng)))
    r = score_swarm(s)
    ss = next(x for x in r.signals if x.name == "same_second")
    assert ss.summary == "32 accounts paired within 3 s"
    assert ss.score >= 0.5


def test_unclear_reason_when_one_family():
    s, info = swarm_case()
    # spread the same posts over two days: no burst, copy and accounts remain
    rng = random.Random(13)
    s = [x.model_copy(update={"at": T0 + timedelta(seconds=rng.uniform(0, 172800))}) for x in s]
    r = score_swarm(s, info)
    assert r.verdict == "swarm"  # accounts + copy without timing still needs no p-value
    r = score_swarm(s)  # without account info: copy only
    assert r.verdict == "unclear" and r.why_unclear == "only one kind of sign"


def test_helpers():
    assert _handle_shape("amber4821.bsky.social") == "a9999.bsky.social"
    assert _handle_shape("x:John_1990") == "a_9999"
    assert abs(_binom_sf(3, 10, 0.1) - 0.0702) < 1e-3
    assert _binom_sf(0, 10, 0.1) == 1.0


def test_fast_enough():
    s, info = organic_case(n=250)
    s2, info2 = swarm_case(n=250)
    for case, inf in ((s, info), (s2 + s, {**info, **info2})):
        score_swarm(case, inf)
        t = time.perf_counter()
        score_swarm(case, inf)
        assert time.perf_counter() - t < 0.5


def test_config_is_versioned():
    assert SwarmConfig().version
    assert score_swarm([]).version == SwarmConfig().version


# ---------------------------------------------------------------- swarm-v2 regressions ----------
# Each case below was a wrong verdict in swarm-v1 (review of 2d2dabc).

LIVE_END = T0 + timedelta(hours=1)
LIVE_WIN = (LIVE_END - timedelta(hours=24), LIVE_END)


def test_wilson_bound_values():
    assert abs(_wilson(5, 5, 1.96) - 0.566) < 0.002
    assert abs(_wilson(43, 43, 1.96) - 0.918) < 0.002
    assert _wilson(0, 10, 1.96) == 0.0 and _wilson(3, 0, 1.96) == 0.0


def test_live_uniform_hour_same_file_is_not_swarm():
    """v1: 10 people posting one file evenly over an hour came out swarm 55% of the time
    (same_file was the selection rule, and the null spread posts past the window's end)."""
    for seed in range(20):
        rng = random.Random(seed)
        s = [
            post(i, f"did:plc:h{i}", T0 + timedelta(seconds=rng.uniform(0, 3600)), "", "cidH")
            for i in range(10)
        ]
        r = score_swarm(s, {}, basis="live", selected_by_file=True, window=LIVE_WIN)
        assert r.verdict != "swarm", seed
        assert r.families["timing"] < 0.5, seed
        assert not any(x.name in ("same_file", "own_files") for x in r.signals)
        # the old call (no window, no selection flag) no longer finds a burst either
        assert score_swarm(s, {}).families["timing"] < 0.5, seed


def test_newest_20_sample_has_no_burst():
    """v1: a search's newest 20 posts (uniform over 15 min) always had timing p <= 0.01."""
    for seed in range(20):
        rng = random.Random(seed)
        s = [
            post(i, f"did:plc:n{i}", T0 + timedelta(seconds=rng.uniform(0, 900)), "", "cidN")
            for i in range(20)
        ]
        r = score_swarm(s, {})
        assert r.families["timing"] == 0.0, seed
        assert r.verdict != "swarm", seed
        assert r.verdict != "organic"  # 15 minutes cannot show a spread either


def test_single_fan_cannot_fire_boost():
    """v1: one poster liking the other five posts within a minute made fast_boost 1.0 -> swarm."""
    accs = [f"did:plc:f{i}" for i in range(6)]
    s = [post(i, a, T0 + timedelta(hours=2 * i), "", "cidF") for i, a in enumerate(accs)]
    s += [
        Sighting(
            id=f"l{i}",
            platform="bluesky",
            account=accs[0],
            kind="like",
            target=f"p{i}",
            at=T0 + timedelta(hours=2 * i, seconds=40),
        )
        for i in range(1, 6)
    ]
    for by_file in (False, True):
        r = score_swarm(s, {}, basis="live", selected_by_file=by_file, window=LIVE_WIN)
        assert r.families["boost"] < 0.5
        assert not any(x.name in ("fast_boost", "closed_loop") for x in r.signals)
        assert r.verdict != "swarm"


def test_friends_liking_each_other_is_not_a_loop():
    """v1: six friends posting one reaction image and liking each other -> closed_loop 1.0."""
    rng = random.Random(2)
    accs = [f"did:plc:f{i}" for i in range(6)]
    s = [
        post(i, a, T0 + timedelta(minutes=60 * i + rng.uniform(0, 30)), "", "cidR")
        for i, a in enumerate(accs)
    ]
    k = 0
    for i in range(6):
        for j in (1, 2):
            s.append(
                Sighting(
                    id=f"l{k}",
                    platform="bluesky",
                    account=accs[(i + j) % 6],
                    kind="like",
                    target=f"p{i}",
                    at=T0 + timedelta(minutes=60 * i + 40 + 10 * j),
                )
            )
            k += 1
    for by_file in (False, True):
        r = score_swarm(s, {}, basis="live", selected_by_file=by_file, window=LIVE_WIN)
        assert not any(x.name == "closed_loop" for x in r.signals)
        assert r.verdict != "swarm"


def test_old_signup_wave_cohort_is_ordinary():
    """v1: ten accounts that joined in the same week 200 days ago -> same_cohort 1.0 -> swarm,
    with or without a baseline."""
    rng = random.Random(4)
    s = [
        post(i, f"did:plc:w{i}", T0 + timedelta(hours=3 * i + rng.uniform(0, 2)), "", "cidW")
        for i in range(10)
    ]
    info = {
        f"did:plc:w{i}": AccountInfo(
            key=f"did:plc:w{i}",
            created_at=T0 - timedelta(days=200 + rng.uniform(0, 6)),
            followers=rng.randint(5, 80),
            has_avatar=True,
            bio="hi",
        )
        for i in range(10)
    }
    for bl in (None, Baseline(fresh_rate=0.5, empty_rate=0.4, n=1000)):
        for by_file in (False, True):
            r = score_swarm(s, info, basis="live", baseline=bl, selected_by_file=by_file)
            assert r.families["accounts"] < 0.5
            assert not any(x.name == "same_cohort" for x in r.signals)
            assert r.verdict != "swarm"


def test_recent_cohort_fires_only_with_a_baseline_test():
    rng = random.Random(16)
    s, info = [], {}
    for i in range(12):
        acc = f"did:plc:c{i}"
        s.append(post(i, acc, T0 + timedelta(hours=2 * i), "", f"c{i}"))
        info[acc] = AccountInfo(
            key=acc, created_at=T0 - timedelta(days=3, hours=rng.uniform(0, 72))
        )
    untested = score_swarm(s, info)
    tested = score_swarm(s, info, baseline=Baseline(fresh_rate=0.05, n=500))
    common = score_swarm(s, info, baseline=Baseline(fresh_rate=0.9, n=500))
    sig = lambda r: next((x for x in r.signals if x.name == "same_cohort"), None)  # noqa: E731
    assert sig(untested).score <= 0.45
    assert sig(tested).score >= 0.7
    assert sig(tested).summary == "12 of 12 joined the same week"
    assert sig(common) is None  # the baseline says this is ordinary
    # a tested signal needs 8 accounts with a known age
    few = {k: v for k, v in list(info.items())[:7]}
    assert sig(score_swarm(s, few, baseline=Baseline(fresh_rate=0.05, n=500))).score <= 0.45


def test_five_of_five_lurkers_do_not_fire():
    """v1: 5 of 5 accounts without bio or avatar (binomial p = 0.0103) fired accounts -> swarm."""
    s = [post(i, f"did:plc:e{i}", T0 + timedelta(hours=4 * i), "", "cidE") for i in range(5)]
    info = {
        f"did:plc:e{i}": AccountInfo(key=f"did:plc:e{i}", has_avatar=False, bio="")
        for i in range(5)
    }
    for bl in (None, Baseline(empty_rate=0.4, fresh_rate=0.1, n=1000)):
        for by_file in (False, True):
            r = score_swarm(s, info, basis="live", baseline=bl, selected_by_file=by_file)
            assert r.families["accounts"] < 0.5
            assert r.verdict != "swarm"
    # 30 of 30 with a low base rate is a tested, firing signal
    s, info = swarm_case()
    r = score_swarm(s, info, baseline=Baseline(empty_rate=0.2, fresh_rate=0.05, n=1000))
    empty = next(x for x in r.signals if x.name == "empty_profiles")
    assert empty.score >= 0.85


def test_300_accounts_in_2_s_is_never_organic():
    """v1: 300 accounts within 2 s with varied words and own uploads -> organic ("Chill")."""
    rng = random.Random(1)
    s = [
        post(i, f"a{i}", T0 + timedelta(seconds=rng.uniform(0, 2)), own_text(rng, 10), f"m{i}")
        for i in range(300)
    ]
    r = score_swarm(s, window=WIN)
    assert "timing" in r.fired and r.p_value is not None and r.p_value <= 0.01
    assert r.verdict == "unclear" and r.why_unclear == "signs both ways"
    # without a window the 2 s cannot be tested, and a 2 s sample shows no spread either
    r = score_swarm(s)
    assert r.verdict == "unclear" and r.why_unclear == "posts span only minutes"


def test_fandom_handles_are_not_templated():
    """v1: fans named after one star shared a 5-letter prefix -> templated_handles -> swarm."""
    rng = random.Random(6)
    hs = [
        "taylorsversion",
        "taylornation13",
        "taylorgang",
        "taylorismymom",
        "swiftie89",
        "folklorefan",
        "taylorfan22",
        "tayloredit",
    ]
    s = [
        post(i, f"did:plc:t{i}", T0 + timedelta(seconds=rng.uniform(0, 900)), "", "cidT")
        for i in range(len(hs))
    ]
    info = {
        f"did:plc:t{i}": AccountInfo(key=f"did:plc:t{i}", handle=h + ".bsky.social")
        for i, h in enumerate(hs)
    }
    r = score_swarm(s, info, basis="live", selected_by_file=True, window=LIVE_WIN)
    assert not any(x.name == "templated_handles" for x in r.signals)
    assert r.verdict != "swarm"


def test_templated_handles_need_six_handles():
    s, info = swarm_case(n=5)
    assert not any(x.name == "templated_handles" for x in score_swarm(s, info).signals)
    s, info = swarm_case(n=12)
    assert any(x.name == "templated_handles" for x in score_swarm(s, info).signals)


def test_selected_by_file_drops_file_signals():
    s, info = swarm_case()
    r = score_swarm(s, info, selected_by_file=True, window=WIN)
    names = {x.name for x in r.signals}
    assert "same_file" not in names and "own_files" not in names
    assert r.verdict == "swarm"  # timing, accounts and copied words still agree
    r = score_swarm(s, None, selected_by_file=True)  # copy (words) only, no window
    assert r.verdict == "unclear"


def test_own_files_capped_and_not_for_x():
    rng = random.Random(17)
    s = [post(i, f"did:plc:o{i}", T0 + timedelta(hours=i), "", f"f{i}") for i in range(12)]
    g = next(x for x in score_swarm(s).signals if x.name == "own_files")
    assert g.score == 0.5 and g.polarity == "against"
    x = [
        Sighting(
            id=f"x{i}",
            platform="x",
            account=f"x:u{i}",
            at=T0 + timedelta(seconds=rng.uniform(0, 86400)),
            media_key=f"3_{i}",
        )
        for i in range(12)
    ]
    assert not any(y.name == "own_files" for y in score_swarm(x).signals)


def test_organic_needs_no_family_fired():
    s, info = organic_case()
    r = score_swarm(s, info, basis="live", window=(T0, T0 + timedelta(hours=48)))
    assert r.verdict == "organic" and not r.fired and r.guard >= 0.6


def test_clockwork_alone_cannot_carry_a_two_family_swarm():
    rng = random.Random(12)
    text = "everyone should buy this token today before the price goes up again"
    s = [
        post(i, f"x:sched{i}", T0 + timedelta(seconds=97 * i + rng.uniform(-2, 2)), text)
        for i in range(15)
    ]
    r = score_swarm(s)
    assert {"timing", "copy"} <= set(r.fired)
    lead = max((x for x in r.signals if x.family == "timing"), key=lambda x: x.score)
    assert lead.name == "clockwork"  # thresholded, no p-value
    assert r.verdict == "unclear" and r.why_unclear == "timing could be chance"


def test_same_post_under_several_ids_is_one_post():
    """v1: each post seen as p<i>, a URL and dup<i> -> "5 accounts posted 3+ times"."""
    s = []
    for i in range(5):
        at = T0 + timedelta(hours=3 * i)
        for sid in (f"p{i}", f"https://x.com/u{i}/status/{i}", f"dup{i}"):
            s.append(Sighting(id=sid, platform="x", account=f"x:u{i}", at=at, media_key=f"m{i}"))
    s.append(
        Sighting(
            id="b0",
            platform="x",
            account="x:fan",
            kind="repost",
            target="dup0",
            at=T0 + timedelta(seconds=5),
        )
    )
    r = score_swarm(s)
    assert r.n_posts == 5 and r.n_boosts == 1
    assert not any(x.name == "repeat_posters" for x in r.signals)


def test_duplicate_ids_resolve_the_same_in_any_order():
    text = "buy this coin now it is going to the moon today"
    a = Sighting(id="p0", platform="x", account="x:u0", at=T0, text=text)
    b = Sighting(id="p0", platform="x", account="x:u0", at=T0, text="")
    rest = [
        Sighting(
            id=f"p{i}", platform="x", account=f"x:u{i}", at=T0 + timedelta(minutes=i), text=text
        )
        for i in range(1, 6)
    ]
    assert score_swarm([a, b, *rest]).model_dump() == score_swarm([b, a, *rest]).model_dump()


def test_repeat_posters_needs_three_accounts():
    s = [post(i, "x:loud", T0 + timedelta(minutes=10 * i), "", f"m{i}") for i in range(50)]
    s += [post(100 + i, f"x:o{i}", T0 + timedelta(hours=2 * i), "", f"n{i}") for i in range(4)]
    assert not any(x.name == "repeat_posters" for x in score_swarm(s).signals)
    s = [
        post(f"{a}_{j}", f"x:r{a}", T0 + timedelta(hours=5 * j + a), "", f"m{a}{j}")
        for a in range(6)
        for j in range(3 if a < 3 else 1)
    ]
    rep = next(x for x in score_swarm(s).signals if x.name == "repeat_posters")
    assert rep.summary == "3 accounts posted it 3+ times"


def test_only_timing_and_copy_link_posts():
    s, info = swarm_case()
    k = 0
    for i in range(30):
        for j in (1, 2, 3):
            s.append(
                Sighting(
                    id=f"r{k}",
                    platform="bluesky",
                    account=f"did:plc:bot{(i + j) % 30}",
                    kind="repost",
                    target=f"s{i}",
                    at=s[i].at + timedelta(seconds=5 * j),
                )
            )
            k += 1
    r = score_swarm(s, info, window=WIN)
    assert {x.family for x in r.signals if x.examples} <= {"timing", "copy"}
    assert all(not x.examples for x in r.signals if x.family in ("accounts", "boost"))
    assert any(x.name == "fast_boost" for x in r.signals)
    assert all(not x.examples for x in r.signals if x.polarity == "against")


def test_scores_rank_by_strength_not_saturate():
    small, info_s = swarm_case(n=8, seed=3)
    big, info_b = swarm_case(n=40, seed=3)
    a = score_swarm(small, info_s, selected_by_file=True, window=WIN)
    b = score_swarm(big, info_b, selected_by_file=True, window=WIN)
    assert a.verdict == b.verdict == "swarm"
    assert a.score < b.score < 1.0


# ---------------------------------------------------------------- swarm-v3 regressions ----------
# Two ad rings from production (live, selected_by_file, 24 h window) that swarm-v2 called
# "unclear": the accounts family stopped at 0.49 because shares were scored raw, not as their
# excess over the baseline, and minted handles (nextbuy01, nextbuy-2, logannb001) were missed.

PROD_BASELINE = Baseline(fresh_rate=0.0308, n=38459)
RING_BIO = "Premium research peptides for laboratory use only fast worldwide shipping"
RING_1 = (
    "nextbuyvip89 emmaelizebethi76 nextbuy01 nextbuyvip03 nextbuy nextbuy2 nextbuy346 nextbuy84 "
    "nextbuy567i nextbuy-1 nextbuy-2 nextbuy-3 jhonnext1 logannb001 scottnb002 chalesnb003 "
    "reminext04 drakenext04 mitchnb07 philipnb06 pamelanb08 lissanb09 carltonnb10 bily92 "
    "harry2026lee frank1999gsb"
).split()
RING_2 = (
    "goodstuff05 goodstu goodstuff19-96 goodstuff19 goodstuff19-00 goodstuff19-0 goodstuff94 "
    "goodstuff90 goodstuff01 goodstuff02 goodstuff033 goodstuff998 goodstuff56 seaversgood01 "
    "jodygood02 hawwigood03 willgood04 carltongood05 jorsh01 andyconor90 gemma1g nextbuy-3 "
    "genlabs101 genlabs202 goodstuff-3 goodstuff-2 goodstuff-1"
).split()


def ring_case(handles, *, fresh, same_week, bios, alike_bios, top_text, burst_n, burst_s, seed):
    """One picture posted once by each ring account over ~6 h: `burst_n` of them inside `burst_s`
    seconds, `fresh` accounts under 30 days old (`same_week` of them made in one week), `bios`
    accounts with a bio (`alike_bios` the same), and the ad copy: `top_text` accounts with one
    7-word line, all but one of the rest in three other copied 8-word lines."""
    rng = random.Random(seed)
    n = len(handles)
    first = T0 - timedelta(hours=10)
    offs = sorted(rng.uniform(0, burst_s) for _ in range(burst_n))
    offs += sorted(rng.uniform(burst_s + 1800, 6 * 3600) for _ in range(n - burst_n))
    lines = [
        "SHOP NOW next generation metabolic research peptide",
        "best metabolic research peptide on the market today",
        "order your research peptide today with fast shipping",
        "lab grade metabolic peptide now in stock order now",
    ]
    rest = n - top_text - 1
    texts = [lines[0]] * top_text
    for j in range(rest):
        texts.append(lines[1 + j % 3])
    texts.append("my cat thinks this is a very strange advert")
    s, info = [], {}
    for i, h in enumerate(handles):
        acc = f"did:plc:ring{seed}x{i}"
        at = first + timedelta(seconds=offs[i])
        s.append(post(f"r{seed}_{i}", acc, at, texts[i], "cidRing"))
        if i < same_week:
            created = first - timedelta(days=12, hours=rng.uniform(0, 120))
        elif i < fresh:
            created = first - timedelta(days=rng.uniform(1, 28))
        else:
            created = first - timedelta(days=rng.uniform(60, 900))
        bio = (
            (RING_BIO if i < alike_bios else "dad runner coffee and long walks") if i < bios else ""
        )
        info[acc] = AccountInfo(
            key=acc,
            handle=f"{h}.bsky.social",
            created_at=created,
            followers=rng.randint(0, 40),
            bio=bio,
            has_avatar=rng.random() < 0.5,
        )
    return s, info


def score_live(s, info):
    win = (T0 - timedelta(hours=23), T0 + timedelta(hours=1))
    return score_swarm(
        s, info, basis="live", selected_by_file=True, window=win, baseline=PROD_BASELINE
    )


def test_ad_ring_nextbuy_is_swarm():
    s, info = ring_case(
        RING_1,
        fresh=14,
        same_week=11,
        bios=7,
        alike_bios=6,
        top_text=15,
        burst_n=11,
        burst_s=300,
        seed=35,
    )
    r = score_live(s, info)
    assert r.verdict == "swarm", (r.why_unclear, r.families)
    assert {"accounts", "copy"} <= set(r.fired)
    names = {x.name: x for x in r.signals}
    assert names["same_cohort"].score >= 0.4  # 14 of 26 fresh against a 3% baseline
    assert names["templated_handles"].score >= 0.5  # nextbuy + counter, name + nb + counter
    assert names["same_bio"].score >= 0.5  # 6 of the 7 bios there are
    assert r.families["copy"] >= 0.5 and r.guard < 0.6


def test_ad_ring_goodstuff_is_swarm():
    s, info = ring_case(
        RING_2,
        fresh=16,
        same_week=7,
        bios=6,
        alike_bios=5,
        top_text=15,
        burst_n=7,
        burst_s=960,
        seed=36,
    )
    r = score_live(s, info)
    assert r.verdict == "swarm", (r.why_unclear, r.families)
    assert {"accounts", "copy"} <= set(r.fired)
    names = {x.name: x for x in r.signals}
    assert names["same_cohort"].score >= 0.5
    assert names["templated_handles"].score >= 0.5
    assert names["templated_handles"].numbers["minted"] >= 16


def test_tested_share_scores_its_excess_over_the_baseline():
    s, info = ring_case(
        RING_1,
        fresh=14,
        same_week=0,
        bios=0,
        alike_bios=0,
        top_text=15,
        burst_n=0,
        burst_s=300,
        seed=37,
    )
    sig = lambda r: next(x for x in r.signals if x.name == "same_cohort")  # noqa: E731
    low = sig(score_swarm(s, info, baseline=Baseline(fresh_rate=0.03, n=5000)))
    high = sig(score_swarm(s, info, baseline=Baseline(fresh_rate=0.25, n=5000)))
    assert low.score > 0.4 > high.score > 0  # same 14 of 26, weaker against a higher rate
    assert low.score > 1.2 * 0.355  # v2 scored the raw share's bound (0.355) whatever the rate
    assert sig(score_swarm(s, info)).score <= 0.45  # no baseline: untested, capped


def test_fans_and_ordinary_names_are_not_minted():
    rng = random.Random(40)
    cases = [
        # fans with meaningful numbers on one stem
        [
            "swiftie13",
            "swiftie89",
            "swiftie1989",
            "swiftie22",
            "swiftie2014",
            "folklorefan",
            "taylornation13",
            "lovergirl",
            "midnightsrain",
            "cardigan",
        ],
        # ordinary first names with small numbers that share a last letter pair
        [
            "dylan01",
            "logan7",
            "ryan3",
            "brian12",
            "megan2001",
            "susan88",
            "katie",
            "marcus",
            "olivia",
            "noah",
        ],
    ]
    for hs in cases:
        s = [
            post(i, f"did:plc:o{i}", T0 + timedelta(seconds=rng.uniform(0, 900)), "", "cidO")
            for i in range(len(hs))
        ]
        info = {
            f"did:plc:o{i}": AccountInfo(key=f"did:plc:o{i}", handle=f"{h}.bsky.social")
            for i, h in enumerate(hs)
        }
        r = score_swarm(s, info, basis="live", selected_by_file=True, window=LIVE_WIN)
        t = next((x for x in r.signals if x.name == "templated_handles"), None)
        assert t is None or t.score < 0.5, (hs, t)
        assert r.verdict != "swarm"


def test_same_bio_is_measured_among_accounts_with_a_bio():
    accs = [f"did:plc:b{i}" for i in range(20)]
    s = [post(i, a, T0 + timedelta(hours=i), "", f"c{i}") for i, a in enumerate(accs)]
    info = {a: AccountInfo(key=a, bio=RING_BIO if i < 5 else "") for i, a in enumerate(accs)}
    info[accs[5]] = AccountInfo(key=accs[5], bio="I paint birds and drink too much coffee")
    bio = next(x for x in score_swarm(s, info).signals if x.name == "same_bio")
    assert bio.numbers == {"kind": "bio", "alike": 5, "with_bio": 6}
    assert bio.score >= 0.5
    # three accounts sharing a short generic bio among many different ones stay low
    info = {
        a: AccountInfo(key=a, bio="love my family" if i < 3 else own_text(random.Random(i), 6))
        for i, a in enumerate(accs)
    }
    bio = next((x for x in score_swarm(s, info).signals if x.name == "same_bio"), None)
    assert bio is None or bio.score < 0.2


# ---------------------------------------------------------------- one_publisher ----------------
# A leisure trust's venue accounts posting one job advert at 09:00 with a scheduler is
# coordinated but not inauthentic. one_publisher is the only guard that overrides fired families,
# and only when the accounts family did not fire.

PUB_NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
PUB_WIN = (PUB_NOW - timedelta(hours=20), PUB_NOW + timedelta(hours=4))


def _day(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def publisher_case(rows, texts, times, *, avatar=True):
    """rows: (handle, created_at, followers, display name, bio)"""
    s, info = [], {}
    for i, (h, created, followers, name, bio) in enumerate(rows):
        acc = f"did:plc:{h.replace('-', '').replace('_', '')}"
        s.append(post(f"pub_{i}", acc, times[i], texts[i], "cidPub"))
        info[acc] = AccountInfo(
            key=acc,
            handle=f"{h}.bsky.social",
            created_at=created,
            followers=followers,
            following=1,
            name=name,
            bio=bio,
            has_avatar=avatar,
        )
    return s, info


def score_pub(s, info, baseline=PROD_BASELINE):
    return score_swarm(
        s, info, basis="live", selected_by_file=True, window=PUB_WIN, baseline=baseline
    )


def cv_life_case():
    """Group 29ae401b0260: CV Life (Coventry) and four venue accounts, one job advert, 2 s."""
    rows = [("cvlife", _day("2024-12-06"), 74, None, None)] + [
        (f"{venue}-coventry", _day("2025-01-02"), 2 + j * 2, None, None)
        for j, venue in enumerate(["ahc", "at7", "moathouse", "xcel"])
    ]
    rng = random.Random(29)
    text = "We're hiring! Join our team as a Leisure Assistant. Apply now at cvlife.com/jobs"
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 2)) for _ in rows]
    return publisher_case(rows, [text] * len(rows), times)


ROOF_CITIES = [
    ("greenfd", "Greenfield"),
    ("jackson", "Jackson"),
    ("kenosha", "Kenosha"),
    ("milwaukee", "Milwaukee"),
    ("oakcreek", "Oak Creek"),
    ("waukesha", "Waukesha"),
    ("wauwatosa", "Wauwatosa"),
]
ROOF_TEXT = (
    "Roof quote: full scope, free estimate, gutters and siding too. Online: https://example.com/q"
)


def roof_depot_case():
    rows = [
        (
            f"roofdepot{c}",
            _day("2026-05-29"),
            4 + j % 3,
            f"Roof Depot {n}",
            f"Roofing, gutters, and exterior upgrades in {n}, WI. https://example.com/{c}",
        )
        for j, (c, n) in enumerate(ROOF_CITIES)
    ]
    rng = random.Random(30)
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 840)) for _ in rows]
    return publisher_case(rows, [ROOF_TEXT] * len(rows), times)


def quality_first_case():
    rows = [
        (f"qualityfirst{c}", _day(d), f, f"Quality First {c.title()}", None)
        for c, d, f in [
            ("camp", "2026-05-07", 48),
            ("conc", "2026-05-12", 60),
            ("home", "2026-05-20", 86),
            ("reno", "2026-05-28", 70),
            ("sac", "2026-06-02", 55),
        ]
    ]
    rng = random.Random(31)
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 600)) for _ in rows]
    return publisher_case(rows, [ROOF_TEXT] * len(rows), times)


def test_cv_life_job_advert_is_organic():
    """v3 called this Shill: timing (5 accounts in 2 s) and copy (same words x5) fired."""
    r = score_pub(*cv_life_case())
    assert {"timing", "copy"} <= set(r.fired) and "accounts" not in r.fired
    assert r.verdict == "organic", (r.why_unclear, r.fired)
    pub = next(x for x in r.signals if x.name == "one_publisher")
    assert pub.summary == "4 accounts, one name"
    assert pub.polarity == "against" and pub.family == "accounts" and not pub.examples
    assert pub.score >= 0.6 and pub.numbers["established"] == 1
    assert r.guard >= 0.6


def test_franchise_accounts_with_one_name_are_unclear():
    """Five-month-old branch accounts: marketing and an ad network look the same."""
    for case in (roof_depot_case, quality_first_case):
        r = score_pub(*case())
        assert {"timing", "copy"} <= set(r.fired) and "accounts" not in r.fired, case
        assert r.verdict == "unclear" and r.why_unclear == "one name, new accounts", case
        pub = next(x for x in r.signals if x.name == "one_publisher")
        assert pub.numbers["established"] == 0


def test_rings_are_not_one_publisher():
    for kw in (
        dict(
            handles=RING_1,
            fresh=14,
            same_week=11,
            bios=7,
            alike_bios=6,
            top_text=15,
            burst_n=11,
            burst_s=300,
            seed=35,
        ),
        dict(
            handles=RING_2,
            fresh=16,
            same_week=7,
            bios=6,
            alike_bios=5,
            top_text=15,
            burst_n=7,
            burst_s=960,
            seed=36,
        ),
    ):
        r = score_live(*ring_case(**kw))
        assert r.verdict == "swarm"
        assert not any(x.name == "one_publisher" for x in r.signals)


def test_genlabs_like_ring_stays_swarm():
    rng = random.Random(53)
    hs = (
        [f"genlabs{n}" for n in range(30, 52)]
        + ["genlab51", "genlab47"]
        + [f"yourmuscleshop{n}" for n in range(1, 25)]
        + ["bobby1", "kate22", "mike_rx", "lara", "tomtom7"]
    )
    rows = []
    for j, h in enumerate(hs):
        if j < 36:
            created = PUB_NOW - timedelta(days=rng.uniform(2, 25))
        elif j < 43:
            created = PUB_NOW - timedelta(days=rng.uniform(60, 700))
        else:
            created = None
        rows.append((h, created, rng.randint(0, 30), None, None))
    texts = [
        rng.choice(
            [
                "GenLabs research peptides lab tested and shipped worldwide order today",
                "best research peptides at GenLabs lab tested and shipped order now",
            ]
        )
        for _ in rows
    ]
    times = [PUB_NOW - timedelta(seconds=rng.uniform(0, 6 * 3600)) for _ in rows]
    r = score_pub(*publisher_case(rows, texts, times, avatar=False))
    assert r.verdict == "swarm" and "accounts" in r.fired
    assert not any(x.name == "one_publisher" for x in r.signals)


def test_fresh_accounts_sharing_a_name_are_not_excused():
    """A ring that names its fresh accounts stem + place: the fresh test fires, no excuse."""
    places = [
        "texas",
        "ohio",
        "florida",
        "georgia",
        "arizona",
        "nevada",
        "iowa",
        "utah",
        "maine",
        "idaho",
    ]
    rows = [
        (f"patriotvoice{p}", PUB_NOW - timedelta(days=9, hours=j), 3, None, None)
        for j, p in enumerate(places)
    ]
    rng = random.Random(41)
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 120)) for _ in rows]
    text = "They are lying to you about the election wake up and share this now"
    s, info = publisher_case(rows, [text] * len(rows), times)
    r = score_pub(s, info)
    assert r.verdict == "swarm" and "accounts" in r.fired
    assert not any(x.name == "one_publisher" for x in r.signals)
    # without a baseline (a lookup) the fresh test cannot fire: abstain, never organic
    r = score_pub(s, info, baseline=None)
    assert r.verdict == "unclear" and r.why_unclear == "one name, new accounts"


def test_generic_tokens_are_not_one_name():
    rng = random.Random(42)
    words = ["daily", "city", "tech", "sport", "metro", "local", "world", "crypto", "game", "film"]
    rows = [
        (
            f"{w}{k}news" if k else f"{w}news",
            _day("2020-03-01"),
            500,
            "Official News",
            None,
        )
        for k in ("", "the", "real")
        for w in words
    ]
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 60)) for _ in rows]
    text = "BREAKING: the river bridge is closed in both directions until further notice"
    # all old and created on one day, so only the generic exclusion stops the guard
    r = score_pub(*publisher_case(rows, [text] * len(rows), times))
    assert len(rows) == 30
    assert not any(x.name == "one_publisher" for x in r.signals)
    assert r.verdict != "organic"


def test_fans_sharing_a_stem_are_not_one_publisher():
    rng = random.Random(43)
    hs = [
        "taylorsversion",
        "taylornation",
        "taylorgang",
        "taylorismymom",
        "taylorfan",
        "tayloredit",
        "swiftiesforever",
        "folklorefan",
    ]
    rows = [
        (h, _day("2019-01-01") + timedelta(days=rng.uniform(0, 2000)), 300, None, None) for h in hs
    ]
    times = [PUB_NOW + timedelta(seconds=rng.uniform(0, 30)) for _ in rows]
    text = "she really said that and the whole fandom lost it today again"
    r = score_pub(*publisher_case(rows, [text] * len(rows), times))
    assert not any(x.name == "one_publisher" for x in r.signals)  # not created together
    assert r.verdict != "organic"
