"""Schedule E Part I, rental real estate and royalties (unit 3e-2b).

Line numbers follow the 2025 Schedule E (Form 1040) and its instructions. A
property with personal use follows Pub. 527 (2025) chapter 5: expenses are
split by rental days over rental plus personal days (Worksheet 5-1 line F); a
dwelling is used as a home when personal days pass the greater of 14 and 10% of
the fair rental days (Schedule E instructions, line 2); a home rented under 15
days reports neither rents nor expenses, and one rented 15 days or more limits
the operating expenses and depreciation to the rents (Worksheet 5-1). A rental
not used as a home is passive: its loss, and its prior-year unallowed loss,
is limited by Form 8582 (planner.taxprep.f8582), each property its own
activity, and line 22 takes what Part VIII allows. A royalty, or a dwelling
used as a home, is not passive (line 22 instructions).

Each property is typed as ``rental rents 18000 mortgage 4000 taxes 2000
expenses 3000 depreciation 3000 days 300 personal 0`` or ``royalty royalties
1200 expenses 100 depletion 50``, separated by semicolons, lettered A, B, C in
the order typed. ``expenses`` is the rest of lines 5-11, 13-15, 17 and 19 as
one figure (line 19 here); ``direct`` is a rental-only expense (advertising,
agent fees) that is not split by days; ``carryover`` and ``carrydep`` are last
year's Worksheet 5-1 lines 7a and 7b; ``prior`` is the property's prior-year
unallowed passive loss (last year's Form 8582 Part VII column (c)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from planner.taxprep import f8582

FORM = "Sch E"
EXAMPLE = (
    "rental rents 18000 mortgage 4000 taxes 2000 expenses 3000 depreciation "
    "3000 days 300 personal 0; royalty royalties 1200"
)
WORDS = {
    "rental": (
        "rents",
        "mortgage",
        "taxes",
        "expenses",
        "direct",
        "depreciation",
        "carryover",
        "carrydep",
        "prior",
        "days",
        "personal",
    ),
    "royalty": ("royalties", "expenses", "depletion"),
}
REQUIRED = {"rental": ("rents", "days", "personal"), "royalty": ("royalties",)}
LETTERS = "ABCDEFGHI"  # three properties to a page
MONEY_MAX = 100_000_000


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each property as a dict of its typed words, or [] for ``none``."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";") if e.strip()):
        kind, *rest = entry.split()
        kind = kind.lower()
        if kind not in WORDS:
            raise ValueError(
                f"{key}: {entry!r} starts with rental or royalty (like {EXAMPLE}), "
                "or type none"
            )
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} pairs each word with an amount")
        got: dict[str, Any] = {"kind": kind}
        for word, amount in zip(rest[::2], rest[1::2], strict=True):
            word = word.lower()
            if word not in WORDS[kind]:
                raise ValueError(
                    f"{key}: {word!r} is not a {kind} word ({', '.join(WORDS[kind])})"
                )
            if word in got:
                raise ValueError(f"{key}: {word} is typed twice in {entry!r}")
            try:
                v = float(amount.replace(",", "").lstrip("$"))
            except ValueError:
                raise ValueError(f"{key}: {word} {amount!r} is not a number") from None
            if v < 0 or v > MONEY_MAX:
                raise ValueError(f"{key}: {word} {amount} is out of range")
            got[word] = int(round(v))
        missing = [w for w in REQUIRED[kind] if w not in got]
        if missing:
            raise ValueError(
                f"{key}: {entry!r} needs {', '.join(missing)} (like {EXAMPLE})"
            )
        if kind == "rental" and got["days"] + got["personal"] > 366:
            raise ValueError(f"{key}: days plus personal days pass 366 in {entry!r}")
        out.append(got)
    if len(out) > len(LETTERS):
        raise ValueError(f"{key}: at most {len(LETTERS)} properties")
    return out


@dataclass
class Column:
    """One property's Schedule E column, before the passive-loss limit."""

    letter: str
    kind: str
    home: bool = False  # a dwelling used as a home (Pub. 527 ch. 5)
    excluded: bool = False  # a home rented under 15 days: nothing reported
    worksheet: bool = False  # Worksheet 5-1 limited the expenses
    lines: dict[str, float] = field(default_factory=dict)  # 3, 4, 12, 16, ... 21
    source: dict[str, str] = field(default_factory=dict)
    carry: tuple[float, float] = (0.0, 0.0)  # Worksheet 5-1 lines 7a, 7b
    personal: tuple[float, float] = (0.0, 0.0)  # personal mortgage, taxes
    prior: float = 0.0  # prior-year unallowed passive loss

    @property
    def name(self) -> str:
        return f"rental {self.letter}"

    @property
    def passive(self) -> bool:
        return self.kind == "rental" and not self.home and not self.excluded

    @property
    def net(self) -> float:
        return self.lines.get("21", 0.0)


