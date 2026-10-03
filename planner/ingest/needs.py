# ruff: noqa: E501
"""The Needed panel: what the plan still lacks for a year, and where it comes from.

Every fact a planner or tax line relies on is declared once here, with the
document that supplies it. ``needed()`` diffs that list against the ledger
(form boxes, YTD facts), the profile and the typed answers, and reports each
item as actual (a form or an answer), estimate (year-to-date rows), missing,
or don't-have. Only facts no dropped document supplied are ever asked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from planner.config import ASSUMPTION_FIELDS, load_assumptions
from planner.ledger import db, portfolio
from planner.paths import Layout

PROFILE = "profile"
PRIOR = "prior"  # the year before the plan year (the filed return)
YEAR = "year"

MANUAL_VALUES = "values"
MANUAL_DONT_HAVE = "dont_have"
ACCOUNT = "account:"  # dynamic keys: account:<number>, account:<number>:death
FILING = ("single", "married_joint", "married_separate", "head_of_household")


@dataclass(frozen=True)
class Need:
    key: str
    label: str
    why: str
    source: str  # the document that supplies it, with where to get it
    kind: str  # date | int | money | fraction | enum | str
    scope: str = YEAR
    boxes: tuple[tuple[str, str], ...] = ()  # (form, box) ledger lookups, summed
    estimate: tuple[tuple[str, str], ...] = ()  # YTD facts that stand in meanwhile
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Status:
    need: Need
    state: str  # actual | estimate | missing | dont_have
    value: Any = None
    origin: str = ""  # "SSA 2026", "YTD vanguard_income", "typed", ...


@dataclass
class NeedsReport:
    year: int
    items: list[Status] = field(default_factory=list)

    def by_state(self, state: str) -> list[Status]:
        return [s for s in self.items if s.state == state]

    @property
    def done(self) -> bool:
        return not self.by_state("missing")


_VG = "Vanguard: My Accounts > Tax center > download the year's forms"
_SSA = "ssa.gov/myaccount > Your Social Security Statement (PDF)"
_RET = "last year's filed return (the preparer's PDF or tax software export)"
_NONE = "no document supplies this; type it"

NEEDS: tuple[Need, ...] = (
    Need(
        "birth_date",
        "Birth date",
        "age for IRA access, Medicare and Social Security",
        _NONE,
        "date",
        PROFILE,
    ),
    Need(
        "filing_status",
        "Filing status",
        "every bracket and threshold",
        _NONE,
        "enum",
        PROFILE,
        choices=FILING,
    ),
    Need(
        "state", "State", "state income tax and Medicaid rules", _NONE, "str", PROFILE
    ),
    Need(
        "county", "County", "the ACA benchmark (SLCSP) premium", _NONE, "str", PROFILE
    ),
    Need(
        "spending_floor",
        "Spending floor (annual $)",
        "the lowest the household can run on",
        _NONE,
        "money",
        PROFILE,
    ),
    Need(
        "spending_ceiling",
        "Spending ceiling (annual $)",
        "comfortable spending",
        _NONE,
        "money",
        PROFILE,
    ),
    Need(
        "cash_target",
        "Cash buffer target ($)",
        "the cash line on the dashboard",
        _NONE,
        "money",
        PROFILE,
    ),
    Need(
        "mortgage_monthly",
        "Mortgage P&I and escrow per month ($)",
        "the month-by-month cash line",
        "the mortgage statement",
        "money",
        PROFILE,
    ),
    Need(
        "premium_monthly",
        "Health premium per month ($, net of the advance credit)",
        "the month-by-month cash line",
        "the marketplace invoice",
        "money",
        PROFILE,
    ),
    Need(
        "withdrawal_rate",
        "Withdrawal rate",
        "the glide path, e.g. 0.035",
        _NONE,
        "fraction",
        PROFILE,
    ),
    Need(
        "inflation",
        "Inflation",
        "planning assumption, e.g. 0.025",
        _NONE,
        "fraction",
        PROFILE,
    ),
    Need(
        "return_floor",
        "Pessimistic real return",
        "sequence-risk band",
        _NONE,
        "fraction",
        PROFILE,
    ),
    Need(
        "return_track", "Planning real return", "glide path", _NONE, "fraction", PROFILE
    ),
    Need(
        "ss_claim_age",
        "Planned Social Security claim age",
        "the age/year table",
        _NONE,
        "int",
        PROFILE,
    ),
    Need(
        "conversion_margin",
        "Conversion margin ($)",
        "room kept below a threshold",
        _NONE,
        "money",
        PROFILE,
    ),
    Need(
        "conversion_cap",
        "Conversion cap ($)",
        "hard cap on one year's Roth conversion",
        _NONE,
        "money",
        PROFILE,
    ),
    Need(
        "conversion_objective",
        "Conversion objective",
        "which candidate the planner recommends",
        _NONE,
        "enum",
        PROFILE,
        choices=(
            "ltcg_0pct",
            "bracket_12",
            "aca_400",
            "medicaid_under",
            "medicaid_over",
        ),
    ),
    Need(
        "roth_basis_contributions",
        "Roth IRA contributions, lifetime total ($)",
        "the part of the Roth balance that is spendable at any age",
        "the Roth custodian's contribution history (Vanguard: Balances & holdings > "
        "the Roth account > Contributions), or the sum of every Form 5498 box 10",
        "money",
        PROFILE,
        estimate=(("5498", "10"),),
    ),
    Need(
        "hsa_coverage",
        "High-deductible health plan this year (none, self or family)",
        "whether an HSA contribution is a lever, and its limit",
        "the plan's summary of benefits (an HSA-eligible plan says so) or the "
        "marketplace plan listing",
        "enum",
        PROFILE,
        choices=("none", "self", "family"),
    ),
    Need(
        "workplace_plan",
        "Covered by a retirement plan at work this year (yes or no)",
        "whether a traditional IRA contribution is deductible in full",
        "W-2 box 13 'Retirement plan' (checked = yes); a SEP or solo 401(k) of "
        "your own also counts",
        "enum",
        PROFILE,
        choices=("yes", "no"),
    ),
    Need(
        "ss_estimate_62",
        "SS monthly estimate at 62",
        "claim-age comparison",
        _SSA,
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_62"),),
    ),
    Need(
        "ss_estimate_67",
        "SS monthly estimate at 67",
        "claim-age comparison",
        _SSA,
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_67"),),
    ),
    Need(
        "ss_estimate_70",
        "SS monthly estimate at 70",
        "claim-age comparison",
        _SSA,
        "money",
        PROFILE,
        boxes=(("SSA", "monthly_70"),),
    ),
    Need(
        "prior_agi",
        "Prior-year AGI (1040 line 11)",
        "safe harbor and the delta report",
        _RET,
        "money",
        PRIOR,
        boxes=(("1040", "11"),),
    ),
    Need(
        "prior_total_tax",
        "Prior-year total tax (1040 line 24)",
        "the 100%/110% safe harbor",
        _RET,
        "money",
        PRIOR,
        boxes=(("1040", "24"),),
    ),
    Need(
        "prior_nc_tax",
        "Prior-year NC income tax (D-400 line 15)",
        "the NC safe harbor",
        _RET,
        "money",
        PRIOR,
        boxes=(("NC-D400", "15"),),
    ),
    Need(
        "prior_capital_loss_carryforward",
        "Capital loss carried into this year ($)",
        "offsets this year's gains before any is taxed",
        "last year's return: the Capital Loss Carryover Worksheet in the Schedule D "
        "instructions (0 when Schedule D line 16 was not a loss)",
        "money",
        PRIOR,
    ),
    Need(
        "fed_withheld",
        "Federal income tax withheld",
        "credited evenly to the four installments",
        "W-2 box 2, 1099-R box 4 (zero when nothing withholds)",
        "money",
        boxes=(("W-2", "2"), ("1099-R", "4")),
    ),
    Need(
        "nc_withheld",
        "NC income tax withheld",
        "credited evenly to the four installments",
        "W-2 box 17, 1099-R box 14 (zero when nothing withholds)",
        "money",
        boxes=(("W-2", "17"), ("1099-R", "14")),
    ),
    Need(
        "wages",
        "Wages",
        "ordinary income",
        "W-2 box 1 from the employer; type 0 if none",
        "money",
        boxes=(("W-2", "1"),),
    ),
    Need(
        "se_income",
        "Self-employment net income",
        "SE tax, QBI, Schedule C",
        "Schedule C line 31 from the bank CSV once its rows are categorised "
        "(planner categorize); 1099-NEC / 1099-K from each payer stand in",
        "money",
        boxes=(("SCH-C", "31"),),
        estimate=(("1099-NEC", "1"), ("1099-K", "1a")),
    ),
    Need(
        "interest",
        "Taxable interest",
        "ordinary income",
        "1099-INT (" + _VG + ")",
        "money",
        boxes=(("1099-INT", "1"),),
        estimate=(("YTD", "interest"),),
    ),
    Need(
        "ordinary_dividends",
        "Ordinary dividends",
        "ordinary income and MAGI",
        "1099-DIV box 1a (" + _VG + ")",
        "money",
        boxes=(("1099-DIV", "1a"),),
        estimate=(("YTD", "dividends"),),
    ),
    Need(
        "qualified_dividends",
        "Qualified dividends",
        "0% / 15% rate stacking",
        "1099-DIV box 1b",
        "money",
        boxes=(("1099-DIV", "1b"),),
    ),
    Need(
        "short_term_gains",
        "Short-term gain or loss",
        "ordinary income",
        "1099-B (" + _VG + ") or the realized-gains CSV",
        "money",
        boxes=(("1040-SCHD", "7"),),
        estimate=(("YTD", "st_gain"),),
    ),
    Need(
        "long_term_gains",
        "Long-term gain or loss",
        "0% LTCG room",
        "1099-B or the realized-gains CSV",
        "money",
        boxes=(("1040-SCHD", "15"),),
        estimate=(("YTD", "lt_gain"),),
    ),
    Need(
        "ira_distributions",
        "Taxable IRA distributions",
        "ordinary income",
        "1099-R box 2a from each IRA custodian",
        "money",
        boxes=(("1099-R", "2a"),),
    ),
    Need(
        "social_security",
        "Social Security benefits",
        "up to 85% taxable; all of it counts in ACA MAGI",
        "SSA-1099 box 5 (ssa.gov/myaccount > replacement documents); type 0 "
        "before you claim",
        "money",
        boxes=(("SSA-1099", "5"),),
    ),
    Need(
        "roth_conversion",
        "Roth conversion this year",
        "the conversion ledger",
        "5498 box 3 (arrives in May) or your conversion confirmation",
        "money",
        boxes=(("5498", "3"),),
    ),
    Need(
        "traditional_ira_contribution",
        "Traditional IRA contribution",
        "the IRA deduction",
        "5498 box 1 or the custodian's confirmation",
        "money",
        boxes=(("5498", "1"),),
    ),
    Need(
        "hsa_contribution",
        "HSA contribution",
        "the HSA deduction",
        "5498-SA box 2",
        "money",
        boxes=(("5498-SA", "2"),),
    ),
    Need(
        "se_health_premiums",
        "Health premiums paid (self-employed)",
        "the SE health deduction and ACA reconciliation",
        "the marketplace or insurer billing statement",
        "money",
    ),
    Need(
        "slcsp_monthly",
        "Benchmark silver (SLCSP) premium, monthly",
        "the ACA credit",
        "1095-A column B (January) or healthcare.gov's tax tool",
        "money",
        boxes=(("1095-A", "slcsp_01"),),
    ),
)


def manual_path(lay: Layout, year: int) -> Path:
    return lay.data / "manual" / f"{year}.yaml"


def profile_path(lay: Layout) -> Path:
    return lay.data / "profile" / "assumptions.yaml"


def dont_have_path(lay: Layout) -> Path:
    return lay.data / "profile" / "dont_have.yaml"


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def load_manual(lay: Layout, year: int) -> dict[str, Any]:
    data = _read(manual_path(lay, year))
    data.setdefault(MANUAL_VALUES, {})
    data.setdefault(MANUAL_DONT_HAVE, [])
    return data


def load_profile(lay: Layout) -> dict[str, Any]:
    path = profile_path(lay)
    if not path.exists():
        example = lay.config / "assumptions.example.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return load_assumptions(path)


def account_need(number: str, death: bool = False) -> Need:
    """The two items an export cannot answer about an account it names: what
    kind of account it is, and for an inherited IRA, when the clock started."""
    if death:
        return Need(
            f"{ACCOUNT}{number}:death",
            f"Date of death for inherited IRA {number}",
            "the 10-year emptying deadline",
            "the inheritance paperwork or the custodian's beneficiary letter",
            "date",
            PROFILE,
        )
    return Need(
        f"{ACCOUNT}{number}",
        f"Account type for {number}",
        "which balances are spendable, locked or convertible",
        "the account's statement header: brokerage = taxable, traditional IRA, "
        "inherited IRA, Roth IRA, HSA, or a bank account = cash",
        "enum",
        PROFILE,
        choices=portfolio.TYPES,
    )


def need_for(key: str) -> Need:
    for n in NEEDS:
        if n.key == key:
            return n
    if key.startswith(ACCOUNT):
        number, _, tail = key[len(ACCOUNT) :].partition(":")
        if number and tail in ("", "death"):
            return account_need(number, death=tail == "death")
    raise KeyError(f"no such item: {key}")


def parse_value(need: Need, text: str) -> Any:
    """Typed, validated; a bad answer is an error, never a guess."""
    s = text.strip()
    if need.kind == "date":
        from datetime import date

        return date.fromisoformat(s).isoformat()
    if need.kind in ("int", "money"):
        cleaned = s.replace("$", "").replace(",", "")
        neg = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()")
        value = int(round(float(cleaned)))
        return -value if neg else value
    if need.kind == "fraction":
        v = float(s.rstrip("%"))
        v = v / 100 if s.endswith("%") or v > 1 else v
        if not 0 <= v <= 1:
            raise ValueError(f"{need.key}: a fraction between 0 and 1")
        return v
    if need.kind == "enum":
        low = s.lower()
        if low not in need.choices:
            raise ValueError(f"{need.key}: one of {', '.join(need.choices)}")
        return low
    return s


def enter(lay: Layout, year: int, key: str, text: str) -> Any:
    """Store one typed answer: profile keys go to the profile, others to the
    year's manual file. Returns the stored value."""
    need = need_for(key)
    value = parse_value(need, text)
    if key.startswith(ACCOUNT):
        number, _, tail = key[len(ACCOUNT) :].partition(":")
        portfolio.save_account(
            lay, number, **{"date_of_death" if tail else "type": value}
        )
    elif need.scope == PROFILE:
        path = profile_path(lay)
        load_profile(lay)
        data = _read(path)
        data[key] = value
        _write(path, {k: data.get(k) for k in ASSUMPTION_FIELDS})
        dh = _read(dont_have_path(lay))
        if key in dh.get(MANUAL_DONT_HAVE, []):
            dh[MANUAL_DONT_HAVE].remove(key)
            _write(dont_have_path(lay), dh)
    else:
        data = load_manual(lay, year)
        data[MANUAL_VALUES][key] = value
        if key in data[MANUAL_DONT_HAVE]:
            data[MANUAL_DONT_HAVE].remove(key)
        _write(manual_path(lay, year), data)
    return value


