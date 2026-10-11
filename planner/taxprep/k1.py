"""Schedule K-1 from partnerships (Form 1065), S corporations (Form 1120-S)
and estates and trusts (Form 1041), onto Schedule E Parts II and III (unit
3e-3a).

Boxes and where they go follow the 2025 Schedules K-1 and the Partner's,
Shareholder's and Beneficiary's Instructions:

- Ordinary business income (1065 and 1120-S box 1, 1041 box 6) is passive
  unless you materially participated.
- Net rental real estate (box 2; 1041 box 7) and other rental income (box 3;
  1041 box 8) are passive for everyone but a real estate professional.
- Guaranteed payments (1065 box 4c) are nonpassive income, column (k).
- The section 179 deduction (1065 box 12, 1120-S box 11) is column (j), or a
  passive deduction through Form 8582.
- A trust's other portfolio and nonbusiness income (1041 box 5) is column (f).
  Its directly apportioned deductions (box 9, codes A-C) are a deduction in
  column (e), or a passive one in column (c).
- Net earnings from self-employment (1065 box 14, code A) go to Schedule SE
  line 2.
- Portfolio income is never passive (IRC 469(e)(1)) and stays off Schedule E
  Parts II and III: interest (1065 box 5, 1120-S box 4, 1041 box 1) goes to
  Schedule B line 1, ordinary and qualified dividends (1065 6a-6b, 1120-S
  5a-5b, 1041 2a-2b) to Schedule B line 5 and Form 1040 line 3a, royalties
  (1065 box 7, 1120-S box 6) to Schedule E line 4, and the net short- and
  long-term capital gains (1065 boxes 8 and 9a, 1120-S 7 and 8a, 1041 3 and 4a)
  to Schedule D lines 5 and 12 (unit 3e-3b). The collectibles (28%) gain
  (1065 box 9b, 1120-S 8b, 1041 4b) is line 4 of the 28% Rate Gain Worksheet
  and a trust's unrecaptured section 1250 gain (1041 box 4c) line 11 of the
  Unrecaptured Section 1250 Gain Worksheet (Schedule D lines 18 and 19, unit
  3e-6b2); a partnership's or S corporation's (1065 9c, 1120-S 8c) is line 5,
  through Form 4797, and is not taken.
- The section 199A statement (1065 box 20 code Z, 1120-S box 17 code V, 1041
  box 14 code I) gives the qualified business income, W-2 wages and UBIA.

Each K-1's passive boxes are one activity for Form 8582 (planner.taxprep.f8582),
in Part V; ``active`` after passive or nonpassive puts a partnership or S
corporation K-1 whose only passive box is 2 (rental real estate, you a general
partner or shareholder who actively participated) in Part IV. ``prior`` is the
activity's prior-year unallowed loss (last year's Form 8582 Part VII column
(c)). Column (g) or (c) takes what Part VIII allows.

Each K-1 is typed as ``partnership passive ordinary -4000 rental 1200``,
``scorp nonpassive ordinary 30000 section179 2000 qbi 30000`` or ``trust
passive ordinary 800 portfolio 300``. Separate the K-1s with semicolons; each
kind is lettered A, B, C in the order typed. A leading ``spouse`` puts the
self-employment earnings on the spouse's Schedule SE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from planner.taxprep import f8582

EXAMPLE = (
    "partnership passive ordinary -4000 rental 1200; scorp nonpassive ordinary "
    "30000 section179 2000 qbi 30000; trust passive ordinary 800 portfolio 300"
)
FORMS = {"partnership": "1065", "scorp": "1120-S", "trust": "1041"}
# Each word's box on that kind's K-1
BOXES = {
    "partnership": {
        "ordinary": "1",
        "rental": "2",
        "otherrental": "3",
        "guaranteed": "4c",
        "section179": "12",
        "se": "14A",
        "interest": "5",
        "dividends": "6a",
        "qualified": "6b",
        "royalties": "7",
        "stgain": "8",
        "ltgain": "9a",
        "collectibles": "9b",
        "qbi": "20Z",
        "w2wages": "20Z",
        "ubia": "20Z",
    },
    "scorp": {
        "ordinary": "1",
        "rental": "2",
        "otherrental": "3",
        "section179": "11",
        "interest": "4",
        "dividends": "5a",
        "qualified": "5b",
        "royalties": "6",
        "stgain": "7",
        "ltgain": "8a",
        "collectibles": "8b",
        "qbi": "17V",
        "w2wages": "17V",
        "ubia": "17V",
    },
    "trust": {
        "portfolio": "5",
        "ordinary": "6",
        "rental": "7",
        "otherrental": "8",
        "deductions": "9",
        "interest": "1",
        "dividends": "2a",
        "qualified": "2b",
        "stgain": "3",
        "ltgain": "4a",
        "collectibles": "4b",
        "unrecaptured1250": "4c",
        "qbi": "14I",
        "w2wages": "14I",
        "ubia": "14I",
    },
}
SIGNED = (
    *("ordinary", "rental", "otherrental", "se", "qbi"),
    *("stgain", "ltgain", "collectibles"),
)
# The boxes Schedule E Parts II and III take; the portfolio boxes go elsewhere
PART_E = (
    *("ordinary", "rental", "otherrental", "guaranteed"),
    *("section179", "portfolio", "deductions", "prior"),
)
PORTFOLIO = (
    *("interest", "dividends", "qualified", "royalties", "stgain", "ltgain"),
    *("collectibles", "unrecaptured1250"),
)
ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th")
ALWAYS_PASSIVE = ("rental", "otherrental")
LETTERS = "ABCDEFGHI"
MONEY_MAX = 100_000_000


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each K-1 as a dict of its typed words, or [] for ``none``."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";") if e.strip()):
        tokens = entry.split()
        spouse = tokens[0].lower() == "spouse"
        tokens = tokens[1:] if spouse else tokens
        if len(tokens) < 2 or tokens[0].lower() not in FORMS:
            raise ValueError(
                f"{key}: {entry!r} starts with partnership, scorp or trust (like "
                f"{EXAMPLE}), or type none"
            )
        kind, flag, *rest = (t.lower() if i < 2 else t for i, t in enumerate(tokens))
        if flag not in ("passive", "nonpassive"):
            raise ValueError(
                f"{key}: {entry!r} says passive or nonpassive after {kind} "
                "(nonpassive when you materially participated)"
            )
        active = bool(rest) and rest[0].lower() == "active"
        rest = rest[1:] if active else rest
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} pairs each word with an amount")
        got: dict[str, Any] = {
            "kind": kind,
            "passive": flag == "passive",
            "spouse": spouse,
        }
        for word, amount in zip(rest[::2], rest[1::2], strict=True):
            word = word.lower()
            if word not in BOXES[kind] and word != "prior":
                raise ValueError(
                    f"{key}: {word!r} is not a {kind} word "
                    f"({', '.join(BOXES[kind])}, prior)"
                )
            if word in got:
                raise ValueError(f"{key}: {word} is typed twice in {entry!r}")
            try:
                v = float(amount.replace(",", "").lstrip("$"))
            except ValueError:
                raise ValueError(f"{key}: {word} {amount!r} is not a number") from None
            if abs(v) > MONEY_MAX or (v < 0 and word not in SIGNED):
                raise ValueError(f"{key}: {word} {amount} is out of range")
            got[word] = int(round(v))
        if len(got) == 3:
            raise ValueError(f"{key}: {entry!r} has no amounts (like {EXAMPLE})")
        if got.get("qualified", 0) > got.get("dividends", 0):
            raise ValueError(
                f"{key}: qualified dividends are part of the ordinary dividends; "
                f"{entry!r} has more qualified than dividends"
            )
        if active and (
            kind == "trust"
            or "rental" not in got
            or "otherrental" in got
            or (got["passive"] and "ordinary" in got)
        ):
            raise ValueError(
                f"{key}: active is for a partnership or S corporation K-1 whose "
                f"only passive box is 2 (rental); {entry!r} is not"
            )
        if active:
            got["active"] = True
        if spouse and "se" not in got:
            raise ValueError(
                f"{key}: spouse marks whose Schedule SE takes box 14 code A; "
                f"{entry!r} has no se"
            )
        out.append(got)
    for kind in FORMS:
        if sum(e["kind"] == kind for e in out) > len(LETTERS):
            raise ValueError(f"{key}: at most {len(LETTERS)} {kind} K-1s")
    return out


@dataclass
class Row:
    """One K-1's Schedule E row: line 28 (partnership, S corporation) or line
    33 (estate, trust). ``passive`` holds each passive activity's amount (a
    loss below zero); ``loss`` and ``income`` the nonpassive columns."""

    letter: str
    kind: str
    entry: dict[str, Any]
    passive: list[tuple[str, float]] = field(default_factory=list)
    prior: float = 0.0  # the passive activity's prior-year unallowed loss
    loss: float = 0.0  # column (i) or (e)
    section179: float = 0.0  # column (j)
    income: float = 0.0  # column (k) or (f)

    @property
    def form(self) -> str:
        return FORMS[self.kind]

    @property
    def name(self) -> str:
        return f"{self.kind} {self.letter}"

    @property
    def line(self) -> str:
        return "33" if self.kind == "trust" else "28"

    @property
    def passive_income(self) -> float:
        return sum(a for _, a in self.passive if a > 0)

    @property
    def passive_loss(self) -> float:
        return -sum(a for _, a in self.passive if a < 0)


def rows(entries: list[dict[str, Any]]) -> list[Row]:
    """Each K-1's row with its passive activities and nonpassive columns."""
    out: list[Row] = []
    count = dict.fromkeys(FORMS, 0)
    for e in entries:
        kind = e["kind"]
        if not any(w in e for w in (*PART_E, "prior")):
            continue  # portfolio boxes only: nothing on Schedule E Part II or III
        r = Row(LETTERS[count[kind]], kind, e, prior=float(e.get("prior", 0)))
        count[kind] += 1
        box = BOXES[kind]
        for word in ("ordinary", "rental", "otherrental"):
            if word not in e:
                continue
            amount = float(e[word])
            if e["passive"] or word in ALWAYS_PASSIVE:
                r.passive.append((f"box {box[word]}", amount))
            elif amount < 0:
                r.loss -= amount
            else:
                r.income += amount
        if cut := float(e.get("deductions", 0)):
            if e["passive"]:
                r.passive.append(("box 9", -cut))
            else:
                r.loss += cut
        if e.get("section179"):
            if e["passive"]:
                r.passive.append((f"box {box['section179']}", -float(e["section179"])))
            else:
                r.section179 = float(e["section179"])
        r.income += float(e.get("guaranteed", 0)) + float(e.get("portfolio", 0))
        out.append(r)
    return out