def _rental(c: Column, p: dict[str, Any]) -> None:
    def get(word: str) -> float:
        return float(p.get(word, 0))

    days, personal = p["days"], p["personal"]
    share = days / (days + personal) if days + personal else 1.0
    f = f"x {days}/{days + personal} rental days" if personal else ""
    c.home = personal > max(14, 0.10 * days)
    c.personal = (get("mortgage") * (1 - share), get("taxes") * (1 - share))
    if c.home and days < 15:
        c.excluded = True
        c.personal = (get("mortgage"), get("taxes"))
        return
    mortgage, taxes = get("mortgage") * share, get("taxes") * share
    operating, dep = get("expenses") * share, get("depreciation") * share
    rents, direct = get("rents"), get("direct")
    other_src = f"expenses {f}".strip() + (" + direct" if direct else "")
    dep_src = f"depreciation {f}".strip()
    over = mortgage + taxes + direct + operating + dep > rents
    if c.home and (over or p.get("carryover") or p.get("carrydep")):
        # Worksheet 5-1: interest, taxes and direct expenses first (2e), then
        # operating expenses up to what is left (4f), then depreciation (6e).
        left = max(rents - (mortgage + taxes + direct), 0.0)  # line 3
        ops_all = operating + get("carryover")  # line 4e (4b-4c not split out)
        ops = min(left, ops_all)  # line 4f
        dep_all = dep + get("carrydep")  # line 6d
        dep_ok = min(left - ops, dep_all)  # line 6e
        c.carry = (ops_all - ops, dep_all - dep_ok)  # lines 7a, 7b
        c.worksheet = True
        operating, dep = ops, dep_ok
        other_src = "Worksheet 5-1 lines 2d + 4f"
        dep_src = "Worksheet 5-1 line 6e"
    c.lines = {
        "3": rents,
        "12": mortgage,
        "16": taxes,
        "18": dep,
        "19": operating + direct,
    }
    c.source = {
        "3": "rents",
        "12": f"mortgage {f}".strip(),
        "16": f"taxes {f}".strip(),
        "18": dep_src,
        "19": other_src,
    }


def columns(entries: list[dict[str, Any]]) -> list[Column]:
    """Lines 3-21 of each typed property."""
    out = []
    for letter, p in zip(LETTERS, entries, strict=False):
        c = Column(letter, p["kind"])
        if c.kind == "rental":
            _rental(c, p)
            c.prior = float(p.get("prior", 0))
        else:
            c.lines = {
                "4": float(p["royalties"]),
                "18": float(p.get("depletion", 0)),
                "19": float(p.get("expenses", 0)),
            }
            c.source = {"4": p.get("source", "royalties")}
            c.source |= {"18": "depletion", "19": "expenses"}
        if not c.excluded:
            income = c.lines.get("3", 0.0) + c.lines.get("4", 0.0)
            c.lines["20"] = sum(
                c.lines[k] for k in ("12", "16", "18", "19") if k in c.lines
            )
            c.lines["21"] = income - c.lines["20"]
        out.append(c)
    return out


def passive_net(cols: list[Column]) -> float:
    """The passive rentals' line 21s combined, before prior-year losses."""
    return sum(c.net for c in cols if c.passive)


def magi_part(cols: list[Column]) -> float:
    """What Schedule E adds to Form 8582 line 6 modified AGI: every nonpassive
    line 21 and passive net income, never a passive loss."""
    return sum(c.net for c in cols if not c.passive) + max(passive_net(cols), 0.0)


@dataclass
class Result:
    lines: list[tuple[str, str, float, str]]  # (line, label, value, source)
    total: float  # line 26, onto Schedule 1 line 5 (or line 41)
    notes: list[str]


LABELS = {
    "3": "Rents received",
    "4": "Royalties received",
    "12": "Mortgage interest paid to banks, etc.",
    "16": "Taxes",
    "18": "Depreciation expense or depletion",
    "19": "Other",
    "20": "Total expenses",
    "21": "Income or (loss)",
    "22": "Deductible rental real estate loss after limitation",
}


