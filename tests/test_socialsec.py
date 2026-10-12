"""Unit 3f-4: Social Security claiming for the household. Each person's own
benefit at the claim age, a spouse's benefit on the other's record, what a
survivor keeps, and this year's earnings test, pinned to the worked examples
in 20 CFR 404.313 and 404.410 and the tables in 404.409. Synthetic people only."""

from __future__ import annotations

import pytest

from planner.engine.tax import r
from planner.ingest.needs import enter, needed
from planner.paths import Layout
from planner.plan import glidepath
from planner.plan import socialsec as ss
from tests.test_spending import AS_OF, lay  # noqa: F401

Y = 2026


def test_cfr_worked_examples() -> None:
    # 404.410(a): PIA 980.50, full retirement age 65 and 8 months, claimed at
    # 62 (44 months early): the reduction is 228.78 (SSA then rounds to 228.80)
    assert ss.fra_months("1941-06-15", Y) == 788
    assert r(980.50 * (1 - ss.own_factor("1941-06-15", 62, Y))) == 228.78
    # 404.410(b): an unreduced wife's benefit of 412.40 at 63, full retirement
    # age 65 and 4 months (28 months early): the reduction is 80.18 + rounding
    assert ss.fra_months("1939-06-15", Y) == 784
    assert r(412.40 * (1 - ss.spouse_factor("1939-06-15", 63, Y))) == 80.19
    # 404.410(c)(1): an unreduced widow's benefit of 785.70 at 64, survivor full
    # retirement age 65 and 4 months: 16 x .285 / 64 months, 55.98
    assert ss.survivor_fra_months("1941-06-15") == 784
    assert r(785.70 * (1 - ss.survivor_factor("1941-06-15", 64))) == 55.98
    # 404.313(b): born 1/15/1933, 12 credits at 11/24 of 1%: 5.5% of 782.60
    assert ss.fra_months("1933-01-15", Y) == 780
    assert r(782.60 * (ss.own_factor("1933-01-15", 66, Y) - 1)) == 43.04


def test_full_retirement_age_tables() -> None:
    # 404.409(a) through the engine; a January 1 birth reads as the year before
    for birth, months in (
        ("1937-12-31", 780),
        ("1938-01-01", 780),
        ("1938-01-02", 782),
        ("1950-07-04", 792),
        ("1957-03-03", 798),
        ("1960-01-01", 802),
        ("1960-01-02", 804),
    ):
        assert ss.fra_months(birth, Y) == months, birth
    # 404.409(b): the widow(er)'s table runs two years behind
    for birth, months in (
        ("1939-12-31", 780),
        ("1940-01-02", 782),
        ("1944-06-01", 790),
        ("1950-06-01", 792),
        ("1959-06-01", 798),
        ("1962-01-01", 802),
        ("1962-01-02", 804),
    ):
        assert ss.survivor_fra_months(birth) == months, birth
    # the standard factors at full retirement age 67
    assert round(ss.own_factor("1970-05-05", 62, Y), 6) == 0.7
    assert round(ss.own_factor("1970-05-05", 70, Y), 6) == 1.24
    assert round(ss.spouse_factor("1970-05-05", 62, Y), 6) == 0.65
    assert round(ss.survivor_factor("1970-05-05", 60), 6) == 0.715
    assert ss.spouse_factor("1970-05-05", 68, Y) == 1.0  # no delayed credits


def test_pia_from_the_statement() -> None:
    # full retirement age 66 and 6 months: the age-67 figure has 6 credits
    pia, origin = ss.pia("1957-03-03", {67: 3120.0}, Y)
    assert r(pia) == 3000.0 and "less its delayed credits" in origin
    pia, origin = ss.pia("1970-05-05", {62: 2100.0}, Y)
    assert r(pia) == 3000.0 and "grossed up" in origin
    pia, _ = ss.pia("1970-05-05", {70: 3720.0}, Y)
    assert r(pia) == 3000.0
    assert ss.pia("1970-05-05", {}, Y) == (0.0, "no SSA estimate")
    # the statement's own figure wins at its age; other ages from the PIA
    p = ss.person("you", "1970-05-05", 62, {62: 2050.0, 67: 3000.0}, Y)
    assert p.own == 2050.0 and p.note is None
    p = ss.person("you", "1970-05-05", 64, {67: 3000.0}, Y)
    assert r(p.own) == 2400.0 and "age-64 benefit 2,400.00" in str(p.note)


def _couple(head_claim: int = 67) -> tuple[ss.Person, ss.Person]:
    head = ss.person("you", "1964-04-20", head_claim, {67: 3000.0}, Y)
    spouse = ss.person("spouse", "1966-09-12", 62, {67: 1000.0}, Y)
    return head, spouse


def test_spousal_benefit_starts_with_the_later_claim() -> None:
    head, spouse = _couple()
    extra, start = ss.spousal(spouse, head, Y)
    # half of 3,000 less the spouse's own 1,000 = 500; the head files in 2031,
    # when the spouse is 65 (24 months early at 25/36 of 1%)
    assert start == 65 and r(extra) == r(500 * (1 - 24 * 25 / 36 / 100))
    assert ss.spousal(head, spouse, Y) == (0.0, None)  # no excess the other way
    sched = ss.schedule(head, spouse, Y, 95)
    assert sched[62] == 0.0
    assert r(sched[64]) == 8400.0  # the spouse's own 700 a month from 62
    assert r(sched[67]) == r(36000 + 8400 + 12 * extra)
    unclaimed = ss.person("spouse", "1966-09-12", None, {67: 1000.0}, Y)
    assert ss.spousal(unclaimed, head, Y) == (0.0, None)
    assert r(ss.schedule(head, unclaimed, Y, 95)[67]) == 36000.0


