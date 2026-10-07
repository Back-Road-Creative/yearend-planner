"""The benefits screen (master plan Stage 5, unit 5a): a registry of programs,
each with its place, who it covers, its dates, how it counts income and the
household, its look-back and asset test, other conditions, how to apply, its
official source and the date that source was last read. Each program reads one
of three results for the household:

- possibly eligible: every condition the planner models is met;
- not eligible under what's modeled: a modeled condition fails;
- not enough information: a condition that decides it was not asked.

A result is a screen, not a determination: the program's agency decides. The
figures come from the engine (policyengine-us) for this household and year,
with no network at run time. Unit 5a covers health coverage below Medicare:
the premium tax credit, cost-sharing reductions, Medicaid and CHIP. Unit 5b
adds Medicare's costs: the income surcharge (IRMAA) this year's income sets,
the Medicare Savings Programs and Extra Help.

Sources, read 2026-10-07:
- IRC 36B(c)(1)(A): household income from 100 to 400 percent of the poverty
  line (no 400 percent cap 2021-2025, P.L. 117-169; the engine's
  ``gov.aca.ptc_income_eligibility`` holds the year's line); 36B(c)(1)(C): a
  married taxpayer files jointly; 36B(c)(2)(B): no credit for a month the
  person is eligible for other minimum essential coverage (Medicaid, CHIP);
  36B(c)(2)(C): an affordable employer offer counts as that coverage.
- 45 CFR 155.305(g)(1): cost-sharing reductions need credit eligibility and
  income at most 250 percent of the poverty line, in a silver plan (an Indian
  enrollee excepted); 155.305(g)(2)(i)-(iii): the 100-150, over 150-200 and
  over 200-250 percent bands; 45 CFR 156.420(a)(1)-(3): those bands' silver
  plan variations at 94, 87 and 73 percent actuarial value.
- 45 CFR 155.410(e): open enrollment from November 1 through January 15 for
  benefit years through 2026; from 2027 it ends by December 31.
- 42 CFR 435.603(g): no asset test where Medicaid counts MAGI; 435.603(j): MAGI
  rules do not apply at age 65 or older when age is a condition, or on the
  basis of blindness or disability (those groups test assets);
  42 CFR 435.915(a): coverage up to the third month before the application;
  42 USC 1396p(c)(1)(B)(i): a 60-month look-back on asset transfers for
  long-term care.
- 42 CFR 457.310(b): a CHIP child is under 19, over the state's Medicaid level
  and within its CHIP level, not Medicaid-eligible and not otherwise insured.
- 20 CFR 418.1010(b)(6): IRMAA's MAGI is AGI plus tax-exempt interest (and
  excluded foreign and territory income); 418.1115: the premium year reads the
  return of two years before; 418.1205(a)-(g): a spouse's death, marriage,
  divorce, work stopping or cut back, lost income property, a pension ended or
  an employer settlement lets SSA use a newer year (Form SSA-44). The brackets
  are the engine's CMS figures (``gov.hhs.medicare.part_b.irmaa``, ``part_d``).
- 42 USC 1396d(p): the Medicare Savings Programs count income the SSI way;
  QMB to 100 percent of the poverty line (1396d(p)(2)), SLMB to 120 and QI to
  135 (1396a(a)(10)(E)(iii)-(iv)); resources to the engine's limit
  (``gov.hhs.medicare.savings_programs.eligibility.asset``), which several
  states waive.
- 42 CFR 423.773(b)(1): Extra Help needs income under 150 percent of the
  poverty line for the family (plan years from 2024) and resources within
  423.773(d)(2); 423.773(c)(1): QMB, SLMB, QI, SSI or full Medicaid makes a
  person eligible without applying; 423.772: the family, income (a spouse's
  counts) and liquid resources; SSA POMS HI 03030.025: the resource limits by
  year, before the 1,500/3,000 burial allowance SSA adds unless declined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from planner.engine.household import Household
from planner.engine.tax import (
    TaxResult,
    _ptc_capped,
    _system,
    compute,
    irmaa,
    people,
    r,
)
from planner.ledger import portfolio
from planner.paths import Layout
from planner.plan import magi
from planner.plan.inputs import Overrides

POSSIBLE = "possibly eligible"
NOT = "not eligible under what's modeled"
UNKNOWN = "not enough information"
REVIEWED = "2026-10-07"
CHIP_AGE = 19  # 42 CFR 457.310(b) via 457.320: a child is under 19
MEDICARE_AGE = 65  # 42 CFR 435.603(j)(2): MAGI rules stop where age decides
CSR_BANDS = ((1.50, 94), (2.00, 87), (2.50, 73))  # 155.305(g)(2), 156.420(a)
IRMAA_LAG = 2  # 20 CFR 418.1115: a premium year reads the return two years before
MSP_TIERS = (  # 42 USC 1396d(p)(2), 1396a(a)(10)(E)(iii)-(iv)
    ("QMB", "pays the Part B premium, Part A if owed, and the cost sharing"),
    ("SLMB", "pays the Part B premium"),
    ("QI", "pays the Part B premium"),
)
EXTRA_HELP_LINE = 1.50  # 42 CFR 423.773(b)(1), plan years from 2024
# SSA POMS HI 03030.025, individual and couple, before the burial allowance
EXTRA_HELP_RESOURCES = {
    2024: (15_720, 31_360),
    2025: (16_100, 32_130),
    2026: (16_590, 33_100),
}
BURIAL = (1_500, 3_000)  # added unless the applicant declines it
MARRIED = ("JOINT", "SEPARATE")


@dataclass(frozen=True)
class Program:
    key: str
    name: str
    place: str
    who: str
    dates: str
    income: str
    household: str
    lookback: str
    assets: str
    other: str
    apply: str
    sources: tuple[str, ...]
    modeled: str  # what the planner checks, and from where
    reviewed: str = REVIEWED


REGISTRY: tuple[Program, ...] = (
    Program(
        "aca_ptc",
        "Premium tax credit (marketplace)",
        "every state and DC, through HealthCare.gov or the state's marketplace",
        "people buying a marketplace plan who are not eligible for other "
        "coverage; married couples file jointly",
        "open enrollment November 1 to January 15 (through the 2026 plan year; "
        "from 2027 it ends by December 31); a qualifying event opens a special "
        "period; the credit settles on the tax return (Form 8962)",
        "MAGI: AGI plus tax-exempt interest and untaxed Social Security, "
        "against last year's poverty guideline",
        "the tax household: the filer, a spouse and the dependents claimed",
        "none",
        "none",
        "an affordable employer offer, Medicare, Medicaid or CHIP ends it; "
        "income under 100% qualifies only for some lawfully present immigrants",
        "HealthCare.gov or the state marketplace",
        ("IRC 36B(c)(1)(A), (c)(1)(C), (c)(2)(B), (c)(2)(C)", "45 CFR 155.410(e)"),
        "income line from the engine's parameters; the credit itself from the "
        "engine at the typed plan",
    ),
    Program(
        "aca_csr",
        "Cost-sharing reductions (silver plans)",
        "every state and DC, through HealthCare.gov or the state's marketplace",
        "people eligible for the premium tax credit who enroll in a silver plan",
        "as the premium tax credit; it applies to the plan year enrolled",
        "as the premium tax credit, against last year's poverty guideline",
        "as the premium tax credit",
        "none",
        "none",
        "a silver plan (an Indian enrollee may take any metal level)",
        "HealthCare.gov or the state marketplace (chosen with the plan)",
        ("45 CFR 155.305(g)(1)-(2)", "45 CFR 156.420(a)(1)-(3)"),
        "the 100-150, 150-200 and 200-250% bands on the engine's MAGI",
    ),
    Program(
        "medicaid",
        "Medicaid",
        "every state and DC; each sets its own groups and limits (expansion "
        "states cover adults to 138% of the poverty line)",
        "adults, parents, children, pregnant people; at 65 or older, blind or "
        "disabled, the aged/blind/disabled groups",
        "any time of year; coverage can reach back three months before the application",
        "MAGI against this year's poverty guideline, monthly (under 65); the "
        "aged/blind/disabled groups count income the SSI way",
        "the tax household (MAGI groups)",
        "60 months of asset transfers when long-term care is asked for",
        "none in the MAGI groups; the aged/blind/disabled groups test assets",
        "immigration status, state residence; a work requirement where the "
        "state or federal law sets one",
        "the state Medicaid agency, or HealthCare.gov (which sends it on)",
        (
            "42 CFR 435.603(g), (j)",
            "42 CFR 435.915(a)",
            "42 USC 1396p(c)(1)(B)(i)",
        ),
        "the engine's is_medicaid_eligible for each member; at 65 or older an "
        "eligible answer waits on the asset test",
    ),
    Program(
        "chip",
        "Children's Health Insurance Program (CHIP)",
        "every state and DC; each sets its own CHIP limit above its Medicaid limit",
        "children under 19 (and in some states pregnant people) not eligible "
        "for Medicaid and not otherwise insured",
        "any time of year",
        "MAGI against this year's poverty guideline",
        "the tax household",
        "none",
        "none",
        "no other health insurance; some states charge a premium or wait",
        "the state Medicaid/CHIP agency, InsureKidsNow.gov or HealthCare.gov",
        ("42 CFR 457.310(b)",),
        "the engine's is_chip_eligible for each member under 19",
    ),
    Program(
        "medicare_irmaa",
        "Medicare premium without the income surcharge (IRMAA)",
        "every state and DC; Social Security sets it, Medicare charges it",
        "everyone on Medicare Part B or D",
        "each year's premium reads the tax return of two years before; SSA "
        "sends the notice in the fall; an appeal or Form SSA-44 any time",
        "MAGI: AGI plus tax-exempt interest (and excluded foreign or territory "
        "income), against brackets by filing status",
        "the tax return: a joint return's MAGI sets both spouses' premiums",
        "two years (a life-changing event lets SSA use a newer year)",
        "none",
        "married filing separately and living together uses the steepest "
        "brackets; the surcharge is per person on Medicare",
        "automatic; a life-changing event: Form SSA-44 to Social Security",
        ("20 CFR 418.1010(b)(6)", "20 CFR 418.1115", "20 CFR 418.1205(a)-(g)"),
        "this year's AGI and tax-exempt interest against the engine's CMS "
        "brackets, for a spouse or filer 65 or older two years on",
    ),
    Program(
        "msp",
        "Medicare Savings Programs (QMB, SLMB, QI)",
        "every state and DC, through the state Medicaid agency; some states "
        "raise the income limits or drop the asset test",
        "people with Medicare Part A (or who can get it)",
        "any time of year; QMB starts the month after approval, SLMB and QI "
        "can reach back three months",
        "SSI-countable income (a $20 general exclusion; $65 and half of the "
        "rest of earnings), monthly, against this year's guideline",
        "the person, or a married couple",
        "none",
        "resources to the year's limit (a home, one car and burial funds do "
        "not count), unless the state waives the test",
        "Medicare Part A; QI is first come, first served each year",
        "the state Medicaid agency",
        ("42 USC 1396d(p)", "42 USC 1396a(a)(10)(E)(iii)-(iv)"),
        "the engine's QMB, SLMB and QI income tests for each member on "
        "Medicare; resources from every account on file against the "
        "engine's limit and the state's waiver",
    ),
    Program(
        "extra_help",
        "Extra Help with Medicare drug costs (Part D low-income subsidy)",
        "every state and DC, through Social Security",
        "people with Medicare Part D (or who can get it)",
        "any time of year; QMB, SLMB, QI, SSI or full Medicaid qualifies a "
        "person without applying, for the rest of the year (and the next "
        "year if it starts July or later)",
        "the SSI way, a spouse's income counted, under 150% of the guideline "
        "for the family",
        "you, a spouse living with you and relatives living with you who "
        "depend on you for half their support",
        "none",
        "liquid resources (accounts, stocks, bonds, real estate other than "
        "the home) to the year's limit plus a burial allowance",
        "Medicare Part D",
        "Social Security (ssa.gov/extrahelp) or the state Medicaid agency",
        (
            "42 CFR 423.772",
            "42 CFR 423.773(b)(1), (c)(1), (d)(2)",
            "SSA POMS HI 03030.025",
        ),
        "the engine's SSI-countable income against 150% of this year's "
        "guideline; resources from every account on file; eligible without "
        "applying when the Medicare Savings Programs screen possibly eligible",
    ),
)
BY_KEY = {p.key: p for p in REGISTRY}


@dataclass(frozen=True)
class Member:
    label: str
    age: int
    medicaid: bool
    chip: bool
    medicare: bool = False  # the engine's is_medicare_eligible (65 or older)
    msp: str = ""  # the engine's QMB, SLMB or QI income tier, if any


@dataclass(frozen=True)
class Facts:
    """What the screen reads, from one engine run."""

    year: int
    filing_status: str
    ratio: float  # ACA MAGI / last year's guideline (the credit and CSR)
    medicaid_pct: float  # MAGI against this year's guideline (Medicaid)
    capped: bool  # the year ends the credit above 400%
    ptc: float  # the credit at the typed plan
    members: tuple[Member, ...]
    irmaa_magi: float = 0.0  # AGI plus tax-exempt interest (20 CFR 418.1010)
    msp_income: float = 0.0  # SSI-countable income, yearly (you and a spouse)
    msp_fpg: float = 0.0  # the guideline for one or a couple (MSP)
    fpg: float = 0.0  # the guideline for the family (Extra Help)
    msp_limit: float | None = None  # the MSP resource limit; None: waived
    resources: float | None = None  # every account on file; None: not on file


@dataclass(frozen=True)
class Result:
    key: str
    status: str
    why: tuple[str, ...]
    amount: float | None = None

    @property
    def program(self) -> Program:
        return BY_KEY[self.key]


@dataclass
class Benefits:
    year: int
    results: list[Result] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def result(self, key: str) -> Result:
        return next(x for x in self.results if x.key == key)

    def lines(self) -> list[str]:
        out = []
        for x in self.results:
            amount = f" ({x.amount:,.2f})" if x.amount is not None else ""
            out.append(f"{x.program.name}: {x.status}{amount}")
            out += [f"  {w}" for w in x.why]
            out.append(f"  apply: {x.program.apply}")
        out.append(
            "a screen, not a decision: the agency decides; sources read "
            f"{REVIEWED} (planner benefits --programs lists them)"
        )
        return out


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def medicaid(f: Facts) -> Result:
    why, seen = [], set()
    for m in f.members:
        if not m.medicaid:
            st, w = NOT, "not in a Medicaid group at this income and state"
        elif m.age >= MEDICARE_AGE:
            st = UNKNOWN
            w = (
                "income passes, but at 65 or older Medicaid tests assets "
                "(42 CFR 435.603(j)) and assets were not asked"
            )
        else:
            st, w = POSSIBLE, f"in a MAGI group at {f.medicaid_pct:.0f}% of the line"
        seen.add(st)
        why.append(f"{m.label} (age {m.age}): {w}")
    status = next(s for s in (POSSIBLE, UNKNOWN, NOT) if s in seen)
    why.append("blindness and disability groups (asset-tested) are not screened")
    return Result("medicaid", status, tuple(why))


def chip(f: Facts) -> Result:
    kids = [m for m in f.members if m.age < CHIP_AGE]
    if not kids:
        return Result(
            "chip",
            NOT,
            ("no one under 19 on the return (pregnancy coverage is not asked)",),
        )
    why, any_yes = [], False
    for m in kids:
        if m.chip:
            any_yes, w = True, "within the state's CHIP limit"
        elif m.medicaid:
            w = "Medicaid-eligible instead (42 CFR 457.310(b)(2)(i))"
        else:
            w = "over the state's CHIP limit"
        why.append(f"{m.label} (age {m.age}): {w}")
    why.append("other health insurance bars CHIP; not asked")
    return Result("chip", POSSIBLE if any_yes else NOT, tuple(why))


def ptc(f: Facts) -> Result:
    if f.filing_status == "SEPARATE":
        return Result(
            "aca_ptc",
            NOT,
            (
                "married filing separately (IRC 36B(c)(1)(C)); the abuse and "
                "abandonment exception is not asked",
            ),
        )
    if all(m.age >= MEDICARE_AGE for m in f.members):
        return Result(
            "aca_ptc",
            UNKNOWN,
            (
                "everyone is 65 or older: premium-free Medicare Part A ends it "
                "(36B(c)(2)(B)); whether Part A would cost a premium is not asked",
            ),
        )
    covered = [m for m in f.members if m.medicaid or m.chip]
    if covered and len(covered) == len(f.members):
        return Result(
            "aca_ptc",
            NOT,
            ("everyone screens for Medicaid or CHIP, which bars it (36B(c)(2)(B))",),
        )
    if f.ratio < 1.0:
        return Result(
            "aca_ptc",
            NOT,
            (
                f"MAGI is {_pct(f.ratio)} of last year's line, under 100% "
                "(36B(c)(1)(A); the lawfully present immigrant exception is "
                "not asked)",
            ),
        )
    if f.capped and f.ratio > 4.0:
        return Result(
            "aca_ptc",
            NOT,
            (f"MAGI is {_pct(f.ratio)} of last year's line, over 400% in {f.year}",),
        )
    why = [f"MAGI is {_pct(f.ratio)} of last year's line"]
    if covered:
        names = ", ".join(m.label for m in covered)
        why.append(f"not for {names}: Medicaid or CHIP instead (36B(c)(2)(B))")
    older = [m.label for m in f.members if m.age >= MEDICARE_AGE]
    if older:
        why.append(f"not for {', '.join(older)} once on Medicare (65 or older)")
    why.append("an affordable employer offer ends it; not asked")
    if f.ptc > 0:
        return Result("aca_ptc", POSSIBLE, tuple(why), r(f.ptc))
    why.append("no marketplace premium typed: the credit is figured at the plan")
    return Result("aca_ptc", POSSIBLE, tuple(why))


def csr(f: Facts, credit: Result) -> Result:
    if credit.status != POSSIBLE:
        return Result(
            "aca_csr",
            NOT,
            ("needs premium tax credit eligibility (45 CFR 155.305(g)(1))",),
        )
    for top, av in CSR_BANDS:
        if f.ratio <= top:
            return Result(
                "aca_csr",
                POSSIBLE,
                (
                    f"MAGI is {_pct(f.ratio)} of last year's line, in the band to "
                    f"{_pct(top)}: a silver plan at {av}% actuarial value "
                    "(45 CFR 156.420(a))",
                ),
            )
    return Result(
        "aca_csr",
        NOT,
        (f"MAGI is {_pct(f.ratio)} of last year's line, over 250%",),
    )


def _adults(f: Facts) -> list[Member]:
    return [m for m in f.members if m.label in ("you", "spouse")]


def premium(f: Facts) -> Result:
    """The Medicare premium this year's income sets for two years on."""
    year = f.year + IRMAA_LAG
    older = [m for m in _adults(f) if m.age + IRMAA_LAG >= MEDICARE_AGE]
    if not older:
        return Result(
            "medicare_irmaa",
            NOT,
            (
                f"no one on the return is 65 by {year}: no Medicare premium rests "
                "on this year's income",
            ),
        )
    got = irmaa(year, f.filing_status, f.irmaa_magi)
    names = ", ".join(m.label for m in older)
    why = [f"this year's MAGI {f.irmaa_magi:,.2f} sets the {year} premium for {names}"]
    if got.figures < year:
        why.append(
            f"at the {got.figures} brackets, the latest set; CMS sets {year}'s "
            "each fall"
        )
    tail = (
        "a life-changing event (a spouse's death, marriage, divorce, work "
        "stopping or cut back, lost income property or pension) lets Social "
        "Security use a newer year: Form SSA-44 (20 CFR 418.1205)"
    )
    if not (got.part_b or got.part_d):
        why.append(f"no surcharge: Part B at the standard {got.base:,.2f} a month")
        return Result("medicare_irmaa", POSSIBLE, (*why, tail))
    why.append(
        f"a surcharge of {got.part_b:,.2f} a month on Part B (standard "
        f"{got.base:,.2f}) and {got.part_d:,.2f} on Part D, for each person"
    )
    amount = r((got.part_b + got.part_d) * 12 * len(older))
    return Result("medicare_irmaa", NOT, (*why, tail), amount)


