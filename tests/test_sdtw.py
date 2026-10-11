"""The Schedule D Tax Worksheet (2025 Schedule D instructions) in place of the
engine's own 28% rate and unrecaptured section 1250 gain path. Each figure is
hand-worked from the worksheet's 47 lines with the 2025 single brackets
(11,925 / 48,475 / 103,350 / 197,300 / 250,525), the 0% and 15% capital gain
tops (48,350 / 533,400) and the 15,750 standard deduction; joint doubles the
brackets to 394,600 and the deduction to 31,500."""

from __future__ import annotations

from typing import Any

import pytest

from planner.engine.household import Household, Person
from planner.engine.tax import (
    SDTW_VARS,
    line_16,
    preferential,
    table_tax,
    values,
)

pytestmark = pytest.mark.engine
NAMES = (
    "taxable_income",
    "income_tax_before_credits",
    "income_tax_main_rates",
    "capital_gains_tax",
    "alternative_minimum_tax",
    "adjusted_net_capital_gain",
    *SDTW_VARS,
)


def _v(wages: int, gains: int, rate28: float = 0, s1250: float = 0, **kw: Any) -> Any:
    inputs = {}
    if rate28:
        inputs["capital_gains_28_percent_rate_gain"] = rate28
    if s1250:
        inputs["unrecaptured_section_1250_gain"] = s1250
    h = Household(
        age=40,
        filing_status=kw.pop("filing_status", "SINGLE"),
        state="NC",
        wages=wages,
        long_term_gains=gains,
        tax_unit_inputs=inputs,
        **kw,
    )
    return values(2025, h, NAMES)


def test_collectibles_in_the_12pct_bracket_are_taxed_at_12pct_not_twice() -> None:
    # Line 21 = max(24,250, min(34,250, 34,250)) = 34,250: every dollar sits
    # in line 21, line 42 is 0, and line 45 = line 46 = tax on 34,250. The
    # engine's own path charged 3,871.50 plus 28% of the 10,000 again.
    v = _v(40_000, 10_000, rate28=10_000)
    assert v["taxable_income"] == 34_250
    assert v["income_tax_before_credits"] == pytest.approx(3_871.50, abs=0.01)
    assert v["capital_gains_tax"] == 0
    assert v["alternative_minimum_tax"] == 0


def test_high_income_bands_15_25_and_28() -> None:
    # 1 = 334,250; 10 = 50,000; 13 = 15,000; 21 = 284,250; 30 = 15,000 at 15%;
    # 39 = 15,000 at 25%; 42 = 20,000 at 28%; 44 = tax on 284,250 = 69,034.75.
    v = _v(300_000, 50_000, rate28=20_000, s1250=15_000)
    assert v["income_tax_main_rates"] == pytest.approx(69_034.75, abs=0.01)
    assert v["capital_gains_tax"] == pytest.approx(2_250 + 3_750 + 5_600, abs=0.01)
    assert v["income_tax_before_credits"] == pytest.approx(80_634.75, abs=0.01)


def test_below_the_24pct_top_the_rate_gains_stay_at_ordinary_rates() -> None:
    # 1 = 134,250; 13 = 10,000; 14 = 124,250; 19 = 134,250 (under 197,300);
    # 21 = 124,250; 30 = 10,000 at 15%; 38 = 30,000 + 124,250 - 134,250 =
    # 20,000 > 35, so 39 = 0; 42 = 0. 44 = 22,667; 45 = 24,167 < 46 = 25,067.
    v = _v(120_000, 30_000, rate28=10_000, s1250=10_000)
    assert v["income_tax_before_credits"] == pytest.approx(24_167.00, abs=0.01)
    assert v["capital_gains_excluded_from_taxable_income"] == 10_000


def test_the_band_above_line_19_is_taxed_at_28pct() -> None:
    # 1 = 214,250; 19 = 197,300 (the 24% top); 21 = 197,300; 42 = 16,950 at
    # 28% = 4,746; 44 = 40,199; 45 = 44,945 < 46 = 45,623 (32% on 16,950).
    v = _v(200_000, 30_000, rate28=30_000)
    assert v["income_tax_before_credits"] == pytest.approx(44_945.00, abs=0.01)


def test_joint_line_19_is_the_joint_24pct_top() -> None:
    # 1 = 108,500; 19 = 108,500 (under 394,600); 21 = 108,500, so all of it is
    # ordinary: 2,385 + 8,772 + 22% of 11,550 = 13,698.
    v = _v(120_000, 20_000, s1250=20_000, filing_status="JOINT", spouse=Person(age=40))
    assert v["taxable_income"] == 108_500
    assert v["income_tax_before_credits"] == pytest.approx(13_698.00, abs=0.01)


def test_without_rate_gains_the_engine_path_is_unchanged() -> None:
    v = _v(40_000, 10_000)
    assert v["income_tax_before_credits"] == pytest.approx(2_671.50, abs=0.01)
    assert preferential(v) == v["adjusted_net_capital_gain"]


def test_preferential_is_line_1_less_line_21_with_rate_gains() -> None:
    v = _v(120_000, 30_000, rate28=10_000, s1250=10_000)
    assert preferential(v) == 10_000 == v["taxable_income"] - 124_250


def test_line_16_puts_line_21_through_the_tax_table() -> None:
    # Line 21 is all of taxable income, so line 44 is the Tax Table's row for
    # 34,250 (34,250-34,300: 1,192.50 + 12% of 22,350 = 3,874.50, rounded).
    v = _v(40_000, 10_000, rate28=10_000)
    regular = v["income_tax_before_credits"] - v["alternative_minimum_tax"]
    line, gap = line_16(regular, v["taxable_income"], preferential(v), 2025, "SINGLE")
    assert line == table_tax(34_250, 2025, "SINGLE") == 3_875
    assert gap == pytest.approx(3.50, abs=0.01)
