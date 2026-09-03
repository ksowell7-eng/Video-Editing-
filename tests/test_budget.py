"""The cap has to hold even when a run dies mid-generation."""

import json

import pytest

from pipeline.budget import Budget
from pipeline.errors import BudgetExceeded


@pytest.fixture
def ledger(tmp_path):
    return Budget(tmp_path / "budget.json", run_id="r1", max_per_run=5.0, max_total=20.0)


def test_a_reservation_counts_against_the_cap_immediately(ledger):
    ledger.reserve("gen", 3.0)
    assert ledger.spent()[0] == pytest.approx(3.0)
    assert ledger.remaining() == pytest.approx(2.0)


def test_the_per_run_cap_refuses_the_call_before_it_is_made(ledger):
    ledger.reserve("first", 4.0)
    with pytest.raises(BudgetExceeded, match="per-run cap"):
        ledger.reserve("second", 2.0)


def test_the_lifetime_cap_spans_separate_runs(tmp_path):
    path = tmp_path / "budget.json"
    for i in range(4):
        Budget(path, run_id=f"run{i}", max_per_run=6.0, max_total=20.0).reserve("gen", 5.0)
    with pytest.raises(BudgetExceeded, match="lifetime cap"):
        Budget(path, run_id="run4", max_per_run=6.0, max_total=20.0).reserve("gen", 5.0)


def test_settling_replaces_the_estimate_with_the_real_cost(ledger):
    reservation = ledger.reserve("gen", 3.0)
    reservation.settle(1.25)
    assert ledger.spent()[0] == pytest.approx(1.25)


def test_releasing_frees_the_hold_when_nothing_was_charged(ledger):
    reservation = ledger.reserve("gen", 4.0)
    reservation.release("provider returned an error")
    assert ledger.spent()[0] == pytest.approx(0.0)
    ledger.reserve("retry", 4.0)     # the budget is available again


def test_an_abandoned_reservation_still_counts(ledger):
    # The crash case: reserved, never settled. The next run must see the spend.
    ledger.reserve("gen", 4.0)
    reopened = Budget(ledger.path, run_id="r1", max_per_run=5.0, max_total=20.0)
    assert reopened.spent()[0] == pytest.approx(4.0)


def test_unknown_pricing_is_refused_when_failing_closed(ledger):
    with pytest.raises(BudgetExceeded, match="No price known"):
        ledger.reserve("mystery", None)


def test_unknown_pricing_is_allowed_when_failing_open(tmp_path):
    budget = Budget(tmp_path / "b.json", run_id="r", max_per_run=1.0, max_total=1.0,
                    fail_closed=False)
    budget.reserve("mystery", None)
    assert budget.spent()[0] == 0.0


def test_a_corrupt_ledger_is_quarantined_not_lost(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{not json at all")
    budget = Budget(path, run_id="r", max_per_run=5.0, max_total=5.0)
    budget.reserve("gen", 1.0)
    assert budget.spent()[0] == pytest.approx(1.0)
    assert list(tmp_path.glob("budget.corrupt-*.json")), "the unreadable file should be kept"


def test_spend_is_isolated_per_run(tmp_path):
    path = tmp_path / "budget.json"
    Budget(path, run_id="a", max_per_run=5.0, max_total=20.0).reserve("gen", 3.0)
    other = Budget(path, run_id="b", max_per_run=5.0, max_total=20.0)
    run_spend, total_spend = other.spent()
    assert run_spend == 0.0
    assert total_spend == pytest.approx(3.0)


def test_the_ledger_stays_valid_json(ledger):
    ledger.reserve("gen", 1.0).settle(0.5, note="done")
    entries = json.loads(ledger.path.read_text())["entries"]
    assert entries[0]["state"] == "settled"
    assert entries[0]["note"] == "done"
