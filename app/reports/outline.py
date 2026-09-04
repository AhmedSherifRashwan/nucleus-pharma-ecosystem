"""The report outline (PRD s18, s32).

The outline is derived from what the user asked for and what the specialists
actually produced. It is not a fixed template: a QTPP request and a stability
investigation do not deserve the same table of contents, and a section with no
supporting work is worse than no section at all.

Deriving it is a model judgement, so it is a typed contract like every other
model boundary, and it is validated in code before anything is composed.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.schemas.common import StrictModel

MIN_SECTIONS = 3
MAX_SECTIONS = 20


class OutlineSubsection(StrictModel):
    heading: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=600)


class OutlineSection(StrictModel):
    heading: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=800)
    subsections: list[OutlineSubsection] = Field(default_factory=list, max_length=25)
    # Which saved work products carry the material for this section.
    source_artifact_refs: list[str] = Field(default_factory=list, max_length=20)
    expected_tables: list[str] = Field(default_factory=list, max_length=8)


class ReportOutline(StrictModel):
    """The table of contents, before any content exists to fill it."""

    document_title: str = Field(min_length=1, max_length=250)
    product: str | None = None
    strength: str | None = None
    dosage_form: str | None = None
    sections: list[OutlineSection] = Field(min_length=MIN_SECTIONS, max_length=MAX_SECTIONS)

    @model_validator(mode="after")
    def _headings_are_distinct(self) -> ReportOutline:
        seen = [s.heading.strip().lower() for s in self.sections]
        if len(set(seen)) != len(seen):
            raise ValueError("section headings must be distinct")
        return self


def known_refs_only(outline: ReportOutline, available: set[str]) -> ReportOutline:
    """Drop artifact references the outline invented.

    A composer prompt built around a non-existent artifact would produce a
    section with nothing behind it, which is precisely the failure this pipeline
    exists to prevent.
    """
    cleaned = [
        section.model_copy(
            update={
                "source_artifact_refs": [r for r in section.source_artifact_refs if r in available]
            }
        )
        for section in outline.sections
    ]
    return outline.model_copy(update={"sections": cleaned})