def activities(rs: list[Row]) -> list[f8582.Activity]:
    """Each K-1 with a passive box or a prior-year loss as a Form 8582
    activity."""
    return [
        f8582.Activity(
            r.name,
            f"Sch E, line {r.line}{r.letter}",
            income=r.passive_income,
            loss=r.passive_loss,
            prior=r.prior,
            active=bool(r.entry.get("active")),
        )
        for r in rs
        if r.passive or r.prior
    ]


def nonpassive_net(rs: list[Row]) -> float:
    """The nonpassive columns, for Form 8582's modified AGI."""
    return sum(r.income - r.loss - r.section179 for r in rs)


@dataclass
class Result:
    lines: list[tuple[str, str, float, str]]
    part2: float  # line 32
    part3: float  # line 37
    by_kind: dict[str, float]  # each kind's net as drafted
    passive_pships: float  # the passive part of partnership and S corp income
    notes: list[str]


COLUMNS = {
    "28": (
        ("g", "Passive loss allowed"),
        ("h", "Passive income from Schedule K-1"),
        ("i", "Nonpassive loss allowed"),
        ("j", "Section 179 expense deduction"),
        ("k", "Nonpassive income from Schedule K-1"),
    ),
    "33": (
        ("c", "Passive deduction or loss allowed"),
        ("d", "Passive income from Schedule K-1"),
        ("e", "Deduction or loss from Schedule K-1"),
        ("f", "Other income from Schedule K-1"),
    ),
}


