"""lookup/: the Nemotron reading, two readings side by side, the step scheduler, the caches,
the SSE stream (no network).

The Nemotron replies in tests/fixtures/lookup/ were recorded from the Verda Super VL endpoint
(lookup/README.md has the measurement); the snapshot is a trimmed real x_post lookup.
Concurrency tests order steps with threading.Event, never with sleeps: a step that should
overlap another waits (bounded) for it and records whether it saw it.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import lookup as lookup_route
from lookup import cache as lcache
from lookup.assess import NemotronAssessor, _Draft, disagree, guard, parse_draft
from lookup.models import Account, LookupInput, LookupResult, Post
from lookup.service import Deps, Lookup
from media.nemotron import NotVisionModelError
from sources.models import PageFacts
from tests.test_lookup import (
    COPY,
    POST_MD,
    PROFILE_MD,
    SUBJ,
    TEXT,
    FakeOxylabs,
    fake_store,
)

FIX = Path(__file__).parent / "fixtures" / "lookup"
WAIT_S = 2.0  # bound on every Event wait: a wrong schedule fails the test, never hangs it
SUPER_VL = "nemotron-3.5-super-vl"
NOT_VL = "private/nvidia/nemotron-3.5-super-120b-a12b"  # Super, but not the VL model
X_URL = f"https://x.com/ada_example/status/{SUBJ}"


def _fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _snapshot() -> LookupResult:
    return LookupResult.model_validate(_fixture("x_post_snapshot.json"))


def _reply(content, *, model: str = SUPER_VL, finish: str = "stop", usage: dict | None = None):
    """An OpenAI ChatCompletion-shaped object (only the attributes the code reads)."""
    u = usage or {}
    details = u.get("completion_tokens_details") or {}
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(
            prompt_tokens=u.get("prompt_tokens"),
            completion_tokens=u.get("completion_tokens"),
            total_tokens=u.get("total_tokens"),
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=details.get("reasoning_tokens")
            ),
        ),
    )


def _recorded(name: str, **over):
    rec = _fixture(name)
    return _reply(
        rec["raw_content"],
        model=over.get("model", rec["raw_model"]),
        finish=rec["finish"],
        usage=rec["usage"],
    )


class FakeOpenAI:
    """client.chat.completions.create(**kw): records kw, replays the given reply."""

    def __init__(self, reply):
        self.calls: list[dict] = []
        self._reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self._reply


def _nemotron(reply, **kw) -> tuple[NemotronAssessor, FakeOpenAI]:
    client = FakeOpenAI(reply)
    na = NemotronAssessor(
        "", base_url="https://verda.example/v1", model=SUPER_VL, client=client, **kw
    )
    return na, client


def _until(pred) -> bool:
    """Wait (bounded) for a condition another thread makes true; True when it did."""
    end = time.monotonic() + WAIT_S
    while not pred():
        if time.monotonic() > end:
            return False
        threading.Event().wait(0.002)
    return True


class FakeReader:
    """A reader with a fixed call. `after(job)`: it answers only once `job` holds another
    reading, so the arrival order is fixed (the readers still start together)."""

    def __init__(self, provider: str, model: str, reading: str):
        self.provider, self.model, self.reading = provider, model, reading
        self.job: Lookup | None = None
        self.deadline: float | None = None
        self.saw_other: bool | None = None

    def after(self, job: Lookup) -> Lookup:
        self.job = job
        return job

    def assess(self, r: LookupResult, *, deadline: float | None = None):
        self.deadline = deadline
        if self.job is not None:
            self.saw_other = _until(lambda: len(self.job.result.readings) >= 1)
        d = _Draft(
            summary=f"{self.model} summary",
            origin="o",
            corroboration="c",
            spread_pattern="p",
            cites=[1],
            llm_reading=self.reading,
        )
        return guard(d, r, model=self.model, latency_ms=5)


def _deps(oxylabs=None, **kw) -> Deps:
    return Deps(oxylabs=oxylabs or FakeOxylabs(), bsky=None, **kw)


def _run(inp: LookupInput, deps: Deps, jid: str = "lk_f") -> Lookup:
    job = Lookup(jid, inp, deps)
    job.run()
    assert job.result.state == "done", job.result.error
    return job


def _steps(job: Lookup) -> dict:
    return {s.id: s for s in job.result.steps}


# ---------------------------------------------------------------- 1. NemotronAssessor


def test_nemotron_reading_from_recorded_reply():
    snap = _snapshot()
    rec = _fixture("nemotron_reply_x_post_off.json")
    raw_cites = json.loads(rec["raw_content"])["cites"]
    valid = {x.id for x in snap.receipts}
    assert set(raw_cites) - valid  # the trimmed snapshot leaves some cited ids unknown

    # the endpoint may answer with its full id: the Assessment carries what it returned
    returned = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
    na, client = _nemotron(_recorded("nemotron_reply_x_post_off.json", model=returned))
    a = na.assess(snap, deadline=time.monotonic() + 3.0)

    assert a.model == returned
    assert a.cites == [c for c in dict.fromkeys(raw_cites) if c in valid]  # unknown ids dropped
    assert set(a.cites) <= valid and a.cites
    assert a.llm_reading == "organic_looking"  # a reading, never a status
    assert a.llm_reading in ("organic_looking", "copy_campaign_looking", "unclear")
    assert a.cost_usd is None and a.latency_ms is not None

    (kw,) = client.calls
    assert kw["model"] == SUPER_VL
    rf = kw["response_format"]
    assert rf["type"] == "json_schema"
    assert set(rf["json_schema"]["schema"]["properties"]) >= {"summary", "cites", "llm_reading"}
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert 1.0 <= kw["timeout"] <= 3.0  # clamped to the lookup's deadline, not timeout_s=20
    assert all(isinstance(m["content"], str) for m in kw["messages"])  # text-only
    sent = json.loads(kw["messages"][1]["content"])
    assert {r["id"] for r in sent["receipts"]} == valid


def test_nemotron_request_options():
    snap = _snapshot()
    rep = _recorded("nemotron_reply_x_post_off.json")
    na, client = _nemotron(rep, reasoning=True, structured=False, timeout_s=15)
    na.assess(snap)  # no deadline: its own timeout
    na.assess(snap, deadline=time.monotonic() - 5)  # past deadline: the 1 s floor
    first, late = client.calls
    assert "response_format" not in first
    assert first["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert first["timeout"] == 15 and late["timeout"] == 1.0


def test_nemotron_reading_guard_and_schema():
    snap = _snapshot()
    body = json.loads(_fixture("nemotron_reply_x_post_off.json")["raw_content"])
    # only unknown ids cited: the call is dropped to "unclear"
    na, _ = _nemotron(_reply(json.dumps({**body, "cites": [900, 901]})))
    a = na.assess(snap)
    assert a.cites == [] and a.llm_reading == "unclear"
    # a status word is not a reading: the reply is rejected, nothing is stored
    na, _ = _nemotron(_reply(json.dumps({**body, "llm_reading": "matched"})))
    with pytest.raises(ValueError, match="no valid reading"):
        na.assess(snap)
    # fences and a <think> block around the JSON are tolerated
    wrapped = "<think>x</think>\n```json\n" + json.dumps(body) + "\n```"
    assert parse_draft(wrapped).llm_reading == "organic_looking"


# ---------------------------------------------------------------- 2. not the VL model


def test_non_vl_model_id_is_refused():
    na, _ = _nemotron(_recorded("nemotron_reply_x_post_off.json", model=NOT_VL))
    with pytest.raises(NotVisionModelError, match="not a Nemotron Super VL"):
        na.assess(_snapshot())


def test_non_vl_model_stores_no_reading_in_a_lookup():
    na, _ = _nemotron(_recorded("nemotron_reply_x_post_off.json", model=NOT_VL))
    job = _run(LookupInput(text=TEXT), _deps(assessor=na))
    r = job.result
    st = _steps(job)["assess"]
    assert st.state == "failed" and "NotVisionModelError" in st.detail
    assert r.assessment is None and r.readings == [] and not r.readings_disagree
    assert not any(rc.tool == "nemotron" for rc in r.receipts)
    assert _steps(job)["second_reading"].state == "skipped"


# ---------------------------------------------------------------- 3. empty "length" reply


def test_recorded_length_reply_is_a_value_error():
    rec = _fixture("nemotron_reply_length.json")
    assert rec["finish"] == "length" and not rec["raw_content"]
    na, _ = _nemotron(_recorded("nemotron_reply_length.json"))
    with pytest.raises(ValueError, match="finish_reason=length"):
        na.assess(_snapshot())


def test_length_reply_leaves_the_second_reading_as_the_assessment():
    na, _ = _nemotron(_recorded("nemotron_reply_length.json"))
    gem = FakeReader("gemini", "gemini-test", "organic_looking")
    job = _run(LookupInput(text=TEXT), _deps(assessor=na, second_assessor=gem))
    r = job.result
    st = _steps(job)
    assert st["assess"].state == "failed" and "finish_reason=length" in st["assess"].detail
    assert st["second_reading"].state == "done"
    assert r.assessment is not None and r.assessment.model == "gemini-test"
    assert [a.model for a in r.readings] == ["gemini-test"]
    assert not r.readings_disagree
    (rc,) = [rc for rc in r.receipts if rc.tool == "gemini"]
    assert rc.endpoint == "generateContent"


# ---------------------------------------------------------------- 4. two readings


@pytest.mark.parametrize(
    ("store", "status"), [(None, "insufficient"), (fake_store, "matched")], ids=["none", "matched"]
)
def test_disagreeing_readings_are_both_shown_and_never_set_status(store, status):
    nem = FakeReader("nemotron", SUPER_VL, "copy_campaign_looking")
    gem = FakeReader("gemini", "gemini-test", "organic_looking")
    deps = _deps(store=store, assessor=nem, second_assessor=gem, second_reading_s=7.0)
    t0 = time.monotonic()
    job = gem.after(Lookup("lk_d", LookupInput(text=TEXT), deps))
    job.run()
    r = job.result
    assert gem.saw_other  # both ran in parallel on the same snapshot
    assert [a.model for a in r.readings] == [SUPER_VL, "gemini-test"]
    assert r.assessment.model == SUPER_VL
    assert r.readings_disagree is True
    assert any("Both are shown; neither sets the status." in n for n in r.notes)
    assert r.status == status
    assert gem.deadline <= t0 + 7.0 + 1.0  # the second reading's own bound
    assert {rc.tool for rc in r.receipts} >= {"nemotron", "gemini"}
    (nrc,) = [rc for rc in r.receipts if rc.tool == "nemotron"]
    assert nrc.endpoint == "chat.completions"
    assert _steps(job)["assess"].state == _steps(job)["second_reading"].state == "done"


def test_primary_stays_the_assessment_when_the_second_arrives_first():
    gem = FakeReader("gemini", "gemini-test", "unclear")
    nem = FakeReader("nemotron", SUPER_VL, "organic_looking")
    job = nem.after(
        Lookup("lk_p", LookupInput(text=TEXT), _deps(assessor=nem, second_assessor=gem))
    )
    job.run()
    r = job.result
    assert nem.saw_other
    # Gemini's reading arrived first; the primary still leads `readings` and is `assessment`
    assert [a.model for a in r.readings] == [SUPER_VL, "gemini-test"]
    assert r.assessment.model == SUPER_VL
    assert r.readings_disagree  # 'unclear' is a call too


def test_agreeing_readings_add_no_note():
    nem = FakeReader("nemotron", SUPER_VL, "organic_looking")
    gem = FakeReader("gemini", "gemini-test", "organic_looking")
    job = _run(LookupInput(text=TEXT), _deps(assessor=nem, second_assessor=gem))
    assert len(job.result.readings) == 2 and not job.result.readings_disagree
    assert not any("readings differ" in n for n in job.result.notes)
    assert disagree([]) is False


# ---------------------------------------------------------------- 5. step scheduling

# the post links a page through an X card (t.co, resolved from its redirect header only)
CARD_URL = "https://t.co/abc123"
LINKED_URL = "https://news.example/comets"
CARD_LINES = (
    "[![Comets story](https://pbs.twimg.com/card_img/1/abc?format=jpg&name=small)\n\n"
    f"Comets land, officials say]({CARD_URL})[From news.example]({CARD_URL})\n\n"
)
_IMG = "![](https://pbs.twimg.com/media/HXAAAA?format=webp&name=large)\n\n"
# the post appears twice on the page (conversation, relevant people): the card in both
POST_CARD_MD = POST_MD.replace(_IMG + "[9:50 PM", CARD_LINES + "[9:50 PM").replace(
    "  " + _IMG + "  [9:50 PM",
    "".join("  " + ln + "\n" if ln else "\n" for ln in CARD_LINES.split("\n")[:-1]) + "  [9:50 PM",
)


def _redirects() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD" and str(request.url) == CARD_URL:
            return httpx.Response(301, headers={"location": LINKED_URL})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


class GatedOxylabs(FakeOxylabs):
    """FakeOxylabs whose slow calls wait (bounded) for the call that should overlap them."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self.searches = threading.Event()  # the X and the exact-text web search were called
        self.verify = threading.Event()  # a candidate copy's own page was asked for
        self.profile_saw_searches: bool | None = None
        self.linked_saw_verify: bool | None = None

    def search(self, query, max_results=10, deadline=None):
        kind = "x" if query.startswith("site:x.com") else "web" if query.startswith('"') else "-"
        with self._lock:
            self._seen.add(kind)
            if {"x", "web"} <= self._seen:
                self.searches.set()
        return super().search(query, max_results, deadline)

    def scrape_markdown(self, url, deadline=None):
        if url.endswith("/ada_example"):  # the author's profile: slow
            self.profile_saw_searches = self.searches.wait(WAIT_S)
        if url.endswith(SUBJ):
            self.calls.append(("scrape", url))
            return POST_CARD_MD, {}
        if url.endswith(COPY):
            self.verify.set()
        return super().scrape_markdown(url, deadline)

    def scrape_facts(self, url, deadline=None):
        if url == LINKED_URL:  # the linked page's fact extraction: slow
            self.linked_saw_verify = self.verify.wait(WAIT_S)
        self.calls.append(("facts", url))
        return PageFacts(url=url, final_url=url, publisher="Example News", headline="Comets")