def dont_have(lay: Layout, year: int, key: str) -> None:
    need = need_for(key)
    path = dont_have_path(lay) if need.scope == PROFILE else manual_path(lay, year)
    data = _read(path) if need.scope == PROFILE else load_manual(lay, year)
    data.setdefault(MANUAL_DONT_HAVE, [])
    if key not in data[MANUAL_DONT_HAVE]:
        data[MANUAL_DONT_HAVE].append(key)
    _write(path, data)


def _sum_boxes(
    conn: sqlite3.Connection, boxes: tuple[tuple[str, str], ...], year: int | None
) -> tuple[float, str] | None:
    total = 0.0
    origins: list[str] = []
    for form, box in boxes:
        for f in db.facts_for(conn, year, form):
            if f.box == box:
                total += f.value
                origins.append(f"{f.form} {f.tax_year} {f.issuer}")
    if not origins:
        return None
    return total, "; ".join(dict.fromkeys(origins))


def needed(lay: Layout, year: int) -> NeedsReport:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return _needed(conn, lay, year)
    finally:
        conn.close()


def _needed(conn: sqlite3.Connection, lay: Layout, year: int) -> NeedsReport:
    profile = load_profile(lay)
    manual = load_manual(lay, year)
    profile_dh = set(_read(dont_have_path(lay)).get(MANUAL_DONT_HAVE, []))
    report = NeedsReport(year)
    for need in NEEDS:
        if need.scope == PROFILE and profile.get(need.key) is not None:
            report.items.append(Status(need, "actual", profile[need.key], "profile"))
            continue
        if need.key in manual[MANUAL_VALUES]:
            report.items.append(
                Status(need, "actual", manual[MANUAL_VALUES][need.key], "typed")
            )
            continue
        fact_year = (
            None if need.scope == PROFILE else year - 1 if need.scope == PRIOR else year
        )
        hit = _sum_boxes(conn, need.boxes, fact_year) if need.boxes else None
        if hit is not None:
            report.items.append(Status(need, "actual", hit[0], hit[1]))
            continue
        est = _sum_boxes(conn, need.estimate, fact_year) if need.estimate else None
        if est is not None:
            report.items.append(Status(need, "estimate", est[0], est[1]))
            continue
        dh = profile_dh if need.scope == PROFILE else set(manual[MANUAL_DONT_HAVE])
        report.items.append(Status(need, "dont_have" if need.key in dh else "missing"))
    accounts = portfolio.load_accounts(lay)
    for number in portfolio.seen_accounts(conn):
        entry = accounts.get(number, {})
        need = account_need(number)
        if entry.get("type"):
            report.items.append(Status(need, "actual", entry["type"], "accounts.yaml"))
        else:
            report.items.append(Status(need, "missing"))
        if entry.get("type") == "inherited_ira":
            need = account_need(number, death=True)
            death = entry.get("date_of_death")
            state = "actual" if death else "missing"
            report.items.append(
                Status(need, state, death, "accounts.yaml" if death else "")
            )
    return report


def need_value(conn: sqlite3.Connection, lay: Layout, year: int, key: str) -> Any:
    """One item's value as the Needed panel sees it (typed, form or estimate),
    or None when it is missing or marked don't-have."""
    for st in _needed(conn, lay, year).items:
        if st.need.key == key:
            return st.value if st.state in ("actual", "estimate") else None
    return None
