"""Phase 10, unit 2c: every income stream forecast to December 31 (finding
F03). A statement's year-to-date figure is actual only through its last row;
the owner types the rest of the year as a pay schedule, a remaining amount or
a full-year figure, with a low and high for an uncertain stream. Synthetic
figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest.needs import enter
from planner.ledger import db
from planner.paths import Layout
from planner.plan import forecast, inputs

runner = CliRunner()
YEAR = 2026


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("ordinary_dividends", "3,000"),
        ("qualified_dividends", "3,000"),
    ):
        enter(lay, YEAR, key, text)
    return lay


def _statement_interest(lay: Layout, amount: float, last: str) -> None:
    """A bank download whose interest rows run through ``last``, folded into
    the YTD facts the way ``planner derive`` does."""
    row = db.Row(
        source="examplebank",
        kind="transaction",
        row_key=f"examplebank:{last}",
        account="44444444",
        date=last,
        tax_year=YEAR,
        type="Interest",
        description="interest paid",
        symbol="",
        quantity=None,
        price_cents=None,
        amount_cents=db.to_cents(amount),
        basis_cents=None,
        acquired=None,
        term=None,
        line=1,
        raw="",
    )
    from planner.ingest.derive import derive

    conn = db.connect(lay.data / "ledger" / "planner.db")
    db.add_document(
        conn,
        fingerprint=f"statement:{last}",
        file_name="examplebank.csv",
        kind="csv",
        pages=1,
        batch="b1",
        rows=[row],
    )
    derive(conn, YEAR)
    conn.close()


def test_a_pay_schedule_counts_the_payments_left_in_the_year() -> None:
    d = date
    assert forecast.payments(d(2026, 11, 6), "biweekly", YEAR) == [
        d(2026, 11, 6),
        d(2026, 11, 20),
        d(2026, 12, 4),
        d(2026, 12, 18),
    ]
    assert forecast.payments(d(2026, 10, 31), "monthly", YEAR) == [
        d(2026, 10, 31),
        d(2026, 11, 30),
        d(2026, 12, 31),
    ]
    assert forecast.payments(d(2026, 12, 1), "semimonthly", YEAR) == [
        d(2026, 12, 1),
        d(2026, 12, 16),
    ]
    assert forecast.payments(d(2026, 9, 15), "quarterly", YEAR) == [
        d(2026, 9, 15),
        d(2026, 12, 15),
    ]
    assert forecast.payments(d(2026, 12, 28), "weekly", YEAR) == [d(2026, 12, 28)]
    assert forecast.payments(d(2026, 6, 1), "once", YEAR) == [d(2026, 6, 1)]


def test_year_to_date_interest_plus_a_schedule_is_the_full_year(lay: Layout) -> None:
    _statement_interest(lay, 900, "2026-09-30")
    forecast.save(
        lay,
        YEAR,
        "interest",
        forecast.Typed(cadence="monthly", amount=100, next="2026-10-31"),
    )
    inp = inputs.build(lay, YEAR)
    assert inp.household.interest == 1200
    assert inp.state("interest") == inputs.ESTIMATE
    assert "through 2026-09-30" in inp.origins["interest"]
    assert "3 monthly payments of 100 from 2026-10-31" in inp.origins["interest"]
    (s,) = [s for s in inp.forecast if s.key == "interest"]
    assert (s.actual, s.through, s.remaining, s.full_year) == (
        900,
        "2026-09-30",
        300,
        1200,
    )
    assert (s.owner, s.tax_type) == ("self", "ordinary")


def test_year_to_date_with_nothing_forecast_says_so(lay: Layout) -> None:
    _statement_interest(lay, 900, "2026-09-30")
    inp = inputs.build(lay, YEAR)
    assert inp.household.interest == 900
    assert any(
        n.startswith(
            "interest 900 is year-to-date through 2026-09-30; nothing is "
            "forecast for the rest of the year"
        )
        for n in inp.notes
    )


def test_a_typed_full_year_replaces_the_forecast_rather_than_adding(
    lay: Layout,
) -> None:
    _statement_interest(lay, 900, "2026-09-30")
    forecast.save(lay, YEAR, "interest", forecast.Typed(full_year=1500))
    inp = inputs.build(lay, YEAR)
    assert inp.household.interest == 1500
    assert inp.origins["interest"] == "typed full year"


def test_a_stream_whose_figure_is_in_ignores_its_forecast(lay: Layout) -> None:
    enter(lay, YEAR, "interest", "700")
    forecast.save(lay, YEAR, "interest", forecast.Typed(remaining=300))
    inp = inputs.build(lay, YEAR)
    assert inp.household.interest == 700
    assert "interest: the actual figure is in (typed); its forecast is not used" in (
        inp.notes
    )


def test_a_pay_stub_starts_a_wage_stream(lay: Layout) -> None:
    forecast.save(
        lay,
        YEAR,
        "wages",
        forecast.Typed(
            ytd=40000,
            through="2026-09-30",
            cadence="biweekly",
            amount=2000,
            next="2026-10-09",
        ),
    )
    inp = inputs.build(lay, YEAR)
    assert inp.household.wages == 52000  # six paydays from Oct 9 to Dec 18
    assert "wages" not in inp.unknown and inp.state("wages") == inputs.ESTIMATE


def test_a_schedule_without_the_amount_so_far_leaves_the_stream_unknown(
    lay: Layout,
) -> None:
    forecast.save(
        lay,
        YEAR,
        "wages",
        forecast.Typed(cadence="biweekly", amount=2000, next="2026-10-09"),
    )
    inp = inputs.build(lay, YEAR)
    assert "wages" in inp.unknown and inp.household.wages == 0
    assert any(
        n.startswith("wages: the forecast needs the amount so far") for n in inp.notes
    )


def test_low_and_high_bound_an_uncertain_stream(lay: Layout) -> None:
    _statement_interest(lay, 900, "2026-09-30")
    forecast.save(lay, YEAR, "interest", forecast.Typed(remaining=300, low=0, high=600))
    inp = inputs.build(lay, YEAR)
    assert inp.household.interest == 1200
    assert inp.ranges["interest"] == (900, 1500)
    assert "interest full year 1,200 (low 900, high 1,500)" in inp.notes


@pytest.mark.parametrize(
    ("typed", "words"),
    [
        (
            dict(full_year=1000, cadence="monthly", amount=10, next="2026-10-01"),
            "full year",
        ),
        (dict(cadence="monthly", next="2026-10-01"), "--amount"),
        (dict(cadence="monthly", amount=10, next="2027-01-05"), "in 2026"),
        (dict(cadence="fortnightly", amount=10, next="2026-10-01"), "cadence"),
        (dict(remaining=300, low=400, high=500), "low"),
        (dict(ytd=100), "--through"),
        (dict(owner="neighbor", remaining=1), "owner"),
    ],
)
def test_a_forecast_that_cannot_be_used_is_refused(
    lay: Layout, typed: dict[str, Any], words: str
) -> None:
    with pytest.raises(forecast.ForecastError, match=words):
        forecast.save(lay, YEAR, "interest", forecast.Typed(**typed))


def test_a_spouse_stream_needs_a_joint_return(lay: Layout) -> None:
    forecast.save(lay, YEAR, "interest", forecast.Typed(owner="spouse", full_year=500))
    with pytest.raises(inputs.OverrideError, match="spouse"):
        inputs.build(lay, YEAR)


def test_total_income_goes_to_the_line_the_owner_names(lay: Layout) -> None:
    ov = inputs.Overrides(total_income=10000, total_income_line="se_income")
    inp = inputs.build(lay, YEAR, ov)
    assert inp.household.se_income == 7000  # the total less 3,000 of dividends
    assert "wages" in inp.unknown and inp.household.wages == 0
    assert inp.origins["se_income"].startswith("total_income override")
    with pytest.raises(inputs.OverrideError, match="total_income_line"):
        inputs.Overrides(total_income=1, total_income_line="social_security")
    forecast.save(lay, YEAR, "se_income", forecast.Typed(full_year=5000))
    with pytest.raises(inputs.OverrideError, match="forecast"):
        inputs.build(lay, YEAR, ov)


def test_the_q4_dividend_shortcut_and_a_dividend_forecast_do_not_both_count(
    lay: Layout,
) -> None:
    from planner.ingest.needs import _write, load_manual, manual_path

    data = load_manual(lay, YEAR)
    del data["values"]["ordinary_dividends"]  # no 1099-DIV yet: forecast it
    _write(manual_path(lay, YEAR), data)
    forecast.save(lay, YEAR, "ordinary_dividends", forecast.Typed(full_year=4000))
    assert inputs.build(lay, YEAR).household.qualified_dividends == 3000
    with pytest.raises(inputs.OverrideError, match="q4_dividend_estimate"):
        inputs.build(lay, YEAR, inputs.Overrides(q4_dividend_estimate=500))


def test_the_forecast_command_types_lists_and_clears_a_stream(lay: Layout) -> None:
    _statement_interest(lay, 900, "2026-09-30")
    typed = ["forecast", "interest", "--year", "2026", "--cadence", "monthly"]
    r = runner.invoke(app, [*typed, "--amount", "100", "--next", "2026-10-31"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["forecast", "--year", "2026"])
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines() if ln.startswith("interest"))
    assert "900" in line and "2026-09-30" in line and "1,200" in line
    r = runner.invoke(app, [*typed, "--amount", "x", "--next", "2026-10-31"])
    assert r.exit_code == 2 and "refused" in r.output
    r = runner.invoke(app, ["forecast", "interest", "--year", "2026", "--clear"])
    assert r.exit_code == 0 and forecast.load(lay, YEAR) == {}