def test_searches_start_before_the_author_profile_is_back():
    ox = GatedOxylabs()
    job = _run(LookupInput(url=X_URL), _deps(ox, http=_redirects()))
    st = _steps(job)
    assert ox.profile_saw_searches is True
    assert st["author"].state == "done"
    for sid in ("copies_x", "copies_web"):
        assert st[sid].state == "done"
        assert st[sid].started_at < st["author"].ended_at, sid


def test_verify_starts_before_a_slow_linked_page_finishes():
    ox = GatedOxylabs()
    job = _run(LookupInput(url=X_URL), _deps(ox, http=_redirects()))
    st = _steps(job)
    assert ox.linked_saw_verify is True
    assert st["linked"].state == "done" and st["verify"].state == "done"
    assert st["verify"].started_at < st["linked"].ended_at
    assert ("facts", LINKED_URL) in ox.calls  # the t.co card was resolved, then read
    (pg,) = [pg for pg in job.result.pages if pg.role == "linked"]
    assert pg.final_url == LINKED_URL
    posts = {p.id: p for p in job.result.posts}
    assert posts[f"x:{COPY}"].verified and posts[f"x:{COPY}"].role == "quote"


# ---------------------------------------------------------------- 6. web input

WEB_URL = "https://news.example/comets-story"
FACTS_HEADLINE = "Comets have landed, a joke about the ground stop"


