"""Each drafted state's estimated-tax rules (unit 3d-2), from the state's own
2026 instructions: the due dates, the share due by each, the de minimis, the
current-year percentage, the prior-year step for a high AGI. The federal
150,000 AGI line halves for married filing separately (IRC 6654(d)(1)(C)(ii)).
Synthetic figures only."""

from __future__ import annotations

from datetime import date

import pytest

from planner import states
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import calendar, esttax
from tests.test_spending import lay  # noqa: F401

OWN = ("CA", "NY", "PA", "IL", "OH", "GA", "NC", "MI", "NJ", "VA")


def test_the_drafted_states_have_their_own_rules() -> None:
    for code in OWN:
        rule, own = states.rules(code)
        assert own and rule.source, code
    assert states.rules("CO") == (states.FEDERAL, False)


def test_due_dates_are_each_states_own() -> None:
    fed = esttax.due_dates(2026)
    assert fed == [
        date(2026, 4, 15),
        date(2026, 6, 15),
        date(2026, 9, 15),
        date(2027, 1, 15),
    ]
    for code in ("ny", "pa", "il", "oh", "ga", "mi", "nj", "nc"):
        assert esttax.due_dates(2026, code) == fed, code
    # Form 760ES: May 1, then the federal June, September and January dates
    assert esttax.due_dates(2026, "va") == [date(2026, 5, 1), *fed[1:]]
    # Form 540-ES: 30% April, 40% June, nothing in September, 30% January
    assert esttax.due_dates(2026, "ca") == [fed[0], fed[1], fed[3]]
    assert states.rules("CA")[0].installments() == (
        (1, (0, 4, 15), 0.30),
        (2, (0, 6, 15), 0.70),
        (4, (1, 1, 15), 1.00),
    )


@pytest.mark.parametrize(
    ("agency", "owe", "separate", "exempt"),
    [
        ("fed", 999.99, False, True),  # "at least $1,000"
        ("fed", 1_000.0, False, False),
        ("ca", 499.99, False, True),  # "at least $500 ($250 ... separately)"
        ("ca", 300.0, False, True),
        ("ca", 300.0, True, False),
        ("ny", 299.99, False, True),  # "at least $300"
        ("ny", 300.0, False, False),
        ("pa", 429.99, False, True),  # "at least $430"
        ("pa", 430.0, False, False),
        ("il", 1_000.0, False, True),  # "exceed $1,000"
        ("il", 1_000.01, False, False),
        ("oh", 500.0, False, True),  # "more than $500"
        ("mi", 500.0, False, True),
        ("mi", 500.01, False, False),
        ("nj", 400.0, False, True),  # "more than $400"
        ("nj", 400.01, False, False),
        ("va", 1_000.0, False, True),  # "more than $1,000"
        ("va", 1_000.01, False, False),
        ("nc", 999.99, False, True),
    ],
)
def test_de_minimis(agency: str, owe: float, separate: bool, exempt: bool) -> None:
    assert esttax.de_minimis(agency, owe, separate) is exempt


def test_georgia_has_no_tax_amount_test() -> None:
    # Form 500-ES: the test is $1,000 of income not subject to withholding,
    # not a tax figure; any tax left after withholding is treated as due
    assert esttax.de_minimis("ga", 1.0) is False


