from datetime import UTC, datetime

import pytest

from analyst.context import ClusterContext, EvidenceItem
from analyst.prompt import MAX_EVIDENCE_CHARS, SYSTEM, build_prompt
from analyst.service import analyze
from analyst.verdict import CitedEvidence, VerdictDraft

T0 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)


def ctx(status="coordinated", evidence=True) -> ClusterContext:
    items = [
        EvidenceItem(
            1,
            "signal",
            "31 of 31 accounts posted the same text",
            {"signal": "duplicate_text", "family": "content", "score": 1.0, "polarity": "for"},
            "https://bsky.app/profile/a/post/1",
            "detector:v1.2",
        ),
        EvidenceItem(
            2,
            "signal",
            "Posts landed within 40 seconds",
            {"signal": "burst", "family": "timing", "score": 0.9, "polarity": "for"},
            None,
            "detector:v1.2",
        ),
        EvidenceItem(
            3,
            "signal",
            "Accounts reference a breaking news event",
            {"signal": "news_event", "family": "guard", "score": 0.3, "polarity": "against"},
            None,
            "detector:v1.2",
        ),
        EvidenceItem(
            4,
            "source",
            "Linked page shared by 31 of 31 accounts",
            {"type": "linked_page", "claims": ["The supply was poisoned."]},
            "https://news.example/story",
            "oxylabs-web-api/scrape",
        ),
    ]
    return ClusterContext(
        id=7,
        status=status,
        score=0.95,
        confidence=0.9,
        p_value=0.002,
        size=31,
        window_start=T0,
        window_end=T0,
        synthetic=False,
        updated_at=T0,
        signals={"families_fired": ["content", "timing"]},
        evidence=items if evidence else [],
        sample_posts=["the supply was poisoned"],
    )


class FakeModel:
    name = "fake-gemini"

    def __init__(self, draft: VerdictDraft):
        self._draft = draft
        self.calls = 0
        self.prompt = ""

    def draft(self, system, prompt):
        self.calls += 1
        self.prompt = prompt
        return self._draft


def draft(status="coordinated", ids=(1, 2), confidence=0.8, counter=("news event",)):
    return VerdictDraft(
        status=status,
        confidence=confidence,
        summary="s",
        evidence=[CitedEvidence(evidence_id=i, why="w") for i in ids],
        counter_evidence=list(counter),
        would_change_verdict=["x"],
    )


def test_agreeing_verdict_passes_unchanged():
    v = analyze(ctx(), FakeModel(draft()))
    assert v.status == "coordinated" and v.llm_assessment == "coordinated"
    assert v.agrees_with_detector is True and v.disagreement_reason == ""
    assert v.adjustments == []
    assert v.model == "fake-gemini" and v.detector_status == "coordinated"


def test_invented_citations_are_dropped_and_can_force_insufficient():
    v = analyze(ctx(), FakeModel(draft(ids=(1, 99))))
    assert [c.evidence_id for c in v.evidence] == [1]
    assert v.status == "coordinated"  # the detector's call is never overridden
    assert v.llm_assessment == "insufficient" and v.agrees_with_detector is False
    assert "citations" in v.disagreement_reason
    assert any("99" in a for a in v.adjustments)


@pytest.mark.parametrize(
    "detector,model_says",
    [
        ("insufficient", "coordinated"),
        ("likely_organic", "coordinated"),
        ("insufficient", "likely_organic"),
        ("coordinated", "likely_organic"),
    ],
)
def test_disagreement_is_recorded_not_applied(detector, model_says):
    d = draft(status=model_says).model_copy(update={"disagreement_reason": "because"})
    v = analyze(ctx(status=detector), FakeModel(d))
    assert v.status == detector == v.detector_status
    assert v.llm_assessment == model_says
    assert v.agrees_with_detector is False and v.disagreement_reason == "because"


def test_disagreement_without_reason_is_flagged():
    v = analyze(ctx(status="likely_organic"), FakeModel(draft(status="coordinated")))
    assert v.agrees_with_detector is False
    assert v.disagreement_reason == "The model gave no reason."
    assert any("without giving a reason" in a for a in v.adjustments)


def test_duplicate_citations_count_once():
    v = analyze(ctx(), FakeModel(draft(ids=(1, 1))))
    assert [c.evidence_id for c in v.evidence] == [1]
    assert v.llm_assessment == "insufficient"


def test_model_may_abstain():
    d = draft(status="insufficient", ids=()).model_copy(update={"disagreement_reason": "thin"})
    v = analyze(ctx(), FakeModel(d))
    assert v.status == "coordinated" and v.llm_assessment == "insufficient"
    assert v.adjustments == [] and v.disagreement_reason == "thin"


