"""Reverse-image integration with deterministic provider, HTTP, and visual-tool doubles."""

import hashlib
import ipaddress
import json
import re
import socket
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lookup import imagetrace
from lookup.assess import brief
from lookup.models import ImageMatch, ImageRef, LookupInput, Post
from lookup.service import Deps, Lookup
from media.nemotron import MediaAnalysis
from sources import reverse_image
from sources.oxylabs import OxylabsError
from sources.reverse_image import ImageHit, ReverseResult


def png(red):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
        + chunk(b"IEND", b"")
    )


BASE, EDITED, OTHER, CROPPED = (png(red) for red in (0, 40, 80, 120))
OLD = datetime(2008, 4, 22, tzinfo=UTC)
RECENT = datetime(2022, 3, 1, tzinfo=UTC)
POST_ID = "2121370648207671396"
IMAGE_URL = "https://pbs.twimg.com/media/base.png?format=webp"


def hit(path="old.png", *, engine="tineye", when=OLD, rank=0, thumb=None):
    return ImageHit(
        engine=engine,
        image_url=f"https://images.example/{path}",
        thumb_url=thumb,
        page_url="https://www.zooomr.com/photos/old"
        if when == OLD
        else "https://en.wikipedia.org/wiki/Disaster_Girl",
        domain="zooomr.com" if when == OLD else "wikipedia.org",
        first_seen=when,
        time_source="crawl_date" if when else None,
        width=388,
        height=257,
        title="Disaster Girl",
        rank=rank,
    )


def result(engine, url, hits):
    return ReverseResult(
        engine=engine,
        query_image_url=url,
        request_url=f"https://{engine}.com/search?url=picture",
        hits=hits,
        total=17200 if engine == "tineye" else None,
        earliest=OLD if engine == "tineye" else None,
        call={"request_id": f"{engine}-request", "latency_ms": 7},
    )


class Searches:
    def __init__(self, hits=None):
        self.hits = (
            hits
            if hits is not None
            else [
                hit("recent.png", when=RECENT, rank=0),
                hit(rank=1),
                hit("edited.png", when=None, rank=2),
                hit("different.png", when=None, rank=3),
            ]
        )
        self.calls = []

    def tineye(self, client, url, *, deadline):
        self.calls.append(("tineye", url, deadline))
        return result("tineye", url, self.hits)

    def yandex(self, client, url, *, sites, deadline):
        self.calls.append((f"yandex-{sites}", url, deadline))
        return result("yandex", url, [hit(engine="yandex", when=None)])


class TextSources:
    last_call = {"request_id": "text-request"}

    def search(self, query, *, max_results=10, deadline=None):
        return []

    def scrape_markdown(self, url, *, deadline=None):
        if not url.endswith(POST_ID):
            raise RuntimeError("No profile")
        return (
            "## Conversation\n\n* "
            + (
                "[![@example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/example)\n\n"
                "[Example](/example)[@example](/example)\n\n"
                "Here are the words on this post about a picture\n\n"
                f"![Image]({IMAGE_URL})\n\n"
                f"[10:32 PM · Nov 13, 2026](/example/status/{POST_ID})·[1,928\n\n"
                f"Views](/example/status/{POST_ID})\n\n2\n\n26"
            ).replace("\n", "\n  ")
            + "\n",
            {},
        )


class Reader:
    def analyze(self, media, *, context=None):
        return MediaAnalysis(
            media_hash=media.media_hash,
            model="nemotron-3.5-super-vl",
            media_type="meme",
            description="A girl standing near a house.",
            extracted_text=[],
            reuse_cues=[],
            manipulation_cues=[],
            confidence=0.9,
            latency_ms=1,
        )


def relate(base, other):
    assert base == BASE
    kind = {BASE: "same", EDITED: "edited", OTHER: "different", CROPPED: "cropped"}[other]
    return SimpleNamespace(
        kind=kind,
        distance=0 if kind == "same" else 6,
        crop_box=(0.12, 0.0, 0.88, 1.0) if kind == "cropped" else None,
        edited_region=(0.0, 0.0, 1.0, 0.31) if kind == "edited" else None,
        note="Visible image comparison",
    )


def describe(client, original, variant, *, timeout_s):
    assert BASE in (original, variant) and {original, variant} - {BASE} <= {EDITED, CROPPED}
    assert 0 < timeout_s <= 20
    return SimpleNamespace(
        changes=["Caption changed from one line to another"],
        kinds=["caption_changed"],
        latency_ms=9,
        model="nemotron-3.5-super-vl",
    )


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    def resolve(host, port, **kwargs):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(imagetrace.socket, "getaddrinfo", resolve)


def transport(request):
    assert request.url.host == "93.184.216.34"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert request.extensions["sni_hostname"] == request.headers["host"]
    data = {"/edited.png": EDITED, "/different.png": OTHER, "/cropped.png": CROPPED}.get(
        request.url.path, BASE
    )
    return httpx.Response(200, headers={"content-type": "image/png"}, content=data)


def job_for(searches=None, **overrides):
    searches = searches or Searches()
    deps = dict(
        oxylabs=TextSources(),
        reverse_image=True,
        public_base_url="https://public.example",
        search_tineye=searches.tineye,
        search_yandex=searches.yandex,
        relate=relate,
        describe_changes=describe,
        nemotron=Reader(),
        image_transport=httpx.MockTransport(transport),
        media_max=0,
    )
    deps.update(overrides)
    ref = ImageRef(sha256=hashlib.sha256(BASE).hexdigest(), mime="image/png", bytes=len(BASE))
    return Lookup("lk_trace", LookupInput(image=ref), Deps(**deps), image=BASE)


def run_trace(job):
    job._add_upload_post()
    with ThreadPoolExecutor(max_workers=1) as pool:
        trace = imagetrace.start(job, pool)
        trace.future.result(timeout=5)
        trace.finish()
    return trace