class WebOxylabs(FakeOxylabs):
    """The page's markdown is quick; its fact extraction waits until a search was called."""

    def __init__(self):
        super().__init__()
        self.searched = threading.Event()
        self.facts_saw_search: bool | None = None

    def search(self, query, max_results=10, deadline=None):
        self.searched.set()
        return super().search(query, max_results, deadline)

    def scrape_markdown(self, url, deadline=None):
        if url == WEB_URL:
            self.calls.append(("scrape", url))
            return f"Example News\n\n# {TEXT}\n\nThe story.", {"url": url}
        return super().scrape_markdown(url, deadline)

    def scrape_facts(self, url, deadline=None):
        if url == WEB_URL:
            self.facts_saw_search = self.searched.wait(WAIT_S)
            self.calls.append(("facts", url))
            return PageFacts(
                url=url, final_url=url, publisher="Example News", headline=FACTS_HEADLINE
            )
        return super().scrape_facts(url, deadline)


def _web_page_checks(job: Lookup):
    r = job.result
    pages = [pg for pg in r.pages if pg.url == WEB_URL]
    assert len(pages) == 1
    (pg,) = pages
    facts_rc = next(rc for rc in r.receipts if rc.summary.startswith("Linked page read"))
    assert pg.role == "linked" and pg.receipt_id == facts_rc.id  # the facts, not the markdown
    assert pg.publisher == "Example News" and pg.headline == FACTS_HEADLINE
    assert r.subject_id == pg.id