def schedule(rs: list[Row], allowed: dict[str, float]) -> Result:
    """Lines 28-37. ``allowed`` is each K-1's passive loss Form 8582 allows
    (planner.taxprep.f8582.Result.allowed), by its name."""
    lines: list[tuple[str, str, float, str]] = []
    notes: list[str] = []
    by_kind = dict.fromkeys(FORMS, 0.0)
    passive_pships = 0.0
    totals: dict[str, float] = {}
    for r in rs:
        line = r.line
        allow = allowed.get(r.name, 0.0)
        cols = (
            {"g": allow, "h": r.passive_income, "i": r.loss}
            | {"j": r.section179, "k": r.income}
            if line == "28"
            else {"c": allow, "d": r.passive_income, "e": r.loss, "f": r.income}
        )
        src = {
            "g": "Form 8582 Part VIII column (c)",
            "c": "Form 8582 Part VIII column (c)",
            "h": "passive boxes with income",
            "d": "passive boxes with income",
            "i": "nonpassive box 1 loss",
            "e": "nonpassive box 6-9 loss and deductions",
            "j": f"box {BOXES[r.kind].get('section179', '')} (nonpassive)",
            "k": "nonpassive box 1 income + box 4c",
            "f": "nonpassive box 6-8 income + box 5",
        }
        for col, label in COLUMNS[line]:
            if cols[col]:
                lines.append(
                    (
                        f"{line}{r.letter}({col})",
                        f"{label} ({r.kind} {r.letter}, Form {r.form})",
                        cols[col],
                        src[col],
                    )
                )
                totals[line + col] = totals.get(line + col, 0.0) + cols[col]
        net = r.passive_income - allow + r.income - r.loss - r.section179
        by_kind[r.kind] += net
        if r.kind != "trust":
            passive_pships += r.passive_income - allow
        if r.passive_loss or r.loss:
            notes.append(
                f"Schedule E {r.kind} {r.letter}: the draft takes the K-1 loss as "
                "within your basis and at risk; "
                + (
                    "an S corporation loss needs Form 7203 attached (column (e))"
                    if r.kind == "scorp"
                    else "check the partner's basis worksheet and Form 6198"
                    if r.kind == "partnership"
                    else "check the beneficiary's instructions"
                )
            )
    part2 = part3 = 0.0
    if any(r.kind != "trust" for r in rs):
        for col, label in COLUMNS["28"]:
            ln = "29a" if col in ("h", "k") else "29b"
            lines.append(
                (f"{ln}({col})", f"Totals: {label}", totals.get("28" + col, 0.0), "")
            )
        l30 = totals.get("28h", 0.0) + totals.get("28k", 0.0)
        l31 = -(
            totals.get("28g", 0.0) + totals.get("28i", 0.0) + totals.get("28j", 0.0)
        )
        part2 = l30 + l31
        lines += [
            ("30", "Add columns (h) and (k) of line 29a", l30, "29a (h) + (k)"),
            (
                "31",
                "Add columns (g), (i) and (j) of line 29b",
                l31,
                "29b (g) + (i) + (j)",
            ),
            (
                "32",
                "Total partnership and S corporation income or (loss)",
                part2,
                "30 + 31",
            ),
        ]
    if any(r.kind == "trust" for r in rs):
        for col, label in COLUMNS["33"]:
            ln = "34a" if col in ("d", "f") else "34b"
            lines.append(
                (f"{ln}({col})", f"Totals: {label}", totals.get("33" + col, 0.0), "")
            )
        l35 = totals.get("33d", 0.0) + totals.get("33f", 0.0)
        l36 = -(totals.get("33c", 0.0) + totals.get("33e", 0.0))
        part3 = l35 + l36
        lines += [
            ("35", "Add columns (d) and (f) of line 34a", l35, "34a (d) + (f)"),
            ("36", "Add columns (c) and (e) of line 34b", l36, "34b (c) + (e)"),
            ("37", "Total estate and trust income or (loss)", part3, "35 + 36"),
        ]
    if sum(r.kind != "trust" for r in rs) > 4 or sum(r.kind == "trust" for r in rs) > 2:
        notes.append(
            "Schedule E: K-1s past line 28 D or line 33 B go on a continuation "
            "Schedule E page 2"
        )
    notes.append(
        "Schedule E line 27 is drafted No: a prior-year loss held back by the "
        "basis or at-risk rules, or unreimbursed partnership expenses, changes "
        "Part II (see its instructions); a prior-year passive loss goes through "
        "Form 8582 (prior)"
    )
    return Result(lines, part2, part3, by_kind, passive_pships, notes)