def test_upload_end_to_end_dates_dedup_relations_receipts_and_brief():
    searches = Searches()
    job = job_for(searches)
    job.run()
    assert job.result.state == "done", job.result.error
    matches = job.result.image_matches
    assert [match.image_url.rsplit("/", 1)[-1] for match in matches] == [
        "old.png",
        "recent.png",
        "edited.png",
    ]
    assert job.result.image_origin_id == "img:1" and job.result.origin_id is None
    assert matches[0].engine == "tineye" and matches[0].first_seen == OLD
    assert matches[-1].changes and matches[-1].edited_region == (0.0, 0.0, 1.0, 0.31)
    edge = next(edge for edge in job.result.edges if edge.rel == "variant")
    assert edge.from_ == "img:1" and edge.to == "img:3" and edge.tool == "nemotron"
    assert len(searches.calls) == 3
    assert all(
        url == "https://public.example/api/lookup/lk_trace/image" for _, url, _ in searches.calls
    )
    receipts = job.result.receipts
    engine_receipts = [receipt for receipt in receipts if receipt.tool in ("tineye", "yandex")]
    assert len(engine_receipts) == 3 and all(receipt.ok for receipt in engine_receipts)
    assert engine_receipts[0].request_id and engine_receipts[0].source_url
    assert any(
        "17,200" in receipt.summary and "2008-04-22" in receipt.summary for receipt in receipts
    )
    assert len([receipt for receipt in receipts if receipt.tool == "fetch"]) == 4
    assert all(match.receipt_id in {receipt.id for receipt in receipts} for match in matches)
    prompt = json.loads(brief(job.result))
    assert prompt["image_origin_id"] == "img:1"
    assert prompt["image_matches"][-1]["changes"] == matches[-1].changes
    assert any("at least that old" in note for note in job.result.notes)


def test_x_post_uses_its_first_image_without_waiting_for_ocr():
    searches = Searches([hit()])
    deps = job_for(searches, nemotron=None).deps
    job = Lookup("lk_x", LookupInput(url=f"https://x.com/example/status/{POST_ID}"), deps)
    job.run()
    assert job.result.state == "done", job.result.error
    assert len(searches.calls) == 3
    assert all("format=jpg" in url and "name=large" in url for _, url, _ in searches.calls)
    assert job.result.image_matches[0].relation == "same"
    assert job.result.image_origin_id == "img:1"
    assert job.result.origin_id == f"x:{POST_ID}"


@pytest.mark.parametrize("kind", ["x_post", "bsky_post"])
def test_post_skips_cards_and_uses_first_actual_image(kind):
    searches = Searches([hit()])
    job = job_for(searches)
    job._upload = None
    job.result.kind = kind
    job._media_urls = [
        "https://pbs.twimg.com/card_img/one",
        "https://cdn.bsky.app/base.png",
        "https://cdn.bsky.app/other.png",
    ]
    with ThreadPoolExecutor(max_workers=1) as pool:
        trace = imagetrace.start(job, pool)
        trace.future.result(timeout=5)
        trace.finish()
    assert {url for _, url, _ in searches.calls} == {"https://cdn.bsky.app/base.png"}


@pytest.mark.parametrize(
    "created,expected",
    [
        (datetime(2007, 1, 1, tzinfo=UTC), None),
        (datetime(2008, 4, 22, 12, tzinfo=UTC), None),
        (datetime(2008, 4, 23, tzinfo=UTC), None),
        (datetime(2008, 4, 24, tzinfo=UTC), "img:1"),
    ],
)
def test_origin_requires_entire_crawl_day_before_every_verified_post(created, expected):
    job = job_for(Searches([hit()]))
    job.result.posts.append(
        Post(
            id="x:old",
            platform="x",
            role="copy",
            created_at=created,
            verified=True,
            media_urls=["https://pbs.twimg.com/media/copy.jpg?name=large"],
        )
    )
    job.result.posts.append(
        Post(id="x:hint", platform="x", created_at=datetime(2000, 1, 1, tzinfo=UTC))
    )
    run_trace(job)
    assert job.result.image_origin_id == expected
    if expected is None:
        assert any("provably older" in note for note in job.result.notes)


def test_cropped_is_origin_eligible_and_described():
    job = job_for(Searches([hit("cropped.png")]))
    run_trace(job)
    assert job.result.image_origin_id == "img:1"
    assert job.result.image_matches[0].crop_box == (0.12, 0.0, 0.88, 1.0)
    assert job.result.image_matches[0].changes
    assert all(edge.from_ != edge.to for edge in job.result.edges)


def test_no_image_origin_uses_subject_for_edited_edge():
    searches = Searches([hit("edited.png", when=None)])
    searches.yandex = lambda client, url, **kwargs: result("yandex", url, [])
    job = job_for(searches, nemotron=None)
    run_trace(job)
    assert job.result.image_origin_id is None
    assert job.result.edges[0].from_ == job.result.subject_id
    assert job.result.edges[0].tool == "tineye"


def test_one_engine_failure_is_isolated():
    searches = Searches()

    def failed(*args, **kwargs):
        raise RuntimeError("fake deepfake manipulated upstream error")

    searches.tineye = failed
    job = job_for(searches)
    run_trace(job)
    assert job._step("reverse_image").state == "done"
    assert job.result.image_matches[0].engine == "yandex"
    assert any(receipt.tool == "tineye" and not receipt.ok for receipt in job.result.receipts)
    assert all("upstream error" not in receipt.summary for receipt in job.result.receipts)


@pytest.mark.parametrize("module", ["media.variants"])
def test_missing_variants_retains_unknown_reverse_results(monkeypatch, module):
    real_import = imagetrace.import_module

    def missing(name):
        if name == module:
            raise ModuleNotFoundError(name)
        return real_import(name)

    monkeypatch.setattr(imagetrace, "import_module", missing)
    job = job_for(**{"relate" if module == "media.variants" else "describe_changes": None})
    run_trace(job)
    assert job._step("variations").state == "skipped"
    assert job._step("variations").detail == imagetrace.MISSING
    assert job.result.image_matches and all(
        match.relation == "unknown" for match in job.result.image_matches
    )
    assert not any(receipt.tool == "fetch" for receipt in job.result.receipts)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/image",
        "http://10.12.3.4/image",
        "http://169.254.169.254/image",
        "http://[::1]/image",
        "http://224.0.0.1/image",
        "file:///etc/passwd",
        "https://user:password@images.example/image",
        "http://example.com:2375/version",
        "http://images.example:443/image",
        "https://images.example:80/image",
        "https://images.example:8443/image",
        "http://images.example:0/image",
    ],
)
def test_ssrf_refused_without_a_request(url):
    requests = []
    job = job_for(image_transport=httpx.MockTransport(lambda request: requests.append(request)))
    trace = imagetrace.Trace(job)
    assert trace._download(url) is None
    assert requests == []
    assert job.result.receipts and not job.result.receipts[-1].ok


