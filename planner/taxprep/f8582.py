"""Form 8582, Passive Activity Loss Limitations (unit 3e-4).

Lines and columns follow the 2025 Form 8582 and its instructions. Part I
totals rental real estate with active participation (lines 1a-1d, from Part
IV) and every other passive activity (lines 2a-2d, from Part V); line 3 is
their sum. At zero or more every loss is allowed, prior-year unallowed losses
included. Otherwise Part II gives the special allowance when line 1d is a loss:
line 9 is the smaller of the line 1d or line 3 loss (line 4) and 50% of
$150,000 less modified AGI, at most $25,000 (lines 5-8; $75,000 and $12,500
married filing separately and living apart all year). Line 11, the losses
allowed, is line 9 plus the passive income (line 10). Part VI shares line 9
among the Part IV activities with an overall loss, by their losses; Part VII
shares what stays unallowed (line 3 less line 9) among every activity with a
loss left, by those losses; Part VIII gives each activity its allowed loss,
its current loss plus its prior-year unallowed loss less its unallowed share.

Married filing separately and living with your spouse at any time in the year
puts rental real estate in Part V and skips Part II. Not handled, and named:
prior-year unallowed commercial revitalization deductions (line 3 and the line
9 worksheet) and Part IX, for an activity whose loss is split across forms.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

FORM = "Form 8582"
# Lines 5 and 8: (line 5, the line 8 cap); married filing separately and
# living apart all year in the second entry.
ALLOWANCE = {False: (150_000.0, 25_000.0), True: (75_000.0, 12_500.0)}
CENT = 0.005


@dataclass
class Activity:
    """One passive activity's Part IV or Part V row. ``loss`` and ``prior``
    are positive amounts; ``active`` is rental real estate with active
    participation."""

    name: str
    where: str  # the form or schedule and line the loss is reported on
    income: float = 0.0  # column (a)
    loss: float = 0.0  # column (b)
    prior: float = 0.0  # column (c), last year's Part VII column (c)
    active: bool = False

    @property
    def overall(self) -> float:
        """Column (d) when zero or more, column (e) when less."""
        return self.income - self.loss - self.prior


@dataclass
class Result:
    lines: list[tuple[str, str, float, str]] = field(default_factory=list)
    allowed: dict[str, float] = field(default_factory=dict)  # positive losses
    unallowed: dict[str, float] = field(default_factory=dict)  # to next year
    notes: list[str] = field(default_factory=list)


def _rows(
    r: Result, part: str, acts: Sequence[Activity], cols: dict[str, list[float]]
) -> None:
    labels = PARTS[part]
    for n, a in enumerate(acts, 1):
        for col, label in labels.items():
            r.lines.append(
                (f"{part}-{n}({col})", f"{label} ({a.name})", cols[col][n - 1], a.where)
            )


PARTS = {
    "IV": {"a": "Net income", "b": "Net loss", "c": "Unallowed loss"},
    "V": {"a": "Net income", "b": "Net loss", "c": "Unallowed loss"},
    "VI": {
        "a": "Loss",
        "b": "Ratio",
        "c": "Special allowance",
        "d": "Subtract (c) from (a)",
    },
    "VII": {"a": "Loss", "b": "Ratio", "c": "Unallowed loss"},
    "VIII": {"a": "Loss", "b": "Unallowed loss", "c": "Allowed loss"},
}


def _ratios(amounts: list[float]) -> list[float]:
    total = sum(amounts)
    return [a / total for a in amounts] if total else [0.0] * len(amounts)


def compute(acts: Sequence[Activity], *, magi: float, separate: str | None) -> Result:
    """Parts I-VIII. ``magi`` is the line 6 modified AGI; ``separate`` is None
    unless married filing separately: "apart" (all year) or "together"."""
    r = Result()
    if not acts:
        return r
    add = r.lines.append
    together = separate == "together"
    iv = [a for a in acts if a.active and not together]
    v = [a for a in acts if not (a.active and not together)]
    if together and any(a.active for a in acts):
        r.notes.append(
            "Form 8582: married filing separately and living with your spouse "
            "during the year, rental real estate goes in Part V, not Part IV, "
            "and Part II's special allowance is not allowed"
        )

    def part1(line: str, group: list[Activity], part: str) -> float:
        a, b, c = (
            sum(x.income for x in group),
            sum(x.loss for x in group),
            sum(x.prior for x in group),
        )
        for k, label, val, col in (
            ("a", "Activities with net income", a, "a"),
            ("b", "Activities with net loss", -b, "b"),
            ("c", "Prior years' unallowed losses", -c, "c"),
        ):
            if val:
                add((line + k, label, val, f"Part {part}, column ({col})"))
        d = a - b - c
        add(
            (
                line + "d",
                f"Combine lines {line}a, {line}b and {line}c",
                d,
                f"{line}a + {line}b + {line}c",
            )
        )
        return d

    l1d = part1("1", iv, "IV") if iv else 0.0
    l2d = part1("2", v, "V") if v else 0.0
    l3 = add_line(r, "3", "Combine lines 1d and 2d", l1d + l2d, "1d + 2d")
    for part, group in (("IV", iv), ("V", v)):
        _rows(
            r,
            part,
            group,
            {
                "a": [a.income for a in group],
                "b": [-a.loss for a in group],
                "c": [-a.prior for a in group],
            },
        )
    if l3 >= 0:
        r.allowed = {a.name: a.loss + a.prior for a in acts}
        r.unallowed = dict.fromkeys(r.allowed, 0.0)
        r.notes.append(
            "Form 8582 line 3 is zero or more: every passive loss is allowed, "
            "prior-year unallowed losses included"
        )
        return r

    l9 = 0.0
    if l1d < 0 and not together:
        l4 = add_line(
            r,
            "4",
            "Smaller of the loss on line 1d or line 3",
            min(-l1d, -l3),
            "1d, 3 (as positive amounts)",
        )
        five, cap = ALLOWANCE[separate == "apart"]
        add_line(
            r,
            "5",
            "Enter $150,000"
            if five == 150_000
            else "Enter $75,000 (married filing separately, apart all year)",
            five,
            "fixed",
        )
        l6 = add_line(
            r,
            "6",
            "Modified adjusted gross income, not less than zero",
            max(magi, 0.0),
            "AGI without passive losses (instructions)",
        )
        if l6 < five:
            l7 = add_line(r, "7", "Subtract line 6 from line 5", five - l6, "5 - 6")
            l8 = add_line(
                r,
                "8",
                f"Line 7 x 50%, not more than {cap:,.0f}",
                min(l7 * 0.5, cap),
                "7 x 50%",
            )
            l9 = min(l4, l8)
        add_line(r, "9", "Smaller of line 4 or line 8", l9, "4, 8")
    l10 = add_line(
        r,
        "10",
        "Income on lines 1a and 2a",
        sum(a.income for a in acts if a.income > 0),
        "1a + 2a",
    )
    add_line(
        r, "11", "Total losses allowed from all passive activities", l9 + l10, "9 + 10"
    )

    losers = [a for a in acts if a.overall < 0]
    left = {a.name: -a.overall for a in losers}
    vi = [a for a in iv if a.overall < 0]
    if l9:
        a_col = [-a.overall for a in vi]
        ratio = _ratios(a_col)
        c_col = [x * l9 for x in ratio]
        d_col = [x - c for x, c in zip(a_col, c_col, strict=True)]
        _rows(r, "VI", vi, {"a": a_col, "b": ratio, "c": c_col, "d": d_col})
        left |= {a.name: d for a, d in zip(vi, d_col, strict=True)}
    vii = [a for a in losers if left[a.name] > CENT]
    a_col = [left[a.name] for a in vii]
    ratio = _ratios(a_col)
    c_col = [x * (-l3 - l9) for x in ratio]
    _rows(r, "VII", vii, {"a": a_col, "b": ratio, "c": c_col})
    unallowed = {a.name: c for a, c in zip(vii, c_col, strict=True)}
    r.unallowed = {a.name: unallowed.get(a.name, 0.0) for a in acts}
    r.allowed = {a.name: a.loss + a.prior - r.unallowed[a.name] for a in acts}
    _rows(
        r,
        "VIII",
        vii,
        {
            "a": [a.loss + a.prior for a in vii],
            "b": [r.unallowed[a.name] for a in vii],
            "c": [r.allowed[a.name] for a in vii],
        },
    )
    held = sum(r.unallowed.values())
    if held > CENT:
        r.notes.append(
            f"Form 8582: {held:,.2f} of passive loss is not allowed this year; "
            "each activity's Part VII column (c) carries to next year's Part IV "
            "or V column (c)"
        )
    return r


def add_line(r: Result, line: str, label: str, value: float, src: str) -> float:
    r.lines.append((line, label, value, src))
    return value