def test_confidence_is_clamped_and_missing_counter_evidence_noted():
    v = analyze(ctx(), FakeModel(draft(confidence=1.7, counter=())))
    assert v.confidence == 1.0
    assert len(v.adjustments) == 2


def test_no_evidence_skips_the_model():
    model = FakeModel(draft())
    v = analyze(ctx(evidence=False), model)
    assert model.calls == 0
    assert v.status == "coordinated" and v.model == "none"
    assert v.llm_assessment is None and v.agrees_with_detector is None


def test_prompt_lists_citable_ids_polarity_and_claims():
    p = build_prompt(ctx())
    assert "[1] (signal FOR content/duplicate_text" in p
    assert "[3] (signal AGAINST guard/news_event" in p
    assert "claims: The supply was poisoned." in p
    assert "<https://news.example/story>" in p
    assert '"detector_status": "coordinated"' in p


def test_media_ocr_template_and_provenance_reach_prompt_and_are_citable():
    context = ctx()
    context.evidence.extend(
        [
            EvidenceItem(
                51,
                "media",
                "A brief media summary",
                {
                    "description": "A law firm marketing graphic",
                    "extracted_text": ["LAW FIRMS USE VIDEO MARKETING", "CHICAGO"],
                    "reuse_cues": ["city placeholder"],
                    "manipulation_cues": ["cropped header"],
                    "n_accounts": 3,
                    "n_posts": 5,
                },
                "https://bsky.app/profile/a/post/media",
                "nemotron-3.5-super-vl",
            ),
            EvidenceItem(
                52,
                "media",
                "Two images share a template",
                {
                    "type": "ocr_template",
                    "shared_tokens": ["law", "firms", "marketing"],
                    "varying_tokens": ["chicago", "austin"],
                    "n_images": 2,
                    "n_accounts": 3,
                },
                None,
                "nemotron-3.5-super-vl",
            ),
        ]
    )
    model = FakeModel(draft(ids=(51, 52)))
    verdict = analyze(context, model)
    assert "LAW FIRMS USE VIDEO MARKETING / CHICAGO" in model.prompt
    assert '"shared_tokens": "law / firms / marketing"' in model.prompt
    assert '"varying_tokens": "chicago / austin"' in model.prompt
    for expected in (
        "[51] (media)",
        "[52] (media)",
        '"model": "nemotron-3.5-super-vl"',
        "A law firm marketing graphic",
        "city placeholder",
        "cropped header",
        '"n_accounts": 3',
        '"n_posts": 5',
        '"n_images": 2',
        "<https://bsky.app/profile/a/post/media>",
    ):
        assert expected in model.prompt
    assert "Nemotron 3.5 Super VL" in SYSTEM
    assert "OCR text is not proof of who made it" in SYSTEM
    assert [citation.evidence_id for citation in verdict.evidence] == [51, 52]
    assert verdict.llm_assessment == "coordinated" and verdict.adjustments == []


def test_media_prompt_bounds_fields_and_total_evidence():
    context = ctx(evidence=False)
    for ident in range(100):
        context.evidence.append(
            EvidenceItem(
                ident,
                "media",
                "summary " * 1000,
                {
                    "extracted_text": ["OCR " * 1000],
                    "reuse_cues": ["reuse " * 1000],
                    "manipulation_cues": ["crop " * 1000],
                },
                None,
                "nemotron-3.5-super-vl",
            )
        )
    prompt = build_prompt(context)
    assert "OCR " * 151 not in prompt
    assert "reuse " * 41 not in prompt
    assert "crop " * 49 not in prompt
    assert "Further evidence omitted" in prompt
    assert len(prompt) < MAX_EVIDENCE_CHARS + 1000
    evidence = prompt.split("Evidence items (cite by number):\n", 1)[1].split("\n\nSample posts")[0]
    assert len(evidence) <= MAX_EVIDENCE_CHARS


def test_legacy_media_row_without_optional_details_is_supported():
    context = ctx(evidence=False)
    context.evidence.append(EvidenceItem(9, "media", "Legacy summary", {}, None, None))
    assert "[9] (media)" in build_prompt(context)
    assert "Legacy summary" in build_prompt(context)