@pytest.mark.parametrize(
    "destination",
    [
        "http://10.0.0.1/secret",
        "http://example.com:2375/version",
        "https://images.example:8443/image",
    ],
)
def test_redirect_to_private_address_or_odd_port_is_refused_and_receipted(destination):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": destination, "set-cookie": "session=private"}
        )

    job = job_for(image_transport=httpx.MockTransport(redirect))
    assert imagetrace.Trace(job)._download("https://images.example/redirect") is None
    assert len(requests) == 1 and len(job.result.receipts) == 2
    assert not job.result.receipts[-1].ok


def test_public_redirect_does_not_forward_cookies_or_authorization():
    requests = []

    def redirect(request):
        requests.append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/done", "set-cookie": "secret=value"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=BASE)

    job = job_for(image_transport=httpx.MockTransport(redirect))
    assert imagetrace.Trace(job)._download("https://images.example/redirect")[0] == BASE
    assert len(requests) == 2


@pytest.mark.parametrize(
    "headers",
    [{"content-type": "text/html"}, {"content-type": "image/png", "content-length": "99999999"}],
)
def test_download_requires_image_type_and_byte_limit(headers):
    job = job_for(
        image_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, content=BASE)
        )
    )
    assert imagetrace.Trace(job)._download("https://images.example/invalid") is None


def test_thumbnail_fallback_and_thumbnail_dedup():
    searches = Searches(
        [
            hit("unavailable.png", thumb="https://thumb.example/thumb.png"),
            hit("duplicate.png", thumb="https://thumb.example/thumb.png", when=RECENT),
        ]
    )
    searches.yandex = lambda client, url, **kwargs: result("yandex", url, [])

    def fetch(request):
        if request.url.path == "/unavailable.png":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=BASE)

    job = job_for(searches, image_transport=httpx.MockTransport(fetch))
    run_trace(job)
    assert len(job.result.image_matches) == 1 and job.result.image_matches[0].relation == "same"
    downloads = [receipt for receipt in job.result.receipts if receipt.tool == "fetch"]
    assert len(downloads) == 2 and not downloads[0].ok and downloads[1].ok


@pytest.mark.parametrize(
    "options,detail",
    [
        ({"public_base_url": ""}, "public image URL"),
        ({"public_base_url": "/relative"}, "public image URL"),
        ({"reverse_image": False}, "disabled"),
    ],
)
def test_skip_without_public_url_or_when_disabled(options, detail):
    searches = Searches()
    job = job_for(searches, **options)
    run_trace(job)
    assert job._step("reverse_image").state == "skipped"
    assert detail in job._step("reverse_image").detail and searches.calls == []


def test_budget_exhaustion_starts_no_provider_calls():
    searches = Searches()
    job = job_for(searches, time_budget_s=1)
    run_trace(job)
    assert searches.calls == [] and job.result.receipts == []
    assert job._step("reverse_image").state == "skipped"


