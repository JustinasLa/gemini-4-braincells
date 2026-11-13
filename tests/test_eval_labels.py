"""Hand-label eval (eval/labels.py, eval/label_server.py). Offline: no DB, no network."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from eval import labels as L
from eval.label_server import create_app

T0 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)  # even hour -> dev


# ---------------------------------------------------------------- kappa / rates


def test_kappa_perfect_and_chance():
    perfect = [("coordinated", "coordinated"), ("organic", "organic"), ("cant_tell", "cant_tell")]
    assert L.cohen_kappa(perfect)["kappa"] == 1.0
    # rater B always says organic: observed agreement equals chance agreement -> kappa 0
    pairs = [("coordinated", "organic"), ("organic", "organic")] * 5
    k = L.cohen_kappa(pairs, L.DEFINITE)
    assert k["kappa"] == 0.0
    assert k["n"] == 10


def test_kappa_textbook_value():
    # 2x2 table: both yes 20, A yes/B no 5, A no/B yes 10, both no 15 (n=50)
    # po = 0.7, pe = 0.5*0.6 + 0.5*0.4 = 0.5 -> kappa 0.4
    pairs = (
        [("coordinated", "coordinated")] * 20
        + [("coordinated", "organic")] * 5
        + [("organic", "coordinated")] * 10
        + [("organic", "organic")] * 15
    )
    k = L.cohen_kappa(pairs, L.DEFINITE)
    assert k["kappa"] == pytest.approx(0.4)
    assert k["observed"] == pytest.approx(0.7)
    assert k["expected"] == pytest.approx(0.5)
    lo, hi = k["ci95"]
    assert lo < 0.4 < hi


def test_kappa_undefined_and_empty():
    assert L.cohen_kappa([])["kappa"] is None
    same = [("organic", "organic")] * 4  # one category only: pe = 1
    assert L.cohen_kappa(same, L.DEFINITE)["kappa"] is None


def test_wilson_matches_known_interval():
    lo, hi = L.wilson(8, 10)
    assert (lo, hi) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))
    assert L.wilson(0, 0) is None
    lo, hi = L.wilson(0, 20)
    assert lo == 0.0 and 0 < hi < 0.2


def test_weighted_rate_equal_weights_is_plain_rate():
    r = L.weighted_rate([(True, 3.0)] * 8 + [(False, 3.0)] * 2)
    assert r["rate"] == 0.8 and r["n_eff"] == 10.0 and r["k"] == 8
    assert r["ci95"] == L.wilson(8, 10)


def test_weighted_rate_undoes_oversampling():
    # 1 hit from a stratum sampled 1-in-1, 1 miss from a stratum sampled 1-in-9
    r = L.weighted_rate([(True, 1.0), (False, 9.0)])
    assert r["rate"] == pytest.approx(0.1)
    assert r["n_eff"] < 2


# ---------------------------------------------------------------- sampler


def _cands(n_co=10, n_org=200, n_ins=60, seed=1):
    rng = random.Random(seed)
    out, cid = [], 0
    for status, k in (("coordinated", n_co), ("likely_organic", n_org), ("insufficient", n_ins)):
        for _ in range(k):
            cid += 1
            out.append(L.Candidate(cid, cid, status, round(rng.uniform(0.5, 1.0), 3), T0))
    return out


def test_sample_caps_rare_status_and_fills_total():
    picked, strata = L.stratified_sample(_cands(), 100, seed=7)
    assert len(picked) == 100
    by = {}
    for p in picked:
        by[p["candidate"].status] = by.get(p["candidate"].status, 0) + 1
    assert by["coordinated"] == 10  # only 10 exist: all of them
    assert by["likely_organic"] + by["insufficient"] == 90
    assert abs(by["likely_organic"] - by["insufficient"]) <= 1  # equal target shares
    assert sum(s["sampled"] for s in strata.values()) == 100
    assert len({p["candidate"].cluster_id for p in picked}) == 100  # no repeats


def test_sample_weights_and_bands():
    picked, strata = L.stratified_sample(_cands(), 100, seed=7)
    for p in picked:
        st = strata[p["stratum"]]
        assert p["weight"] == pytest.approx(st["population"] / st["sampled"])
        assert st["score_min"] <= p["candidate"].score <= st["score_max"]
    org = sorted((s for s in strata.values() if s["status"] == "likely_organic"),
                 key=lambda s: s["band"])  # fmt: skip
    assert len(org) == 4
    assert all(a["score_max"] <= b["score_min"] for a, b in zip(org, org[1:], strict=False))
    # weights sum to the population of each status
    tot = {}
    for p in picked:
        tot[p["candidate"].status] = tot.get(p["candidate"].status, 0) + p["weight"]
    assert tot["likely_organic"] == pytest.approx(200)
    assert tot["coordinated"] == pytest.approx(10)


def test_sample_is_deterministic_and_shuffled():
    a, _ = L.stratified_sample(_cands(), 50, seed=3)
    b, _ = L.stratified_sample(_cands(), 50, seed=3)
    assert [p["candidate"].cluster_id for p in a] == [p["candidate"].cluster_id for p in b]
    statuses = [p["candidate"].status for p in a]
    assert statuses != sorted(statuses)  # strata are interleaved in labelling order


def test_sample_smaller_population_than_n():
    picked, _ = L.stratified_sample(_cands(2, 3, 1), 100, seed=1)
    assert len(picked) == 6


def test_one_per_campaign():
    c = [L.Candidate(i, i // 3, "likely_organic", 0.6, T0) for i in range(9)]
    c += [
        L.Candidate(100, None, "coordinated", 0.9, T0),
        L.Candidate(101, None, "coordinated", 0.9, T0),
    ]
    reps = L.one_per_campaign(c, random.Random(0))
    assert len(reps) == 3 + 2  # 3 campaigns, plus 2 clusters without a campaign
    assert len({r.campaign_id for r in reps if r.campaign_id is not None}) == 3


def test_split_rule():
    assert L.split_of(T0) == "dev"
    assert L.split_of(T0 + timedelta(hours=1)) == "heldout"


# ---------------------------------------------------------------- evidence is blind


def _evidence():
    ws, we = T0, T0 + timedelta(minutes=10)
    members = [
        {"did": f"did:plc:m{i}", "role": "core", "handle": f"m{i}.bsky.social",
         "display_name": None, "created_at": T0 - timedelta(days=3 + i), "did_created_at": None,
         "followers": i, "follows": 10, "posts_count": 5, "has_avatar": False,
         "description_len": 0, "status": None}
        for i in range(4)
    ]  # fmt: skip
    posts = [
        {"uri": f"at://did:plc:m{i}/app.bsky.feed.post/p{i}", "did": f"did:plc:m{i}",
         "text": "buy $XYZ now", "created_at": ws, "seen_at": ws + timedelta(seconds=10 * i),
         "reply_to": None, "quote_uri": None, "embed_urls": ["https://xyz.example/"],
         "media_hashes": [], "text_hash": "h1"}
        for i in range(4)
    ]  # fmt: skip
    target = "at://did:plc:other/app.bsky.feed.post/t1"
    actions = [
        {"kind": "repost", "did": f"did:plc:m{i}", "subject_uri": target,
         "subject_did": "did:plc:other", "seen_at": ws + timedelta(seconds=30 + i)}
        for i in range(3)
    ]  # fmt: skip
    return L.build_evidence(ws, we, "10m", members, posts, actions,
                            {target: {"did": "did:plc:other", "text": "promo"}}, [])  # fmt: skip


def test_build_evidence_aggregates():
    e = _evidence()
    assert e["summary"]["accounts"] == 4
    assert e["summary"]["younger_than_30d"] == 4
    assert e["summary"]["reposts"] == 3
    assert sum(e["timeline"]["post"]) == 4 and sum(e["timeline"]["repost"]) == 3
    assert e["shared_targets"][0]["accounts"] == 3
    assert e["shared_targets"][0]["url"] == "https://bsky.app/profile/did:plc:other/post/t1"
    assert e["shared_links"][0] == {"url": "https://xyz.example/", "posts": 4, "accounts": 4}
    assert e["repeated_texts"][0]["accounts"] == 4
    assert e["posts"][0]["t_s"] == 0


def _keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from _keys(v)


def test_evidence_has_no_detector_fields():
    keys = set(_keys(_evidence()))
    banned = {"status", "score", "p_value", "confidence", "families_fired", "cluster_id",
              "campaign_id", "signals", "verdict", "analyst", "detector_version"}  # fmt: skip
    assert not keys & banned


# ---------------------------------------------------------------- resolution + score


def _sample(statuses_weights):
    items = []
    for k, (status, w, split) in enumerate(statuses_weights, 1):
        items.append(
            {"item": k, "stratum": status, "weight": w, "split": split,
             "detector": {"cluster_id": 1000 + k, "status": status, "score": 0.9},
             "evidence": {}}
        )  # fmt: skip
    return {"name": "t", "items": items, "detector_versions": {"v": len(items)}}


def _lab(item, label, who="a"):
    return {"type": "label", "item": item, "labeller": who, "label": label}


def test_final_labels_rules():
    labs = {
        "a": {1: _lab(1, "coordinated"), 2: _lab(2, "organic"), 3: _lab(3, "organic")},
        "b": {1: _lab(1, "coordinated", "b"), 2: _lab(2, "coordinated", "b")},
    }
    adj = {}
    f = L.final_labels([1, 2, 3, 4], labs, adj)
    assert f[1] == {"label": "coordinated", "how": "agreed"}
    assert f[2]["how"] == "disagree" and f[2]["label"] is None
    assert f[3]["how"] == "single"
    assert f[4]["how"] == "unlabelled"
    f = L.final_labels([2], labs, {2: {"label": "organic"}})
    assert f[2] == {"label": "organic", "how": "adjudicated"}


def test_score_precision_fpr_and_kappa():
    # 4 flagged (w=1): humans say 3 coordinated, 1 organic -> precision 0.75
    # 4 chill (w=10): humans say organic -> FPR = 1 / (1 + 40) weighted
    s = _sample([("coordinated", 1.0, "dev")] * 4 + [("likely_organic", 10.0, "dev")] * 4)
    truth = ["coordinated", "coordinated", "coordinated", "organic"] + ["organic"] * 4
    labs = {who: {k: _lab(k, lab, who) for k, lab in enumerate(truth, 1)} for who in ("ann", "bob")}
    r = L.score(s, labs, {})
    m = r["by_split"]["dev"]
    assert m["precision"]["rate"] == 0.75 and m["precision"]["n"] == 4
    assert m["fpr"]["k"] == 1 and m["fpr"]["n"] == 5
    assert m["fpr"]["rate"] == pytest.approx(1 / 41, abs=1e-4)
    assert m["coordinated_among_chill"]["rate"] == 0.0
    assert r["agreement"]["labellers"] == ["ann", "bob"]
    assert r["agreement"]["kappa_3way"]["kappa"] == 1.0
    assert r["by_split"]["heldout"]["n_definite"] == 0
    rows = L.eval_rows(r, s)
    assert [row["split"] for row in rows] == ["dev"]  # no row for an empty split
    assert rows[0]["dataset"] == "live_hand_labels"
    assert rows[0]["precision"] == 0.75 and rows[0]["n"] == 8


def test_cant_tell_excluded_from_rates():
    s = _sample([("coordinated", 1.0, "heldout")] * 2)
    labs = {w: {1: _lab(1, "cant_tell", w), 2: _lab(2, "coordinated", w)} for w in ("a", "b")}
    m = L.score(s, labs, {})["by_split"]["heldout"]
    assert m["n_cant_tell"] == 1 and m["precision"]["n"] == 1 and m["precision"]["rate"] == 1.0


# ---------------------------------------------------------------- store + server


def test_store_latest_wins(tmp_path):
    p = tmp_path / "x.labels.jsonl"
    L.append_record(p, _lab(1, "organic"))
    L.append_record(p, _lab(1, "coordinated"))
    L.append_record(p, {"type": "adjudication", "item": 2, "labeller": "c", "label": "organic"})
    labs, adj = L.load_records(p)
    assert labs["a"][1]["label"] == "coordinated"
    assert adj[2]["label"] == "organic"
    with pytest.raises(ValueError):
        L.append_record(p, _lab(1, "spam"))


def test_clean_name():
    assert L.clean_name(" Justinas ") == "justinas"
    with pytest.raises(ValueError):
        L.clean_name("   ")


@pytest.fixture
def client(tmp_path):
    s = _sample([("coordinated", 1.0, "dev"), ("likely_organic", 5.0, "heldout")])
    for it in s["items"]:
        it["evidence"] = {"secret_free": True}
    s["created_at"] = "2026-11-13T22:00:00+00:00"
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "t.json").write_text(json.dumps(s))
    return TestClient(create_app("t", root=tmp_path)), tmp_path


def test_server_never_sends_detector_block(client):
    c, _ = client
    assert c.get("/").status_code == 200
    r = c.get("/api/item/1", params={"labeller": "Ann"}).json()
    assert set(r) == {"item", "n_items", "evidence", "mine"}
    blob = json.dumps(r)
    assert "coordinated" not in blob and "1001" not in blob and "status" not in blob


def test_server_label_flow_and_disagreements(client):
    c, root = client

    def post(path, item, who, label):
        return c.post(path, json={"item": item, "labeller": who, "label": label})

    assert post("/api/label", 1, "ann", "coordinated").is_success
    assert post("/api/label", 1, "bob", "organic").is_success
    assert (
        c.post("/api/label", json={"item": 2, "labeller": "ann", "label": "bogus"}).status_code
        == 400
    )
    assert (
        c.post("/api/label", json={"item": 9, "labeller": "ann", "label": "organic"}).status_code
        == 404
    )
    p = c.get("/api/progress", params={"labeller": "ann"}).json()
    assert p["done"] == {"1": "coordinated"} and p["labellers"] == {"ann": 1, "bob": 1}
    assert p["disagreements"] == 1
    d = c.get("/api/disagreements").json()["items"]
    assert d[0]["item"] == 1 and {x["label"] for x in d[0]["labels"]} == {"coordinated", "organic"}
    assert post("/api/adjudicate", 1, "ann", "organic").is_success
    assert c.get("/api/disagreements").json()["items"] == []
    labs, adj = L.load_records(L.labels_path("t", root))
    assert adj[1]["label"] == "organic" and labs["bob"][1]["label"] == "organic"
    assert c.get("/api/item/1", params={"labeller": "ann"}).json()["mine"]["label"] == "coordinated"


def _race_write(path, k):
    for i in range(150):
        rec = {**_lab(i % 50 + 1, "organic", f"w{k}"), "note": "\U0001f642" * 2000}
        L.append_record(path, rec)


def _race_read(path, stop, errors):
    while not stop.is_set():
        try:
            L.load_records(path)
        except Exception:  # a torn record: JSON/Unicode decode error
            with errors.get_lock():
                errors.value += 1
            return


def test_store_readers_never_see_half_a_record(tmp_path):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    p, stop, errors = tmp_path / "c.labels.jsonl", ctx.Event(), ctx.Value("i", 0)
    writers = [ctx.Process(target=_race_write, args=(p, k)) for k in range(3)]
    readers = [ctx.Process(target=_race_read, args=(p, stop, errors)) for _ in range(3)]
    for x in writers + readers:
        x.start()
    for x in writers:
        x.join()
    stop.set()
    for x in readers:
        x.join()
    assert errors.value == 0
