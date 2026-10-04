"""The real-calculation proof. 2026 single, $50,000 wages, NC:
taxable 50,000 - 16,100 = 33,900; tax = 10% x 12,400 + 12% x 21,500 = 3,820
(Rev. Proc. 2025-32)."""

from __future__ import annotations

import pytest

from planner.engine.selfcheck import format_years, parse_years, run


@pytest.mark.engine
def test_selfcheck_matches_hand_worked_2026_figure() -> None:
    r = run(2026, 50_000)
    assert r.income_tax == pytest.approx(3820.0, abs=1.0)
    assert r.engine_version


def test_years_format_and_parse_round_trip() -> None:
    years = (2015, 2018, 2019, 2020, 2026)
    assert format_years(years) == "2015, 2018-2020, 2026"
    assert parse_years("2015, 2018-2020, 2026") == years
    assert parse_years("") == () and format_years(()) == "none"


@pytest.mark.engine
def test_selfcheck_reports_the_years_the_engine_publishes() -> None:
    from planner.engine.tax import published_years

    years = published_years()
    assert 2025 in years and 2026 in years and list(years) == sorted(years)
    assert run(2026, 50_000).years == years
