"""FxTwitter as the first X post reader (lookup/fxtwitter.py), with the Oxylabs page as fallback."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from lookup.fxtwitter import (
    Conversation,
    FxNotFound,
    FxTwitterClient,
    FxTwitterError,
    to_item,
)
from lookup.models import LookupInput
from lookup.service import Deps, Lookup
from tests.test_lookup import COPY, SUBJ, TEXT, FakeOxylabs

FIX = Path(__file__).parent / "fixtures"
VG_ID = "2121220535044464871"
VG = json.loads((FIX / "fxtwitter" / f"conversation_visegrad24_{VG_ID}.json").read_text())
GT = json.loads((FIX / "fxtwitter" / "conversation_garrytan_2121462697787342932.json").read_text())
SENSITIVE_MD = (FIX / "xpages" / f"post_sensitive_visegrad24_{VG_ID}.md").read_text()


def _client(handler) -> FxTwitterClient:
    return FxTwitterClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- parsing


def test_note_post_maps_to_item_with_exact_counts_and_full_text():
    it = to_item(VG["status"])
    assert (it.handle, it.status_id, it.name) == ("visegrad24", VG_ID, "Visegrád 24")
    assert it.text.startswith("Complaint filed against Krakow police officer")
    assert it.text.endswith("so the response was lawful.")  # the long post, not cut at 280
    assert (it.replies, it.reposts, it.likes, it.bookmarks) == (129, 154, 2045, 181)
    assert it.views and it.views > 150_000
    assert it.media_urls == ["https://pbs.twimg.com/media/HXAWy5bXEAAwkrS.jpg?name=orig"]
    assert it.quoted_id is None and it.created_at.isoformat().startswith("2026-11-13T12:36:00")


def test_quote_post_and_reply_page():
    conv = FxTwitterClient._conversation(GT, with_replies=True)
    m = conv.main
    assert (m.handle, m.quoted_handle, m.quoted_id) == (
        "garrytan",
        "TheJensenLetter",
        "2121339322385440842",
    )
    assert m.quoted_text.startswith("Still hard to believe")
    assert len(conv.replies) == 4 and not any(r.is_main for r in conv.replies)
    assert conv.cursor and conv.parent_id is None


def test_reply_knows_its_parent():
    reply = dict(VG["replies"][0])
    conv = FxTwitterClient._conversation({"status": reply, "replies": []}, with_replies=True)
    assert (conv.parent_id, conv.parent_handle) == (VG_ID, "visegrad24")


def test_zero_counts_stay_zero_not_hidden():
    st = dict(VG["status"], likes=0, reposts=0, bookmarks=0)
    it = to_item(st)
    assert (it.likes, it.reposts, it.bookmarks) == (0, 0, 0)


# ---------------------------------------------------------------- client


def test_client_conversation_and_cursor():
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        return httpx.Response(200, json=VG)

    c = _client(handler)
    conv = c.conversation(VG_ID, cursor="abc")
    assert seen == [f"https://api.fxtwitter.com/2/conversation/{VG_ID}?cursor=abc"]
    assert conv.main.status_id == VG_ID and len(conv.replies) == 6
    assert c.last_call["endpoint"] == "/2/conversation" and c.last_call["http_status"] == 200


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (404, {"status": None, "thread": None, "author": None, "code": 404}, FxNotFound),
        (200, {"status": None, "code": 200}, FxNotFound),
        (500, {"code": 500, "message": "oops"}, FxTwitterError),
        (429, {"code": 429}, FxTwitterError),
        (200, ["not", "an", "object"], FxTwitterError),
    ],
)
def test_client_errors(status, body, exc):
    c = _client(lambda req: httpx.Response(status, json=body))
    with pytest.raises(exc):
        c.status(VG_ID)


def test_client_non_json_and_network_errors():
    with pytest.raises(FxTwitterError):
        _client(lambda req: httpx.Response(502, text="<html>bad gateway")).status(VG_ID)

    def boom(req):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(FxTwitterError):
        _client(boom).status(VG_ID)


# ---------------------------------------------------------------- lookup


class FakeFx:
    def __init__(self, convs: dict[str, dict] | None = None, fail: Exception | None = None):
        self.convs, self.fail, self.calls = convs or {}, fail, []
        self.last_call = {"endpoint": "/2/conversation", "latency_ms": 3}

    def _get(self, sid, replies):
        self.calls.append((sid, replies))
        if self.fail:
            raise self.fail
        if sid not in self.convs:
            raise FxNotFound("no such post")
        return FxTwitterClient._conversation(self.convs[sid], with_replies=replies)

    def conversation(self, sid, *, cursor=None, deadline=None) -> Conversation:
        return self._get(sid, True)

    def status(self, sid, *, deadline=None) -> Conversation:
        return self._get(sid, False)


class SensitiveOxylabs(FakeOxylabs):
    def scrape_markdown(self, url, deadline=None):
        self.calls.append(("scrape", url))
        if url.endswith(VG_ID):
            return SENSITIVE_MD, {}
        return super().scrape_markdown(url, deadline)


def _vg_job(fx) -> tuple[Lookup, SensitiveOxylabs]:
    ox = SensitiveOxylabs()
    job = Lookup(
        "lk_vg",
        LookupInput(url=f"https://x.com/visegrad24/status/{VG_ID}"),
        Deps(oxylabs=ox, fxtwitter=fx, bsky=None, store=None, nemotron=None, assessor=None),
    )
    job.run()
    return job, ox


def test_post_hidden_logged_out_is_read_through_fxtwitter():
    job, ox = _vg_job(FakeFx({VG_ID: VG}))
    r = job.result
    assert r.subject_id == f"x:{VG_ID}" and r.unreadable is None
    post = next(p for p in r.posts if p.id == r.subject_id)
    assert post.text.startswith("Complaint filed") and post.verified
    assert post.metrics.likes == 2045
    assert ("scrape", f"https://x.com/visegrad24/status/{VG_ID}") not in ox.calls
    rec = next(x for x in r.receipts if x.id == post.receipt_id)
    assert (rec.tool, rec.endpoint) == ("fxtwitter", "/2/conversation")
    assert "FxTwitter" in job._step("post").detail
    # the replies on the first page join as replies of the post (capped by replies_max)
    replies = [p for p in r.posts if p.role == "reply"]
    assert 0 < len(replies) <= job.deps.replies_max
    # the text was searched for
    assert job._step("copies_web").state != "skipped"
    assert any("FxTwitter" in n for n in r.notes)


def test_fxtwitter_down_falls_back_to_the_page_and_names_the_sensitive_notice():
    job, ox = _vg_job(FakeFx(fail=FxTwitterError("HTTP 503")))
    r = job.result
    assert ("scrape", f"https://x.com/visegrad24/status/{VG_ID}") in ox.calls
    assert r.subject_id is None and r.unreadable == "Hidden from logged-out view"
    st = job._step("post")
    assert st.state == "done" and "marked sensitive" in st.detail


def test_without_fxtwitter_the_page_read_is_unchanged():
    job, ox = _vg_job(None)
    assert job.result.unreadable == "Hidden from logged-out view"
    assert not any(x.tool == "fxtwitter" for x in job.result.receipts)


def test_candidate_copy_is_verified_through_fxtwitter():
    """A search hit is read through FxTwitter; one FxTwitter says is gone is dropped without
    an Oxylabs page read."""
    subj = dict(VG["status"], id=SUBJ, text=TEXT, media={})
    subj["author"] = dict(subj["author"], screen_name="ada_example")
    copy = dict(subj, id=COPY, text=TEXT + " so true")
    copy["author"] = dict(subj["author"], screen_name="cy_example")
    fx = FakeFx({SUBJ: {"status": subj, "replies": []}, COPY: {"status": copy}})
    ox = FakeOxylabs()
    job = Lookup(
        "lk_cp",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=ox, fxtwitter=fx, bsky=None, store=None, nemotron=None, assessor=None),
    )
    job.run()
    cp = next(p for p in job.result.posts if p.id == f"x:{COPY}")
    assert cp.verified and cp.role == "copy"
    rec = next(x for x in job.result.receipts if x.id == cp.receipt_id)
    assert rec.tool == "fxtwitter"
    assert not any(c[0] == "scrape" and "/status/" in c[1] for c in ox.calls)
    assert (COPY, False) in fx.calls
    # the unrelated hit (x.com/zed/...) is not on FxTwitter: dropped, no page read either
    assert not any(p.id == "x:2121364193509277999" and p.verified for p in job.result.posts)


# ---------------------------------------------------------------- review round 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["status"].update(author=["not", "a", "dict"]),
        lambda d: d["status"].update(text=["a", "list"]),
        lambda d: d["status"].update(id={"x": 1}),
        lambda d: d.update(status="a string"),
    ],
)
def test_malformed_main_post_is_an_fxtwitter_error(mutate):
    data = json.loads(json.dumps(VG))
    mutate(data)
    with pytest.raises(FxTwitterError):
        FxTwitterClient._conversation(data, with_replies=True)


def test_malformed_optional_parts_do_not_lose_the_post():
    data = json.loads(json.dumps(VG))
    data["cursor"] = "not a dict"
    data["replies"] = [{"id": "x"}, ["junk"], data["replies"][0]]
    data["status"]["media"] = {"photos": [{"url": 5}, "junk", {"url": "http://evil.example/a"}]}
    data["status"]["quote"] = {"id": None, "author": "x"}
    conv = FxTwitterClient._conversation(data, with_replies=True)
    assert conv.main.status_id == VG_ID and conv.main.media_urls == []
    assert conv.main.quoted_id is None and conv.cursor is None
    assert len(conv.replies) == 1  # only the well-formed reply


def test_malformed_answer_falls_back_to_the_page_read():
    bad = json.loads(json.dumps(VG))
    bad["status"]["author"] = "nobody"

    class BadFx(FakeFx):
        def conversation(self, sid, *, cursor=None, deadline=None):
            self.calls.append((sid, True))
            return FxTwitterClient._conversation(bad, with_replies=True)

    job, ox = _vg_job(BadFx())
    assert ("scrape", f"https://x.com/visegrad24/status/{VG_ID}") in ox.calls
    assert job.result.unreadable == "Hidden from logged-out view"


def test_link_card_thumbnail_is_kept():
    it = to_item(GT["status"]["quote"])
    assert it.card_url == "https://standwithjensen.com"
    assert it.card_image.startswith("https://pbs.twimg.com/card_img/")


def test_call_metadata_is_per_thread():
    import threading

    gate = threading.Barrier(2)

    def handler(req: httpx.Request):
        gate.wait(timeout=5)  # both requests in flight at once
        return httpx.Response(200, json=VG)

    c = _client(handler)
    seen = {}

    def read(name):
        c.status(VG_ID)
        seen[name] = c.last_call

    ts = [threading.Thread(target=read, args=(n,)) for n in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert seen["a"] is not seen["b"]
    assert c.last_call == {}  # this thread made no call


def test_fallback_shares_the_candidate_read_bound(monkeypatch):
    """FxTwitter that takes the read's whole bound leaves no second bound for Oxylabs."""
    import lookup.service as svc

    clock = [1000.0]
    monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])

    class SlowFx(FakeFx):
        def status(self, sid, *, deadline=None):
            clock[0] += 8.0  # the whole verify_read_s
            raise FxTwitterError("timed out")

    ox = FakeOxylabs()
    job = Lookup(
        "lk_slow",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=ox, fxtwitter=SlowFx(), time_budget_s=600),
    )
    from lookup.models import Post

    cand = Post(id=f"x:{COPY}", platform="x", url=f"https://x.com/cy_example/status/{COPY}")
    assert job._verify_x(cand, TEXT) is None
    assert not any(c == ("scrape", cand.url) for c in ox.calls)