def test_web_page_heading_starts_the_searches_before_the_facts():
    ox = WebOxylabs()
    job = _run(LookupInput(url=WEB_URL), _deps(ox))
    st = _steps(job)
    assert ox.facts_saw_search is True
    assert st["copies_x"].started_at < st["linked"].ended_at
    assert any(rc.summary.startswith("Page text read") for rc in job.result.receipts)
    _web_page_checks(job)


def test_web_page_facts_that_land_first_are_not_replaced_by_the_quick_read():
    """_page_text checks and adds in one critical section: when the fact extraction is back
    first, the quick read adds no page and no receipt, and the facts stand."""
    ox = WebOxylabs()
    ox.searched.set()  # the facts are not held back by the searches here
    linked_added = threading.Event()
    job = Lookup("lk_w", LookupInput(url=WEB_URL), _deps(ox))
    add_page, markdown = job.add_page, ox.scrape_markdown

    def gated_add_page(pg):
        out = add_page(pg)
        if pg.publisher:  # _linked's page
            linked_added.set()
        return out

    def slow_markdown(url, deadline=None):
        if url == WEB_URL:
            assert linked_added.wait(WAIT_S)
        return markdown(url, deadline)

    job.add_page, ox.scrape_markdown = gated_add_page, slow_markdown
    job.run()
    assert linked_added.is_set()
    assert not any(rc.summary.startswith("Page text read") for rc in job.result.receipts)
    _web_page_checks(job)