def test_uncooperative_engine_cannot_publish_after_finish(monkeypatch):
    release, completed = threading.Event(), threading.Event()
    searches = Searches()

    def late(client, url, **kwargs):
        release.wait(5)
        return result("tineye", url, [hit()])

    searches.tineye = late
    job = job_for(searches, time_budget_s=10.08)
    monkeypatch.setattr(job, "_spend", lambda *args: None)
    original = imagetrace.Trace._complete

    def complete(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if threading.current_thread().name.startswith("image-trace") and release.is_set():
            completed.set()

    monkeypatch.setattr(imagetrace.Trace, "_complete", complete)
    trace = run_trace(job)
    before = job.result.model_dump()
    release.set()
    assert completed.wait(2) and trace.closed.is_set()
    assert job.result.model_dump() == before


def test_reverse_search_starts_before_synchronous_upload_ocr():
    started = threading.Event()
    searches = Searches([hit()])
    original = searches.tineye

    def search(*args, **kwargs):
        started.set()
        return original(*args, **kwargs)

    searches.tineye = search

    class CheckingReader(Reader):
        def analyze(self, media, *, context=None):
            assert started.wait(2)
            return super().analyze(media, context=context)

    job = job_for(searches, nemotron=CheckingReader(), media_max=1)
    job.run()
    assert job.result.state == "done" and started.is_set()
    assert job._step("media").state == "done"


def test_caps_on_matches_diffs_and_assessment():
    searches = Searches([hit(f"edited{index}.png", when=None) for index in range(25)])

    def fetch(request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=EDITED)

    job = job_for(
        searches,
        image_matches_max=16,
        image_diffs_max=2,
        image_transport=httpx.MockTransport(fetch),
    )
    run_trace(job)
    assert len(job.result.image_matches) == 16
    assert sum(bool(match.changes) for match in job.result.image_matches) == 2
    assert len(json.loads(brief(job.result))["image_matches"]) == 12


def test_all_generated_wording_is_cautious():
    def unsafe_relation(base, other):
        relation = relate(base, other)
        relation.note = (
            "fake, FAKED, fakes, deepfake, DEEPFAKES, manipulated, "
            "manipulation, doctored, DOCTORING"
        )
        return relation

    def unsafe_diff(*args, **kwargs):
        diff = describe(*args, **kwargs)
        diff.changes = [
            "Fake caption, manipulated face, deepfake background, DOCTORED sky, doctoring"
        ]
        return diff

    job = job_for(relate=unsafe_relation, describe_changes=unsafe_diff)
    run_trace(job)
    text = " ".join(
        job.result.notes
        + [step.detail or "" for step in job.result.steps]
        + [receipt.summary for receipt in job.result.receipts]
        + [match.relation_note for match in job.result.image_matches]
        + [change for match in job.result.image_matches for change in match.changes]
    )
    assert not re.search(
        r"\b(?:fake[ds]?|deepfakes?|manipulated|manipulation|doctored|doctoring)\b", text, re.I
    )
    assert "edited sky" in text
    assert (
        imagetrace._wording("The doctored image; Doctor Who; fakeout")
        == "The edited image; Doctor Who; fakeout"
    )


def test_dns_mixed_public_private_answers_refused(monkeypatch):
    monkeypatch.setattr(
        imagetrace.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="not public"):
        imagetrace._pinned_url("https://images.example/test")


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_public_download_pins_dns_answer(scheme, port):
    pinned, host, sni = imagetrace._pinned_url(f"{scheme}://images.example:{port}/test")
    assert str(pinned) == f"{scheme}://93.184.216.34/test"
    assert host == "images.example" and sni == "images.example"


def test_new_result_contract_serializes_boxes_as_arrays():
    match = ImageMatch(
        id="img:1",
        engine="tineye",
        image_url="https://images.example/test",
        crop_box=(0.1, 0.2, 0.3, 0.4),
    )
    assert match.model_dump(mode="json")["crop_box"] == [0.1, 0.2, 0.3, 0.4]
    # media.variants.relate() returns fractions 0..1 of the upload; validation must keep them
    again = ImageMatch.model_validate(
        match.model_dump() | {"crop_box": (0.12, 0.0, 0.88, 1.0), "edited_region": [0, 0, 1, 0.31]}
    )
    assert again.crop_box == (0.12, 0.0, 0.88, 1.0)
    assert again.edited_region == (0.0, 0.0, 1.0, 0.31)


def test_earliest_tineye_hit_wins_even_if_promoted_result_is_first():
    searches = Searches([hit("wiki.png", when=RECENT, rank=0), hit(rank=6)])
    job = job_for(searches, image_matches_max=1)
    run_trace(job)
    assert job.result.image_matches[0].domain == "zooomr.com"
    assert job.result.image_origin_id == "img:1"


def test_three_engine_calls_start_in_parallel_with_individual_caps():
    barrier = threading.Barrier(3)
    searches = Searches([hit()])

    def search(client, url, *, deadline, sites=None):
        cap = 20 if sites is None else 15
        assert 0 < deadline - time.monotonic() <= cap
        barrier.wait(timeout=2)
        return result("tineye" if sites is None else "yandex", url, [hit()])

    job = job_for(searches, search_tineye=search, search_yandex=search)
    run_trace(job)
    assert job._step("reverse_image").detail.startswith("3/3")


def test_lazy_imports_call_both_optional_interfaces(monkeypatch):
    imports = []

    def imported(name):
        imports.append(name)
        return SimpleNamespace(relate=relate, describe_changes=describe)

    monkeypatch.setattr(imagetrace, "import_module", imported)
    job = job_for(relate=None, describe_changes=None)
    run_trace(job)
    assert imports == ["media.variants", "media.imagediff"]
    assert any(match.changes for match in job.result.image_matches)


@pytest.mark.parametrize("stage", ["download", "diff"])
def test_completed_matches_survive_but_late_workers_never_publish(monkeypatch, stage):
    release, started, completed = threading.Event(), threading.Event(), threading.Event()
    searches = Searches([hit(), hit("different.png", when=None), hit("edited.png", when=None)])

    def delayed_fetch(request):
        if stage == "download" and request.url.path == "/edited.png":
            started.set()
            release.wait(5)
        return transport(request)

    def delayed_diff(*args, **kwargs):
        started.set()
        release.wait(5)
        return describe(*args, **kwargs)

    job = job_for(
        searches,
        time_budget_s=10.2,
        image_transport=httpx.MockTransport(delayed_fetch),
        describe_changes=delayed_diff,
    )
    monkeypatch.setattr(job, "_spend", lambda *args: None)
    original = imagetrace.Trace._complete

    def complete(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if release.is_set():
            completed.set()

    monkeypatch.setattr(imagetrace.Trace, "_complete", complete)
    try:
        run_trace(job)
        assert started.is_set()
        assert job.result.image_origin_id == "img:1"
        assert all("different.png" not in match.image_url for match in job.result.image_matches)
        before = job.result.model_dump()
    finally:
        release.set()
    assert completed.wait(2)
    assert job.result.model_dump() == before


def test_origin_waits_for_post_verification():
    job = job_for(Searches([hit()]))
    job._add_upload_post()
    with ThreadPoolExecutor(max_workers=1) as pool:
        trace = imagetrace.start(job, pool)
        trace.future.result(timeout=5)
        job.result.posts.append(
            Post(
                id="x:early",
                platform="x",
                role="copy",
                created_at=datetime(2000, 1, 1, tzinfo=UTC),
                media_urls=["https://pbs.twimg.com/media/copy.jpg?name=large"],
                verified=True,
            )
        )
        trace.finish()
    assert job.result.image_origin_id is None


def test_streaming_byte_cap_without_content_length(monkeypatch):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield BASE[:32]
            yield BASE[32:]

    monkeypatch.setattr(imagetrace, "DOWNLOAD_BYTES", 32)
    job = job_for(
        image_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=Stream(),
            )
        )
    )
    assert imagetrace.Trace(job)._download("https://images.example/large") is None
    assert not job.result.receipts[-1].ok


def test_existing_http_credentials_are_not_used_for_untrusted_images():
    with httpx.Client(auth=("user", "password"), cookies={"session": "secret"}) as client:
        job = job_for(http=client)
        run_trace(job)
    assert job.result.image_matches[0].relation == "same"


def test_budget_gate_says_skipped_even_with_time_in_assessment_reserve():
    searches = Searches()
    job = job_for(searches, time_budget_s=12)
    run_trace(job)
    assert searches.calls == [] and job.result.receipts == []
    assert job._step("reverse_image").state == "skipped"
    assert job._step("reverse_image").detail == "time budget used up"


def test_compressed_http_body_is_refused_before_decompression():
    job = job_for(
        image_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png", "content-encoding": "gzip"},
                stream=httpx.ByteStream(b"untrusted compressed data"),
            )
        )
    )
    assert imagetrace.Trace(job)._download("https://images.example/compressed") is None
    assert not job.result.receipts[-1].ok


