"""DOCX to PDF conversion (PRD s18, Phase 12).

Conversion is a convenience, not the deliverable: the DOCX is authoritative. If
no converter is installed the run still succeeds and the DOCX still ships, so
this module reports absence rather than raising.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.audit.logger import get_logger

log = get_logger(__name__)

# LibreOffice is the only converter that renders python-docx output faithfully
# on a machine with no Word install.
CANDIDATES = (
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
)

CONVERT_TIMEOUT_SECONDS = 180


def find_converter() -> str | None:
    for candidate in CANDIDATES:
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def pdf_available() -> bool:
    return find_converter() is not None


async def convert_to_pdf(
    docx_path: Path | str, *, output_dir: Path | str | None = None
) -> Path | None:
    """Convert a DOCX to PDF beside it. Returns None when conversion is unavailable."""
    source = Path(docx_path)
    if not source.is_file():
        raise FileNotFoundError(source)

    converter = find_converter()
    if converter is None:
        log.info("pdf_conversion_unavailable", reason="no LibreOffice found on this machine")
        return None

    target_dir = Path(output_dir) if output_dir else source.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        converter,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(target_dir),
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), CONVERT_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        log.warning("pdf_conversion_timeout", path=str(source))
        return None

    expected = target_dir / f"{source.stem}.pdf"
    if process.returncode != 0 or not expected.is_file():
        log.warning(
            "pdf_conversion_failed",
            returncode=process.returncode,
            error=stderr.decode(errors="replace")[:300],
        )
        return None
    log.info("pdf_generated", path=str(expected), bytes=expected.stat().st_size)
    return expected
