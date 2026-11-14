"""Where a meme template came from, as written by Know Your Meme and Wikipedia: the entry, its
picture (so the caller can check it is the same photograph), and dated sentences quoted
verbatim with the references they cite.

Nothing here is our own finding. Every milestone is one sentence from one of those pages,
copied as written (markdown links reduced to their text) with the page and its footnotes, so
a reader can open the source and find the same sentence.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote, unquote, urlsplit

KYM = "https://knowyourmeme.com"
_ENTRY = re.compile(r"^https?://(?:www\.)?knowyourmeme\.com/(?:sensitive/)?memes/([a-z0-9-]+)/?$")
_PHOTO = re.compile(r"^https?://(?:www\.)?knowyourmeme\.com/(?:sensitive/)?photos/\d+-([a-z0-9-]+)")
_ICON = re.compile(r"https://i\.kym-cdn\.com/entries/icons/(?:original|mobile|facebook)/[\w/.%-]+")
_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FOOTNOTE = re.compile(r"\[\[(\d+)\]\]\(#fn\d+\)")
_REFERENCE = re.compile(r"^\[\[(\d+)\]\]\(#fnr\d+\)\s*(.*)$")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"((?:19|20)\d{2})"
# most specific first; each gives (month, day, year) groups, missing ones None
_DATES = [
    re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+{_YEAR}\b"),
    re.compile(rf"\b{_DAY}\s+{_MONTH},?\s+{_YEAR}\b"),
    re.compile(rf"\b{_MONTH},?\s+{_YEAR}\b"),
    re.compile(rf"\b{_MONTH}\s+{_DAY}\b(?!,?\s*\d)"),
    re.compile(rf"(?<![\d/.-]){_YEAR}(?![\d/.-])"),
]
_SENTENCE = re.compile(r"(?<=[.!?\]])\s+(?=[A-Z\"“])")
_NOTE_MARK = re.compile(r"\u27e6(\d+)\u27e7")  # ⟦n⟧: a footnote kept through sentence splitting
MAX_MILESTONES = 8


@dataclass(frozen=True, slots=True)
class Milestone:
    """One dated sentence from a source page. `date` is ISO, as precise as the sentence:
    "2007-01-02", "2005-01" or "2005"."""

    date: str
    text: str
    source: str  # "Know Your Meme" | "Wikipedia"
    url: str  # the page (section anchor when known)
    cites: list[str] = field(default_factory=list)  # the references the sentence cites

    @property
    def year(self) -> int:
        return int(self.date[:4])


@dataclass(frozen=True, slots=True)
class OriginEntry:
    source: str
    name: str
    url: str
    image_url: str | None
    summary: str  # the entry's first sentence, verbatim
    added: str | None = None  # when the entry was written (KYM "Added"), ISO date
    milestones: list[Milestone] = field(default_factory=list)


def kym_entry_url(url: str | None) -> str | None:
    """The Know Your Meme entry a KYM page belongs to: the entry itself, or a photo's entry
    (/photos/130-disaster-girl -> /memes/disaster-girl)."""
    if not url:
        return None
    url = url.split("#", 1)[0].split("?", 1)[0]
    if m := _ENTRY.match(url):
        return f"{KYM}/memes/{m.group(1)}"
    if m := _PHOTO.match(url):
        return f"{KYM}/memes/{m.group(1)}"
    return None


def _plain(markdown: str) -> tuple[str, list[str]]:
    """Markdown -> its reading text, and the footnote numbers it cites, in order."""
    notes = _FOOTNOTE.findall(markdown) + _NOTE_MARK.findall(markdown)
    text = _NOTE_MARK.sub("", _FOOTNOTE.sub("", markdown))
    text = _LINK.sub(lambda m: "" if m.group(0).startswith("!") else m.group(1), text)
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip(), notes


def _date_of(sentence: str, year_before: int | None) -> tuple[str, int] | None:
    for i, pattern in enumerate(_DATES):
        m = pattern.search(sentence)
        if not m:
            continue
        g = m.groups()
        if i == 0:
            month, day, year = _MONTHS[g[0].lower()], int(g[1]), int(g[2])
        elif i == 1:
            day, month, year = int(g[0]), _MONTHS[g[1].lower()], int(g[2])
        elif i == 2:
            month, day, year = _MONTHS[g[0].lower()], None, int(g[1])
        elif i == 3:
            if year_before is None:
                continue
            month, day, year = _MONTHS[g[0].lower()], int(g[1]), year_before
        else:
            month, day, year = None, None, int(g[0])
        if not 1990 <= year <= date.today().year:
            continue
        try:
            if day:
                return date(year, month, day).isoformat(), year
        except ValueError:
            day = None
        return (f"{year}-{month:02d}" if month else str(year)), year
    return None


def dated_sentences(
    paragraphs: list[str], *, source: str, url: str, references: dict[str, str] | None = None
) -> list[Milestone]:
    """Each sentence that carries a date, verbatim, with the references it cites. A month and
    day without a year ("later that year on November 29th") take the paragraph's last year."""
    out: list[Milestone] = []
    for paragraph in paragraphs:
        year: int | None = None
        # footnotes become ⟦n⟧ so "views.[[2]](#fn2) The original" still splits after "]"
        marked = _FOOTNOTE.sub(lambda m: f"\u27e6{m.group(1)}\u27e7]", paragraph.strip())
        for raw in _SENTENCE.split(marked):
            raw = raw.replace("\u27e7]", "\u27e7")
            text, notes = _plain(raw)
            if len(text) < 25:
                continue
            found = _date_of(text, year)
            if found is None:
                continue
            when, year = found
            cites = [references[n] for n in dict.fromkeys(notes) if references and n in references]
            out.append(Milestone(date=when, text=text[:400], source=source, url=url, cites=cites))
    return out