def test_fast_yandex_is_published_and_compared_while_tineye_exhausts_13_second_budget():
    compared = threading.Event()
    observed = []
    searches = Searches()

    def slow(client, url, *, deadline):
        # the provider window is 3.1 s (13.1 s budget - 10 s reserve); 1 s was tight under load
        observed.append(compared.wait(timeout=3))
        threading.Event().wait(max(0, deadline - time.monotonic()))
        raise TimeoutError("provider deadline")

    def compare(base, data):
        compared.set()
        return relate(base, data)

    searches.tineye = slow
    job = job_for(searches, relate=compare, time_budget_s=13.1)
    run_trace(job)
    assert observed == [True]
    assert len(job.result.image_matches) == 1
    assert job.result.image_matches[0].engine == "yandex"
    assert job.result.image_matches[0].relation == "same"
    assert len([receipt for receipt in job.result.receipts if receipt.tool == "fetch"]) == 1
    assert sum(receipt.tool == "yandex" and receipt.ok for receipt in job.result.receipts) == 2


def test_later_tineye_adds_dated_origin_after_yandex_is_compared():
    compared = threading.Event()
    searches = Searches([hit("dated.png")])
    original = searches.tineye

    def later(*args, **kwargs):
        assert compared.wait(timeout=2)
        return original(*args, **kwargs)

    def compare(base, data):
        compared.set()
        return relate(base, data)

    searches.tineye = later
    # cap 2: Yandex may take one slot before TinEye answers, the other is TinEye's; a compared
    # match is never replaced, so the downloads stay within the cap
    job = job_for(searches, relate=compare, image_matches_max=2)
    run_trace(job)
    assert len(job.result.image_matches) == 2
    origin = next(m for m in job.result.image_matches if m.id == job.result.image_origin_id)
    assert origin.engine == "tineye" and origin.first_seen == OLD and origin.relation == "same"
    assert origin.image_url.endswith("/dated.png")
    assert len([receipt for receipt in job.result.receipts if receipt.tool == "fetch"]) == 2


def test_downloads_never_exceed_the_cap_when_tineye_lands_after_yandex():
    """Sol r2 #1: fast Yandex hits were compared, then evicted by TinEye's, and the
    replacements were downloaded too (20 kept, 40 downloaded)."""
    compared = threading.Event()

    class Many(Searches):
        def tineye(self, client, url, *, deadline):
            assert compared.wait(timeout=2)
            return result("tineye", url, [hit(f"t{i}.png", rank=i) for i in range(10)])

        def yandex(self, client, url, *, sites, deadline):
            tag = "s" if sites else "v"
            hits = [hit(f"{tag}{i}.png", engine="yandex", when=None, rank=i) for i in range(20)]
            return result("yandex", url, hits)

    def compare(base, data):
        compared.set()
        return relate(base, data)

    job = job_for(Many(), relate=compare)
    run_trace(job)
    fetches = [receipt for receipt in job.result.receipts if receipt.tool == "fetch"]
    assert len(fetches) <= 20
    matches = job.result.image_matches
    assert len(matches) == 20
    tineye = [m for m in matches if m.engine == "tineye"]
    assert len(tineye) == 10 and all(m.relation == "same" for m in tineye)
    assert job.result.image_origin_id in {m.id for m in tineye}


def test_thumbnail_added_after_a_failed_download_is_still_compared():
    """Sol r2 #2: the full image fails, a later Yandex sites hit brings a thumbnail."""
    failed = threading.Event()

    def fetch(request):
        if request.url.path == "/broken.png":
            failed.set()
            return httpx.Response(404)
        return transport(request)

    view = replace(hit("broken.png", engine="yandex", when=None), page_url=None)
    site = replace(view, thumb_url="https://images.example/thumb.png")

    class Late(Searches):
        def tineye(self, client, url, *, deadline):
            return result("tineye", url, [])

        def yandex(self, client, url, *, sites, deadline):
            if not sites:
                return result("yandex", url, [view])
            assert failed.wait(timeout=2)
            time.sleep(0.2)  # the comparison has given up on the full image by now
            return result("yandex", url, [site])

    job = job_for(Late(), image_transport=httpx.MockTransport(fetch))
    run_trace(job)
    [match] = job.result.image_matches
    assert match.thumb_url == site.thumb_url and match.relation == "same"
    urls = [receipt.source_url for receipt in job.result.receipts if receipt.tool == "fetch"]
    assert urls.count("https://images.example/broken.png") == 1
    assert "https://images.example/thumb.png" in urls


def test_adopted_page_url_brings_its_own_domain():
    """Sol r2 #3: imageview set domain from the image host; the sites page is elsewhere."""
    job = job_for()
    trace = imagetrace.Trace(job)
    view = replace(hit(engine="yandex", when=None), page_url=None, domain="images.example")
    site = replace(view, page_url="https://article.example/story", domain="article.example")
    trace._accept(result("yandex", "query", [view]), 1)
    trace._accept(result("yandex", "query", [site]), 2)
    [match] = job.result.image_matches
    assert match.page_url == "https://article.example/story"
    assert match.domain == "article.example"


class FixtureSources(TextSources):
    def __init__(self, name="disaster", failure=None, view_downloaded=None):
        self.name = name
        self.failure = failure
        self.view_downloaded = view_downloaded
        self.local = threading.local()

    @property
    def last_call(self):
        return getattr(self.local, "call", {})

    def scrape_markdown(self, url, *, deadline=None):
        parsed = httpx.URL(url)
        engine = (
            "tineye"
            if parsed.host == "tineye.com"
            else ("yandex_sites" if parsed.params.get("cbir_page") == "sites" else "yandex")
        )
        self.local.call = {
            "request_id": f"fixture-{engine}",
            "latency_ms": 11,
            "cached": True,
            "fetched_at": RECENT,
        }
        if self.failure == "http":
            raise OxylabsError("upstream error")
        if self.failure == "parse":
            return "## Please confirm you are not a robot", {}
        if engine == "yandex_sites" and self.view_downloaded is not None:
            assert self.view_downloaded.wait(timeout=2)
        suffix = "json" if engine == "tineye" else "md"
        path = (
            Path(__file__).parent / "fixtures" / "reverse_image" / f"{engine}_{self.name}.{suffix}"
        )
        return path.read_text(encoding="utf-8"), {}