# ---------------------------------------------------------------- 7. verify / profile caps


def _copy_md(handle: str, sid: str, text: str = TEXT) -> str:
    """A post page in POST_MD's shape for another account (handles without underscores)."""
    md = POST_MD.replace(TEXT, text) if text != TEXT else POST_MD
    return (
        md.replace(SUBJ, sid)
        .replace("ada\\_example", handle)
        .replace("ada_example", handle)
        .replace("Ada Example", handle)
    )


IDS = {f"c{k}": str(2121370648207671400 + k) for k in range(1, 6)}


class CopierOxylabs(FakeOxylabs):
    def scrape_markdown(self, url, deadline=None):
        self.calls.append(("scrape", url))
        for name, sid in IDS.items():
            if url.endswith(sid):
                return _copy_md(f"copier{name[1:]}", sid), {}
        handle = url.rsplit("/", 1)[-1]
        if handle.startswith("copier"):
            return PROFILE_MD.replace("ada\\_example", handle).replace("ada_example", handle), {}
        raise RuntimeError("not found")


def _candidate(name: str, sim: float) -> Post:
    sid = IDS[name]
    h = f"copier{name[1:]}"
    return Post(
        id=f"x:{sid}",
        platform="x",
        url=f"https://x.com/{h}/status/{sid}",
        author_handle=h,
        text=TEXT,
        role="mention",
        similarity=sim,
    )


def test_verify_and_profile_caps_hold_across_queue_calls():
    ox = CopierOxylabs()
    job = Lookup("lk_c", LookupInput(text=TEXT), _deps(ox, verify_max=3, profiles_max=1))
    # copier1's account is already read: it is not read again and does not use the cap
    job.add_account(Account(platform="x", handle="Copier1", followers=5))
    for name, sim in (("c1", 0.9), ("c2", 0.8)):
        job.add_post(_candidate(name, sim))
    job._queue_verify()  # first call: both read (inline: no pool outside run())
    for name, sim in (("c3", 0.6), ("c4", 0.95), ("c5", 0.7)):
        job.add_post(_candidate(name, sim))
    job._queue_verify()  # second call: one slot left, the best match takes it
    job._queue_verify()  # nothing left

    scraped = [u for kind, u in ox.calls if kind == "scrape"]
    post_reads = [u for u in scraped if "/status/" in u]
    assert [u.rsplit("/", 1)[-1] for u in post_reads] == [IDS["c1"], IDS["c2"], IDS["c4"]]
    assert [u for u in scraped if "/status/" not in u] == ["https://x.com/copier2"]
    assert job._v_posts == [f"x:{IDS[n]}" for n in ("c1", "c2", "c4")]

    job._join_verify()
    st = _steps(job)["verify"]
    assert st.state == "done" and st.detail.startswith("3 read: 3 copies")
    assert "2 more candidates not read" in st.detail
    posts = {p.id: p for p in job.result.posts}
    for n in ("c3", "c5"):  # never read on their own page: never a copy
        assert posts[f"x:{IDS[n]}"].role == "mention" and not posts[f"x:{IDS[n]}"].verified
    assert all(p.verified for p in job.result.posts if p.role in ("copy", "origin", "quote"))
    job._finish_graph()
    assert job.result.spread.n_posts == 3  # only the verified copies count


def test_no_candidates_skips_verify():
    job = Lookup("lk_e", LookupInput(text=TEXT), _deps(CopierOxylabs()))
    job._queue_verify()
    job._join_verify()
    assert _steps(job)["verify"].state == "skipped"


# ---------------------------------------------------------------- 8. caches


def test_ttl_cache_expiry_and_lru():
    now = [100.0]
    c = lcache.TTLCache(ttl_s=10, max_items=2, clock=lambda: now[0])
    c["a"] = 1
    now[0] += 10
    assert c.get("a") == 1 and "a" in c  # not yet older than the TTL
    now[0] += 0.5
    assert c.get("a") is None and len(c) == 0  # expired and dropped
    c["a"], c["b"] = 1, 2
    assert c["a"] == 1  # touch: b is now the least recently used
    c["c"] = 3
    assert "b" not in c and c.get("a") == 1 and c.get("c") == 3
    with pytest.raises(KeyError):
        c["b"]
    off = lcache.TTLCache(ttl_s=0)
    off["x"] = 1
    assert len(off) == 0  # ttl 0: caching off


