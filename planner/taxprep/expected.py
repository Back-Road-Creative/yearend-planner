"""The forms the year should produce, predicted from the ledger, and which of
them have arrived.

Four sources of expectation, each named on the item: last year's issuers (a
payer who sent a form last year usually sends one again), the accounts (a
taxable account with dividends, interest or sales means a consolidated 1099
from that institution; an IRA with a withdrawal or conversion means a 1099-R),
the answers in the Needed panel (wages mean a W-2, SE income a 1099-NEC from
each client who sends one, interest and dividends a 1099-INT and 1099-DIV, a
marketplace premium a 1095-A, a mortgage a 1098, a Social Security claim age
reached an SSA-1099) and anything that arrived unpredicted. Each item is
received, superseded (a corrected copy is due) or still expected, with the date
the issuer owes it to you and where to download it. A form that is late and
needed to file is a Needed-panel item.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from planner.ingest.needs import DOCS, load_profile, need_value
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.plan import calendar

RECEIVED = "received"
SUPERSEDED = "superseded"
EXPECTED = "expected"
# Information returns: what an issuer sends you (the 1040, D-400 and the SSA
# statement are read too, but nobody owes them to you).
INFO = (
    "W-2",
    "1099-NEC",
    "1099-K",
    "1099-R",
    "1099-INT",
    "1099-DIV",
    "1099-B",
    "1099-SA",
    "5498",
    "5498-SA",
    "1095-A",
    "1098",
    "SSA-1099",
)
# Month and day the issuer must furnish it (IRC 6041-6050 and the 1095-A rule
# in 36B(f)(3)); a broker's consolidated statement has until February 15 (IRC
# 6045(b)), so dividends and interest use the later date and nothing is called
# late early. The 5498 forms come after the filing deadline.
DUE = {
    "1099-B": (2, 15),
    "1099-DIV": (2, 15),
    "1099-INT": (2, 15),
    "5498": (5, 31),
    "5498-SA": (5, 31),
}
JAN_31 = (1, 31)
AFTER_FILING = ("5498", "5498-SA")
WHERE = {
    "W-2": DOCS["w2"].path,
    "1099-NEC": "each client's payment portal or email; ask any client who paid "
    "you and sent none",
    "1099-K": "the payment processor's dashboard (tax forms)",
    "1095-A": DOCS["f1095a"].path,
    "1098": DOCS["f1098"].path,
    "SSA-1099": DOCS["ssa_1099"].path,
}
INSTITUTION = "the institution's tax center (Vanguard: My Accounts > Tax center)"
# A placeholder issuer matches any issuer of that form.
ANY = (
    "your employer",
    "each client",
    "the marketplace",
    "your loan servicer",
    "your HSA custodian",
    "each payer",
)
TRANSACTION_FORMS = (
    (re.compile(r"dividend|capital gain", re.I), "1099-DIV"),
    (re.compile(r"interest", re.I), "1099-INT"),
    (re.compile(r"\bsell|\bsold|redemption|exchange", re.I), "1099-B"),
)
IRA_OUT = re.compile(r"distribution|withdrawal|conversion|rmd", re.I)
CONTRIBUTION = re.compile(r"contribution", re.I)
IRA_TYPES = ("trad_ira", "inherited_ira", "roth")


@dataclass
class Expected:
    form: str
    issuer: str
    reason: str
    due: str
    to_file: bool = True
    state: str = EXPECTED
    documents: list[str] = field(default_factory=list)
    late: bool = False

    @property
    def where(self) -> str:
        return WHERE.get(self.form, INSTITUTION)


@dataclass
class Inventory:
    year: int
    as_of: str
    items: list[Expected] = field(default_factory=list)

    @property
    def outstanding(self) -> list[Expected]:
        return [e for e in self.items if e.state != RECEIVED and e.to_file]

    @property
    def late(self) -> list[Expected]:
        return [e for e in self.outstanding if e.late]


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def same_issuer(expected: str, actual: str) -> bool:
    if expected in ANY:
        return True
    a, b = _norm(expected), _norm(actual)
    if not a or not b:
        return False
    return a in b or b in a or a.split()[0] == b.split()[0]


def due_date(form: str, year: int) -> date:
    month, day = DUE.get(form, JAN_31)
    return calendar.shift(date(year + 1, month, day))


def _institution(source: str) -> str:
    return source.split("_")[0]


def _add(out: list[Expected], year: int, form: str, issuer: str, reason: str) -> None:
    for e in out:
        if e.form == form and (
            same_issuer(e.issuer, issuer) or same_issuer(issuer, e.issuer)
        ):
            if reason not in e.reason:
                e.reason += f"; {reason}"
            return
    out.append(
        Expected(
            form,
            issuer,
            reason,
            due_date(form, year).isoformat(),
            to_file=form not in AFTER_FILING,
        )
    )


def _from_last_year(conn: sqlite3.Connection, year: int, out: list[Expected]) -> None:
    seen = {(f.form, f.issuer) for f in db.facts_for(conn, year - 1) if f.form in INFO}
    for form, issuer in sorted(seen):
        _add(out, year, form, issuer, f"sent one for {year - 1}")


def _from_accounts(
    conn: sqlite3.Connection, lay: Layout, year: int, out: list[Expected]
) -> None:
    typed = {k: v.get("type") for k, v in portfolio.load_accounts(lay).items()}
    for row in db.rows_for(conn, tax_year=year):
        kind = typed.get(row.account)
        who = _institution(row.source)
        text = f"{row.type} {row.description}"
        if kind == "taxable":
            if row.kind == "realized":
                _add(out, year, "1099-B", who, f"sales in {row.account}")
                continue
            if row.kind not in ("income", "transaction"):
                continue
            if re.search("reinvest", row.type, re.I):
                continue
            for pattern, form in TRANSACTION_FORMS:
                if pattern.search(text):
                    _add(out, year, form, who, f"{row.type.lower()} in {row.account}")
                    break
        elif kind in IRA_TYPES and row.kind == "transaction":
            if IRA_OUT.search(text):
                _add(out, year, "1099-R", who, f"money out of {row.account}")
            elif CONTRIBUTION.search(text):
                _add(out, year, "5498", who, f"contribution to {row.account}")
        elif kind == "hsa" and row.kind == "transaction":
            if IRA_OUT.search(text):
                _add(out, year, "1099-SA", who, f"money out of {row.account}")
            elif CONTRIBUTION.search(text):
                _add(out, year, "5498-SA", who, f"contribution to {row.account}")
    sources = {r.account: _institution(r.source) for r in db.rows_for(conn)}
    for conv in db.conversions(conn):
        if conv.date.startswith(str(year)):
            who = sources.get(conv.source_account, conv.source_account)
            _add(out, year, "1099-R", who, f"Roth conversion {conv.date}")


def _positive(conn: sqlite3.Connection, lay: Layout, year: int, key: str) -> bool:
    value = need_value(conn, lay, year, key)
    return value is not None and float(value) > 0


def _from_answers(
    conn: sqlite3.Connection, lay: Layout, year: int, out: list[Expected]
) -> None:
    profile = load_profile(lay)
    if _positive(conn, lay, year, "wages"):
        _add(out, year, "W-2", "your employer", "wages this year")
    if _positive(conn, lay, year, "se_income"):
        _add(
            out,
            year,
            "1099-NEC",
            "each client",
            "self-employment income (Schedule C counts every receipt, form or not)",
        )
    if _positive(conn, lay, year, "interest"):
        _add(out, year, "1099-INT", "each payer", "interest income")
    if _positive(conn, lay, year, "ordinary_dividends"):
        _add(out, year, "1099-DIV", "each payer", "dividend income")
    if any(_positive(conn, lay, year, k) for k in ("premium_monthly", "slcsp_monthly")):
        _add(out, year, "1095-A", "the marketplace", "marketplace coverage")
    if _positive(conn, lay, year, "mortgage_monthly"):
        _add(out, year, "1098", "your loan servicer", "mortgage payments")
        for e in out:
            if e.form == "1098":
                e.to_file = False  # used only when itemizing beats the standard
    if profile.get("hsa_coverage") in ("self", "family"):
        _add(out, year, "5498-SA", "your HSA custodian", "HSA-eligible coverage")
    birth, claim = profile.get("birth_date"), profile.get("ss_claim_age")
    if birth and claim is not None:
        born = date.fromisoformat(str(birth))
        if year - born.year >= float(claim):
            _add(
                out,
                year,
                "SSA-1099",
                "Social Security Administration",
                f"claim age {claim} reached",
            )


def _match(items: list[Expected], facts: Iterable[db.FactRow], state: str) -> None:
    for f in facts:
        for e in items:
            if e.form == f.form and same_issuer(e.issuer, f.issuer):
                if e.state != RECEIVED:
                    e.state = state
                if f.file_name not in e.documents:
                    e.documents.append(f.file_name)
                break


def inventory(lay: Layout, year: int, as_of: date | None = None) -> Inventory:
    today = as_of or date.today()
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        items: list[Expected] = []
        _from_last_year(conn, year, items)
        _from_accounts(conn, lay, year, items)
        _from_answers(conn, lay, year, items)
        accepted = [f for f in db.facts_for(conn, year) if f.form in INFO]
        old = [f for f in db.facts_for(conn, year, status=SUPERSEDED) if f.form in INFO]
    finally:
        conn.close()
    _match(items, old, SUPERSEDED)
    _match(items, accepted, RECEIVED)
    for f in accepted:
        if not any(e.form == f.form and same_issuer(e.issuer, f.issuer) for e in items):
            _add(items, year, f.form, f.issuer, "arrived")
            _match(items, [f], RECEIVED)
    for e in items:
        e.late = e.state != RECEIVED and today > date.fromisoformat(e.due)
    items.sort(key=lambda e: (e.state == RECEIVED, e.due, e.form, e.issuer))
    return Inventory(year, today.isoformat(), items)


def lines(inv: Inventory) -> list[str]:
    out = []
    for e in inv.items:
        tag = "LATE" if e.late else e.state
        when = "" if e.state == RECEIVED else f" due {e.due}"
        extra = "" if e.to_file else " (not needed to file)"
        docs = f": {', '.join(e.documents)}" if e.documents else ""
        out.append(f"{tag:10} {e.form:9} {e.issuer}{when}{extra}{docs}  [{e.reason}]")
    left = len(inv.outstanding)
    out.append(
        "every expected form is in" if not left else f"{left} form(s) still to come"
    )
    return out


def render(inv: Inventory) -> str:
    out = [f"Forms for {inv.year} (as of {inv.as_of})", *lines(inv)]
    for e in inv.outstanding:
        out.append(f"  {e.form} from {e.issuer}: {e.where}")
    return "\n".join(out) + "\n"