def activities(cols: list[Column], *, active: bool) -> list[f8582.Activity]:
    """Each passive property as a Form 8582 activity: Part IV when you actively
    participated (``active``), Part V otherwise."""
    return [
        f8582.Activity(
            c.name,
            f"Sch E, line 22{c.letter}",
            income=max(c.net, 0.0),
            loss=max(-c.net, 0.0),
            prior=c.prior,
            active=active,
        )
        for c in cols
        if c.passive and (c.net or c.prior)
    ]


def schedule(cols: list[Column], allowed: dict[str, float]) -> Result:
    """Lines 3-26. ``allowed`` is each passive property's loss Form 8582
    allows (planner.taxprep.f8582.Result.allowed), by its name."""
    notes: list[str] = []
    lines: list[tuple[str, str, float, str]] = []
    total_in = total_out = 0.0
    for c in cols:
        if c.excluded:
            notes.append(
                f"Schedule E property {c.letter} is a home rented under 15 days: "
                "the rents are not income and no rental expense is deducted "
                "(Pub. 527 ch. 5); its mortgage interest and taxes are Schedule A "
                "items"
            )
            continue
        for ln in ("3", "4", "12", "16", "18", "19", "20", "21"):
            if ln in c.lines and (c.lines[ln] or ln in ("3", "4", "20", "21")):
                src = c.source.get(ln, {"20": "12 + 16 + 18 + 19"}.get(ln, ""))
                if ln == "21":
                    src = ("3" if c.kind == "rental" else "4") + " - 20"
                lines.append((ln + c.letter, LABELS[ln], c.lines[ln], src))
        if c.net >= 0:
            total_in += c.net
        if c.passive and (c.net < 0 or c.prior):
            l22 = -allowed.get(c.name, 0.0)
            lines.append(
                (
                    "22" + c.letter,
                    LABELS["22"],
                    l22,
                    "Form 8582 Part VIII column (c)"
                    + (" (with its prior-year loss)" if c.prior else ""),
                )
            )
            total_out += l22
        elif c.net >= 0:
            continue
        elif c.kind == "rental":  # a home's loss is not passive
            lines.append(
                ("22" + c.letter, LABELS["22"], c.net, "line 21 (not passive: a home)")
            )
            total_out += c.net
        else:
            total_out += c.net  # a royalty loss goes straight to line 25
    for c in cols:
        if any(c.carry):
            notes.append(
                f"Schedule E property {c.letter}: carry {c.carry[0]:,.2f} of "
                f"operating expenses and {c.carry[1]:,.2f} of depreciation to next "
                "year's Worksheet 5-1 (lines 7a, 7b)"
            )
        if any(c.personal) and not c.excluded:
            notes.append(
                f"Schedule E property {c.letter}: the personal-use share of its "
                f"mortgage interest ({c.personal[0]:,.2f}) and taxes "
                f"({c.personal[1]:,.2f}) is not a rental expense; it is a Schedule "
                "A item only where Pub. 527 ch. 5 and Pub. 936 allow"
            )
    kept = [c for c in cols if not c.excluded]
    for ln, label, key in (
        ("23a", "Total of line 3, rental properties", "3"),
        ("23b", "Total of line 4, royalty properties", "4"),
        ("23c", "Total of line 12", "12"),
        ("23d", "Total of line 18", "18"),
        ("23e", "Total of line 20", "20"),
    ):
        lines.append(
            (ln, label, sum(c.lines.get(key, 0.0) for c in kept), f"line {key}, all")
        )
    lines.append(("24", "Income", total_in, "positive line 21s"))
    lines.append(("25", "Losses", total_out, "royalty losses on 21 + line 22s"))
    total = total_in + total_out
    lines.append(
        (
            "26",
            "Total rental real estate and royalty income or (loss)",
            total,
            "24 + 25",
        )
    )
    if any(c.worksheet and (c.lines["12"] or c.lines["16"]) for c in kept):
        notes.append(
            "Worksheet 5-1 takes the rental share of mortgage interest and taxes on "
            "lines 2a-2b, the itemizer's path; with the standard deduction they move "
            "to lines 4b-4c and are limited with the operating expenses"
        )
    if len(kept) > 3:
        notes.append(
            "Schedule E: properties past C go on a second page (lines 1-22 only)"
        )
    return Result(lines, total, notes)