@pytest.mark.parametrize("failure", ["http", "parse"])
def test_failed_real_engine_calls_keep_url_and_cached_client_provenance(failure):
    job = job_for(
        oxylabs=FixtureSources(failure=failure),
        search_tineye=None,
        search_yandex=None,
    )
    trace = run_trace(job)
    receipts = job.result.receipts
    assert len(receipts) == 3
    assert {receipt.source_url for receipt in receipts} == {
        reverse_image.tineye_url(trace.query_url),
        reverse_image.yandex_url(trace.query_url),
        reverse_image.yandex_url(trace.query_url, sites=True),
    }
    assert {receipt.request_id for receipt in receipts} == {
        "fixture-tineye",
        "fixture-yandex",
        "fixture-yandex_sites",
    }
    assert all(not receipt.ok and receipt.cached for receipt in receipts)
    assert all(receipt.latency_ms == 11 and receipt.at == RECENT for receipt in receipts)
    assert all(receipt.summary.endswith("search unavailable") for receipt in receipts)


def test_duplicate_enrichment_at_cap_survives_inflight_comparison():
    job = job_for(image_matches_max=1)
    trace = imagetrace.Trace(job)
    first = replace(
        hit(engine="yandex", when=None),
        page_url=None,
        domain=None,
        width=None,
        height=None,
    )
    original = trace._accept(result("yandex", "query", [first]), 1)[0]
    site = replace(
        first,
        page_url="https://article.example/story",
        domain="article.example",
        thumb_url="https://thumb.example/image",
        width=500,
        height=375,
    )
    assert trace._accept(result("yandex", "query", [site]), 2) == []
    dated = replace(site, engine="tineye", first_seen=RECENT, time_source="crawl_date")
    trace._accept(result("tineye", "query", [dated]), 3)
    trace._accept(result("tineye", "query", [replace(dated, first_seen=OLD)]), 4)
    trace._accept(result("yandex", "query", [first]), 5)
    trace._publish_match(original.model_copy(update={"relation": "same"}))
    enriched = job.result.image_matches[0]
    assert len(job.result.image_matches) == 1 and enriched.id == original.id
    assert enriched.page_url == site.page_url and enriched.domain == site.domain
    assert enriched.thumb_url == site.thumb_url and (enriched.width, enriched.height) == (500, 375)
    assert enriched.first_seen == OLD and enriched.time_source == "crawl_date"
    assert enriched.engine == "tineye" and enriched.relation == "same"


@pytest.mark.parametrize("kind", ["same", "resized"])
@pytest.mark.parametrize("name,total,count", [("disaster", 17200, 38), ("imgflip", 16540, 42)])
def test_captured_fixtures_through_real_search_into_lookup_origins_and_receipts(
    name, total, count, kind
):
    downloaded = threading.Event() if name == "disaster" else None

    def fetch(request):
        if downloaded is not None and request.headers["host"] == "pochemuha.ru":
            downloaded.set()
        return transport(request)

    def compare(base, data):
        relation = relate(base, data)
        relation.kind = kind
        return relation

    job = job_for(
        oxylabs=FixtureSources(name, view_downloaded=downloaded),
        search_tineye=None,
        search_yandex=None,
        relate=compare,
        image_transport=httpx.MockTransport(fetch),
        image_matches_max=100,
    )
    job.run()
    assert job.result.state == "done", job.result.error
    matches = job.result.image_matches
    assert len(matches) == count
    origin = next(match for match in matches if match.id == job.result.image_origin_id)
    assert origin.image_url == "http://static.zooomr.com/images/592360_8ca5f7f005_s.jpg"
    assert origin.page_url == "http://www.zooomr.com/people/americangirl/"
    assert origin.first_seen == OLD and origin.time_source == "crawl_date"
    assert origin.engine == "tineye" and origin.relation == kind
    if name == "disaster":
        copies = [match for match in matches if match.domain == "pochemuha.ru"]
        assert len(copies) == 1 and downloaded.is_set()
        assert copies[0].page_url == "https://pochemuha.ru/zagadochnye-podzhogi-na-ferme-uilli"
        assert copies[0].relation == kind
    receipts = job.result.receipts
    engine_receipts = [receipt for receipt in receipts if receipt.tool in ("tineye", "yandex")]
    assert len(engine_receipts) == 3
    assert all(
        receipt.ok and receipt.cached and receipt.request_id and receipt.source_url
        for receipt in engine_receipts
    )
    summaries = {receipt.summary for receipt in engine_receipts}
    assert (
        f"TinEye: {total:,} matches, earliest crawl 2008-04-22 "
        "(when the engine first saw it, not a publish date)" in summaries
    )
    assert {"Yandex imageview: 18 matches", "Yandex sites: 20 matches"} <= summaries
    fetch_receipts = [receipt for receipt in receipts if receipt.tool == "fetch"]
    assert len(fetch_receipts) == count
    assert len(engine_receipts) + len(fetch_receipts) == count + 3
    assert all(
        receipt.ok and f"comparison with the upload: {kind}" in receipt.summary
        for receipt in fetch_receipts
    )
    assert any("tineye first seen 2008-04-22" in receipt.summary for receipt in fetch_receipts)


def test_variations_detail_counts_comparisons_dropped_as_different():
    """Sol r3: a comparison that found a different picture still counts as compared."""

    class One(Searches):
        def tineye(self, client, url, *, deadline):
            return result("tineye", url, [hit("different.png", when=None)])

        def yandex(self, client, url, *, sites, deadline):
            return result("yandex", url, [])

    job = job_for(One())
    run_trace(job)
    assert job.result.image_matches == []
    step = next(step for step in job.result.steps if step.id == "variations")
    assert step.state == "done"
    assert (
        step.detail == "1 image compared (1 a different picture, not shown); 0 change descriptions"
    )


@pytest.mark.parametrize("role", ["author_recent", "reply", "mention", "screenshotted"])
def test_posts_without_the_picture_do_not_block_the_image_origin(role):
    """Entire review: the author's other posts, replies and a screenshotted post say nothing
    about when this picture first appeared."""
    job = job_for(Searches([hit()]))
    job.result.posts.append(
        Post(
            id="x:other",
            platform="x",
            role=role,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            verified=True,
        )
    )
    run_trace(job)
    assert job.result.image_origin_id == "img:1"


def test_missing_imagediff_still_compares_without_change_descriptions(monkeypatch):
    """Entire review: relate() alone classifies; only the Nemotron change text is skipped."""
    real_import = imagetrace.import_module

    def missing(name):
        if name == "media.imagediff":
            raise ModuleNotFoundError(name)
        return real_import(name)

    monkeypatch.setattr(imagetrace, "import_module", missing)
    job = job_for(describe_changes=None)
    run_trace(job)
    step = job._step("variations")
    assert step.state == "done" and "not installed" in step.detail
    relations = {m.image_url.rsplit("/", 1)[-1]: m.relation for m in job.result.image_matches}
    assert relations["edited.png"] == "edited" and relations["old.png"] == "same"
    assert all(not m.changes for m in job.result.image_matches)
    assert job.result.image_origin_id == "img:1"


