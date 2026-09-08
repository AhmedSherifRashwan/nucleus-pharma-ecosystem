"""Document naming (PRD s18.2).

A filename reaches the client. `Slack_C0BUYN3KV50_1788515577.573649_...` is the
channel and a Unix timestamp, which is what the project was auto-named when the
thread opened - accurate, and useless on a desk.

The subject of the work is a better name, and the request is where it is stated.
"""

from __future__ import annotations

import re

AUTO_PROJECT_PREFIXES = ("slack ", "slack_")

STOPWORDS = {
    "a",
    "an",
    "and",
    "the",
    "for",
    "of",
    "to",
    "in",
    "on",
    "with",
    "please",
    "provide",
    "prepare",
    "produce",
    "detailed",
    "detail",
    "report",
    "give",
    "me",
    "can",
    "you",
    "could",
    "would",
    "i",
    "we",
    "want",
    "need",
    "based",
    "start",
    "starting",
    "thereafter",
    "upon",
    "completion",
    "undertake",
    "ensure",
    "all",
    "according",
    "taking",
    "into",
    "account",
    "assess",
}

MAX_WORDS = 9
MAX_CHARS = 70


def is_auto_named(project_name: str | None) -> bool:
    """Was this project named after its Slack thread rather than its subject?"""
    if not project_name:
        return True
    return project_name.strip().lower().startswith(AUTO_PROJECT_PREFIXES)


def subject_from_request(request: str) -> str:
    """A short subject phrase from the user's own words.

    Deliberately extractive. Naming a document from the request is a place where
    a paraphrase could quietly change what the file claims to be.
    """
    first = re.split(r"[.!?\n]", (request or "").strip(), maxsplit=1)[0]
    first = re.sub(r"[^A-Za-z0-9 %\-/]+", " ", first)
    words = [w for w in first.split() if w]

    kept: list[str] = []
    for word in words:
        if word.lower() in STOPWORDS and not kept:
            continue
        if word.lower() in STOPWORDS and len(kept) < 2:
            continue
        kept.append(word)
        if len(kept) >= MAX_WORDS:
            break

    subject = " ".join(kept).strip(" -/")
    if len(subject) > MAX_CHARS:
        subject = subject[:MAX_CHARS].rsplit(" ", 1)[0]
    return subject or "Nucleus Analysis"


def document_name(project_name: str | None, user_request: str) -> str:
    """The name to put in the filename: the project's, unless it is a thread id."""
    if is_auto_named(project_name):
        return subject_from_request(user_request)
    assert project_name is not None
    return project_name
