"""Master plan F01: a value is known (zero included), an estimate, unknown, or
does not apply, and the four stay apart all the way to the output. An unknown
is left out of the arithmetic, so every figure that rests on one says so and is
never shown as actual or ready: missing wages, a missing premium, a missing
cost basis and missing withholding each tested. Synthetic households only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from planner.dashboard import page
from planner.ingest import ingest
from planner.ingest.needs import enter, profile_path
from planner.paths import Layout
from planner.plan import esttax, glidepath, inputs
from planner.plan import year as year_plan
from planner.taxprep import draft, package
from tests.test_capgains import lay as gains_lay  # noqa: F401
from tests.test_csv import drop
from tests.test_spending import lay as glide_lay  # noqa: F401

AFTER = date(2027, 2, 15)  # the year is over: nothing is projected any more


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("interest", "1,000"),
    ):
        enter(lay, 2026, key, text)
    return lay


def test_the_four_states_stay_apart(lay: Layout) -> None:
    inp = inputs.build(lay, 2026)
    assert inp.state("wages") == inputs.UNKNOWN
    assert inp.state("interest") == inputs.KNOWN
    # moot without tips: another answer makes it not apply, so it is not unknown
    assert inp.state("tipped_occupation_code") == inputs.NOT_APPLICABLE
    assert "wages" in inp.tax_unknown and "inflation" not in inp.tax_unknown
    enter(lay, 2026, "wages", "0")
    known_zero = inputs.build(lay, 2026)
    assert known_zero.state("wages") == inputs.KNOWN
    assert known_zero.household.wages == 0 and "wages" not in known_zero.tax_unknown
    standin = inputs.Inputs(2026, None, {"wages": "YTD"}, ["wages"])  # type: ignore[arg-type]
    assert standin.state("wages") == inputs.ESTIMATE


def test_missing_wages_is_never_shown_as_actual(lay: Layout) -> None:
    plan = year_plan.assemble(lay, 2026, AFTER)
    magi = plan.section("magi")
    assert "wages" in magi.rests_on
    assert any(
        n.startswith("rests on unknown (left out, not zero): ") and "wages" in n
        for n in magi.notes
    )
    # after the year a figure is "actual" only when nothing it rests on is unknown
    assert page._tag(magi, projected=False) == page.ESTIMATE
    full = year_plan.Section("magi", True)
    assert page._tag(full, projected=False) == page.ACTUAL
    enter(lay, 2026, "wages", "0")
    assert "wages" not in year_plan.assemble(lay, 2026, AFTER).section("magi").rests_on


def test_a_missing_premium_is_named_not_counted_as_zero(
    glide_lay: Layout,  # noqa: F811
) -> None:
    prof = yaml.safe_load(profile_path(glide_lay).read_text(encoding="utf-8"))
    prof["premium_monthly"] = None
    profile_path(glide_lay).write_text(yaml.safe_dump(prof), encoding="utf-8")
    g = glidepath.glide(glide_lay, 2026, date(2026, 7, 10))
    said = " ".join(g.notes)
    assert "counted as zero" not in said
    assert "premium_monthly unknown: left out of the cash line" in said
    assert "mortgage_monthly" not in said  # typed, so known
    plan = year_plan.assemble(glide_lay, 2026, date(2026, 7, 10))
    assert "premium_monthly" in plan.section("glide").rests_on
    assert "premium_monthly" in plan.section("cash").rests_on
    assert "mortgage_monthly" not in plan.section("glide").rests_on


def test_missing_withholding_is_named_not_counted_as_zero(
    glide_lay: Layout,  # noqa: F811
) -> None:
    et = esttax.estimate(glide_lay, 2026, date(2026, 7, 10))
    said = " ".join(et.notes)
    assert "counted as zero" not in said
    assert "fed_withheld unknown: the amounts due assume nothing was withheld" in said
    plan = year_plan.assemble(glide_lay, 2026, date(2026, 7, 10))
    assert {"fed_withheld", "nc_withheld"} <= set(plan.section("esttax").rests_on)
    enter(glide_lay, 2026, "fed_withheld", "0")
    enter(glide_lay, 2026, "nc_withheld", "0")
    et = esttax.estimate(glide_lay, 2026, date(2026, 7, 10))
    assert "fed_withheld unknown" not in " ".join(et.notes)
    plan = year_plan.assemble(glide_lay, 2026, date(2026, 7, 10))
    assert "fed_withheld" not in plan.section("esttax").rests_on


NO_BASIS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        "11112222,Small Cap,VB,01/02/2020,06/01/2025,10,2500,,Long-term",
        "",
    ]
)


def test_a_missing_cost_basis_is_unknown_and_the_draft_not_ready(
    gains_lay: Layout,  # noqa: F811
) -> None:
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "60,000"),
    ):
        enter(gains_lay, 2025, key, text)
    drop(gains_lay, "lots2.csv", NO_BASIS)
    ingest(gains_lay)
    d = draft.build(gains_lay, 2025)
    basis = [k for k in d.unknown if k.startswith("cost basis: lots2.csv line ")]
    assert len(basis) == 1, d.unknown
    text = draft.render(d)
    assert text.splitlines()[1].startswith(
        "NOT READY for a preparer: it rests on unknown figures (left out, not zero): "
    )
    assert basis[0] in text.splitlines()[1]
    assert "NOT READY for a preparer" in package.html_page(d)
    pack = package.build(gains_lay, 2025)
    assert pack.notes[0].startswith("NOT READY for a preparer"), pack.notes


def test_missing_wages_reach_the_draft_and_the_pack_as_not_ready(lay: Layout) -> None:
    d = draft.build(lay, 2026)
    assert "wages" in d.unknown
    head = draft.render(d).splitlines()[1]
    assert head.startswith("NOT READY for a preparer") and "wages" in head