@pytest.mark.parametrize(
    ("agency", "current", "prior", "prior_agi", "separate", "agi", "want"),
    [
        # the current-year leg: 90%, NJ-2210 80%, Form 500-UET 70%
        ("fed", 10_000.0, 20_000.0, 50_000.0, False, 0.0, 9_000.0),
        ("nj", 10_000.0, 20_000.0, 50_000.0, False, 0.0, 8_000.0),
        ("ga", 10_000.0, 20_000.0, 50_000.0, False, 0.0, 7_000.0),
        ("il", 10_000.0, 20_000.0, 50_000.0, False, 0.0, 9_000.0),
        # the prior-year leg steps to 110% over 150,000 of last year's AGI
        ("fed", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 11_000.0),
        ("ca", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 11_000.0),
        ("ny", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 11_000.0),
        ("mi", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 11_000.0),
        ("il", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 10_000.0),
        ("nj", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 10_000.0),
        ("va", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 10_000.0),
        ("pa", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 10_000.0),
        ("oh", 20_000.0, 10_000.0, 160_000.0, False, 0.0, 10_000.0),
        # ... over 75,000 when married filing separately
        ("fed", 20_000.0, 10_000.0, 80_000.0, True, 0.0, 11_000.0),
        ("fed", 20_000.0, 10_000.0, 80_000.0, False, 0.0, 10_000.0),
        ("ca", 20_000.0, 10_000.0, 80_000.0, True, 0.0, 11_000.0),
        ("ny", 20_000.0, 10_000.0, 80_000.0, True, 0.0, 11_000.0),
        # CA: this year's AGI at 1,000,000 (500,000 separately) closes the
        # prior-year leg
        ("ca", 20_000.0, 10_000.0, 160_000.0, False, 1_000_000.0, 18_000.0),
        ("ca", 20_000.0, 10_000.0, 160_000.0, True, 500_000.0, 18_000.0),
        ("ca", 20_000.0, 10_000.0, 160_000.0, False, 999_999.0, 11_000.0),
        ("fed", 20_000.0, 10_000.0, 160_000.0, False, 1_000_000.0, 11_000.0),
    ],
)
def test_safe_harbor_by_state(
    agency: str,
    current: float,
    prior: float,
    prior_agi: float,
    separate: bool,
    agi: float,
    want: float,
) -> None:
    got, basis = esttax.safe_harbor(
        agency, current, prior, prior_agi, separate=separate, agi=agi
    )
    assert got == want, basis


def test_safe_harbor_names_its_basis() -> None:
    assert esttax.safe_harbor("ga", 10_000.0, None, None)[1] == (
        "70% of this year's projected tax (prior year unknown)"
    )
    assert esttax.safe_harbor("ca", 20_000.0, 1.0, 1.0, agi=2_000_000.0)[1] == (
        "90% of this year's projected tax (this year's AGI is over the line "
        "past which last year's tax is not a safe harbor)"
    )


def test_calendar_skips_an_installment_with_no_share() -> None:
    ds = calendar.deadlines(2026, "CA", {2: ("fed", "ca"), 3: ("fed", "ca")})
    sept = [d.item for d in ds if d.nominal == "2027-09-15"]
    assert sept == ["Q3 estimated payments (federal) if required: required for federal"]
    june = [d.item for d in ds if d.nominal == "2027-06-15"]
    assert june == [
        "Q2 estimated payments (federal and CA) if required: "
        "required for federal and CA"
    ]


def test_calendar_gives_virginia_its_may_date() -> None:
    ds = calendar.deadlines(2026, "VA")
    assert [d.item for d in ds if d.nominal == "2027-05-01"] == [
        "Q1 estimated payments (VA)"
    ]
    april = next(d.item for d in ds if d.nominal == "2027-04-15")
    assert april.startswith("Q1 estimated payments (federal);")


def test_cash_flows_skip_the_empty_installment() -> None:
    ag = esttax.Agency("ca", 10_000.0, None, None, 0.0, "test", 0.0, False)
    et = esttax.EstTax(2026, "2026-12-01", 80_000.0, False, [ag])
    flows, _ = esttax.cash_flows(et)
    nxt = [(f.when, f.amount) for f in flows if f.kind.startswith("next year")]
    # 90% of 10,000 repeated: 30% in April, 40% more in June, none in September
    assert nxt == [("2027-04-15", 2_700.0), ("2027-06-15", 3_600.0)]


@pytest.mark.engine
@pytest.mark.parametrize("code", ["CA", "NY", "VA", "GA"])
def test_a_drafted_state_is_estimated_on_its_own_rules(
    lay: Layout,  # noqa: F811
    code: str,
) -> None:
    enter(lay, 2026, "state", code)
    enter(lay, 2026, "prior_agi", "70,000")
    enter(lay, 2026, "prior_total_tax", "8,000")
    enter(lay, 2026, "prior_state_tax", "2,000")
    enter(lay, 2026, "state_withheld", "0")
    et = esttax.estimate(lay, 2026, date(2026, 3, 1))
    fed, st = et.agencies
    assert st.name == code.lower()
    assert [date.fromisoformat(i.due) for i in st.installments] == esttax.due_dates(
        2026, st.name
    )
    assert not any("own installment rules are not" in n for n in st.notes)
    assert any(f"planner paid --agency {st.name}" in n for n in st.notes)
    for note in states.rules(code)[0].notes:
        assert f"{st.name}: {note}" in st.notes