def _resources(f: Facts, limit: float, rule: str) -> tuple[str, str]:
    if f.resources is None:
        return (
            UNKNOWN,
            f"no accounts on file to test against the {limit:,.0f} limit ({rule})",
        )
    if f.resources > limit:
        return (
            NOT,
            f"accounts on file {f.resources:,.0f}, over the {limit:,.0f} limit "
            f"({rule})",
        )
    return (
        POSSIBLE,
        f"accounts on file {f.resources:,.0f}, within the {limit:,.0f} limit ({rule})",
    )


def msp(f: Facts) -> Result:
    on = [m for m in f.members if m.medicare]
    if not on:
        return Result(
            "msp",
            NOT,
            (
                "no one on the return is on Medicare (65 or older; Medicare "
                "through disability is not asked)",
            ),
        )
    pct = f.msp_income / f.msp_fpg if f.msp_fpg else 0.0
    tiered = [m for m in on if m.msp]
    if not tiered:
        return Result(
            "msp",
            NOT,
            (
                f"countable income {f.msp_income:,.2f} is {_pct(pct)} of the "
                "line, over QI's 135% (42 USC 1396a(a)(10)(E)(iv)); a state with "
                "higher limits is not screened",
            ),
        )
    why = []
    for m in tiered:
        what = dict(MSP_TIERS)[m.msp]
        why.append(f"{m.label}: {m.msp} at {_pct(pct)} of the line ({what})")
    if f.msp_limit is None:
        st, w = POSSIBLE, "this state has no asset test for these programs"
    else:
        st, w = _resources(f, f.msp_limit, "42 USC 1396d(p)(1)(C)")
    base = irmaa(f.year, f.filing_status, 0.0).base
    amount = r(base * 12 * len(tiered)) if st == POSSIBLE else None
    return Result("msp", st, (*why, w), amount)


