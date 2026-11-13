"""Prompt for the verdict model. The model explains; the detector decides what can be flagged."""

import json

from analyst.context import ClusterContext, EvidenceItem

SYSTEM = """You review one group of Bluesky accounts that an automated detector flagged for possible
coordinated amplification (bots or a campaign). You explain the evidence; you do not detect.

Rules:
- Use ONLY the evidence items listed below. Cite them by their number, e.g. evidence_id 123.
  Never invent accounts, numbers, dates, links or facts that are not in the list.
- Status meanings: "coordinated" = the behaviour evidence shows accounts acting in concert;
  "likely_organic" = the evidence fits independent people reacting to the same thing;
  "insufficient" = the evidence does not support either call with confidence.
- The detector's status is the official verdict; you explain it. Your "status" field is YOUR
  OWN assessment of the evidence. If it differs from detector_status, give the concrete reason in
  disagreement_reason (leave it empty when you agree). Honest disagreement is useful and is shown
  to users; never stretch the evidence to agree. When in doubt, say "insufficient".
- Judge behaviour, not opinions. A political, controversial or false message is not evidence of
  coordination by itself, and agreeing with a message is not suspicious.
- Signals marked AGAINST are organic guards: treat them as counter-evidence and address them.
- counter_evidence must name the strongest reasons this might be organic, even if you think it
  is coordinated. would_change_verdict lists concrete observations that would flip your call.
- confidence is your probability that the status is right, from 0.0 to 1.0. Be calibrated.
- Do not guess who is behind the accounts or their identities.
- summary: one or two plain sentences a journalist could read.
- Return JSON only, matching the schema."""


def _evidence_line(e: EvidenceItem) -> str:
    d = e.detail
    if e.kind == "signal":
        tag = (
            f"signal {d.get('polarity', 'for').upper()} {d.get('family', '')}/{d.get('signal', '')}"
        )
        tag += f" score={d.get('score')}"
    elif e.kind == "source" and d.get("type") == "linked_page":
        claims = "; ".join(d.get("claims") or [])
        tag = "source linked_page" + (f" claims: {claims}" if claims else "")
    elif e.kind == "source":
        tag = f"source {d.get('type', '')} {d.get('purpose', '')}".strip()
    else:
        tag = e.kind
    url = f" <{e.source_url}>" if e.source_url else ""
    return f"[{e.id}] ({tag}) {e.summary}{url}"


def build_prompt(ctx: ClusterContext, *, max_posts: int = 8, max_chars: int = 280) -> str:
    sig = ctx.signals or {}
    header = {
        "cluster_id": ctx.id,
        "detector_status": ctx.status,
        "detector_score": ctx.score,
        "detector_confidence": ctx.confidence,
        "timing_p_value": ctx.p_value,
        "accounts": ctx.size,
        "window": f"{ctx.window_start.isoformat()} to {ctx.window_end.isoformat()}",
        "families_fired": sig.get("families_fired"),
        "family_scores": sig.get("family_scores"),
        "organic_guard_score": sig.get("organic_guard_score"),
        "status_rule": sig.get("status_rule"),
    }
    posts = "\n".join(
        f"- {' '.join(t.split())[:max_chars]}" for t in ctx.sample_posts[:max_posts] if t
    )
    evidence = "\n".join(_evidence_line(e) for e in ctx.evidence)
    return (
        f"Detector output:\n{json.dumps(header, default=str, indent=1)}\n\n"
        f"Evidence items (cite by number):\n{evidence or '(none)'}\n\n"
        f"Sample posts (context only, not citable):\n{posts or '(none)'}\n"
    )