# ---------------------------------------------------------------- review round 2


def test_answer_without_status_is_an_error_not_a_missing_post():
    c = _client(lambda req: httpx.Response(200, json={"error": "temporarily unavailable"}))
    with pytest.raises(FxTwitterError) as e:
        c.status(VG_ID)
    assert not isinstance(e.value, FxNotFound)


def test_trickling_answer_ends_at_the_deadline():
    import time as _time

    body = json.dumps(VG).encode()

    def trickle():
        for i in range(0, len(body), 2000):
            _time.sleep(0.15)
            yield body[i : i + 2000]

    c = _client(lambda req: httpx.Response(200, content=trickle()))
    t0 = _time.monotonic()
    with pytest.raises(FxTwitterError, match="not complete in time"):
        c.status(VG_ID, deadline=_time.monotonic() + 1.0)
    assert _time.monotonic() - t0 < 1.5


def test_candidate_falls_back_to_its_page_when_fxtwitter_answers_without_status():
    class OddFx(FakeFx):
        def status(self, sid, *, deadline=None):
            self.calls.append((sid, False))
            raise FxTwitterError("FxTwitter answer has no status")

    ox = FakeOxylabs()
    job = Lookup(
        "lk_odd",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=ox, fxtwitter=OddFx()),
    )
    from lookup.models import Post

    cand = Post(id=f"x:{COPY}", platform="x", url=f"https://x.com/cy_example/status/{COPY}")
    job._verify_x(cand, TEXT)
    assert ("scrape", cand.url) in ox.calls


def test_slow_header_server_cannot_hold_a_read_past_its_bound():
    """A real socket that trickles header bytes faster than any per-operation timeout."""
    import socket
    import threading
    import time as _time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = srv.accept()
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.is_set():
                conn.sendall(b"X-Slow: a\r\n")
                _time.sleep(0.3)
        except OSError:
            pass  # the client closed the connection: what we want
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    c = FxTwitterClient(base_url=f"http://127.0.0.1:{srv.getsockname()[1]}")
    t0 = _time.monotonic()
    try:
        with pytest.raises(FxTwitterError, match="not complete in time"):
            c.status(VG_ID, deadline=_time.monotonic() + 0.5)
        assert _time.monotonic() - t0 < 0.9
    finally:
        stop.set()
        srv.close()
