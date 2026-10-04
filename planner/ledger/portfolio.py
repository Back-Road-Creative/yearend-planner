"""Phase 3: the portfolio as the ledger sees it today.

Accounts come from the dropped exports (every account number in ``rows``) and
are typed once in ``data/profile/accounts.yaml`` through the Needed panel
(``planner enter account:<number> roth``); a balance is typed only for an
account no export covers (a bank account with no balance column). Balances are
the newest holdings snapshot per account, lots the newest cost-basis export.

Accessible money is what can be spent without a penalty: taxable and cash
balances, Roth contributions, and Roth conversions past their clock (the
earlier of January 1 five years after the conversion year and age 59½). The
rest is locked: traditional and inherited IRAs, the HSA, Roth earnings and
unseasoned conversions. The all-time high of the total is persisted so a
drawdown is measured from it, not from the last export.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from planner.ingest.derive import FORM as YTD_FORM
from planner.ingest.derive import Gap, gaps
from planner.ledger import db
from planner.paths import Layout

TYPES = ("taxable", "trad_ira", "inherited_ira", "roth", "hsa", "cash")
SPENDABLE = ("taxable", "cash")
UNKNOWN = "unknown"
INHERITED_YEARS = 10  # the SECURE Act window for a non-spouse inherited IRA
ACCOUNT_FIELDS = (
    "name",
    "type",
    "date_of_death",
    "annual_rmd",  # inherited IRA: the owner had begun RMDs, so yearly RMDs apply
    "balance",
    "balance_date",
)
YES = {"yes": True, "y": True, "true": True, "no": False, "n": False, "false": False}
YTD_INCOME = (
    "dividends",
    "interest",
    "capital_gain_distributions",
    "st_gain",
    "lt_gain",
)


def accounts_path(lay: Layout) -> Path:
    return lay.data / "profile" / "accounts.yaml"


def load_accounts(lay: Layout) -> dict[str, dict[str, Any]]:
    path = accounts_path(lay)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (
        {str(k): dict(v or {}) for k, v in data.items()}
        if isinstance(data, dict)
        else {}
    )


def save_account(lay: Layout, number: str, **fields: Any) -> dict[str, Any]:
    """Merge typed fields into one account's entry; the type must be one of
    ``TYPES`` and a date of death an ISO date. Returns the entry."""
    unknown = sorted(set(fields) - set(ACCOUNT_FIELDS))
    if unknown:
        raise ValueError(f"account {number}: unknown field(s) {', '.join(unknown)}")
    if fields.get("type") is not None and fields["type"] not in TYPES:
        raise ValueError(f"account {number}: type must be one of {', '.join(TYPES)}")
    rmd = fields.get("annual_rmd")
    if rmd is not None and not isinstance(rmd, bool):
        if str(rmd).strip().lower() not in YES:
            raise ValueError(f"account {number}: annual_rmd must be yes or no")
        fields["annual_rmd"] = YES[str(rmd).strip().lower()]
    if fields.get("date_of_death") is not None:
        fields["date_of_death"] = date.fromisoformat(
            str(fields["date_of_death"])
        ).isoformat()
    accounts = load_accounts(lay)
    entry = accounts.setdefault(number, {})
    entry.update({k: v for k, v in fields.items() if v is not None})
    path = accounts_path(lay)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(accounts, sort_keys=True), encoding="utf-8")
    return entry


def seen_accounts(conn: sqlite3.Connection) -> list[str]:
    return [
        str(r["account"])
        for r in conn.execute(
            "SELECT DISTINCT account FROM rows WHERE account != '' ORDER BY account"
        )
    ]


def untyped_accounts(conn: sqlite3.Connection, lay: Layout) -> list[str]:
    accounts = load_accounts(lay)
    return [a for a in seen_accounts(conn) if not accounts.get(a, {}).get("type")]


def account_balance_need(number: str) -> Any:
    """A money item for typing a balance no export carries (bank accounts)."""
    from planner.ingest.needs import PROFILE, Need

    return Need(
        f"balance:{number}",
        f"Balance of {number} ($)",
        "a bank export has no balance column",
        "the bank's statement or app",
        "money",
        PROFILE,
    )


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def accessible_date(converted: date, birth_date: str | None, access_age: float) -> date:
    """When a conversion's principal is penalty-free: January 1 of the fifth
    year after the conversion year, or the IRA access age, whichever is first."""
    clock = date(converted.year + 5, 1, 1)
    if not birth_date:
        return clock
    at_age = add_months(date.fromisoformat(birth_date), round(access_age * 12))
    return min(clock, at_age)


@dataclass(frozen=True)
class Position:
    account: str
    name: str
    type: str
    value: float
    as_of: str
    origin: str  # "vanguard_holdings file.csv" or "typed"


@dataclass(frozen=True)
class Lot:
    account: str
    symbol: str
    acquired: str
    term: str
    quantity: float
    basis: float
    value: float
    name: str = ""  # the fund's name as the brokerage file gives it

    @property
    def gain(self) -> float:
        return round(self.value - self.basis, 2)


@dataclass
class Status:
    as_of: str
    year: int
    positions: list[Position] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    total: float = 0.0
    accessible: float = 0.0
    locked: float = 0.0
    untyped: float = 0.0
    roth_balance: float = 0.0
    roth_contributions: float | None = None
    seasoned: float = 0.0
    unseasoned: float = 0.0
    conversions: list[db.Conversion] = field(default_factory=list)
    ytd_income: dict[str, float] = field(default_factory=dict)
    carryforward: float | None = None
    peak: float = 0.0
    peak_date: str = ""
    inherited: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # acct, death, by
    annual_rmd: dict[str, bool] = field(default_factory=dict)  # inherited acct
    gaps: list[Gap] = field(default_factory=list)  # YTD vs the filed 1099s
    notes: list[str] = field(default_factory=list)

    @property
    def unrealized(self) -> tuple[float, float]:
        short = sum(lot.gain for lot in self.lots if lot.term == "short")
        long = sum(lot.gain for lot in self.lots if lot.term != "short")
        return round(short, 2), round(long, 2)


def rmd_text(st: Status, account: str) -> str:
    """An inherited IRA's yearly-RMD answer in words ("" when not entered)."""
    if account not in st.annual_rmd:
        return ""
    return "yearly RMDs apply" if st.annual_rmd[account] else "no yearly RMDs"