def test_cached_oxylabs_hit_miss_and_failures():
    inner = FakeOxylabs()
    now = [0.0]
    ox = lcache.CachedOxylabs(inner, lcache.TTLCache(ttl_s=60, clock=lambda: now[0]))
    first = ox.scrape_markdown(X_URL)
    call1 = dict(ox.last_call)
    assert call1["cached"] is False and call1["request_id"] == "r1" and call1["fetched_at"]
    assert ox.scrape_markdown(X_URL) == first
    assert inner.calls == [("scrape", X_URL)]  # the second read never reached the provider
    call2 = ox.last_call
    assert call2["cached"] is True and call2["latency_ms"] == 0
    assert call2["fetched_at"] == call1["fetched_at"]  # the time of the original read
    ox.search("q", max_results=5)
    ox.search("q", max_results=10)  # a different request: not the same cache entry
    assert [c for c in inner.calls if c[0] == "search"] == [("search", "q")] * 2
    # failures are never cached
    for _ in range(2):
        with pytest.raises(RuntimeError):
            ox.scrape_markdown("https://x.com/nobody")
    assert inner.calls.count(("scrape", "https://x.com/nobody")) == 2
    now[0] += 61  # TTL over: read again
    ox.scrape_markdown(X_URL)
    assert inner.calls.count(("scrape", X_URL)) == 2 and ox.last_call["cached"] is False


def test_cached_oxylabs_last_call_is_per_thread():
    inner = FakeOxylabs()
    ox = lcache.CachedOxylabs(inner, lcache.TTLCache(ttl_s=60))
    ox.scrape_markdown(X_URL)
    seen = {}

    def other():
        seen["before"] = dict(ox.last_call)
        ox.scrape_markdown(X_URL)
        seen["after"] = dict(ox.last_call)

    th = threading.Thread(target=other)
    th.start()
    th.join(WAIT_S)
    assert seen["before"] == {} and seen["after"]["cached"] is True
    assert ox.last_call["cached"] is False  # this thread's own call is untouched


def _two_cached_runs():
    inner = FakeOxylabs()
    store = lcache.TTLCache(ttl_s=600)
    ox = lcache.CachedOxylabs(inner, store)
    one = _run(LookupInput(url=X_URL), _deps(ox), "lk_1")
    n = len(inner.calls)
    two = _run(LookupInput(url=X_URL), _deps(ox), "lk_2")
    return inner, store, one, two, inner.calls[n:]


def test_cached_reads_in_a_lookup_say_so_and_keep_their_time():
    inner, store, one, two, again = _two_cached_runs()
    # only the failed read (the quoter's unreadable profile) went to the provider again
    assert again and set(again) == {("scrape", "https://x.com/cy_example")}
    ox1 = [rc for rc in one.result.receipts if rc.tool == "oxylabs"]
    ox2 = [rc for rc in two.result.receipts if rc.tool == "oxylabs"]
    assert ox1 and not any(rc.cached or "(cached read" in rc.summary for rc in ox1)
    assert ox2 and len(ox2) == len(ox1)
    fetched = store.get(("markdown", X_URL))[1]["fetched_at"]
    for rc in ox2:
        assert rc.cached and rc.latency_ms == 0
        assert rc.summary.endswith(" UTC)") and "(cached read from " in rc.summary
    subj_rc = next(rc for rc in ox2 if rc.source_url == X_URL)
    assert f"(cached read from {fetched:%H:%M:%S} UTC)" in subj_rc.summary
    subj = next(p for p in two.result.posts if p.id == f"x:{SUBJ}")
    assert subj.metrics_as_of == fetched  # X counts: as of the original read


def test_author_recent_counts_keep_the_cached_read_time():
    _inner, store, _one, two, _ = _two_cached_runs()
    fetched = store.get(("markdown", "https://x.com/ada_example"))[1]["fetched_at"]
    recent = [p for p in two.result.posts if p.role == "author_recent"]
    assert recent
    assert all(p.metrics_as_of == fetched for p in recent)


# ---------------------------------------------------------------- 9. SSE


class _Jobs:
    def __init__(self, *jobs):
        self._jobs = {j.result.id: j for j in jobs}

    def get(self, job_id):
        return self._jobs.get(job_id)


