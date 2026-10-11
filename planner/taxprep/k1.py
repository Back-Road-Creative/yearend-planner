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
- The section 199A statement (1065 box 20 code Z, 1120-S box 17 code V, 1041
  box 14 code I) gives the qualified business income, W-2 wages and UBIA.

Each passive box is its own activity for Form 8582. A passive loss is allowed
in the same share as every other passive loss on the return (planner.taxprep.sche).

Each K-1 is typed as ``partnership passive ordinary -4000 rental 1200``,
``scorp nonpassive ordinary 30000 section179 2000 qbi 30000`` or ``trust
passive ordinary 800 portfolio 300``. Separate the K-1s with semicolons; each
kind is lettered A, B, C in the order typed. A leading ``spouse`` puts the
self-employment earnings on the spouse's Schedule SE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
        "qbi": "20Z",
        "w2wages": "20Z",
        "ubia": "20Z",
    },
    "scorp": {
        "ordinary": "1",
        "rental": "2",
        "otherrental": "3",
        "section179": "11",
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
        "qbi": "14I",
        "w2wages": "14I",
        "ubia": "14I",
    },
}
SIGNED = ("ordinary", "rental", "otherrental", "se", "qbi")  # may be a loss
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
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} pairs each word with an amount")
        got: dict[str, Any] = {
            "kind": kind,
            "passive": flag == "passive",
            "spouse": spouse,
        }
        for word, amount in zip(rest[::2], rest[1::2], strict=True):
            word = word.lower()
            if word not in BOXES[kind]:
                raise ValueError(
                    f"{key}: {word!r} is not a {kind} word ({', '.join(BOXES[kind])})"
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
    loss: float = 0.0  # column (i) or (e)
    section179: float = 0.0  # column (j)
    income: float = 0.0  # column (k) or (f)

    @property
    def form(self) -> str:
        return FORMS[self.kind]

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
        r = Row(LETTERS[count[kind]], kind, e)
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


def passive_totals(rs: list[Row]) -> tuple[float, float]:
    """(passive income, passive losses) across the K-1s, Form 8582 lines 2a-2b
    and 3a-3b."""
    return sum(r.passive_income for r in rs), sum(r.passive_loss for r in rs)


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


def schedule(rs: list[Row], ratio: float, ratio_src: str) -> Result:
    """Lines 28-37. ``ratio`` is the share of each passive loss Form 8582
    allows (planner.taxprep.sche.Result.ratio)."""
    lines: list[tuple[str, str, float, str]] = []
    notes: list[str] = []
    by_kind = dict.fromkeys(FORMS, 0.0)
    passive_pships = 0.0
    totals: dict[str, float] = {}
    for r in rs:
        line = "33" if r.kind == "trust" else "28"
        allowed = r.passive_loss * ratio
        cols = (
            {"g": allowed, "h": r.passive_income, "i": r.loss}
            | {"j": r.section179, "k": r.income}
            if line == "28"
            else {"c": allowed, "d": r.passive_income, "e": r.loss, "f": r.income}
        )
        src = {
            "g": f"box losses x {ratio_src}",
            "c": f"box losses x {ratio_src}",
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
        net = r.passive_income - allowed + r.income - r.loss - r.section179
        by_kind[r.kind] += net
        if r.kind != "trust":
            passive_pships += r.passive_income - allowed
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
        "basis, at-risk or passive rules, or unreimbursed partnership expenses, "
        "changes Part II (see its instructions)"
    )
    return Result(lines, part2, part3, by_kind, passive_pships, notes)


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
