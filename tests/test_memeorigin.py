"""sources.memeorigin: Know Your Meme and Wikipedia entries -> verbatim dated sentences."""

import json
from pathlib import Path

import pytest

from sources.memeorigin import (
    _plain,
    dated_sentences,
    kym_entry_url,
    merge_milestones,
    parse_kym,
    parse_wikipedia,
    wikipedia_title,
)

FIXTURES = Path(__file__).parent / "fixtures" / "memeorigin"


def kym():
    md = (FIXTURES / "kym_disaster_girl.md").read_text()
    return parse_kym(md, "https://knowyourmeme.com/memes/disaster-girl")


def wiki():
    return parse_wikipedia(
        json.loads((FIXTURES / "wiki_summary_disaster_girl.json").read_text()),
        json.loads((FIXTURES / "wiki_extract_disaster_girl.json").read_text()),
    )


def test_kym_entry_urls():
    assert kym_entry_url("https://knowyourmeme.com/photos/130-disaster-girl") == (
        "https://knowyourmeme.com/memes/disaster-girl"
    )
    assert kym_entry_url("https://knowyourmeme.com/memes/disaster-girl/photos") is None
    assert kym_entry_url("https://knowyourmeme.com/memes/disaster-girl#origin") == (
        "https://knowyourmeme.com/memes/disaster-girl"
    )
    assert kym_entry_url("https://example.com/memes/disaster-girl") is None
    # a photo of an event entry: /memes/<slug>, which the site redirects to /memes/events/<slug>
    assert kym_entry_url("https://knowyourmeme.com/photos/1907928-storm-area-51") == (
        "https://knowyourmeme.com/memes/storm-area-51"
    )


@pytest.mark.parametrize(
    "url, entry",
    [
        ("https://knowyourmeme.com/memes/events/storm-area-51", None),
        ("https://knowyourmeme.com/memes/events/suez-canal-jam?ref=x#background", None),
        ("https://knowyourmeme.com/memes/people/john-pork-john-pork-is-calling/", None),
        ("https://www.knowyourmeme.com/memes/subcultures/godzilla", None),
        ("https://knowyourmeme.com/memes/sites/instagram", None),
        ("https://knowyourmeme.com/memes/cultures/frutiger-aero", None),
        (
            "https://knowyourmeme.com/sensitive/memes/events/storm-area-51",
            "https://knowyourmeme.com/memes/events/storm-area-51",
        ),
        ("https://knowyourmeme.com/memes/memes", None),  # the entry about memes
    ],
)
def test_kym_entry_kinds(url, entry):
    """Events, people, subcultures, sites and cultures are entries too, at their own path."""
    want = entry or url.split("#")[0].split("?")[0].rstrip("/").replace("www.", "")
    assert kym_entry_url(url) == want


@pytest.mark.parametrize(
    "url",
    [
        # galleries and other pages of an entry
        "https://knowyourmeme.com/memes/events/storm-area-51/photos",
        "https://knowyourmeme.com/memes/storm-area-51/videos",
        "https://knowyourmeme.com/memes/people/elon-musk/photos",
        "https://knowyourmeme.com/memes/subcultures/godzilla/photos/trending",
        "https://knowyourmeme.com/memes/area-51/children",
        "https://knowyourmeme.com/memes/events/storm-area-51/comments",
        "https://knowyourmeme.com/memes/memes/godzilla/photos",
        "https://knowyourmeme.com/videos/421138-storm-area-51",
        "https://knowyourmeme.com/photos",
        "https://knowyourmeme.com/photos/trending",
        # listings, sorting, search, the submit form, a random entry
        "https://knowyourmeme.com/memes/events",
        "https://knowyourmeme.com/memes/people/",
        "https://knowyourmeme.com/memes/subcultures",
        "https://knowyourmeme.com/memes/sites",
        "https://knowyourmeme.com/memes/cultures",
        "https://knowyourmeme.com/memes/popular",
        "https://knowyourmeme.com/memes/all?sort=newest",
        "https://knowyourmeme.com/memes/popular/page/2",
        "https://knowyourmeme.com/memes/newsworthy",
        "https://knowyourmeme.com/memes/submissions",
        "https://knowyourmeme.com/memes/confirmed",
        "https://knowyourmeme.com/memes/deadpool",
        "https://knowyourmeme.com/memes/researching",
        "https://knowyourmeme.com/memes/trending",
        "https://knowyourmeme.com/memes/random",
        "https://knowyourmeme.com/memes/new?guidelines=1",
        "https://knowyourmeme.com/memes/search?q=godzilla",
        "https://knowyourmeme.com/search?q=storm+area+51",
        "https://knowyourmeme.com/categories/event",
        "https://knowyourmeme.com/types/raid?status=all",
        # people on the site, and its articles
        "https://knowyourmeme.com/users/zach--15",
        "https://knowyourmeme.com/editorials/guides/who-is-john-pork",
        "https://trending.knowyourmeme.com/editorials/guides/what-is-the-frutiger-aero-yams-meme",
    ],
)
def test_kym_non_entries(url):
    assert kym_entry_url(url) is None
    assert wikipedia_title("https://en.wikipedia.org/wiki/Disaster_Girl") == "Disaster_Girl"
    assert wikipedia_title("https://en.wikipedia.org/wiki/File:Disaster_Girl.jpg") is None


