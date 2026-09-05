"""Estimates measured rather than assumed (PRD s17.1)."""

from __future__ import annotations

from app.orchestration.timing import (
    DEFAULT_SPECIALIST_SECONDS,
    DurationModel,
    format_range,
    remaining_seconds,
)


def model(seconds: float, samples: int = 5) -> DurationModel:
    return DurationModel(specialist_seconds=seconds, samples=samples, source="this run")


def test_an_unmeasured_estimate_is_wider_than_a_measured_one():
    """A guess should look like a guess."""
    guess = format_range(1200, measured=False)
    measured = format_range(1200, measured=True)
    assert guess != measured
    low_g, high_g = [int(x) for x in guess.split()[1].split("-")]
    low_m, high_m = [int(x) for x in measured.split()[1].split("-")]
    assert (high_g - low_g) > (high_m - low_m)


def test_the_estimate_tracks_what_specialists_actually_cost():
    """The old constants said 8-10 minutes for a run that took an hour."""
    slow = remaining_seconds(model(300), stage="dispatch", outstanding_groups=4)
    fast = remaining_seconds(model(60), stage="dispatch", outstanding_groups=4)
    assert slow > fast * 2


def test_a_wider_parallel_group_costs_a_little_more_than_one_turn():
    one = DurationModel(specialist_seconds=200).group_seconds(1)
    four = DurationModel(specialist_seconds=200).group_seconds(4)
    assert one < four < one * 2


def test_work_already_done_is_not_counted_again():
    before = remaining_seconds(model(200), stage="dispatch", outstanding_groups=1)
    after = remaining_seconds(
        model(200),
        stage="dispatch",
        outstanding_groups=1,
        challenge_done=True,
        synthesis_done=True,
    )
    assert after < before


def test_a_run_waiting_on_a_human_has_no_estimate():
    for stage in ("awaiting_approval", "awaiting_decision", "finished"):
        assert remaining_seconds(model(200), stage=stage, outstanding_groups=2) == 0


def test_the_report_stage_is_counted_until_it_is_done():
    """Composition is minutes of real work, not a rounding error."""
    with_report = remaining_seconds(model(200), stage="synthesis", outstanding_groups=0)
    without = remaining_seconds(
        model(200), stage="synthesis", outstanding_groups=0, report_done=True
    )
    assert with_report - without > 120


def test_a_deployment_with_no_history_falls_back_to_a_generous_default():
    fallback = DurationModel()
    assert fallback.is_measured is False
    assert fallback.specialist_seconds == DEFAULT_SPECIALIST_SECONDS


def test_short_and_long_estimates_read_naturally():
    assert format_range(0, measured=True) == "finishing up"
    assert format_range(60, measured=True) == "a minute or two"
    assert "minutes" in format_range(900, measured=True)
    assert "hours" in format_range(9000, measured=True)