def _sections(markdown: str, level: str = "## ") -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.startswith(level):
            current = sections.setdefault(line[len(level) :].strip().lower(), [])
            continue
        if line.startswith("#"):
            # a deeper heading stays in its section; a shallower one closes it
            if not line.startswith(level.strip() + "#"):
                current = None
            continue
        if current is not None:
            current.append(line)
    return sections


def _paragraphs(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    return [
        p.strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip() and not p.strip().startswith(("[![", "![", "|", ":"))
    ]


def parse_kym(markdown: str, url: str) -> OriginEntry | None:
    """A Know Your Meme entry page (Oxylabs markdown) -> name, picture, summary, milestones."""
    url = kym_entry_url(url) or url
    titles = [m.start() for m in re.finditer(r"^# (.+)$", markdown, re.M)]
    if not titles:
        return None
    # the page repeats the entry header; the entry's own picture is the first icon after the
    # first "# Name" heading (the sidebars above it carry other entries' icons)
    start = titles[0]
    name = re.match(r"# (.+)", markdown[start:]).group(1).strip()
    icon = _ICON.search(markdown, start)
    added = None
    if m := re.search(r"^Added\s*\n\s*([A-Z][a-z]{2} \d{1,2}, \d{4})", markdown[start:], re.M):
        try:
            from datetime import datetime

            added = datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            added = None
    sections = _sections(markdown[start:])
    references: dict[str, str] = {}
    for line in sections.get("external references", []):
        if m := _REFERENCE.match(line.strip()):
            links = _LINK.findall(m.group(2))
            if links:
                references[m.group(1)] = links[-1][1]
    about = _paragraphs(sections.get("about", []))
    summary = _plain(about[0])[0] if about else ""
    summary = _SENTENCE.split(summary)[0] if summary else ""
    milestones = []
    for section in ("origin", "spread"):
        milestones += dated_sentences(
            _paragraphs(sections.get(section, [])),
            source="Know Your Meme",
            url=f"{url}#{section}",
            references=references,
        )
    image = icon.group(0) if icon else None
    if image:
        image = re.sub(r"/entries/icons/(?:mobile|facebook)/", "/entries/icons/original/", image)
    return OriginEntry(
        source="Know Your Meme",
        name=name[:80],
        url=url,
        image_url=image,
        summary=summary[:400],
        added=added,
        milestones=milestones[:MAX_MILESTONES],
    )


def wikipedia_title(url: str | None) -> str | None:
    """'Disaster_Girl' from an English Wikipedia article URL."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.hostname not in ("en.wikipedia.org", "en.m.wikipedia.org"):
        return None
    m = re.match(r"^/wiki/([^:#?]+)$", parts.path)
    return unquote(m.group(1)) if m else None


def wikipedia_urls(title: str) -> tuple[str, str]:
    """(REST summary URL, plain-text extract URL) for an English Wikipedia title."""
    t = quote(title.replace(" ", "_"), safe="")
    return (
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}",
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1"
        f"&redirects=1&format=json&titles={t}",
    )


def parse_wikipedia(summary: dict[str, Any], extract: dict[str, Any]) -> OriginEntry | None:
    """The REST summary (title, picture, page) and the plain-text extract (dated sentences)."""
    if summary.get("type") != "standard":
        return None  # a disambiguation page or a missing article
    title = summary.get("title") or ""
    page = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page")
    image = (summary.get("originalimage") or summary.get("thumbnail") or {}).get("source")
    pages = ((extract.get("query") or {}).get("pages") or {}).values()
    text = next((p.get("extract") for p in pages if p.get("extract")), "") or ""
    # the article body up to its reception/legacy and reference lists
    body = re.split(r"\n==\s*(?:Reception|Legacy|References|See also|External links)\s*==", text)[0]
    paragraphs = [p for p in re.split(r"\n+", body) if p.strip() and not p.startswith("=")]
    if not title or not page:
        return None
    lead = (summary.get("extract") or "").strip()
    return OriginEntry(
        source="Wikipedia",
        name=title,
        url=page,
        image_url=image,
        summary=_SENTENCE.split(lead)[0][:400] if lead else "",
        milestones=dated_sentences(paragraphs, source="Wikipedia", url=page)[:MAX_MILESTONES],
    )


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())}


def merge_milestones(entries: list[OriginEntry]) -> list[dict[str, Any]]:
    """Milestones from every entry, oldest first. Two sentences from different sources about
    the same year that share enough words are one milestone with both sources (they agree);
    each keeps its own words and link."""
    merged: list[dict[str, Any]] = []
    for entry in entries:
        for m in entry.milestones:
            twin = next(
                (
                    g
                    for g in merged
                    if g["year"] == m.year
                    and m.source not in {q["source"] for q in g["quotes"]}
                    and len(_words(m.text) & _words(g["quotes"][0]["text"])) >= 2
                ),
                None,
            )
            quote_ = {"source": m.source, "text": m.text, "url": m.url, "cites": m.cites}
            # the same source dating two sentences the same way: the first one says it
            again = any(
                (
                    g["date"] == m.date
                    or (g["year"] == m.year and 4 in (len(m.date), len(g["date"])))
                )
                and any(q["source"] == m.source for q in g["quotes"])
                for g in merged
            )
            if again:
                continue  # the same source saying the same thing twice
            if twin:
                twin["quotes"].append(quote_)
                if len(m.date) > len(twin["date"]) and m.date.startswith(twin["date"][:4]):
                    twin["date"] = m.date
            else:
                merged.append({"date": m.date, "year": m.year, "quotes": [quote_]})
    merged.sort(key=lambda g: g["date"])
    return merged
