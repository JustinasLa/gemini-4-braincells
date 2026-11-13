from datetime import UTC, datetime

import pytest

from analyst.context import ClusterContext, EvidenceItem
from analyst.prompt import build_prompt
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