def test_downloads_send_a_browser_user_agent():
    """Entire review: Wikimedia and other CDNs refuse httpx's default agent."""
    from lookup.service import UA

    agents = []

    def fetch(request):
        agents.append(request.headers.get("user-agent"))
        return transport(request)

    job = job_for(image_transport=httpx.MockTransport(fetch))
    run_trace(job)
    assert agents and set(agents) == {UA}


def test_quoted_caption_is_kept_verbatim():
    """Entire review: a quoted caption is evidence; only our own wording is rewritten."""
    assert (
        imagetrace._wording("Caption changed to 'FAKE NEWS'; the doctored look")
        == "Caption changed to 'FAKE NEWS'; the edited look"
    )
    assert imagetrace._wording('Text "Deepfake alert" added') == 'Text "Deepfake alert" added'
    assert imagetrace._wording("The girl's fake smile") == "The girl's edited smile"


def test_search_refused_by_budget_is_skipped_not_failed():
    """Entire review: a call the time budget refuses is a skip with the budget reason."""
    job = job_for(time_budget_s=12.0)  # 10 s reserve + 3 s need > 12 s: every call refused
    run_trace(job)
    step = job._step("reverse_image")
    assert step.state == "skipped" and "time budget" in step.detail


def test_crash_marks_reverse_image_failed_not_timed_out(monkeypatch):
    """Entire review: an exception must not end as "skipped: time budget used up"."""

    def boom(self, result, rid):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(imagetrace.Trace, "_accept", boom)
    job = job_for()
    run_trace(job)
    assert job._step("reverse_image").state == "failed"
    assert job._step("variations").state == "failed"


def test_nemotron_down_still_returns_visual_matches_and_origin():
    """Verda outage 2026-11-14: Nemotron times out on every call. The OCR reading and the
    change descriptions fail; reverse search, comparisons and the image origin don't need it."""

    class Down:
        def analyze(self, media, *, context=None):
            raise TimeoutError("nemotron endpoint timed out")

    def describe_down(client, original, variant, *, timeout_s):
        raise TimeoutError("nemotron endpoint timed out")

    job = job_for(nemotron=Down(), describe_changes=describe_down, media_max=1)
    job.run()
    assert job.result.state == "done", job.result.error
    assert job._step("media").state == "failed"
    assert job._step("reverse_image").state == "done"
    assert job._step("variations").state == "done"
    relations = {m.image_url.rsplit("/", 1)[-1]: m.relation for m in job.result.image_matches}
    assert relations == {"old.png": "same", "recent.png": "same", "edited.png": "edited"}
    assert job.result.image_origin_id == "img:1"
    assert all(not m.changes for m in job.result.image_matches)
    diffs = [
        r for r in job.result.receipts if r.tool == "nemotron" and r.endpoint != "chat.completions"
    ]
    assert diffs and not any(r.ok for r in diffs)


def _carrier(role, media):
    return Post(
        id=f"x:{role}",
        platform="bluesky" if role == "bluesky" else "x",
        role=role,
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
        verified=True,
        media_urls=media,
    )


@pytest.mark.parametrize(
    "role,media,blocks",
    [
        ("bluesky", ["https://cdn.bsky.app/img/feed_fullsize/plain/did/x@jpeg"], True),
        ("copy", [], False),  # a text-only copy can't date the picture
        ("quote", [], False),
        ("author_recent", ["https://pbs.twimg.com/media/unrelated.jpg"], False),
        ("author_recent", ["https://images.example/old.png"], True),  # the picture's own copy
        ("reply", ["https://images.example/old.png"], True),
    ],
)
def test_image_origin_bounded_by_posts_that_may_carry_the_picture(role, media, blocks):
    """Sol r4: carriage, not role alone: a verified Bluesky copy with an image blocks; a
    text-only copy doesn't; any post whose image URL is a same/resized copy blocks."""
    job = job_for(Searches([hit()]))
    job.result.posts.append(_carrier(role, media))
    run_trace(job)
    assert (job.result.image_origin_id is None) is blocks


def test_mismatched_quotes_never_exempt_model_wording():
    """Sol r4: a straight quote paired with a curly one must not shield unquoted text."""
    text = 'Caption changed to "FAKE NEWS”. The image is doctored; the logo says “News".'
    assert not re.search(r"doctored|fake", imagetrace._wording(text), re.I)
    assert imagetrace._wording("“Fake” added") == "“Fake” added"
    long = '"' + "fake " * 60 + '"'
    assert "fake" not in imagetrace._wording(long)


def test_relation_numbers_are_plain_floats():
    """Live run: relate() handed back numpy ints; the model must get plain numbers."""
    from fractions import Fraction

    def odd(base, other):
        r = relate(base, other)
        r.distance = Fraction(6) if r.distance is not None else None
        if r.crop_box:
            r.crop_box = tuple(Fraction(int(v * 100), 100) for v in r.crop_box)
        return r

    job = job_for(Searches([hit("cropped.png")]), relate=odd)
    run_trace(job)
    match = next(m for m in job.result.image_matches if m.relation == "cropped")
    assert type(match.distance) is float and all(type(v) is float for v in match.crop_box)


@pytest.mark.parametrize(
    "post_url",
    [
        "https://pbs.twimg.com/media/BCOVrUMCIAAdN5J.jpg",
        "https://pbs.twimg.com/media/BCOVrUMCIAAdN5J?format=webp",
        "https://pbs.twimg.com/media/BCOVrUMCIAAdN5J?format=jpg&name=small",
    ],
)
def test_carrier_url_variants_still_block_the_origin(post_url):
    """Sol r5: _x_img() adds name=large / swaps webp for jpg; a verified post showing the same
    picture under another rendition URL must still block a later crawl from being the origin."""
    from lookup.service import _x_img

    match = replace(hit(), image_url="https://pbs.twimg.com/media/BCOVrUMCIAAdN5J.jpg")
    job = job_for(Searches([match]))
    job.result.posts.append(_carrier("mention", [_x_img(post_url)]))
    run_trace(job)
    assert job.result.image_matches[0].relation == "same"
    assert job.result.image_origin_id is None