def test_fit_evidence_guard_sees_only_shown_and_signals_come_first():
    from dataclasses import replace

    from analyst.context import EvidenceItem
    from analyst.guard import enforce
    from analyst.prompt import MAX_EVIDENCE_CHARS, build_prompt, fit_evidence
    from analyst.verdict import CitedEvidence, VerdictDraft

    big = "x" * 3000
    media = [
        EvidenceItem(
            id=i,
            kind="media",
            summary="m",
            detail={"description": big, "extracted_text": [big]},
            source_url=None,
            model="nemotron-3.5-super-vl",
        )
        for i in range(1, 40)
    ]
    signal = EvidenceItem(
        id=100,
        kind="signal",
        summary="organic guard",
        detail={"polarity": "against"},
        source_url=None,
        model="detector",
    )
    full = replace(ctx(), evidence=[*media, signal])
    fitted = fit_evidence(full)
    shown = fitted.evidence_ids
    assert 100 in shown and fitted.evidence_omitted > 0
    prompt = build_prompt(fitted)
    assert "[100]" in prompt and "omitted for size" in prompt
    assert len(prompt) < MAX_EVIDENCE_CHARS + 4000
    hidden = next(e.id for e in media if e.id not in shown)
    draft = VerdictDraft(
        status="coordinated",
        confidence=0.8,
        summary="s",
        evidence=[
            CitedEvidence(evidence_id=hidden, why="w"),
            CitedEvidence(evidence_id=100, why="w"),
        ],
        counter_evidence=[],
        would_change_verdict=[],
        disagreement_reason="",
    )
    v = enforce(draft, fitted, model="m")
    assert [c.evidence_id for c in v.evidence] == [100]  # the hidden id is not accepted


def test_tool_returned_stored_evidence_is_citable_even_if_prompt_left_it_out():
    from dataclasses import replace
    from types import SimpleNamespace

    from analyst.context import EvidenceItem
    from analyst.prompt import fit_evidence
    from analyst.service import investigate
    from analyst.verdict import CitedEvidence, VerdictDraft

    big = "x" * 3000
    filler = [
        EvidenceItem(
            id=i,
            kind="media",
            summary="m",
            detail={"description": big, "extracted_text": [big]},
            source_url=None,
            model="nemotron-3.5-super-vl",
        )
        for i in range(1, 40)
    ]
    signal = EvidenceItem(
        id=1000, kind="signal", summary="s", detail={}, source_url=None, model="detector"
    )
    full = replace(ctx(), evidence=[signal, *filler])
    hidden = next(e for e in filler if e.id not in fit_evidence(full).evidence_ids)
    draft = VerdictDraft(
        status="coordinated",
        confidence=0.8,
        summary="s",
        evidence=[
            CitedEvidence(evidence_id=1000, why="w"),
            CitedEvidence(evidence_id=hidden.id, why="w"),
        ],
        counter_evidence=["c"],
        would_change_verdict=[],
        disagreement_reason="",
    )
    agent = SimpleNamespace(
        name="gemini-test",
        run=lambda *a: SimpleNamespace(draft=draft, model_calls=[], cost_usd=0.0),
    )
    tools = SimpleNamespace(returned={hidden.id: hidden}, new_evidence={}, records=[])
    v = investigate(full, agent, tools)
    assert [c.evidence_id for c in v.evidence] == [1000, hidden.id]


def test_refitting_keeps_the_omission_count():
    from dataclasses import replace

    from analyst.context import EvidenceItem
    from analyst.prompt import fit_evidence

    big = "x" * 3000
    items = [
        EvidenceItem(
            id=i,
            kind="media",
            summary="m",
            detail={"description": big, "extracted_text": [big]},
            source_url=None,
            model="m",
        )
        for i in range(1, 40)
    ]
    once = fit_evidence(replace(ctx(), evidence=items))
    assert once.evidence_omitted > 0
    assert fit_evidence(once).evidence_omitted == once.evidence_omitted


def test_evidence_is_citable_only_after_the_tool_call_succeeded():
    from analyst.context import EvidenceItem
    from analyst.tools import Budget, ToolBox

    class Sink:
        pass

    box = ToolBox(
        ctx(),
        web=None,
        sink=Sink(),
        cache=None,
        budget=Budget(max_tool_calls=5, max_web_calls=5, max_seconds=60),
    )
    item = EvidenceItem(
        id=777, kind="source", summary="s", detail={}, source_url=None, model="oxylabs"
    )

    def broken(args, rec):
        box._remember(item, rec)
        raise ValueError("processing failed after the fetch")

    def fine(args, rec):
        box._remember(item, rec)
        return {"evidence_id": item.id}

    box.declarations = lambda: [{"name": "search_web"}]  # no web client in this test
    box._search_web = broken
    box.call("search_web", {"query": "q"})
    assert 777 not in box.returned
    box._search_web = fine
    box.call("search_web", {"query": "q"})
    assert 777 in box.returned
