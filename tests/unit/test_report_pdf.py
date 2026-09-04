"""PDF conversion is a convenience; its absence must not fail a run (PRD s18)."""

from __future__ import annotations

import pytest

from app.reports import pdf


@pytest.mark.asyncio
async def test_missing_converter_returns_none_rather_than_raising(tmp_path, monkeypatch):
    source = tmp_path / "report.docx"
    source.write_bytes(b"not really a docx")
    monkeypatch.setattr(pdf, "find_converter", lambda: None)
    assert await pdf.convert_to_pdf(source) is None


@pytest.mark.asyncio
async def test_a_missing_source_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        await pdf.convert_to_pdf(tmp_path / "absent.docx")


@pytest.mark.asyncio
async def test_a_failed_conversion_reports_none(tmp_path, monkeypatch):
    source = tmp_path / "report.docx"
    source.write_bytes(b"not really a docx")
    # /usr/bin/false stands in for a converter that runs and fails.
    monkeypatch.setattr(pdf, "find_converter", lambda: "/usr/bin/false")
    assert await pdf.convert_to_pdf(source) is None


def test_converter_lookup_does_not_explode_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(pdf.shutil, "which", lambda _: None)
    monkeypatch.setattr(pdf, "CANDIDATES", ("definitely-not-installed",))
    assert pdf.find_converter() is None
    assert pdf.pdf_available() is False