def test_survivor_keeps_the_larger_check() -> None:
    head, spouse = _couple(head_claim=62)  # 2,100: under 82.5% of the PIA
    assert r(ss.survivor(spouse, head)) == 2475.0
    assert ss.survivor(spouse, head, 60) == pytest.approx(2475 * 0.715, abs=0.01)
    head, spouse = _couple(head_claim=70)  # delayed credits pass to the survivor
    assert r(ss.survivor(spouse, head)) == 3720.0
    assert ss.survivor(head, spouse) == head.own  # own check is larger


def test_earnings_test_this_year() -> None:
    # under full retirement age all year: $1 of $2 over 24,480 (2026)
    p = ss.person("you", "1962-08-10", 62, {62: 1800.0}, Y)
    et = ss.earnings_test(p, p.own, 40_000.0, Y)
    assert et is not None and et.withheld == 7760.0 and et.paid_before == 21600.0
    assert "404.430" in et.note and "404.412" in et.note
    assert ss.earnings_test(p, p.own, 20_000.0, Y).withheld == 0.0  # type: ignore[union-attr]
    # the year of full retirement age (March 2026): Jan and Feb count, $1 of $3
    # over 65,160, the year's earnings spread evenly over the months
    p = ss.person("you", "1959-05-10", 64, {67: 2000.0}, Y)
    et = ss.earnings_test(p, 2000.0, 420_000.0, Y)
    assert et is not None and et.paid_before == 4000.0 and et.withheld == 1613.33
    assert ss.earnings_test(p, 2000.0, 9_000_000.0, Y).withheld == 4000.0  # type: ignore[union-attr]
    # not reached: past full retirement age, not yet claimed, never claimed
    assert (
        ss.earnings_test(ss.person("you", "1955-05-10", 62, {62: 1.0}, Y), 1.0, 1e6, Y)
        is None
    )
    assert (
        ss.earnings_test(ss.person("you", "1966-05-10", 62, {62: 1.0}, Y), 1.0, 1e6, Y)
        is None
    )
    assert (
        ss.earnings_test(ss.person("you", "1962-05-10", None, {}, Y), 0.0, 1e6, Y)
        is None
    )


def test_spouse_records_are_asked_of_a_joint_return(lay: Layout) -> None:  # noqa: F811
    keys = {s.need.key for s in needed(lay, Y).items}
    assert "ss_claim_age" in keys and "spouse_ss_claim_age" not in keys
    enter(lay, Y, "filing_status", "married_joint")
    enter(lay, Y, "spouse_birth_date", "1973-02-14")
    by = {s.need.key: s for s in needed(lay, Y).items}
    for key in ("spouse_ss_claim_age", "spouse_ss_estimate_67"):
        assert by[key].state == "missing" and by[key].need.owner == "spouse"
    assert (
        by["spouse_ss_estimate_67"].need.label == "Spouse's SS monthly estimate at 67"
    )
    with pytest.raises(ValueError, match="between 62 and 70"):
        enter(lay, Y, "spouse_ss_claim_age", "61")


def test_glide_carries_both_records(lay: Layout) -> None:  # noqa: F811
    """The synthetic head (born 1971-06-15, 67 at 2,500) plus a spouse born
    1973-02-14 with a 900 PIA claiming at 64."""
    enter(lay, Y, "filing_status", "married_joint")
    enter(lay, Y, "spouse_birth_date", "1973-02-14")
    g = glidepath.glide(lay, Y, AS_OF, balance=1_500_000.0)
    assert any("spouse_ss_claim_age not set" in n for n in g.notes)
    enter(lay, Y, "spouse_ss_estimate_67", "900")
    enter(lay, Y, "spouse_ss_claim_age", "64")
    g = glidepath.glide(lay, Y, AS_OF, balance=1_500_000.0)
    own = 900 * 0.8  # 36 months early
    extra = (1250 - 900) * (1 - 24 * 25 / 36 / 100)  # from 65, when the head files
    by_age = {row.age: row.ss for row in g.rows}
    assert by_age[66] == r(own * 12)  # the spouse is 64 in 2037
    assert by_age[67] == r(30_000 + own * 12 + extra * 12)
    assert any("spouse: spouse's benefit on the other's record" in n for n in g.notes)
    assert any(n.startswith("survivor: if you die, the spouse keep") for n in g.notes)


def test_glide_figures_a_claim_age_off_the_statement(lay: Layout) -> None:  # noqa: F811
    enter(lay, Y, "ss_claim_age", "70")
    g = glidepath.glide(lay, Y, AS_OF, balance=300_000.0)
    assert any("the age-70 benefit 3,100.00/month" in n for n in g.notes)
    assert next(rw for rw in g.rows if rw.age == 70).ss == 37_200.0
