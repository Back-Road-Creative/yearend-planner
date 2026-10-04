"""Phase 4g: the forms the year should produce, on the Phase 4b household
(synthetic: Vanguard dividends and sales in 11111111, SE income, marketplace
premiums, a mortgage)."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ledger import db
from planner.paths import Layout
from planner.taxprep import expected
from tests.test_spending import lay  # noqa: F401

runner = CliRunner()


def _form(home: Layout, name: str, form: str, year: int, issuer: str) -> None:
    conn = db.connect(home.data / "ledger" / "planner.db")
    try:
        db.add_document(
            conn,
            fingerprint=name,
            file_name=name,
            kind="pdf",
            pages=1,
            batch="test",
            facts=[db.Fact(form, year, issuer, "1", "box 1", 100.0, 1)],
        )
    finally:
        conn.close()


def _by(inv: expected.Inventory) -> dict[tuple[str, str], expected.Expected]:
    return {(e.form, e.issuer): e for e in inv.items}


def test_predicted_from_accounts_and_answers(lay: Layout) -> None:  # noqa: F811
    inv = expected.inventory(lay, 2026, date(2026, 12, 1))
    by = _by(inv)
    assert set(by) == {
        ("1099-DIV", "vanguard"),
        ("1099-B", "vanguard"),
        ("1099-INT", "each payer"),
        ("1099-NEC", "each client"),
        ("1095-A", "the marketplace"),
        ("1098", "your loan servicer"),
    }
    div = by[("1099-DIV", "vanguard")]
    assert div.reason == "dividend in 11111111; dividend income"
    assert div.state == expected.EXPECTED
    # February 15, 2027 is Washington's Birthday: the broker has until the 16th
    assert div.due == "2027-02-16" and not div.late
    assert by[("1099-B", "vanguard")].reason == "sales in 11111111"
    assert by[("1095-A", "the marketplace")].due == "2027-02-01"  # Jan 31 a Sunday
    assert not by[("1098", "your loan servicer")].to_file
    assert len(inv.outstanding) == 5 and inv.late == []
    assert expected.lines(inv)[-1] == "5 form(s) still to come"


def test_received_late_last_year_and_unpredicted(lay: Layout) -> None:  # noqa: F811
    _form(lay, "div.pdf", "1099-DIV", 2026, "Vanguard Brokerage Services")
    _form(lay, "int-2025.pdf", "1099-INT", 2025, "First Example Bank (synthetic)")
    _form(lay, "k.pdf", "1099-K", 2026, "Example Pay Inc (synthetic)")
    _form(lay, "b.pdf", "1099-B", 2026, "Vanguard Brokerage Services")
    _form(lay, "b-corrected.pdf", "1099-B", 2026, "Vanguard Brokerage Services")
    inv = expected.inventory(lay, 2026, date(2027, 3, 1))
    by = _by(inv)
    div = by[("1099-DIV", "vanguard")]
    assert (div.state, div.documents) == (expected.RECEIVED, ["div.pdf"])
    b = by[("1099-B", "vanguard")]
    assert b.state == expected.RECEIVED and b.documents == ["b.pdf", "b-corrected.pdf"]
    bank = by[("1099-INT", "First Example Bank (synthetic)")]
    # the interest answer names no payer: last year's bank is that payer
    assert bank.reason == "sent one for 2025; interest income" and bank.late
    assert ("1099-INT", "each payer") not in by
    k = by[("1099-K", "Example Pay Inc (synthetic)")]
    assert (k.state, k.reason) == (expected.RECEIVED, "arrived")
    late = {(e.form, e.issuer) for e in inv.late}
    assert late == {
        ("1099-INT", "First Example Bank (synthetic)"),
        ("1099-NEC", "each client"),
        ("1095-A", "the marketplace"),
    }
    # the received sort last; the 1098 is listed but never late-needed
    assert inv.items[-1].state == expected.RECEIVED
    text = expected.render(inv)
    assert "LATE       1095-A" in text and "HealthCare.gov" in text


def test_conversion_and_ira_contribution(lay: Layout) -> None:  # noqa: F811
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        db.add_conversion(
            conn,
            date="2026-03-02",
            amount_cents=1_000_000,
            taxable_cents=1_000_000,
            source_account="33333333",
            accessible_date="2031-01-01",
        )
    finally:
        conn.close()
    by = _by(expected.inventory(lay, 2026, date(2026, 12, 1)))
    r = by[("1099-R", "vanguard")]
    assert r.reason == "Roth conversion 2026-03-02" and r.to_file
    assert expected.same_issuer("vanguard", "The Vanguard Group (synthetic)")
    assert not expected.same_issuer("fidelity", "The Vanguard Group")


def test_cli_forms_and_needed(lay: Layout) -> None:  # noqa: F811
    r = runner.invoke(app, ["forms", "--year", "2026", "--as-of", "2026-12-01"])
    assert r.exit_code == 0, r.output
    assert "Forms for 2026 (as of 2026-12-01)" in r.output
    assert "5 form(s) still to come" in r.output
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2027-03-01"])
    assert r.exit_code == 0, r.output
    assert "form      1099-NEC from each client (due 2027-02-01)" in r.output
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2026-12-01"])
    assert "form      " not in r.output


def test_waive_takes_a_late_form_off_and_undo_restores_it(
    lay: Layout,  # noqa: F811
) -> None:
    _form(lay, "int-2025.pdf", "1099-INT", 2025, "First Example Bank (synthetic)")
    as_of = date(2027, 3, 1)
    bank = ("1099-INT", "First Example Bank (synthetic)")
    assert bank in {
        (e.form, e.issuer) for e in expected.inventory(lay, 2026, as_of).late
    }
    expected.waive(lay, 2026, *bank, as_of)
    inv = expected.inventory(lay, 2026, as_of)
    held = _by(inv)[bank]
    assert held.waived and held.late and held not in inv.late
    assert held not in inv.outstanding and inv.waived == [held]
    assert expected.lines(inv)[[e.issuer for e in inv.items].index(bank[1])].startswith(
        "waived     1099-INT"
    )
    assert expected.load_waived(lay, 2026) == {bank}
    assert expected.load_waived(lay, 2027) == set()  # one year only
    expected.waive(lay, 2026, *bank, as_of)  # idempotent
    assert expected.load_waived(lay, 2026) == {bank}
    assert expected.unwaive(lay, 2026, *bank)
    assert not expected.unwaive(lay, 2026, *bank)
    inv = expected.inventory(lay, 2026, as_of)
    assert bank in {(e.form, e.issuer) for e in inv.late} and inv.waived == []


def test_waive_refuses_an_unknown_or_received_form(lay: Layout) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="no form still to come: 1099-B from nobody"):
        expected.waive(lay, 2026, "1099-B", "nobody", date(2027, 3, 1))
    _form(lay, "div.pdf", "1099-DIV", 2026, "Vanguard Brokerage Services")
    with pytest.raises(ValueError, match="no form still to come: 1099-DIV"):
        expected.waive(lay, 2026, "1099-DIV", "vanguard", date(2027, 3, 1))
    assert not expected.waived_path(lay).exists()


def test_cli_waive_and_needed_hint(lay: Layout) -> None:  # noqa: F811
    args = ["--year", "2026", "--form", "1099-NEC", "--issuer", "each client"]
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2027-03-01"])
    assert (
        'planner waive --year 2026 --form 1099-NEC --issuer "each client"' in r.output
    )
    r = runner.invoke(app, ["waive", *args])
    assert r.exit_code == 0 and "waived 1099-NEC from each client" in r.output
    r = runner.invoke(app, ["needed", "--year", "2026", "--as-of", "2027-03-01"])
    assert "1099-NEC from each client" not in r.output
    r = runner.invoke(app, ["forms", "--year", "2026", "--as-of", "2027-03-01"])
    assert "waived     1099-NEC" in r.output
    r = runner.invoke(app, ["waive", *args, "--undo"])
    assert r.exit_code == 0 and "back on the Needed list" in r.output
    r = runner.invoke(app, ["waive", *args, "--undo"])
    assert r.exit_code == 2 and "was not waived" in r.output
    r = runner.invoke(
        app, ["waive", "--year", "2026", "--form", "1099-B", "--issuer", "x"]
    )
    assert r.exit_code == 2 and "refused: no form still to come" in r.output
