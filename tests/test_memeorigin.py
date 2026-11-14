"""sources.memeorigin: Know Your Meme and Wikipedia entries -> verbatim dated sentences."""

import json
from pathlib import Path

from sources.memeorigin import (
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
