"""Unit 3e-4a: Form 8582, passive activity loss limitations, Parts I-VIII,
on synthetic activities. Each case is checked against the 2025 form and its
instructions by hand."""

from __future__ import annotations

import pytest

from planner.taxprep import f8582
from planner.taxprep.f8582 import Activity


def _lines(r: f8582.Result) -> dict[str, float]:
    return {ln: round(v, 2) for ln, _, v, _ in r.lines}


def _rent(name: str, **kw: float) -> Activity:
    return Activity(name, "Sch E, line 22", active=True, **kw)


def _pship(name: str, **kw: float) -> Activity:
    return Activity(name, "Sch E, line 28A", **kw)  # type: ignore[arg-type]


def test_line3_income_allows_everything() -> None:
    r = f8582.compute(
        [_rent("rental A", income=5000), _pship("partnership A", loss=3000)],
        magi=80_000,
        separate=None,
    )
    got = _lines(r)
    assert (got["1a"], got["1d"], got["2b"], got["2d"], got["3"]) == (
        5000.0,
        5000.0,
        -3000.0,
        -3000.0,
        2000.0,
    )
    assert "4" not in got and "11" not in got
    assert r.allowed == {"rental A": 0.0, "partnership A": 3000.0}
    assert not any(r.unallowed.values())


def test_special_allowance_in_full() -> None:
    r = f8582.compute([_rent("rental A", loss=10000)], magi=50_000, separate=None)
    got = _lines(r)
    assert [got[k] for k in ("1b", "1d", "3", "4", "5", "6", "7", "8", "9")] == [
        -10000.0,
        -10000.0,
        -10000.0,
        10000.0,
        150000.0,
        50000.0,
        100000.0,
        25000.0,
        10000.0,
    ]
    assert (got["10"], got["11"]) == (0.0, 10000.0)
    assert r.allowed == {"rental A": 10000.0}


def test_phase_out_leaves_an_unallowed_loss() -> None:
    r = f8582.compute(
        [_rent("rental A", loss=14000), _rent("rental B", income=4000)],
        magi=140_000,
        separate=None,
    )
    got = _lines(r)
    assert (got["1a"], got["1d"], got["4"], got["7"], got["8"], got["9"]) == (
        4000.0,
        -10000.0,
        10000.0,
        10000.0,
        5000.0,
        5000.0,
    )
    assert (got["10"], got["11"]) == (4000.0, 9000.0)
    assert r.allowed["rental A"] == pytest.approx(9000.0)
    assert r.unallowed["rental A"] == pytest.approx(5000.0)
    assert any("5,000.00 of passive loss is not allowed" in n for n in r.notes)


def test_allowance_then_pro_rata_unallowed() -> None:
    r = f8582.compute(
        [
            _rent("rental A", loss=6000),
            _pship("partnership A", loss=4000),
            _pship("partnership B", income=2000),
        ],
        magi=146_000,
        separate=None,
    )
    got = _lines(r)
    assert (got["1d"], got["2a"], got["2d"], got["3"]) == (
        -6000.0,
        2000.0,
        -2000.0,
        -8000.0,
    )
    assert (got["4"], got["8"], got["9"], got["10"], got["11"]) == (
        6000.0,
        2000.0,
        2000.0,
        2000.0,
        4000.0,
    )
    # Part VI: the 2,000 allowance all to rental A; Part VII: 6,000 unallowed,
    # half each on rental A's remaining 4,000 and partnership A's 4,000.
    assert (got["VI-1(c)"], got["VI-1(d)"]) == (2000.0, 4000.0)
    assert (got["VII-1(b)"], got["VII-2(b)"]) == (0.5, 0.5)
    assert (got["VII-1(c)"], got["VII-2(c)"]) == (3000.0, 3000.0)
    assert r.allowed == pytest.approx(
        {"rental A": 3000.0, "partnership A": 1000.0, "partnership B": 0.0}
    )
    assert (got["VIII-1(a)"], got["VIII-1(c)"]) == (6000.0, 3000.0)
    assert sum(r.allowed.values()) == pytest.approx(got["11"])


def test_rental_allowance_counts_prior_year_losses() -> None:
    r = f8582.compute(
        [
            _rent("rental A", loss=6000, prior=2000),
            _pship("partnership A", loss=4000),
            _pship("partnership B", income=1000),
        ],
        magi=120_000,
        separate=None,
    )
    got = _lines(r)
    assert (got["1b"], got["1c"], got["1d"], got["3"]) == (
        -6000.0,
        -2000.0,
        -8000.0,
        -11000.0,
    )
    assert (got["4"], got["8"], got["9"], got["11"]) == (
        8000.0,
        15000.0,
        8000.0,
        9000.0,
    )
    assert r.allowed == pytest.approx(
        {"rental A": 8000.0, "partnership A": 1000.0, "partnership B": 0.0}
    )
    assert r.unallowed["partnership A"] == pytest.approx(3000.0)


def test_line_1d_gain_skips_part_ii() -> None:
    r = f8582.compute(
        [_rent("rental A", income=3000, prior=0), _pship("partnership A", loss=5000)],
        magi=40_000,
        separate=None,
    )
    got = _lines(r)
    assert (got["1d"], got["3"]) == (3000.0, -2000.0)
    assert "4" not in got and "9" not in got
    assert (got["10"], got["11"]) == (3000.0, 3000.0)
    assert r.allowed["partnership A"] == pytest.approx(3000.0)
    assert r.unallowed["partnership A"] == pytest.approx(2000.0)


def test_prior_loss_of_an_activity_with_overall_gain_is_allowed() -> None:
    r = f8582.compute(
        [
            _rent("rental A", income=5000, prior=2000),
            _pship("partnership A", loss=10000),
        ],
        magi=40_000,
        separate=None,
    )
    assert r.allowed == pytest.approx({"rental A": 2000.0, "partnership A": 3000.0})
    assert r.unallowed["partnership A"] == pytest.approx(7000.0)


def test_magi_at_line_5_gives_no_allowance() -> None:
    r = f8582.compute([_rent("rental A", loss=5000)], magi=160_000, separate=None)
    got = _lines(r)
    assert (got["6"], got["9"], got["11"]) == (160000.0, 0.0, 0.0)
    assert "7" not in got and "8" not in got
    assert r.unallowed == {"rental A": 5000.0}


def test_separate_lived_apart() -> None:
    r = f8582.compute([_rent("rental A", loss=20000)], magi=60_000, separate="apart")
    got = _lines(r)
    assert (got["5"], got["7"], got["8"], got["9"]) == (
        75000.0,
        15000.0,
        7500.0,
        7500.0,
    )


def test_separate_lived_together_uses_part_v() -> None:
    r = f8582.compute([_rent("rental A", loss=5000)], magi=30_000, separate="together")
    got = _lines(r)
    assert "1b" not in got and got["2b"] == -5000.0
    assert "4" not in got and got["11"] == 0.0
    assert r.unallowed == {"rental A": 5000.0}
    assert any("Part IV" in n for n in r.notes)


def test_negative_magi_is_zero_and_no_activities() -> None:
    r = f8582.compute([_rent("rental A", loss=1000)], magi=-5000, separate=None)
    assert _lines(r)["6"] == 0.0
    empty = f8582.compute([], magi=0, separate=None)
    assert empty.lines == [] and empty.allowed == {}