def test_media_id_collapses_rendition_variants_only():
    ids = {
        imagetrace._media_id(u)
        for u in (
            "https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:abc/bafkrei123@jpeg",
            "https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:abc/bafkrei123@webp",
        )
    }
    assert ids == {"bsky:did:plc:abc/bafkrei123"}
    assert imagetrace._media_id("/api/lookup/lk_1/image") == imagetrace._media_id(
        "https://public.example/api/lookup/lk_1/image"
    )
    assert imagetrace._media_id("https://pbs.twimg.com/media/A.jpg") != imagetrace._media_id(
        "https://pbs.twimg.com/media/B.jpg"
    )


def test_empty_change_list_is_reported_as_no_visible_change():
    """media.imagediff: changes == [] means Nemotron saw no visible change (not a failure)."""

    def nothing(client, original, variant, *, timeout_s):
        return SimpleNamespace(changes=[], kinds=[], latency_ms=5, model="nemotron-3.5-super-vl")

    job = job_for(Searches([hit("edited.png", when=None)]), describe_changes=nothing)
    run_trace(job)
    [match] = [m for m in job.result.image_matches if m.relation == "edited"]
    receipt = next(r for r in job.result.receipts if r.id == match.receipt_id)
    assert receipt.ok and receipt.summary.endswith("Nemotron saw no visible change")
    assert match.changes == []


def test_real_media_variants_relate_through_the_trace():
    """Part 1's real relate() (on main since #76) end to end: an identical copy is "same", a
    smaller copy "resized", a re-captioned copy "edited", an unrelated picture is dropped."""
    import io

    from PIL import Image, ImageDraw

    from media.variants import relate as real_relate

    def picture(size=(320, 220)):
        im = Image.new("RGB", size, (40, 90, 160))
        d = ImageDraw.Draw(im)
        d.ellipse((60, 40, 200, 180), fill=(230, 120, 40))
        d.rectangle((210, 100, 300, 200), fill=(30, 160, 70))
        return im

    def jpeg(im):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()

    base = jpeg(picture())
    captioned = picture()
    ImageDraw.Draw(captioned).text((10, 180), "NEW CAPTION HERE", fill=(255, 255, 255))
    other = Image.new("RGB", (320, 220), (250, 250, 250))
    ImageDraw.Draw(other).line((0, 0, 320, 220), fill=(0, 0, 0), width=25)
    served = {
        "/same.jpg": base,
        "/small.jpg": jpeg(picture().resize((160, 110))),
        "/caption.jpg": jpeg(captioned),
        "/other.jpg": jpeg(other),
    }

    def fetch(request):
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=served[request.url.path]
        )

    hits = [hit(p.strip("/"), rank=i) for i, p in enumerate(served)]
    searches = Searches(hits)
    searches.yandex = lambda client, url, **kwargs: result("yandex", url, [])
    ref = ImageRef(sha256=hashlib.sha256(base).hexdigest(), mime="image/jpeg", bytes=len(base))
    job = Lookup(
        "lk_real",
        LookupInput(image=ref),
        Deps(
            oxylabs=TextSources(),
            reverse_image=True,
            public_base_url="https://public.example",
            search_tineye=searches.tineye,
            search_yandex=searches.yandex,
            relate=real_relate,
            describe_changes=describe_nothing,
            nemotron=Reader(),
            image_transport=httpx.MockTransport(fetch),
            media_max=0,
        ),
        image=base,
    )
    run_trace(job)
    got = {m.image_url.rsplit("/", 1)[-1]: m.relation for m in job.result.image_matches}
    assert got == {"same.jpg": "same", "small.jpg": "resized", "caption.jpg": "edited"}
    edited = next(m for m in job.result.image_matches if m.relation == "edited")
    assert edited.edited_region and all(0.0 <= v <= 1.0 for v in edited.edited_region)
    assert all(isinstance(m.distance, float) for m in job.result.image_matches)
    assert job.result.image_origin_id is not None


def describe_nothing(client, original, variant, *, timeout_s):
    return None


def test_diff_reads_an_upload_as_newer_than_every_copy():
    """Part 4 live: the upload was passed as the earlier image, so a meme that ADDED a caption
    to an older picture read as "caption removed". A copy found on the web predates the upload."""
    calls = []

    def record(client, original, variant, *, timeout_s):
        calls.append((original, variant))
        return SimpleNamespace(
            changes=["A caption was added"],
            kinds=["text_added", "caption_changed", "text_added"],
            latency_ms=3,
            model="nemotron-3.5-super-vl",
        )

    job = job_for(Searches([hit("edited.png", when=None)]), describe_changes=record)
    run_trace(job)
    assert calls and all(o == EDITED and v == BASE for o, v in calls)
    edited = next(m for m in job.result.image_matches if m.relation == "edited")
    assert edited.changes_from == "match"
    assert edited.change_kinds == ["text_added", "caption_changed"]  # deduplicated, in order
    receipt = next(r for r in job.result.receipts if r.id == edited.receipt_id)
    assert "from this copy to the submitted picture" in receipt.summary


@pytest.mark.parametrize(
    "first_seen,match_first",
    [
        (datetime(2008, 4, 22, tzinfo=UTC), True),  # crawled long before the post
        (datetime(2026, 11, 13, tzinfo=UTC), False),  # crawl day ends after the post
        (None, False),  # undated (Yandex): the post's picture goes first
    ],
)
def test_diff_direction_for_a_post_follows_provable_order(first_seen, match_first):
    job = job_for()
    job._upload = None
    job.result.posts.append(
        Post(
            id="x:subject",
            platform="x",
            role="submitted",
            created_at=datetime(2026, 11, 13, 22, 32, tzinfo=UTC),
            verified=True,
        )
    )
    job.result.subject_id = "x:subject"
    trace = imagetrace.Trace(job)
    match = ImageMatch(
        id="img:1",
        engine="tineye" if first_seen else "yandex",
        image_url="https://images.example/edited.png",
        first_seen=first_seen,
        time_source="crawl_date" if first_seen else None,
    )
    assert trace._match_is_older(match) is match_first