def extra_help(f: Facts, savings: Result) -> Result:
    if not any(m.medicare for m in f.members):
        return Result(
            "extra_help",
            NOT,
            (
                "no one on the return is on Medicare (65 or older; Medicare "
                "through disability is not asked)",
            ),
        )
    if savings.status == POSSIBLE:
        return Result(
            "extra_help",
            POSSIBLE,
            (
                "a Medicare Savings Program qualifies you without applying "
                "(42 CFR 423.773(c)(1)(iii))",
            ),
        )
    ratio = f.msp_income / f.fpg if f.fpg else 0.0
    if ratio >= EXTRA_HELP_LINE:
        return Result(
            "extra_help",
            NOT,
            (
                f"countable income {f.msp_income:,.2f} is {_pct(ratio)} of the "
                "family's line, at or over 150% (42 CFR 423.773(b)(1))",
            ),
        )
    why = [
        f"countable income {f.msp_income:,.2f} is {_pct(ratio)} of the family's line"
    ]
    limits = EXTRA_HELP_RESOURCES.get(f.year)
    if limits is None:
        why.append(
            f"the {f.year} resource limit is not in the registry yet "
            "(SSA POMS HI 03030.025 sets it each fall)"
        )
        return Result("extra_help", UNKNOWN, tuple(why))
    i = 1 if f.filing_status in MARRIED else 0
    st, w = _resources(
        f, limits[i] + BURIAL[i], "with the burial allowance; SSA POMS HI 03030.025"
    )
    return Result("extra_help", st, (*why, w))


