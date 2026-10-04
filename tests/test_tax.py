"""Reference cases, each hand-worked from the 2026 figures in config/thresholds.yaml
(Rev. Proc. 2025-32; NC G.S. 105-153.7 rate 3.99%, NC std deduction 12,750).
The engine must land within $1 of every figure."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from planner.engine.household import Household
from planner.engine.tax import (
    compute,
    compute_sweep,
    engine_slcsp,
    repayment_cap,
    thresholds,
    values,
)
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


# --- SE health insurance deduction and the premium tax credit (IRS Pub. 974) ---
#
# 2026, single, North Carolina, 60,000 of self-employment profit, a marketplace
# plan costing 6,000 for the year against a 9,600 benchmark (800 a month).
# Hand-worked inputs: SE tax 60,000 x 92.35% x 15.3% = 8,477.73, half 4,238.87;
# the 2025 poverty line for one person is 15,650 (Form 8962 reads the year
# before), so 300%-400% is 46,950-62,600 MAGI, where the applicable percentage
# is a flat 9.96% (Rev. Proc. 2025-25). Inside that band the credit is
# 9,600 - 9.96% x MAGI and MAGI = 55,761.14 - deduction.
SEHI = {
    **BASE,
    "se_income": 60_000,
    "se_health_premiums": 6_000,
    "slcsp_monthly": 800,
}
SLCSP_YEAR, APPLICABLE = 9_600.0, 0.0996
SE_AGI = 60_000 - 60_000 * 0.9235 * 0.153 / 2  # 55,761.14: MAGI before the deduction


def _credit_2026(magi: float) -> float:
    return SLCSP_YEAR - APPLICABLE * magi


def _credit_2025(magi: float) -> float:
    # the enhanced schedule still in force for 2025 (IRC 36B(b)(3)(A)(iii)): 6.0%
    # at 300% of the poverty line rising evenly to 8.5% at 400%; Form 8962 line 5
    # is a whole percent of the 2024 line (15,060)
    percent = math.floor(100 * magi / 15_060)
    return SLCSP_YEAR - (0.06 + 0.025 * (percent - 300) / 100) * magi


def _pub974_iteration(premiums: float, credit: Any) -> list[tuple[float, float]]:
    """Pub. 974 iterative method, one (deduction, credit) pair per round: price
    the credit on the income the deduction leaves (never more than the premiums),
    take it off the premiums for the next deduction, and stop when neither
    moves by $1."""
    deduction, last, rounds = premiums, 0.0, []
    while True:
        ptc = min(credit(SE_AGI - deduction), premiums)
        rounds.append((deduction, ptc))
        nxt = premiums - ptc
        if abs(nxt - deduction) < 1 and abs(ptc - last) < 1:
            return rounds
        deduction, last = nxt, ptc


def test_se_health_ptc_iteration_pub974() -> None:
    # round 1: deduction 6,000 -> MAGI 49,761.14 -> credit 4,643.79
    # round 2: 1,356.21 -> 54,404.93 -> 4,181.27
    # round 3: 1,818.73 -> 53,942.40 -> 4,227.34;  round 4: 1,772.66 -> 4,222.75
    # round 5: 1,777.25 -> 4,223.21; the next deduction, 6,000 - 4,223.21 =
    #   1,776.79, is 0.46 away and the credit moved 0.05: stop.
    # fixed point: deduction = (6,000 - 9,600 + 9.96% x 55,761.14) / 1.0996 = 1,776.84
    rounds = _pub974_iteration(6_000, _credit_2026)
    assert len(rounds) == 5 and rounds[0][1] == pytest.approx(4_643.79, abs=0.01)
    # an advance credit of 4,800 was paid (Form 1095-A column C): it is repaid or
    # reconciled against the credit allowed, and does not move the deduction
    r = compute(2026, Household(**SEHI, aptc=4_800))
    assert r.excess_aptc == pytest.approx(576.84, abs=D)
    assert r.se_health_deduction == pytest.approx(1_776.84, abs=D)
    assert r.aca_ptc == pytest.approx(4_223.16, abs=D)
    assert r.se_health_deduction == pytest.approx(rounds[-1][0], abs=D)
    assert r.aca_ptc == pytest.approx(rounds[-1][1], abs=D)
    # the premiums are the deduction plus the credit, and the engine's MAGI
    # carries the settled deduction (not the full premium)
    assert r.se_health_deduction + r.aca_ptc == pytest.approx(6_000, abs=D)
    assert r.aca_magi == pytest.approx(SE_AGI - 1_776.84, abs=D)
    assert r.agi == pytest.approx(r.aca_magi, abs=D)
    assert r.se_health_converged and r.se_health_rounds == len(rounds)


def test_se_health_without_a_credit_is_the_plain_deduction() -> None:
    # 120,000 of profit is far over 400% of the poverty line: no credit, so the
    # whole premium is the deduction (limited by profit less half the SE tax).
    r = compute(2026, Household(**{**SEHI, "se_income": 120_000}))
    assert r.aca_ptc == 0 and r.se_health_converged
    assert r.se_health_deduction == pytest.approx(6_000, abs=D)


def test_se_health_deduction_is_limited_by_profit_less_half_the_se_tax() -> None:
    # Worksheet W: 8,000 of profit, SE tax 8,000 x 92.35% x 15.3% = 1,130.30,
    # half 565.15; the limit is 7,434.85. Under 100% of the poverty line there
    # is no credit, so the 9,000 premium deducts only up to the limit.
    r = compute(
        2026, Household(**{**SEHI, "se_income": 8_000, "se_health_premiums": 9_000})
    )
    assert r.aca_ptc == 0
    assert r.se_health_deduction == pytest.approx(7_434.85, abs=D)
    assert r.agi == pytest.approx(0, abs=D)


def test_se_health_premiums_are_not_deducted_without_se_profit() -> None:
    r = compute(2026, Household(**{**SEHI, "se_income": 0, "wages": 50_000}))
    assert r.se_health_deduction == 0
    assert r.agi == pytest.approx(50_000, abs=D)


def test_se_health_without_premiums_matches_the_old_engine_figures() -> None:
    r = compute(2026, Household(**{**SEHI, "se_health_premiums": 0}))
    assert r.se_health_deduction == 0 and r.se_health_rounds == 0
    assert r.aca_ptc == pytest.approx(SLCSP_YEAR - APPLICABLE * SE_AGI, abs=D)


def test_se_health_at_the_400pct_cliff_has_no_fixed_point() -> None:
    # 77,000 of profit and a 9,000 premium: deducting it all puts MAGI at
    # 62,560 (3.998x the poverty line, credit 3,369), which cuts the deduction
    # to 5,631, which lifts MAGI to 65,929, over the cliff: no credit, so the
    # deduction goes back to 9,000. The rounds alternate and never settle.
    h = Household(**{**SEHI, "se_income": 77_000, "se_health_premiums": 9_000})
    r = compute(2026, h)
    assert not r.se_health_converged
    first_ptc = SLCSP_YEAR - APPLICABLE * (77_000 - 77_000 * 0.9235 * 0.153 / 2 - 9_000)
    assert first_ptc == pytest.approx(3_369.0, abs=1)
    # the two-pass answer: deduction 9,000 - 3,369, over the cliff, no credit
    assert r.se_health_deduction == pytest.approx(9_000 - first_ptc, abs=D)
    assert r.aca_ptc == 0
    assert r.aca_fpl_pct > 400 * 15_650 / 15_960  # over the cliff at the settled MAGI


def test_se_health_step_cycle_inside_the_phase_in_band_is_not_the_cliff(
    planner_home: Path,
) -> None:
    # 2025, 56,200 of profit, a 6,000 premium, a 9,600 benchmark. Form 8962
    # line 5 is a whole percent of the poverty line and the applicable
    # percentage rises with income in this band, so crossing a percent moves the
    # credit by about $13-15: the iteration bounces between two points
    # (deduction 112.35 and 126.38, credit about 5,874 and 5,888), both far
    # under 400% of the poverty line and both with a credit. It is a step in the
    # table, not the cliff: report the lower deduction, with the two figures
    # inside the step, as a settled answer.
    h = Household(
        age=50,
        filing_status="SINGLE",
        state="NC",
        se_income=56_200,
        se_health_premiums=6_000,
        slcsp_monthly=800,
    )
    r = compute(2025, h)
    assert r.aca_ptc > 5_000 and r.aca_fpl_pct < 400
    assert r.se_health_converged
    assert r.se_health_deduction == pytest.approx(119, abs=15)
    assert r.se_health_deduction == pytest.approx(112.35, abs=1)  # the lower one
    assert r.se_health_deduction + r.aca_ptc == pytest.approx(6_000, abs=15)
    lay = Layout(planner_home)
    lay.ensure()
    for key, text in (
        ("birth_date", "1975-06-15"),
        ("filing_status", "single"),
        ("state", "NC"),
        ("wages", "0"),
        ("se_income", "56,200"),
        ("se_health_premiums", "6,000"),
        ("slcsp_monthly", "800"),
    ):
        enter(lay, 2025, key, text)
    d = draft.build(lay, 2025)
    assert not [n for n in d.notes if "400%" in n or "cliff" in n], d.notes
    assert d.get("Sch 1", "17") == pytest.approx(r.se_health_deduction, abs=D)


def test_advance_credit_above_the_credit_is_repaid_in_full_from_2026() -> None:
    # advance 4,800 against an allowed credit of 4,223.16: 576.84 excess; 2026
    # has no repayment cap (P.L. 119-21 sec. 71305). The repayment is tax.
    base = compute(2026, Household(**SEHI))
    r = compute(2026, Household(**SEHI, aptc=4_800))
    assert r.excess_aptc == pytest.approx(576.84, abs=D)
    assert r.aptc_repayment == pytest.approx(576.84, abs=D) and r.net_ptc == 0
    assert r.se_health_deduction == pytest.approx(base.se_health_deduction, abs=D)
    assert r.fed_total_tax == pytest.approx(base.fed_total_tax + 576.84, abs=D)


def test_advance_credit_below_the_credit_is_a_net_credit() -> None:
    r = compute(2026, Household(**SEHI, aptc=3_600))
    assert r.net_ptc == pytest.approx(4_223.16 - 3_600, abs=D)
    assert r.excess_aptc == 0 and r.aptc_repayment == 0


def test_repayment_cap_table() -> None:
    assert repayment_cap(2025, "SINGLE", 150) == 375.0
    assert repayment_cap(2025, "SINGLE", 299) == 975.0
    assert repayment_cap(2025, "SINGLE", 399) == 1_625.0
    assert repayment_cap(2025, "JOINT", 350) == 3_250.0
    assert repayment_cap(2025, "SINGLE", 400) is None
    assert repayment_cap(2026, "SINGLE", 150) is None  # P.L. 119-21 sec. 71305
    assert repayment_cap(2024, "SINGLE", 150) is None  # not tabled: repay all


def test_2025_repayment_is_capped_below_400pct() -> None:
    # 2025 (enhanced credit still in force). The rounds, MAGI 55,761.14 less the
    # deduction: 6,000 -> credit capped at the 6,000 premiums -> 0 -> credit
    # 5,278.51 -> 721.49 -> 5,403.23 -> 596.77 -> 5,379.93 -> 620.07 -> 5,381.71
    # -> 618.29, credit 5,381.57 (366% of the poverty line: 7.65%). An advance of
    # 12,000 is 6,618.43 over; under 400% the repayment is capped at 1,625.
    rounds = _pub974_iteration(6_000, _credit_2025)
    r = compute(2025, Household(**SEHI, aptc=12_000))
    assert r.se_health_deduction == pytest.approx(618.29, abs=D)
    assert r.se_health_deduction == pytest.approx(rounds[-1][0], abs=D)
    assert r.aca_ptc == pytest.approx(5_381.57, abs=D)
    assert r.excess_aptc == pytest.approx(6_618.43, abs=D)
    assert r.aptc_repayment == 1_625.0
    assert r.fed_total_tax - compute(2025, Household(**SEHI)).fed_total_tax == (
        pytest.approx(1_625.0, abs=D)
    )


def test_values_and_sweep_use_the_settled_deduction() -> None:
    h = Household(**SEHI)
    got = values(2026, h, ("self_employed_health_insurance_ald", "aca_magi"))
    assert got["self_employed_health_insurance_ald"] == pytest.approx(1_776.84, abs=D)
    assert got["aca_magi"] == pytest.approx(SE_AGI - 1_776.84, abs=D)
    rows = compute_sweep(2026, h, "self_employment_income", 40_000, 60_000, 10_000)
    assert [row["self_employment_income"] for row in rows] == [40_000, 50_000, 60_000]
    for row in rows:
        point = compute(
            2026, Household(**{**SEHI, "se_income": int(row["self_employment_income"])})
        )
        assert row["se_health_deduction"] == pytest.approx(
            point.se_health_deduction, abs=D
        )
        assert row["aca_ptc"] == pytest.approx(point.aca_ptc, abs=D)
        assert row["adjusted_gross_income"] == pytest.approx(point.agi, abs=D)