def test_parse_kym_entry():
    entry = kym()
    assert entry.name == "Disaster Girl"
    # the entry's own icon, not the sidebars' other entries
    assert entry.image_url == "https://i.kym-cdn.com/entries/icons/original/000/000/043/dg1.jpg"
    assert entry.added == "2008-12-30"
    assert entry.summary.startswith("Disaster Girl is a photoshop meme")
    dates = [m.date for m in entry.milestones]
    assert dates[:3] == ["2005-01", "2007-01-02", "2007-11-29"]  # "later that year on Nov 29th"
    first = entry.milestones[0]
    assert "taken by Dave Roth in January 2005" in first.text
    assert "[" not in first.text and "](" not in first.text  # links reduced to their text
    assert "https://travelingroths.blogspot.com/2008/11/i-have-meme-disaster-girl.html" in (
        first.cites
    )
    zooomr = entry.milestones[1]
    assert zooomr.cites == [
        "https://web.archive.org/web/20100218145437/https%3A//www.zooomr.com/photos/travelingroths/592360/"
    ]
    # a footnote right after a full stop still ends the sentence
    buzzfeed = next(m for m in entry.milestones if "Buzzfeed" in m.text)
    assert buzzfeed.date == "2008-10-27" and buzzfeed.text.startswith("The original photo")


def test_parse_wikipedia_and_merge():
    w = wiki()
    assert w.name == "Disaster Girl" and w.url == "https://en.wikipedia.org/wiki/Disaster_Girl"
    assert any(m.date == "2021-04-17" and "180 Ether" in m.text for m in w.milestones)
    merged = merge_milestones([kym(), w])
    assert [g["date"] for g in merged] == sorted(g["date"] for g in merged)
    photo = merged[0]
    # both sources date the photograph to 2005: one milestone, two quotes
    assert photo["date"] == "2005-01"
    assert {q["source"] for q in photo["quotes"]} == {"Know Your Meme", "Wikipedia"}
    # a source never says the same year-only thing twice
    assert sum(1 for g in merged if g["date"] == "2005") == 0


def test_dated_sentences_rules():
    found = dated_sentences(
        [
            "Nothing dated here at all, really nothing. "
            "It was posted on March 3rd, 2012 to a forum[[1]](#fn1). "
            "Then on April 5th it spread. Version 2.2015 is not a year."
        ],
        source="Know Your Meme",
        url="https://knowyourmeme.com/memes/x",
        references={"1": "https://forum.example/t/1"},
    )
    assert [(m.date, m.cites) for m in found] == [
        ("2012-03-03", ["https://forum.example/t/1"]),
        ("2012-04-05", []),
    ]


# real entry pages of the other kinds, as Oxylabs /scrape returned them (2026-11-14)
KINDS = {
    "event": (
        "kym_event_storm_area_51.md",
        "https://knowyourmeme.com/memes/events/storm-area-51",
        "Storm Area 51",
        "Storm Area 51, also known as the Area 51 Raid and Alienstock, is a satirical Facebook",
        {"background", "developments"},
        "2019-06-27",
    ),
    "person": (
        "kym_person_john_pork.md",
        "https://knowyourmeme.com/memes/people/john-pork-john-pork-is-calling",
        "John Pork / John Pork Is Calling",
        "John Pork is a virtual influencer",
        {"history", "online-presence"},
        "2018-09-24",
    ),
    "subculture": (
        "kym_subculture_godzilla.md",
        "https://knowyourmeme.com/memes/subcultures/godzilla",
        "Godzilla",
        "Godzilla is a fictional Japanese giant monster",
        {"history", "online-presence"},
        "1998",
    ),
}


@pytest.mark.parametrize("kind", KINDS)
def test_parse_kym_other_kinds(kind):
    fixture, url, name, summary, sections, first = KINDS[kind]
    markdown = (FIXTURES / fixture).read_text()
    entry = parse_kym(markdown, url)
    assert entry.source == "Know Your Meme" and entry.name == name and entry.url == url
    assert entry.image_url.startswith("https://i.kym-cdn.com/entries/icons/original/")
    assert entry.summary.startswith(summary)
    assert entry.milestones and entry.milestones[0].date == first
    # each dated sentence cites its section on the page, by the page's own anchor id
    assert {m.url.split("#", 1)[1] for m in entry.milestones} == sections
    page = _plain(markdown)[0]
    for m in entry.milestones:
        assert m.source == "Know Your Meme" and m.url.startswith(url + "#")
        assert m.text in page  # the page's own words, never ours
    # the trunk keeps them as quotes credited to Know Your Meme
    for group in merge_milestones([entry]):
        assert all(q["source"] == "Know Your Meme" for q in group["quotes"])


def test_kym_quoted_posts_are_not_the_entry_speaking():
    entry = parse_kym(
        (FIXTURES / "kym_event_storm_area_51.md").read_text(),
        "https://knowyourmeme.com/memes/events/storm-area-51",
    )
    texts = [m.text for m in entry.milestones]
    # the entry's own sentence about the tweets is kept; the embedded tweet it quotes is not
    assert any(t.startswith("On the morning of September 20th, 2019") for t in texts)
    assert not any("@mickakers" in t for t in texts)


def test_parse_kym_needs_an_entry_page():
    # a photo page (what a photo URL is, before it is mapped to its entry) is not an entry
    photo = (FIXTURES / "kym_photo_storm_area_51.md").read_text()
    assert parse_kym(photo, "https://knowyourmeme.com/photos/1907928-storm-area-51") is None
