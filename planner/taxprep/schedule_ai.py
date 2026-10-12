"""Unit 3g-2: Form 2210 Schedule AI, the annualized income installment method
(2025 Form 2210 page 3 and its instructions).

Each period runs from January 1 to the end of March, May and August, then the
whole year. Its income is multiplied up to a year (line 2: 4, 2.4, 1.5 and 1),
the tax on that is figured as on the return (lines 3-19; the engine prices
the annualized household, so the standard deduction, self-employment tax,
other taxes and credits come out as the return figures them), and the
installment is the applicable percentage of it (line 20: 22.5, 45, 67.5 and
90%) less the earlier installments, never more than the regular installment
plus what the earlier columns left unused (lines 21-27).

The typed input is each income line's amount from January 1 through the end of
March, May and August; a line not typed is taken as received evenly through
the year, so annualized it is the year's figure."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from planner.engine.household import Household
from planner.engine.tax import r

KEY = "income_by_period"
FACTORS = (4.0, 2.4, 1.5, 1.0)  # lines 2 and 5
PERCENTS = (0.225, 0.45, 0.675, 0.90)  # line 20
# The Household income lines a period can carry; the ones that may be a loss
# can be typed negative.
FIELDS = (
    "wages",
    "se_income",
    "interest",
    "non_qualified_dividends",
    "qualified_dividends",
    "short_term_gains",
    "long_term_gains",
    "ira_distributions",
    "roth_conversion",
    "pension_income",
    "unemployment",
    "other_income",
    "stock_option_income",
    "cancelled_debt",
    "rental_income",
    "partnership_income",
    "s_corp_income",
    "farm_income",
)
SIGNED = {
    "se_income",
    "short_term_gains",
    "long_term_gains",
    "rental_income",
    "partnership_income",
    "s_corp_income",
    "farm_income",
}
MONEY_MAX = 100_000_000
EXAMPLE = "se_income 0 4000 20000; long_term_gains 0 0 15000"


def parse(key: str, s: str) -> dict[str, list[float]]:
    """Each typed line as its three amounts (through March, May and August),
    or {} for none."""
    if s.strip().lower() == "none":
        return {}
    out: dict[str, list[float]] = {}
    for part in (p.strip() for p in s.split(";")):
        tokens = part.split()
        if len(tokens) != 4:
            raise ValueError(
                f"{key}: each line is an income line and its amounts from January 1 "
                f"through March, May and August (like {EXAMPLE}), or none"
            )
        name = tokens[0]
        if name not in FIELDS:
            raise ValueError(
                f"{key}: {name!r} is not an income line ({', '.join(FIELDS)})"
            )
        if name in out:
            raise ValueError(f"{key}: {name} is typed twice")
        amounts = []
        for t in tokens[1:]:
            try:
                v = float(t.replace(",", "").replace("$", ""))
            except ValueError:
                raise ValueError(f"{key}: {t!r} is not a number") from None
            if v < 0 and name not in SIGNED:
                raise ValueError(f"{key}: {name} is not negative, got {t}")
            if abs(v) > MONEY_MAX:
                raise ValueError(f"{key}: {t} is out of range")
            amounts.append(round(v, 2))
        out[name] = amounts
    return out


def households(plain: Household, typed: dict[str, list[float]]) -> list[Household]:
    """Periods (a)-(c) as annualized households (line 3): ``plain`` (the year
    without the planned year-end items) with each typed line its amount
    through the period times the annualization amount."""
    out = []
    for i in range(3):
        values: dict[str, Any] = {
            name: int(round(v[i] * FACTORS[i])) for name, v in typed.items()
        }
        out.append(replace(plain, **values))
    return out


def installments(tax: list[float], annual: float) -> list[float]:
    """Lines 20-27 from line 19 of each column and Form 2210 line 9: the
    required installments for Part III line 10."""
    out: list[float] = []
    prev26 = prev27 = 0.0
    for line19, pct in zip(tax, PERCENTS, strict=True):
        line23 = max(line19 * pct - sum(out), 0.0)
        line26 = 0.25 * annual + (prev26 - prev27)  # line 25: 0 in column (a)
        line27 = min(line23, line26)
        out.append(r(line27))
        prev26, prev27 = line26, line27
    return out