def screen(f: Facts) -> list[Result]:
    credit = ptc(f)
    savings = msp(f)
    return [
        credit,
        csr(f, credit),
        medicaid(f),
        chip(f),
        premium(f),
        savings,
        extra_help(f, savings),
    ]


def msp_limit(year: int, state: str, filing_status: str) -> float | None:
    """The MSP resource limit for the person or couple, or None where the state
    has dropped the asset test (the engine's parameters)."""
    asset = _system().parameters.gov.hhs.medicare.savings_programs.eligibility.asset
    when = f"{year}-01-01"
    if not bool(asset.applies(when)[state]):
        return None
    node = asset.couple if filing_status in MARRIED else asset.individual
    return float(node(when))


PEOPLE = (
    "is_medicaid_eligible",
    "is_chip_eligible",
    "is_medicare_eligible",
    "is_qmb_eligible",
    "is_slmb_eligible",
    "is_qi_eligible",
    "msp_countable_income",
    "msp_fpg",
)


def facts(
    year: int,
    hh: Household,
    res: TaxResult | None = None,
    resources: float | None = None,
) -> Facts:
    """One engine run's figures for the screen."""
    res = res or compute(year, hh)
    got = people(year, hh, PEOPLE)
    labels = ["you"] + (["spouse"] if hh.spouse is not None else [])
    labels += [f"dependent {i}" for i in range(1, len(hh.dependents) + 1)]
    ages = [hh.age] + ([hh.spouse.age] if hh.spouse is not None else [])
    ages += [d.age for d in hh.dependents]
    tiers = [
        next((k for k, _ in MSP_TIERS if got[f"is_{k.lower()}_eligible"][i]), "")
        for i in range(len(labels))
    ]
    members = tuple(
        Member(label, age, bool(med), bool(ch), bool(mc), tier)
        for label, age, med, ch, mc, tier in zip(
            labels,
            ages,
            got["is_medicaid_eligible"],
            got["is_chip_eligible"],
            got["is_medicare_eligible"],
            tiers,
            strict=True,
        )
    )
    adults = range(len(_adults_of(labels)))
    return Facts(
        year,
        hh.filing_status,
        res.aca_magi / res.aca_fpg if res.aca_fpg else 0.0,
        res.medicaid_fpl_pct,
        _ptc_capped(year),
        res.aca_ptc,
        members,
        r(res.agi + hh.tax_exempt_interest),
        r(max(got["msp_countable_income"][i] for i in adults)),
        got["msp_fpg"][0],
        res.fpg,
        msp_limit(year, hh.state, hh.filing_status),
        resources,
    )