@contextmanager
def _client(jobs):
    app.dependency_overrides[lookup_route.get_jobs] = lambda: jobs
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(lookup_route.get_jobs, None)


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if not ln.startswith(":"))
        if lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_sse_finished_job_sends_one_snapshot_and_ends():
    job = _run(LookupInput(text=TEXT), _deps(), "lk_sse")
    with _client(_Jobs(job)) as c:
        with c.stream("GET", "/api/lookup/lk_sse/events") as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            body = "".join(res.iter_text())
        assert c.get("/api/lookup/lk_nope/events").status_code == 404
    (ev,) = _events(body)
    assert ev == ("snapshot", job.snapshot())


class _ScriptedJob:
    """A job whose snapshot runs through the given states (the last one repeats)."""

    def __init__(self, jid, snaps):
        self.result = SimpleNamespace(id=jid)
        self._snaps = list(snaps)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self._snaps.pop(0) if len(self._snaps) > 1 else self._snaps[0]


def test_sse_running_job_sends_changes_only_and_ends_after_done(monkeypatch):
    monkeypatch.setattr(lookup_route, "SSE_TICK_S", 0.001)
    a = {"id": "lk_run", "state": "running", "steps": ["post"]}
    b = {"id": "lk_run", "state": "running", "steps": ["post", "author"]}
    done = {"id": "lk_run", "state": "done", "steps": ["post", "author"]}
    job = _ScriptedJob("lk_run", [a, a, b, b, done])
    with _client(_Jobs(job)) as c, c.stream("GET", "/api/lookup/lk_run/events") as res:
        body = "".join(res.iter_text())
    assert _events(body) == [("snapshot", a), ("snapshot", b), ("snapshot", done)]
    assert job.calls == 5  # the stream stopped at the first non-running snapshot


# ---------------------------------------------------------------- 10. Bluesky last_call


