"""The Simplified Method Worksheet (2025 Form 1040 instructions, lines 5a and
5b) for a pension or annuity whose Form 1099-R leaves the taxable amount to
you (box 2a blank, or box 2b "Taxable amount not determined" checked), from
the typed annuities answer (unit 3f-2).

One entry is one pension or annuity, figured on its own worksheet; the taxable
parts add up on line 5b and the payments on line 5a. Line 2 is your cost in
the plan at the annuity starting date, line 3 the number of payments from
Table 1 (your age at the starting date and whether it was before November 19,
1996) or, for payments over your life and your beneficiary's starting after
1997, Table 2 (your combined ages). Line 4 is the tax-free part of each
monthly payment; line 8 the part of this year's payments that is tax free,
never more than the cost not yet recovered (lines 6 and 7, from 1987 on);
line 9 the taxable amount. Lines 10 and 11 carry to next year. Not handled,
and named: an annuity starting before July 2, 1986, or a nonqualified
annuity (box 7a code D), which take the General Rule (Pub. 939); the death
benefit exclusion for a death before August 21, 1996 (type it into cost);
the retired public safety officer exclusion (line 5c box 2); disability
pensions before minimum retirement age (line 1h).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

FORM = "Simplified Method"
EXAMPLE = "annuity cost 30000 start 2019-07-01 recovered 9000"
FLAGS = ("spouse",)
MONEY = ("gross", "cost", "recovered")
WHOLE = ("age", "beneficiary", "months")
WORDS = (*MONEY, "start", *WHOLE)
MONEY_MAX = 100_000_000
GENERAL_RULE_BEFORE = date(1986, 7, 2)  # Simplified Method: start after July 1, 1986
TABLE1_SPLIT = date(1996, 11, 19)  # Table 1's two columns
NO_LIMIT_BEFORE = 1987  # starting before 1987: no cap at the cost (lines 6-7)
# Table 1: (age at the starting date up to, before Nov 19 1996, after Nov 18 1996)
TABLE1 = ((55, 300, 360), (60, 260, 310), (65, 240, 260), (70, 170, 210))
TABLE1_OLDER = (120, 160)
# Table 2: (combined ages up to, payments); starting after 1997, joint and survivor
TABLE2 = ((110, 410), (120, 360), (130, 310), (140, 260))
TABLE2_OLDER = 210
TABLE2_AFTER = 1997
LABELS = {
    "1": "Total pension or annuity payments (Form 1099-R box 1)",
    "2": "Your cost in the plan at the annuity starting date",
    "3": "Number from Table 1 or Table 2",
    "4": "Line 2 divided by line 3",
    "5": "Line 4 times the months paid this year",
    "6": "Amount recovered tax free in years after 1986",
    "7": "Line 2 less line 6",
    "8": "Smaller of line 5 or line 7",
    "9": "Taxable amount (to Form 1040 line 5b)",
    "10": "Recovered tax free through this year",
    "11": "Balance of cost to be recovered",
}


@dataclass
class Worksheet:
    who: str  # "you" or "spouse"
    lines: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def taxable(self) -> float:
        return self.lines["9"]


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each annuity as a dict of its typed words, or [] for ``none``."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";") if e.strip()):
        tokens = entry.split()
        if tokens[0].lower() != "annuity":
            raise ValueError(
                f"{key}: {entry!r} starts with annuity (like {EXAMPLE}), or type none"
            )
        got: dict[str, Any] = {}
        rest = tokens[1:]
        while rest and rest[0].lower() in FLAGS:
            got[rest.pop(0).lower()] = True
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} pairs each word with a value")
        for word, raw in zip(rest[::2], rest[1::2], strict=True):
            word = word.lower()
            if word not in WORDS:
                raise ValueError(
                    f"{key}: {word!r} is not an annuity word ({', '.join(WORDS)}); "
                    "spouse comes right after annuity"
                )
            if word in got:
                raise ValueError(f"{key}: {word} is typed twice in {entry!r}")
            got[word] = _value(key, word, raw)
        for need in ("cost", "start"):
            if need not in got:
                raise ValueError(f"{key}: {entry!r} needs {need} (like {EXAMPLE})")
        if not 1 <= got.get("months", 12) <= 12:
            raise ValueError(f"{key}: months is 1 to 12 in {entry!r}")
        out.append(got)
    return out


def _value(key: str, word: str, raw: str) -> Any:
    if word == "start":
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError(f"{key}: start {raw!r} is a date (2019-07-01)") from None
    try:
        v = float(raw.replace(",", "").lstrip("$"))
    except ValueError:
        raise ValueError(f"{key}: {word} {raw!r} is not a number") from None
    if word in WHOLE:
        if v != int(v) or not 0 <= v <= 120:
            raise ValueError(f"{key}: {word} {raw} is a whole number, 0 to 120")
        return int(v)
    if not 0 <= v <= MONEY_MAX:
        raise ValueError(f"{key}: {word} {raw} is out of range")
    return round(v, 2)


def table_number(start: date, age: int, beneficiary: int | None) -> tuple[int, str]:
    """Line 3: the number of payments and the table it came from."""
    if beneficiary is not None and start.year > TABLE2_AFTER:
        combined = age + beneficiary
        for top, n in TABLE2:
            if combined <= top:
                return n, f"Table 2, combined ages {combined}"
        return TABLE2_OLDER, f"Table 2, combined ages {combined}"
    col = 0 if start < TABLE1_SPLIT else 1
    when = "before November 19, 1996" if col == 0 else "after November 18, 1996"
    for top, *ns in TABLE1:
        if age <= top:
            return ns[col], f"Table 1, age {age}, starting {when}"
    return TABLE1_OLDER[col], f"Table 1, age {age}, starting {when}"


def worksheet(e: dict[str, Any], gross: float, age: int, year: int) -> Worksheet:
    """One annuity's worksheet. ``gross`` is line 1, ``age`` the owner's age at
    the annuity starting date. Raises ValueError for an annuity the
    Simplified Method does not cover."""
    start = date.fromisoformat(e["start"])
    who = "spouse" if e.get("spouse") else "you"
    if start < GENERAL_RULE_BEFORE:
        raise ValueError(
            f"the annuity starting {start.isoformat()} began before July 2, 1986: "
            "the General Rule (Pub. 939) figures its taxable part, not drafted"
        )
    if start.year > year:
        raise ValueError(f"the annuity starts {start.isoformat()}, after {year}")
    months = e.get("months") or (12 if start.year < year else 13 - start.month)
    w = Worksheet(who)
    cost = float(e["cost"])
    n, src = table_number(start, age, e.get("beneficiary"))
    w.lines["1"] = gross
    w.lines["2"] = cost
    w.lines["3"] = float(n)
    w.lines["4"] = round(cost / n, 2)
    w.lines["5"] = round(w.lines["4"] * months, 2)
    if start.year < NO_LIMIT_BEFORE:
        w.lines["8"] = w.lines["5"]
    else:
        w.lines["6"] = float(e.get("recovered") or 0)
        w.lines["7"] = round(max(cost - w.lines["6"], 0.0), 2)
        w.lines["8"] = min(w.lines["5"], w.lines["7"])
    w.lines["9"] = round(max(gross - w.lines["8"], 0.0), 2)
    if start.year >= NO_LIMIT_BEFORE:
        w.lines["10"] = round(w.lines["6"] + w.lines["8"], 2)
        w.lines["11"] = round(max(cost - w.lines["10"], 0.0), 2)
    w.notes.append(f"{FORM} line 3: {n} ({src}); line 5: {months} months")
    return w
