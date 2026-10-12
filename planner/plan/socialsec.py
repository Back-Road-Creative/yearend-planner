"""Social Security for the household (unit 3f-4): each person's own benefit at
the planned claim age, a spouse's benefit on the other's record, what a
survivor keeps, and this year's earnings test.

Grounding (20 CFR part 404, read 2026-10-06):

- Full retirement age: 404.409(a) for old-age and spouse's benefits, 404.409(b)
  for a widow(er)'s; age is attained the day before the birthday (404.102), so
  a January 1 birthday reads as the year before. The old-age table and the
  delayed-credit rate come from the engine (``gov.ssa.social_security``).
- Old-age reduction: 5/9 of 1% for each of the first 36 months before full
  retirement age, 5/12 of 1% for each month past 36 (404.410(a); the engine's
  ``early_retirement.reduction_rates``). Delayed credits: 2/3 of 1% a month
  from full retirement age to 70 for births after 1942 (404.313; the engine's
  ``delayed_retirement.credit_rates``, a yearly rate).
- Spouse's benefit: half the worker's primary insurance amount (404.333),
  reduced 25/36 of 1% for each of the first 36 months before the spouse's full
  retirement age and 5/12 of 1% past 36 (404.410(b)); no delayed credits. A
  spouse entitled to their own benefit gets only the excess over it (404.407(a));
  on an early own benefit the excess is reduced at the spouse's rate
  (42 USC 402(q)(3)(B)). Filing for one is filing for both (404.623), so the
  excess starts at the later of the spouse's claim and the worker's.
- Widow(er)'s benefit: the worker's primary insurance amount, or the worker's
  delayed-credit benefit (404.338(a)-(b)); a worker who claimed early leaves
  the larger of that early benefit and 82.5% of the PIA (404.338(c)). Taken
  before the survivor's full retirement age it is reduced by 28.5% times the
  months early over the months from 60 to that age (404.410(c)(1)). The
  survivor keeps the larger of it and their own (404.407).
- Earnings test: in a year before the year of full retirement age, $1 of every
  $2 over the lower exempt amount is withheld; in that year, $1 of every $3 over
  the higher amount, counting only the months before full retirement age
  (404.430(b), 404.415(a)); the engine carries the exempt amounts.

The primary insurance amount is the statement's age-67 figure when full
retirement age is 67, else that figure less its delayed credits (or the age-62
figure grossed up for its reduction, or the age-70 figure less its credits).
Figures are to the cent: SSA rounds each step to the dime and the payment down
to the dollar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from planner.engine.tax import _node, brackets, r

SS = "gov.ssa.social_security"
SPOUSE_RATES = ((36, 25 / 36 / 100), (None, 5 / 12 / 100))  # 404.410(b)
SURVIVOR_MAX_CUT = 0.285  # 404.410(c)(1)
SURVIVOR_FLOOR = 0.825  # 404.338(c)
SPOUSE_SHARE = 0.5  # 404.333
EARLIEST, LATEST = 62, 70  # 404.409(c); delayed credits stop at 70 (404.313(a))
WIDOW_EARLIEST = 60


def _born(birth: date | str) -> date:
    """The birth date as SSA counts it: age is attained the day before the
    birthday (404.102), so a January 1 birth falls in the year before."""
    b = birth if isinstance(birth, date) else date.fromisoformat(str(birth))
    return b - timedelta(days=1)


def fra_months(birth: date | str, year: int) -> int:
    """Full retirement age in months for old-age and spouse's benefits
    (404.409(a); the engine's ``full_retirement_age_by_birth_year``)."""
    table = _node(f"{SS}.full_retirement_age_by_birth_year", year)
    return int(table.calc(_born(birth).year))


def survivor_fra_months(birth: date | str) -> int:
    """Full retirement age in months for widow(er)'s benefits (404.409(b))."""
    y = _born(birth).year
    if y < 1940:
        return 780
    if y < 1945:
        return 780 + 2 * (y - 1939)
    if y < 1957:
        return 792
    if y < 1962:
        return 792 + 2 * (y - 1956)
    return 804


def drc_monthly(birth: date | str, year: int) -> float:
    """Delayed retirement credit per month (404.313(b)(2))."""
    table = _node(
        f"{SS}.retirement_age_adjustment.delayed_retirement.credit_rates", year
    )
    return float(table.calc(_born(birth).year)) / 12


def _stepped(months: int, rates: tuple[tuple[int | None, float], ...]) -> float:
    cut, last = 0.0, 0
    for top, rate in rates:
        span = months - last if top is None else min(months, top) - last
        if span <= 0:
            break
        cut += span * rate
        if top is None:
            break
        last = top
    return cut


def _old_age_rates(year: int) -> tuple[tuple[int | None, float], ...]:
    rows = brackets(
        f"{SS}.retirement_age_adjustment.early_retirement.reduction_rates", year
    )
    tops: list[int | None] = [int(t) for t, _ in rows[1:]] + [None]
    return tuple((top, rate) for top, (_, rate) in zip(tops, rows, strict=True))


def own_factor(birth: date | str, claim_age: int, year: int) -> float:
    """The share of the PIA paid as an own benefit first claimed at
    ``claim_age``: reduced before full retirement age, credited after it."""
    fra = fra_months(birth, year)
    months = claim_age * 12 - fra
    if months < 0:
        return 1 - _stepped(-months, _old_age_rates(year))
    return 1 + drc_monthly(birth, year) * min(months, LATEST * 12 - fra)


def spouse_factor(birth: date | str, start_age: int, year: int) -> float:
    """The share of the spouse's benefit paid when it starts at ``start_age``
    (404.410(b)); no delayed credits."""
    early = fra_months(birth, year) - start_age * 12
    return 1 - _stepped(early, SPOUSE_RATES) if early > 0 else 1.0


def survivor_factor(birth: date | str, start_age: int) -> float:
    """The share of the widow(er)'s benefit paid when it starts at
    ``start_age`` (404.410(c)(1))."""
    fra = survivor_fra_months(birth)
    early = fra - max(start_age, WIDOW_EARLIEST) * 12
    if early <= 0:
        return 1.0
    return 1 - SURVIVOR_MAX_CUT * early / (fra - WIDOW_EARLIEST * 12)


@dataclass
class Person:
    """One person's record, from their SSA statement and plan."""

    who: str  # "you" or "spouse"
    birth: str
    claim_age: int | None
    estimates: dict[int, float]  # statement age -> monthly figure
    pia: float = 0.0
    own: float = 0.0  # monthly own benefit at the claim age
    note: str | None = None


def pia(birth: str, estimates: dict[int, float], year: int) -> tuple[float, str]:
    """The primary insurance amount from the statement, and where it came from."""
    fra = fra_months(birth, year)
    if estimates.get(67) is not None:
        if fra == 804:
            return float(estimates[67]), "the age-67 estimate (full retirement age 67)"
        credit = drc_monthly(birth, year) * (804 - fra)
        return estimates[67] / (
            1 + credit
        ), "the age-67 estimate less its delayed credits"
    if estimates.get(62) is not None:
        return estimates[62] / own_factor(
            birth, 62, year
        ), "the age-62 estimate grossed up"
    if estimates.get(70) is not None:
        return estimates[70] / own_factor(
            birth, 70, year
        ), "the age-70 estimate less its credits"
    return 0.0, "no SSA estimate"


def person(
    who: str, birth: str, claim_age: Any, estimates: dict[int, Any], year: int
) -> Person:
    """Fill a person's PIA and own monthly benefit at the claim age: the
    statement's own figure when it is for that age, else figured from the PIA."""
    est = {a: float(v) for a, v in estimates.items() if v is not None}
    claim = int(claim_age) if claim_age is not None else None
    p = Person(who, birth, claim, est)
    p.pia, origin = pia(birth, est, year)
    if claim is None or not est:
        return p
    if claim in est:
        p.own = est[claim]
        return p
    factor = own_factor(birth, claim, year)
    p.own = p.pia * factor
    p.note = (
        f"{who}: the age-{claim} benefit {r(p.own):,.2f}/month is figured from the "
        f"PIA {r(p.pia):,.2f} ({origin}) x {factor:.4f} (20 CFR 404.410, 404.313)"
    )
    return p


def age_in(birth: str, year: int) -> int:
    return year - date.fromisoformat(birth).year


def spousal(spouse: Person, worker: Person, year: int) -> tuple[float, int | None]:
    """The spouse's monthly benefit on the worker's record beyond their own,
    and the spouse's age when it starts (the later of the two claims)."""
    if spouse.claim_age is None or worker.claim_age is None:
        return 0.0, None
    excess = SPOUSE_SHARE * worker.pia - spouse.pia
    if excess <= 0:
        return 0.0, None
    worker_files = date.fromisoformat(worker.birth).year + worker.claim_age
    start = max(spouse.claim_age, worker_files - date.fromisoformat(spouse.birth).year)
    return excess * spouse_factor(spouse.birth, start, year), start


def survivor(
    survivor_: Person, deceased: Person, start_age: int | None = None
) -> float:
    """The survivor's monthly check after the other dies, both having claimed:
    the larger of their own and the widow(er)'s benefit (404.338, 404.407),
    unreduced from the survivor's full retirement age unless ``start_age``."""
    base = deceased.own
    if deceased.own < deceased.pia:  # claimed early: 82.5% floor (404.338(c))
        base = max(deceased.own, SURVIVOR_FLOOR * deceased.pia)
    factor = 1.0 if start_age is None else survivor_factor(survivor_.birth, start_age)
    return max(survivor_.own, base * factor)


def household(
    profile: dict[str, Any], year: int
) -> tuple[Person, Person | None, list[str]]:
    """Both records from the profile: the head's, and a joint spouse's once
    their claim age is set."""
    notes: list[str] = []
    head = person(
        "you",
        str(profile["birth_date"]),
        profile.get("ss_claim_age"),
        {a: profile.get(f"ss_estimate_{a}") for a in (62, 67, 70)},
        year,
    )
    if head.claim_age is None:
        notes.append("ss_claim_age not set: no Social Security in the table")
    elif not head.estimates:
        notes.append(
            f"no SSA estimate for claim age {head.claim_age}: none in the table"
        )
    if head.note:
        notes.append(head.note)
    spouse = None
    birth = profile.get("spouse_birth_date")
    if profile.get("filing_status") == "married_joint" and birth:
        spouse = person(
            "spouse",
            str(birth),
            profile.get("spouse_ss_claim_age"),
            {a: profile.get(f"spouse_ss_estimate_{a}") for a in (62, 67, 70)},
            year,
        )
        if spouse.claim_age is None:
            notes.append("spouse_ss_claim_age not set: no spouse benefits in the table")
            return head, spouse, notes
        if not spouse.estimates:
            notes.append(
                "no spouse SSA estimate: the spouse's own benefit is taken as none"
            )
        if spouse.note:
            notes.append(spouse.note)
        for a, b in ((spouse, head), (head, spouse)):
            extra, start = spousal(a, b, year)
            if start is not None:
                notes.append(
                    f"{a.who}: spouse's benefit on the other's record "
                    f"{r(extra):,.2f}/month "
                    f"from age {start} (half the PIA {r(b.pia):,.2f} less the own PIA "
                    f"{r(a.pia):,.2f}, reduced for age; 20 CFR 404.333, 404.410(b))"
                )
        if head.claim_age is not None and head.estimates:
            for live, dead in ((spouse, head), (head, spouse)):
                notes.append(
                    f"survivor: if {'you die' if dead is head else 'the spouse dies'}, "
                    f"{'the spouse' if live is spouse else 'you'} keep "
                    f"{r(survivor(live, dead)):,.2f}/month from full retirement age, "
                    f"{r(survivor(live, dead, WIDOW_EARLIEST)):,.2f} if taken at 60 "
                    "(20 CFR 404.338, 404.410(c))"
                )
    return head, spouse, notes


def schedule(
    head: Person, spouse: Person | None, year: int, horizon: int
) -> dict[int, float]:
    """Household benefits a year, keyed by the head's age at year end."""
    out: dict[int, float] = {}
    head_age = age_in(head.birth, year)
    top = spousal(spouse, head, year) if spouse else (0.0, None)
    mine = spousal(head, spouse, year) if spouse else (0.0, None)
    for a in range(head_age, horizon + 1):
        y = year + a - head_age
        total = 0.0
        if head.claim_age is not None and a >= head.claim_age:
            total += head.own * 12
            if mine[1] is not None and a >= mine[1]:
                total += mine[0] * 12
        if spouse is not None and spouse.claim_age is not None:
            s = age_in(spouse.birth, y)
            if s >= spouse.claim_age:
                total += spouse.own * 12
                if top[1] is not None and s >= top[1]:
                    total += top[0] * 12
        out[a] = total
    return out


@dataclass
class EarningsTest:
    who: str
    withheld: float
    paid_before: float  # this year's benefit before the test
    note: str


def earnings_test(
    p: Person, monthly: float, earnings: float, year: int
) -> EarningsTest | None:
    """This year's withholding for a person who has claimed and is under full
    retirement age (404.430(b)); None when the test does not reach them."""
    if p.claim_age is None or not monthly:
        return None
    b = date.fromisoformat(p.birth)
    start = b.year + p.claim_age
    if start > year:
        return None
    first = b.month if start == year else 1  # first month entitled this year
    fra = fra_months(p.birth, year)
    fra_year = b.year + (b.month - 1 + fra) // 12
    fra_month = (b.month - 1 + fra) % 12 + 1
    if fra_year < year or (fra_year == year and fra_month == 1):
        return None
    et = f"{SS}.earnings_test"
    if fra_year == year:
        before = fra_month - 1
        exempt = float(_node(f"{et}.exempt_amount_year_of_fra", year))
        rate = float(_node(f"{et}.reduction_rate_year_of_fra", year))
        counted = earnings * before / 12
        how = (
            f"the {before} months before full retirement age ({counted:,.2f} of "
            f"{earnings:,.2f}, spread evenly)"
        )
    else:
        before = 12
        exempt = float(_node(f"{et}.exempt_amount_under_fra", year))
        rate = float(_node(f"{et}.reduction_rate_under_fra", year))
        counted = earnings
        how = f"earnings {earnings:,.2f}"
    months = max(min(before, 12) - first + 1, 0)
    paid = monthly * months
    withheld = min(max(counted - exempt, 0.0) * rate, paid)
    note = (
        f"{p.who}: earnings test {year}: {how} against {exempt:,.2f} at "
        f"{rate:.4g} withholds {r(withheld):,.2f} of {r(paid):,.2f} "
        "(20 CFR 404.430); SSA credits the withheld months back at full "
        "retirement age (404.412)"
    )
    return EarningsTest(p.who, r(withheld), r(paid), note)