def test_bsky_last_call_is_per_thread():
    from lookup.bsky import BlueskyClient

    c = BlueskyClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"posts": []}))
    )
    both = threading.Barrier(2, timeout=WAIT_S)
    seen = {}

    def worker(name, call):
        call()
        both.wait()  # both requests are done before either reads its last_call
        seen[name] = dict(c.last_call)

    threads = [
        threading.Thread(target=worker, args=("profile", lambda: c.profile("a.bsky.social"))),
        threading.Thread(target=worker, args=("search", lambda: c.search("comets"))),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(WAIT_S)
    assert seen["profile"]["endpoint"] == "app.bsky.actor.getProfile"
    assert seen["search"]["endpoint"] == "app.bsky.feed.searchPosts"
    assert seen["profile"]["status"] == seen["search"]["status"] == 200
    assert c.last_call == {}  # the main thread made no request


# ---------------------------------------------------------------- review round 1 (Sol) fixes


class EarlyTextLookup(Lookup):
    """A post step that publishes its text before its card and images (as _x_post does: the
    text is set a few statements before the card). The scheduler must wait for the step."""

    def _x_post(self):
        self._subject_text = TEXT
        _until_s(lambda: self._step("copies_x").state != "pending", 0.3)
        super()._x_post()


def _until_s(pred, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while not pred():
        if time.monotonic() > end:
            return False
        threading.Event().wait(0.002)
    return True


def test_searches_wait_for_the_whole_post_step_not_just_its_text():
    ox = GatedOxylabs()
    job = EarlyTextLookup("lk_e", LookupInput(url=X_URL), _deps(ox, http=_redirects()))
    job.run()
    st = _steps(job)
    assert st["copies_x"].started_at >= st["post"].ended_at
    assert st["linked"].state == "done"  # the card was known when the steps were planned
    assert st["media"].detail != "the post has no images"


IMG_URL = "https://pbs.twimg.com/media/HXAAAA?format=jpg&name=large"


class FakeImageReader:
    def __init__(self):
        self.calls: list[str | None] = []

    def analyze(self, media, *, context=None):
        from media.nemotron import MediaAnalysis

        self.calls.append(context)
        return MediaAnalysis(
            media_hash=media.media_hash, description="A chart.", extracted_text=[],
            reuse_cues=[], manipulation_cues=[], confidence=0.8, model=SUPER_VL, latency_ms=900,
        )  # fmt: skip


def _image_http(chunks=(b"\xff\xd8jpegbytes",), pause_s: float = 0.0) -> httpx.Client:
    def body():
        for c in chunks:
            if pause_s:
                threading.Event().wait(pause_s)
            yield c

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body())

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cached_image_reading_keeps_its_time_and_depends_on_the_post_text():
    reader = FakeImageReader()
    media, urls = lcache.TTLCache(ttl_s=600), lcache.TTLCache(ttl_s=600)

    def read(text: str):
        job = Lookup("lk_i", LookupInput(text=TEXT), _deps(nemotron=reader, media_cache=media,
                                                            media_urls=urls))  # fmt: skip
        job._subject_text = text
        assert job._read_image(_image_http(), IMG_URL).description == "A chart."
        (rc,) = [r for r in job.result.receipts if r.tool == "nemotron"]
        return rc

    first = read("a caption")
    assert not first.cached and "(cached read" not in first.summary and len(reader.calls) == 1
    (_, read_at) = media.get(next(iter(media._data)))
    second = read("a caption")
    assert second.cached and second.latency_ms == 0 and len(reader.calls) == 1
    assert f"(cached read from {read_at:%H:%M:%S} UTC)" in second.summary
    third = read("another caption on the same photo")  # the context changes the reading
    assert not third.cached and len(reader.calls) == 2


def test_image_download_stops_at_its_deadline():
    from lookup.service import _download_image

    http = _image_http(chunks=[b"\xff\xd8"] + [b"x" * 10] * 20, pause_s=0.03)
    data, _ = _download_image(http, IMG_URL, deadline=time.monotonic() + 0.1)
    assert data is None


def test_no_image_call_without_room_for_it_before_the_reading():
    reader = FakeImageReader()
    # 25 s budget: 10 s reading reserve leaves 15 s, less than one bounded image call needs
    job = _run(LookupInput(url=X_URL), _deps(nemotron=reader, http=_image_http(),
                                             time_budget_s=25))  # fmt: skip
    assert reader.calls == []
    assert "time budget used up" in (_steps(job)["media"].detail or "")


def test_cached_search_dates_are_relative_to_the_original_read():
    _inner, _store, one, two, _ = _two_cached_runs()

    def hint_time(job):
        return next(pg.time for pg in job.result.pages if pg.url == "https://news.example/story")

    assert hint_time(one) is not None and hint_time(two) == hint_time(one)


class SlowReader(FakeReader):
    def assess(self, r, *, deadline=None):
        self.started = True
        threading.Event().wait(1.4)  # past second_reading_s=1.2: the start check lets it run
        return super().assess(r, deadline=deadline)


def test_a_reading_past_its_own_bound_is_not_published():
    primary = FakeReader("nemotron", SUPER_VL, "organic_looking")
    late = SlowReader("gemini", "gemini-test", "copy_campaign_looking")
    job = _run(LookupInput(text=TEXT), _deps(assessor=primary, second_assessor=late,
                                             second_reading_s=1.2))  # fmt: skip
    assert getattr(late, "started", False)  # it was called, and answered too late
    assert [a.model for a in job.result.readings] == [SUPER_VL]
    assert not job.result.readings_disagree
    st = _steps(job)["second_reading"]
    assert st.state == "skipped" and "time" in (st.detail or "")


# ---------------------------------------------------------------- review round 2 (Sol) fixes


def test_lookup_image_client_never_waits_on_a_rate_limiter(monkeypatch):
    from common.config import Settings
    from lookup.service import IMAGE_CALL_S, IMAGE_NEED_S

    s = Settings(verda_inference_key="k", oxylabs_web_api_key="o", gemini_api_key="",
                 media_rpm=1)  # fmt: skip
    monkeypatch.setattr(lookup_route, "get_settings", lambda: s)
    deps = lookup_route.make_deps()
    # MEDIA_RPM=1 would space two image calls 60 s apart; the lookup's client has no spacing,
    # so one image call is bounded by its timeout, which IMAGE_NEED_S covers
    assert deps.nemotron._limiter.interval == 0.0
    assert deps.nemotron._client.timeout == IMAGE_CALL_S < IMAGE_NEED_S
    assert deps.assessor is not None and deps.second_assessor is None  # no Gemini key


class _FakeGemini:
    def __init__(self, version):
        self.version = version
        self.models = self

    def generate_content(self, *, model, contents, config):
        d = _Draft(summary="s", origin="o", corroboration="c", spread_pattern="p", cites=[1],
                   llm_reading="unclear")  # fmt: skip
        return SimpleNamespace(parsed=d, text=None, usage_metadata=None, model_version=self.version)


@pytest.mark.parametrize("version", ["gemini-4-hydrogen-001", None])
def test_gemini_reading_is_labelled_with_the_version_that_answered(version):
    from lookup.assess import Assessor

    a = Assessor.__new__(Assessor)
    a._client, a.model = _FakeGemini(version), "gemini-4-hydrogen"
    out = a.assess(_snapshot(), deadline=time.monotonic() + 5)
    assert out.model == (version or "gemini-4-hydrogen")