def _adults_of(labels: list[str]) -> list[str]:
    return [x for x in labels if x in ("you", "spouse")]


def assess(
    year: int,
    hh: Household,
    res: TaxResult | None = None,
    resources: float | None = None,
) -> Benefits:
    return Benefits(year, screen(facts(year, hh, res, resources)))


def build(
    lay: Layout,
    year: int,
    overrides: Overrides | None = None,
    pj: magi.Projection | None = None,
    today: date | None = None,
) -> Benefits:
    """The screen on the year's projection (MAGI from the ledger and profile),
    with every account on file as the resources the asset tests read."""
    pj = pj or magi.project(lay, year, overrides)
    st = portfolio.status(lay, year, today)
    resources = st.total if st.positions else None
    out = assess(year, pj.inputs.household, pj.result, resources)
    out.notes += [
        "the screen reads the projected full-year income; a program that "
        "counts monthly income tests the month you apply"
    ]
    if resources is not None:
        out.notes.append(
            f"resources: every account on file ({resources:,.2f}), retirement "
            "accounts included; the agencies leave out a home, one car, burial "
            "funds and plans you cannot draw"
        )
    return out


def program_lines() -> list[str]:
    """The registry, one block per program, with its sources and review date."""
    out = []
    for p in REGISTRY:
        out.append(f"{p.name} [{p.key}]")
        for label, value in (
            ("place", p.place),
            ("who", p.who),
            ("dates", p.dates),
            ("income", p.income),
            ("household", p.household),
            ("look-back", p.lookback),
            ("assets", p.assets),
            ("other", p.other),
            ("apply", p.apply),
            ("modeled", p.modeled),
            ("sources", "; ".join(p.sources)),
            ("reviewed", p.reviewed),
        ):
            out.append(f"  {label}: {value}")
    return out
