"""Meme swarm scorer: synthetic swarms, organic spreads and the awkward inputs in between."""

import random
import time
from datetime import UTC, datetime, timedelta

from swarm.config import SwarmConfig
from swarm.model import FAMILIES, AccountInfo, Sighting
from swarm.score import Baseline, _binom_sf, _handle_shape, score_swarm

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
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
    r = score_swarm(s, info)
    assert r.verdict == "swarm", r
    assert set(r.fired) >= {"timing", "accounts", "copy"}
    assert r.score >= 0.9 and r.guard < 0.6
    assert r.p_value is not None and r.p_value <= 0.01
    names = {x.name: x for x in r.signals}
    assert {"burst", "same_text", "same_file", "same_cohort", "templated_handles"} <= set(names)
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
    r = score_swarm(s, info)
    assert r.families["timing"] >= 0.5  # the burst is real
    assert r.verdict != "swarm"
    assert r.verdict == "organic"


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
    assert fam(long) > 0.9 > 0.1 > fam(lol)


def test_empty_text_is_not_reuse():
    s = [post(i, f"x:u{i}", T0 + timedelta(hours=i)) for i in range(10)]
    r = score_swarm(s)
    assert r.families["copy"] == 0.0
    assert not any(x.name == "same_text" for x in r.signals)


def test_missing_account_info_still_works():
    s, _ = swarm_case(with_info=False)
    r = score_swarm(s, None)
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
        # created over a whole year, but all of the last 12 under 30 days old: no week bunch
        age = rng.uniform(1, 29) if i < 12 else rng.uniform(60, 400)
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
    assert clock.score >= 0.9
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
    assert ss.summary == "32 accounts within 3 s"
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