def portfolio(entries: list[dict[str, Any]]) -> list[tuple[str, str, float, str]]:
    """(word, payer, amount, source) for each portfolio box typed. A K-1 is
    named by its kind and place in the order typed, for you to write the
    entity's name."""
    out = []
    count = dict.fromkeys(FORMS, 0)
    for e in entries:
        kind = e["kind"]
        payer = f"Schedule K-1 (Form {FORMS[kind]}), the {ORDINALS[count[kind]]} {kind}"
        count[kind] += 1
        out += [
            (w, payer, float(e[w]), f"K-1 box {BOXES[kind][w]}")
            for w in PORTFOLIO
            if e.get(w)
        ]
    return out


def royalties(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each K-1's royalties as a Schedule E Part I royalty column."""
    return [
        {"kind": "royalty", "royalties": a, "source": f"{payer}, {src}"}
        for w, payer, a, src in portfolio(entries)
        if w == "royalties"
    ]


def se_earnings(entries: list[dict[str, Any]]) -> tuple[float, float]:
    """Box 14 code A on (your, your spouse's) Schedule SE line 2."""
    you = sum(float(e.get("se", 0)) for e in entries if not e["spouse"])
    sp = sum(float(e.get("se", 0)) for e in entries if e["spouse"])
    return you, sp


def qbi(entries: list[dict[str, Any]], kinds: tuple[str, ...]) -> dict[str, float]:
    """The section 199A statements' figures for those kinds of K-1."""
    picked = [e for e in entries if e["kind"] in kinds]
    return {
        w: sum(float(e.get(w, 0)) for e in picked) for w in ("qbi", "w2wages", "ubia")
    }
