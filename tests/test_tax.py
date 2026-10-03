"""Reference cases, each hand-worked from the 2026 figures in config/thresholds.yaml
(Rev. Proc. 2025-32; NC G.S. 105-153.7 rate 3.99%, NC std deduction 12,750).
The engine must land within $1 of every figure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from planner.engine.household import Household
from planner.engine.tax import compute, compute_sweep, engine_slcsp, thresholds
from planner.ingest.needs import enter
from planner.paths import Layout
from planner.plan import inputs
from planner.taxprep import draft

pytestmark = pytest.mark.engine
BASE: dict[str, Any] = {"age": 55, "filing_status": "SINGLE", "state": "NC"}
D = 1.0


def test_thresholds_come_from_the_engine_parameters() -> None:
    th = thresholds(2026, "SINGLE")
    assert th["ltcg_0pct_top"] == 49_450
    assert th["bracket_12pct_top"] == 50_400


def test_zero_income_is_all_zero_with_full_headroom() -> None:
    r = compute(2026, Household(**BASE))
    assert r.fed_total_tax == 0 and r.state_tax == 0 and r.agi == 0
    assert r.room_to_0pct_ltcg == 49_450
    assert r.room_to_12pct_top == 50_400
    assert r.engine_version


def test_roth_conversion_40k_on_small_base() -> None:
    # AGI 44,000; taxable 44,000 - 16,100 = 27,900; 3,000 qualified dividends at 0%;
    # ordinary 24,900 -> 10% x 12,400 + 12% x 12,500 = 2,740.
    # NC: (44,000 - 12,750) x 3.99% = 1,246.88.
    h = Household(
        **BASE, interest=1000, qualified_dividends=3000, roth_conversion=40_000
    )
    r = compute(2026, h)
    assert r.agi == pytest.approx(44_000, abs=D)
    assert r.taxable_income == pytest.approx(27_900, abs=D)
    assert r.fed_income_tax_after_credits == pytest.approx(2_740, abs=D)
    assert r.ltcg_tax == pytest.approx(0, abs=D)
    assert r.qualified_div_and_ltcg_in_taxable == pytest.approx(3_000, abs=D)
    assert r.state_tax == pytest.approx(1_246.88, abs=D)
    assert r.room_to_0pct_ltcg == pytest.approx(21_550, abs=D)
    assert r.medicaid_magi_monthly == pytest.approx(44_000 / 12, abs=D)


def test_ltcg_60k_alone_is_federally_free_but_nc_taxes_it() -> None:
    # taxable 43,900 all preferential, under the 49,450 0% ceiling -> federal 0.
    # NC: (60,000 - 12,750) x 3.99% = 1,885.28.
    r = compute(2026, Household(**BASE, long_term_gains=60_000))
    assert r.fed_income_tax_after_credits == pytest.approx(0, abs=D)
    assert r.ltcg_tax == pytest.approx(0, abs=D)
    assert r.qualified_div_and_ltcg_in_taxable == pytest.approx(43_900, abs=D)
    assert r.state_tax == pytest.approx(1_885.28, abs=D)
    assert r.room_to_0pct_ltcg == pytest.approx(5_550, abs=D)


def test_ltcg_above_the_0pct_ceiling_is_taxed_at_15pct() -> None:
    # taxable 83,900; 49,450 at 0%, 34,450 at 15% = 5,167.50.
    r = compute(2026, Household(**BASE, long_term_gains=100_000))
    assert r.fed_income_tax_after_credits == pytest.approx(5_167.50, abs=D)
    assert r.ltcg_tax == pytest.approx(5_167.50, abs=D)
    assert r.room_to_0pct_ltcg == pytest.approx(-34_450, abs=D)


def test_self_employment_30k() -> None:
    # SE tax: 30,000 x 92.35% x 15.3% = 4,238.87; half (2,119.43) deducted,
    # AGI 27,880.57. QBI: 20% x (30,000 - 2,119.43) = 5,576.11, capped at 20% of
    # taxable income before QBI (27,880.57 - 16,100 = 11,780.57) = 2,356.11;
    # taxable 9,424.46 -> 10% = 942.45.
    r = compute(2026, Household(**BASE, se_income=30_000))
    assert r.se_tax == pytest.approx(4_238.87, abs=D)
    assert r.agi == pytest.approx(27_880.57, abs=D)
    assert r.qbi_deduction == pytest.approx(2_356.11, abs=D)
    assert r.fed_income_tax_after_credits == pytest.approx(942.45, abs=D)
    assert r.fed_total_tax == pytest.approx(5_181.31, abs=D)
    assert r.state_tax == pytest.approx(603.71, abs=D)


def test_sweep_matches_point_computations() -> None:
    h = Household(**BASE, interest=1000, qualified_dividends=3000)
    rows = compute_sweep(2026, h, "taxable_roth_conversions", 0, 40_000, 20_000)
    assert [row["taxable_roth_conversions"] for row in rows] == [0, 20_000, 40_000]
    assert rows[-1]["income_tax"] == pytest.approx(2_740, abs=D)
    assert rows[-1]["state_income_tax"] == pytest.approx(1_246.88, abs=D)
    assert rows[0]["income_tax"] == pytest.approx(0, abs=D)
    with pytest.raises(ValueError):
        compute_sweep(2026, h, "taxable_roth_conversions", 10, 0, 5)


# Average second-lowest-cost silver premium for a 40-year-old, plan year 2025,
# by county: CMS, "Plan Year 2025 Qualified Health Plan Choice and Premiums in
# HealthCare.gov" appendix (2025-qhp-premiums-choice-appendix.xlsx), sheet
# "Avg. SLCSP Prem. 40yo-County", column PY25. CMS publishes these three North
# Carolina counties only.
CMS_PY25_SLCSP_40 = {
    "WAKE_COUNTY_NC": 478.85,
    "MECKLENBURG_COUNTY_NC": 484.17,
    "GUILFORD_COUNTY_NC": 439.91,
}


@pytest.mark.parametrize(("county", "published"), sorted(CMS_PY25_SLCSP_40.items()))
def test_slcsp_for_named_county(county: str, published: float) -> None:
    # The engine prices the benchmark only for a household with income (it is 0
    # at $0 wages); $30,000 of wages is a made-up figure that does not move it.
    h = Household(
        age=40, filing_status="SINGLE", state="NC", county=county, wages=30_000
    )
    assert engine_slcsp(2025, h) == pytest.approx(published, abs=D)


def test_the_county_moves_the_benchmark() -> None:
    # Macon County is in the engine's rating area 1, Wake in 13: same age and
    # year, different benchmark.
    macon = engine_slcsp(
        2026, Household(**BASE, wages=30_000, county="MACON_COUNTY_NC")
    )
    wake = engine_slcsp(2026, Household(**BASE, wages=30_000, county="WAKE_COUNTY_NC"))
    assert macon != wake and min(macon, wake) > 0


@pytest.mark.parametrize(
    ("se", "investment", "taxes"),
    [
        (30_000, 0, ("4",)),  # SE tax only
        (250_000, 0, ("4", "11")),  # SE tax and the 0.9% additional Medicare tax
        (0, 250_000, ("12",)),  # NIIT on investment income, no SE
        (150_000, 250_000, ("4", "12")),  # SE tax and NIIT together
        (12_345, 0, ("4",)),  # low-income SE: the EITC is a refundable credit
    ],
)
def test_fed_total_tax_matches_draft_line_24(
    planner_home: Path, se: int, investment: int, taxes: tuple[str, ...]
) -> None:
    """Form 1040 line 24 is the draft's own line 22 + Schedule 2 line 21. The
    engine's income tax already holds the NIIT; self-employment tax and the
    additional Medicare tax sit outside it, and refundable credits are payments
    (lines 27-31), not a cut in line 24."""
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1971-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "0"),
        ("se_income", str(se)),
        ("interest", "20,000" if investment else "0"),
        ("ordinary_dividends", "30,000" if investment else "0"),
        ("qualified_dividends", "30,000" if investment else "0"),
        ("long_term_gains", "200,000" if investment else "0"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    line_24 = d.get("1040", "24")
    assert line_24 is not None
    for line in taxes:  # the household really owes the tax this case is about
        assert (d.get("Sch 2", line) or 0) > 0, line
    got = compute(2025, inputs.build(lay, 2025).household)
    # the draft rounds each line to cents, the engine only the total: a cent apart
    assert got.fed_total_tax == pytest.approx(line_24, abs=0.015)
    # the credit is real in the low-income case, and line 24 is still gross of it
    assert (got.refundable_credits > 0) == (se == 12_345)


def test_total_tax_fields_are_rounded_like_every_other_field() -> None:
    """Line 22 and line 24 come from engine floats (1744.29296875): both are
    rounded to cents like the rest of the result."""
    r = compute(2025, Household(**{**BASE, "age": 40}, se_income=12_345))
    assert r.fed_total_tax == round(r.fed_total_tax, 2) == 1_744.29
    assert r.fed_income_tax_after_credits == round(r.fed_income_tax_after_credits, 2)
    r = compute(2026, Household(**BASE, long_term_gains=100_000, interest=1_234))
    for v in (r.fed_total_tax, r.fed_income_tax_after_credits):
        assert v == round(v, 2)