@dataclass(frozen=True)
class Layer:
    """One slice of the Roth in the order a withdrawal takes it."""

    kind: str  # contributions | conversion | earnings
    amount: float
    free_from: str  # a conversion's penalty-free date; "" for the others
    label: str

    @property
    def taxed(self) -> bool:
        """Only earnings are income, and only in a nonqualified withdrawal."""
        return self.kind == "earnings"

    def penalty_free(self, as_of: str) -> bool:
        """Contributions always; a conversion once its date passes; earnings
        never before a qualified withdrawal (59 1/2 and 5 years)."""
        if self.kind == "contributions":
            return True
        return self.kind == "conversion" and self.free_from <= as_of


def roth_layers(st: Status) -> list[Layer]:
    """The Roth in withdrawal order (Pub. 590-B ordering rules): contributions,
    then each conversion oldest first, then earnings. The balance caps it: a
    layer the balance does not reach is not there. Unknown contributions count
    as none, so the order errs toward the taxed end."""
    left = st.roth_balance
    out: list[Layer] = []

    def take(kind: str, amount: float, free_from: str, label: str) -> None:
        nonlocal left
        use = round(min(amount, left), 2)
        if use > 0:
            out.append(Layer(kind, use, free_from, label))
            left = round(left - use, 2)

    take(
        "contributions",
        st.roth_contributions or 0.0,
        "",
        "contributions: no tax, no penalty",
    )
    for c in sorted(st.conversions, key=lambda c: (c.date, c.id)):
        take(
            "conversion",
            c.amount,
            c.accessible_date,
            f"conversion {c.date} from {c.source_account}: no tax; "
            f"penalty-free {c.accessible_date}",
        )
    take(
        "earnings",
        left,
        "",
        "earnings: income and a 10% penalty unless qualified (59 1/2 and 5 years)",
    )
    return out


def roth_withdrawal(st: Status, amount: float) -> list[tuple[Layer, float]]:
    """The layers a withdrawal of ``amount`` comes from, and how much of each."""
    parts: list[tuple[Layer, float]] = []
    left = round(amount, 2)
    for layer in roth_layers(st):
        if left <= 0:
            break
        use = round(min(layer.amount, left), 2)
        parts.append((layer, use))
        left = round(left - use, 2)
    return parts


