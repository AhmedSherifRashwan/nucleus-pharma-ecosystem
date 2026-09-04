"""Progress narration (PRD s17.1)."""

from __future__ import annotations

import json

from app.slack.progress import (
    STAGE_LABELS,
    TaskProgress,
    estimate_label,
    event,
    format_duration,
    overall_fraction,
    progress_bar,
    specialist_finished,
    specialist_started,
    status_line,
)


def tasks(*statuses: str) -> list[TaskProgress]:
    return [
        TaskProgress(
            agent_id=f"A0{i}",
            agent_name=f"Specialist {i}",
            objective=f"Objective {i}",
            status=status,
            execution_group=i // 2,
        )
        for i, status in enumerate(statuses, start=1)
    ]


def test_the_bar_fills_as_specialists_return():
    early = overall_fraction("dispatch", tasks("running", "pending", "pending"))
    late = overall_fraction("dispatch", tasks("complete", "complete", "running"))
    assert early < late < 1.0


def test_a_finished_run_shows_a_full_bar():
    assert progress_bar(overall_fraction("finished", tasks("complete"))).endswith("100%")


def test_the_bar_is_a_fixed_width():
    for fraction in (0.0, 0.33, 1.0):
        bar = progress_bar(fraction).split("  ")[0]
        assert len(bar) == 18


def test_a_failed_specialist_still_counts_as_returned():
    """The bar tracks work that is no longer outstanding, not work that succeeded."""
    assert overall_fraction("dispatch", tasks("failed", "pending")) > overall_fraction(
        "dispatch", tasks("running", "pending")
    )


def test_the_estimate_is_always_labelled_approximate():
    line = status_line("dispatch", tasks("running", "pending"), 300)
    assert "approximate" in line
    assert "est." in line


def test_the_estimate_never_claims_false_precision():
    assert estimate_label(0) == "finishing up"
    assert estimate_label(45) == "about a minute"
    assert "-" in estimate_label(600)


def test_elapsed_time_is_human_readable():
    assert format_duration(45) == "45s"
    assert format_duration(754) == "12m 34s"
    assert format_duration(3700) == "1h 01m"


def test_an_event_says_what_just_happened():
    blocks = event(
        "RUN-1",
        ":white_check_mark: *QTPP Expert* — complete",
        stage="dispatch",
        tasks=tasks("complete", "running"),
        elapsed_seconds=600,
        detail="7 finding(s), 3 evidence item(s) recorded.",
    )
    text = json.dumps(blocks)
    assert "QTPP Expert" in text
    assert "7 finding(s)" in text
    assert "RUN-1" in text
    # Every event carries the whole-run picture, for a reader arriving late.
    assert "elapsed" in text and "%" in text


def test_an_event_can_list_parallel_workstreams():
    blocks = event(
        "RUN-1",
        "Starting 2 workstreams in parallel",
        stage="dispatch",
        tasks=tasks("running", "running"),
        bullets=["*QTPP Expert* — draft the QTPP", "*QbD Expert* — build the framework"],
    )
    text = json.dumps(blocks)
    assert "QTPP Expert" in text and "QbD Expert" in text


def test_specialist_start_and_finish_read_as_narration():
    headline, detail = specialist_started("Stability Expert", "Assess the shelf life")
    assert "is now working" in headline
    assert "shelf life" in detail

    headline, detail = specialist_finished("Stability Expert", "complete", findings=4, evidence=2)
    assert "Stability Expert" in headline and "complete" in headline
    assert detail is not None and "4 finding(s)" in detail


def test_a_specialist_needing_data_says_so_rather_than_reading_as_done():
    headline, _ = specialist_finished("QTPP Expert", "needs_information")
    assert "needs more data" in headline


def test_every_stage_has_a_human_label():
    for stage in ("plan", "dispatch", "challenge", "synthesis", "reporting", "finished"):
        assert stage in STAGE_LABELS
        assert STAGE_LABELS[stage][0].isupper()
