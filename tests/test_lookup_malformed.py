"""Malformed public input must not abort a lookup's parsers (no network)."""

import pytest

from lookup.jobs import cache_key
from lookup.models import LookupInput
from lookup.service import Deps, Lookup
from lookup.urls import classify, domain, snowflake_time
from lookup.xpage import XItem, parse_count, parse_profile


@pytest.mark.parametrize(
    "url",
    ["https://[example.com", "https://[not-an-ip]/", "https://example.com\uff0fpath"],
)
def test_malformed_authority_is_unclassifiable_and_cacheable(url):
    assert classify(url) is None
    assert domain(url) is None
    assert cache_key(LookupInput(url=url)) == f"url:{url}"


def test_malformed_bluesky_card_url_does_not_discard_post():
    job = Lookup(
        "lk_bad_card", LookupInput(text="a post with a malformed link"), Deps(oxylabs=None)
    )
    post = job._bsky_post_obj(
        {
            "uri": "at://did:plc:example/app.bsky.feed.post/3abc",
            "author": {"handle": "example.bsky.social"},
            "record": {"text": "The post is still readable"},
            "embed": {"external": {"title": "A card", "uri": "https://[example.com"}},
        },
        "submitted",
        1.0,
        None,
    )
    assert post.text == "The post is still readable"
    assert post.card.title == "A card" and post.card.domain is None


@pytest.mark.parametrize("digits", [30, 100, 1000])
def test_oversized_snowflake_has_no_creation_time(digits):
    sid = "9" * digits
    assert snowflake_time(sid) is None
    assert XItem(handle="example", name=None, status_id=sid, text="post").created_at is None


@pytest.mark.parametrize("count", ["9" * 400, "9" * 308 + "B"])
def test_overflowing_count_is_unknown_without_discarding_profile(count):
    assert parse_count(count) is None
    profile = parse_profile(
        f"# Example\n\n@example\n\n[{count} Following](/example/following)"
        "[12 Followers](/example/verified_followers)",
        "example",
    )
    assert profile.following is None and profile.followers == 12