def positions(conn: sqlite3.Connection, lay: Layout) -> list[Position]:
    accounts = load_accounts(lay)
    out: list[Position] = []
    by_account: dict[str, list[db.LedgerRow]] = {}
    for r in db.latest_rows(conn, "holding"):
        by_account.setdefault(r.account, []).append(r)
    for number, rows in by_account.items():
        entry = accounts.get(number, {})
        out.append(
            Position(
                number,
                str(entry.get("name", "")),
                str(entry.get("type", UNKNOWN)),
                db.from_cents(sum(r.amount_cents or 0 for r in rows)),
                max(r.date or "" for r in rows),
                f"{rows[0].source} {rows[0].file_name}",
            )
        )
    for number in sorted(set(seen_accounts(conn)) | set(accounts)):
        entry = accounts.get(number, {})
        if number in by_account or entry.get("balance") is None:
            continue
        out.append(
            Position(
                number,
                str(entry.get("name", "")),
                str(entry.get("type", UNKNOWN)),
                float(entry["balance"]),
                str(entry.get("balance_date", "")),
                "typed",
            )
        )
    return sorted(out, key=lambda p: p.account)


def lots(conn: sqlite3.Connection) -> list[Lot]:
    return [
        Lot(
            r.account,
            r.symbol,
            r.acquired or "",
            r.term or "",
            r.quantity or 0.0,
            db.from_cents(r.basis_cents or 0),
            db.from_cents(r.amount_cents or 0),
            r.description,
        )
        for r in db.latest_rows(conn, "lot")
    ]


def _roth_contributions(
    conn: sqlite3.Connection, profile: dict[str, Any]
) -> float | None:
    typed = profile.get("roth_basis_contributions")
    if typed is not None:
        return float(typed)
    facts = [f for f in db.facts_for(conn, None, "5498") if f.box == "10"]
    return round(sum(f.value for f in facts), 2) if facts else None


def status(lay: Layout, year: int, as_of: date | None = None) -> Status:
    from planner.ingest.needs import load_profile, need_value

    today = (as_of or date.today()).isoformat()
    profile = load_profile(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        st = Status(today, year, positions(conn, lay), lots(conn))
        st.conversions = db.conversions(conn)
        st.seasoned = round(
            sum(c.amount for c in st.conversions if c.accessible_date <= today), 2
        )
        st.unseasoned = round(sum(c.amount for c in st.conversions) - st.seasoned, 2)
        st.roth_contributions = _roth_contributions(conn, profile)
        st.roth_balance = round(
            sum(p.value for p in st.positions if p.type == "roth"), 2
        )
        spendable = sum(p.value for p in st.positions if p.type in SPENDABLE)
        roth_basis = (st.roth_contributions or 0.0) + st.seasoned
        st.total = round(sum(p.value for p in st.positions), 2)
        st.untyped = round(sum(p.value for p in st.positions if p.type == UNKNOWN), 2)
        st.accessible = round(spendable + min(st.roth_balance, roth_basis), 2)
        st.locked = round(st.total - st.accessible - st.untyped, 2)
        st.ytd_income = {
            f.box: f.value
            for f in db.facts_for(conn, year, YTD_FORM)
            if f.box in YTD_INCOME
        }
        st.gaps = gaps(conn, year)
        st.carryforward = need_value(conn, lay, year, "prior_capital_loss_carryforward")
        peak_cents, st.peak_date = db.record_peak(conn, db.to_cents(st.total), today)
        st.peak = db.from_cents(peak_cents)
        for number, entry in sorted(load_accounts(lay).items()):
            if entry.get("type") != "inherited_ira":
                continue
            death = entry.get("date_of_death")
            if death is None:
                st.notes.append(f"inherited IRA {number}: date of death not entered")
                continue
            by = date(date.fromisoformat(str(death)).year + INHERITED_YEARS, 12, 31)
            st.inherited.append((number, str(death), by.isoformat()))
            if "annual_rmd" in entry:
                st.annual_rmd[number] = bool(entry["annual_rmd"])
            else:
                st.notes.append(
                    f"inherited IRA {number}: whether yearly RMDs apply (the owner "
                    f"had begun RMDs) not entered (planner account {number} "
                    "--annual-rmd or --no-annual-rmd)"
                )
        untyped = [p.account for p in st.positions if p.type == UNKNOWN]
        if untyped:
            st.notes.append(
                f"{len(untyped)} account(s) of unknown type: {', '.join(untyped)} "
                f"(planner needed --year {year})"
            )
        if st.roth_balance and st.roth_contributions is None:
            st.notes.append(
                "Roth contributions unknown; the Roth balance counts as locked "
                f"(planner needed --year {year})"
            )
    finally:
        conn.close()
    return st
