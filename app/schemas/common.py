"""Shared vocabulary for every typed contract (PRD v3.0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# s10.2 specialist task status.
TaskStatus = Literal["complete", "needs_information", "blocked", "failed"]

# s15.1 confidence, tied to evidence quality rather than model tone.
ConfidenceLevel = Literal["very_high", "high", "medium", "low", "speculative"]

# s15.1 evidence reliability. Integer levels, 1 (authoritative) to 4 (unverified).
ReliabilityLevel = Literal[1, 2, 3, 4]

RELIABILITY_DESCRIPTIONS: dict[int, str] = {
    1: "Authoritative - regulator, legislation, pharmacopoeia, controlled client "
    "source, primary study where appropriate",
    2: "High-quality secondary - systematic review, recognized database, reputable "
    "technical or industry analysis",
    3: "Secondary intelligence - company website, trade publication, analyst material",
    4: "Unverified - blogs, forums, social media, unverified AI-generated material",
}

# s16 human decision rights.
DecisionClass = Literal["green", "amber", "red"]

DECISION_CLASS_RULES: dict[str, str] = {
    "green": "System may research and analyse within permissions.",
    "amber": "System may prepare a recommendation but requires explicit human "
    "approval before consequential external action or a major assumption change.",
    "red": "Human decision is mandatory; the system cannot execute a binding action.",
}

Severity = Literal["low", "medium", "high", "critical"]

# s6 / s18 requested deliverable shape.
OutputType = Literal["report", "brief", "analysis", "comparison"]


class StrictModel(BaseModel):
    """Reject unknown fields so provider or gateway drift fails loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
