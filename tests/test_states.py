"""The state registry (unit 3d-1): which states tax income, whose
estimated-tax rules the planner has, and every place that used to assume NC.
Synthetic households only."""

from __future__ import annotations

from datetime import date

import pytest

from planner import coverage, states
from planner.ingest.needs import NEEDS, enter, parse_value
from planner.paths import Layout
from planner.plan import calendar, esttax
from tests.test_spending import lay  # noqa: F401

NO_TAX = {"AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"}


def _need(key: str):  # type: ignore[no-untyped-def]
    return next(n for n in NEEDS if n.key == key)


def test_registry_is_fifty_states_and_dc() -> None:
    assert len(states.STATES) == 51 and "DC" in states.STATES
    assert {c for c, s in states.STATES.items() if not s.income_tax} == NO_TAX
    assert states.get("nc").agency == "nc"
    assert states.rules("NC") == (states.STATES["NC"].est, True)
    # a state whose own rules are not in the planner: the federal ones stand in
    assert states.rules("CO") == (states.FEDERAL, False)
    assert states.payee("NC").search("NCDOR TAX PYMT")  # type: ignore[union-attr]
    assert states.payee("CA") is None
    with pytest.raises(ValueError, match="not a state code"):
        states.get("ZZ")


def test_agencies_follow_the_state() -> None:
    assert esttax.agencies("TX") == ("fed",)
    assert esttax.agencies("NC") == ("fed", "nc")
    assert esttax.agencies("CA") == ("fed", "ca")
    assert esttax.agencies("ZZ") == ("fed",)  # the coverage gate names it
    assert esttax.due_dates(2026, "co") == esttax.due_dates(2026)
    assert [esttax.withheld_key(a) for a in ("fed", "nc", "ca")] == [
        "fed_withheld",
        "nc_withheld",
        "state_withheld",
    ]


def test_state_need_takes_a_code() -> None:
    need = _need("state")
    assert parse_value(need, "ca") == "CA"
    assert parse_value(need, " NC ") == "NC"
    with pytest.raises(ValueError, match="two-letter code"):
        parse_value(need, "North Carolina")


@pytest.mark.parametrize(
    ("state", "nc", "other"),
    [("NC", True, False), ("CA", False, True), ("TX", False, False)],
)
def test_state_items_are_asked_by_state(state: str, nc: bool, other: bool) -> None:
    so_far = {"state": state}
    for key in ("prior_nc_tax", "nc_withheld", "nc_additions", "nc_use_tax"):
        assert _need(key).asked(so_far) is nc, key
    for key in ("prior_state_tax", "state_withheld"):
        assert _need(key).asked(so_far) is other, key


def test_calendar_names_the_households_state() -> None:
    def q(ds: list[calendar.Deadline], n: int) -> str:
        return next(d.item for d in ds if d.item.startswith(f"Q{n} estimated"))

    tx = calendar.deadlines(2026, "TX")
    assert q(tx, 4) == "Q4 estimated payments (federal)"
    assert not any("Medicaid" in d.item for d in tx)
    ca = calendar.deadlines(2026, "CA", {2: ("ca",), 3: ()})
    assert q(ca, 2) == (
        "Q2 estimated payments (federal and CA) if required: required for CA"
    )
    assert any("Medicaid" in d.item for d in calendar.deadlines(2026, "NC"))


def test_coverage_state_gaps(lay: Layout) -> None:  # noqa: F811
    def state_gaps(code: str) -> list[str]:
        return [
            g.reason for g in coverage.gate(lay, code, "SINGLE") if g.area == "state"
        ]

    assert state_gaps("TX") == []  # no income tax, nothing to draft
    assert state_gaps("NC") == []
    assert state_gaps("CA") == []  # Form 540 drafted (unit 3d-6)
    assert state_gaps("NY") == []  # Form IT-201 drafted (unit 3d-7)
    assert state_gaps("PA") == []  # Form PA-40 drafted (unit 3d-8)
    assert state_gaps("IL") == []  # Form IL-1040 drafted (unit 3d-9)
    assert state_gaps("OH") == []  # Form IT 1040 drafted (unit 3d-10)
    assert state_gaps("GA") == []  # Form 500 drafted (unit 3d-11)
    assert state_gaps("MI") and "MI return is not drafted" in state_gaps("MI")[0]
    assert "not a state's two-letter code" in state_gaps("ZZ")[0]


@pytest.mark.engine
def test_a_state_without_its_own_rules_is_estimated_on_federal_ones(
    lay: Layout,  # noqa: F811
) -> None:
    enter(lay, 2026, "state", "CO")
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_state_tax", "2,000")
    enter(lay, 2026, "state_withheld", "0")
    esttax.record(lay, 2026, "co", "2026-04-12", 400.0)
    with pytest.raises(ValueError, match="agency must be"):
        esttax.record(lay, 2026, "tx", "2026-04-12", 1.0)
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    fed, co = et.agencies
    assert (fed.name, co.name) == ("fed", "co")
    assert co.prior_tax == 2_000.0 and co.current_tax > 0
    assert [p.amount for p in co.payments] == [400.0]
    assert [i.due for i in co.installments] == [i.due for i in fed.installments]
    assert any(
        "co: Estimated: the state's own installment rules" in n for n in co.notes
    )


@pytest.mark.engine
def test_a_state_without_income_tax_has_no_state_installments(
    lay: Layout,  # noqa: F811
) -> None:
    enter(lay, 2026, "state", "TX")
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    et = esttax.estimate(lay, 2026, date(2026, 7, 10))
    assert [a.name for a in et.agencies] == ["fed"]
