"""Phase 8b: every typed field is checked before it is stored (a bad answer
is a plain error naming what is expected, never a guess or a traceback), and
data more than 90 days past its last import still plans under a banner."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.dashboard import page, render, serve
from planner.ingest.needs import enter, need_for, needed, parse_value
from planner.ledger import db
from planner.paths import Layout

runner = CliRunner()


@pytest.mark.parametrize(
    ("key", "text", "value"),
    [
        ("wages", " $61,250.40 ", 61250),
        ("short_term_gains", "(1,200)", -1200),
        ("se_income", "-300", -300),
        ("lt_loss_carryover", "0", 0),
        ("ss_claim_age", "67", 67),
        ("hsa_months", "12", 12),
        ("withdrawal_rate", "4%", 0.04),
        ("birth_date", "1971-06-15", "1971-06-15"),
        ("filing_status", " Single ", "single"),
    ],
)
def test_good_answers_are_stored_typed(key: str, text: str, value: object) -> None:
    assert parse_value(need_for(key), text) == value


@pytest.mark.parametrize(
    ("key", "text", "match"),
    [
        ("wages", "", "wages: a dollar amount"),
        ("wages", "lots", "wages: a dollar amount"),
        ("wages", "nan", "wages: a dollar amount"),
        ("wages", "inf", "wages: a dollar amount"),
        ("wages", "1e400", "wages: a dollar amount"),
        ("wages", "-5,000", "wages: not negative"),
        ("premium_monthly", "(20)", "premium_monthly: not negative"),
        ("wages", "250,000,000", "wages: over 100,000,000"),
        ("ss_claim_age", "61", "ss_claim_age: between 62 and 70"),
        ("ss_claim_age", "67.5", "ss_claim_age: a whole number"),
        ("hsa_months", "13", "hsa_months: between 0 and 12"),
        ("withdrawal_rate", "150%", "withdrawal_rate: a fraction between 0 and 1"),
        ("withdrawal_rate", "abc", "withdrawal_rate: a fraction between 0 and 1"),
        ("birth_date", "06/15/1971", "birth_date: a date as YYYY-MM-DD"),
        ("birth_date", "1850-01-01", "birth_date: between 1900-01-01 and today"),
        ("birth_date", "2999-01-01", "birth_date: between 1900-01-01 and today"),
        ("filing_status", "maybe", "filing_status: one of"),
    ],
)
def test_bad_answers_are_refused_with_what_is_expected(
    key: str, text: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_value(need_for(key), text)


def test_text_answers_are_bounded_and_printable() -> None:
    need = next(n for n in _all() if n.kind == "str" and not n.choices)
    assert parse_value(need, "  Fidelity  ") == "Fidelity"
    with pytest.raises(ValueError, match="200 characters"):
        parse_value(need, "x" * 201)
    with pytest.raises(ValueError, match="control characters"):
        parse_value(need, "a\x00b")


def _all() -> list:  # type: ignore[type-arg]
    from planner.ingest.needs import NEEDS

    return list(NEEDS)


def test_cli_and_dashboard_refuse_without_storing(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    r = runner.invoke(app, ["enter", "--year", "2026", "wages", "inf"])
    assert r.exit_code != 0 and "wages: a dollar amount" in r.output
    assert "Traceback" not in r.output
    app_ = serve.App(lay, 2026, date(2026, 10, 1))
    msg = app_.enter("ss_claim_age", "99")
    assert "between 62 and 70" in msg
    with pytest.raises(ValueError):
        enter(lay, 2026, "wages", "-1")
    st = {s.need.key: s.state for s in needed(lay, 2026).items}
    assert st["wages"] == "missing" and st["ss_claim_age"] == "missing"


@pytest.mark.parametrize(
    "args",
    [
        ["esttax", "--year", "1066"],
        [
            "paid",
            "--year",
            "2026",
            "--agency",
            "fed",
            "--on",
            "2026-06-12",
            "--amount",
            "-5",
        ],
        ["run", "--quiet", "--port", "70000"],
    ],
)
def test_cli_numbers_have_bounds(planner_home: Path, args: list[str]) -> None:
    r = runner.invoke(app, args)
    assert r.exit_code == 2, r.output
    assert "Invalid value" in r.output


def _doc(lay: Layout, imported: str) -> None:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint=f"synthetic-{imported}",
            file_name="w2.pdf",
            kind="pdf",
            pages=1,
            batch="b",
            facts=[db.Fact("W-2", 2026, "Employer (synthetic)", "1", "Wages", 1.0, 1)],
        )
        conn.execute("UPDATE documents SET imported_at = ?", (imported,))
        conn.commit()
    finally:
        conn.close()


def test_stale_data_still_plans_under_a_banner(planner_home: Path) -> None:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (("birth_date", "1971-06-15"), ("filing_status", "single")):
        enter(lay, 2026, key, text)
    assert page.gather(lay, 2026, date(2026, 10, 1)).stale_days is None  # no imports
    _doc(lay, "2026-07-01T09:00:00")
    fresh = page.gather(lay, 2026, date(2026, 9, 29))  # 90 days
    assert fresh.stale_days is None
    stale = page.gather(lay, 2026, date(2026, 10, 1))
    assert stale.stale_days == 92 and stale.panels  # still plans
    html = render.html(stale)
    assert 'class="stale"' in html and "92 days since the last import" in html
    live = serve.App(lay, 2026, date(2026, 10, 1)).html()
    assert "92 days since the last import" in live
    assert "92 days since the last import" not in render.html(fresh)
