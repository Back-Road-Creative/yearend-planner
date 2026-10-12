"""Form 8606, Parts I and II (2025 Form 8606 and its instructions), for each
spouse with basis in traditional IRAs, from the typed ira_basis answer (unit
3f-3).

Basis is what went into traditional IRAs already taxed: nondeductible
contributions (line 1, this year's; line 2, earlier years', from the Total
Basis Chart: last year's line 14). When there is basis and a distribution
(line 7) or a Roth conversion (line 8), each carries the same tax-free share,
line 10: the basis (line 5) over the year-end value of all traditional, SEP
and SIMPLE IRAs plus the year's distributions and conversions (line 9). Line
15c is the taxable part of the distributions (to Form 1040 line 4b), line 18
the taxable part of the conversion (also 4b), line 14 the basis carried to
next year. Line 7 is the year's taxable IRA distributions after any QCD
(qualified charitable distributions never reach line 7); a rollover's or a
conversion's 1099-R is counted there only if its box 2a was. Line 4 holds this
year's contributions made after December 31: they are basis next year but
not in this year's share. Not handled, and named: qualified disaster
distributions (line 15b, Form 8915-F) and repayments treated as rollovers (the
Line 15c Worksheet), Part III (distributions from Roth IRAs), the Pub. 590-B
special computation when a contribution may be partly deductible because of
the income limits (type the nondeductible part you figured), returns of
contributions, and recharacterizations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FORM = "Form 8606"
SPOUSE_FORM = "Form 8606 (spouse)"  # each spouse files their own
EXAMPLE = "basis 12000 value 80000 nondeductible 7000"
WORDS = ("basis", "value", "nondeductible", "late")
MONEY_MAX = 100_000_000
PLACES = 5  # line 10: "rounded to at least 3 places"
LABELS = {
    "1": "Nondeductible contributions to traditional IRAs for the year",
    "2": "Total basis in traditional IRAs (last year's line 14)",
    "3": "Add lines 1 and 2",
    "4": "Contributions on line 1 made after December 31",
    "5": "Line 3 less line 4",
    "6": "Value of all traditional IRAs on December 31, plus outstanding rollovers",
    "7": "Distributions from traditional IRAs (not rollovers, QCDs or conversions)",
    "8": "Net amount converted to Roth IRAs",
    "9": "Add lines 6, 7 and 8",
    "10": "Line 5 divided by line 9 (not more than 1)",
    "11": "Nontaxable part of the conversion (line 8 x line 10)",
    "12": "Nontaxable part of the distributions (line 7 x line 10)",
    "13": "Add lines 11 and 12",
    "14": "Total basis in traditional IRAs, to next year's line 2",
    "15a": "Line 7 less line 12",
    "15c": "Taxable amount (to Form 1040 line 4b)",
    "16": "Net amount converted (line 8)",
    "17": "Basis in the conversion (line 11)",
    "18": "Taxable amount of the conversion (to Form 1040 line 4b)",
}


@dataclass
class Form8606:
    who: str  # "you" or "spouse"
    lines: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def nontaxable(self) -> tuple[float, float]:
        """The tax-free part of (the distributions, the conversion)."""
        return self.lines.get("12", 0.0), self.lines.get("11", 0.0)


def parse(key: str, s: str) -> dict[str, float]:
    """The typed words as amounts, or {} for ``none`` (no basis)."""
    if s.strip().lower() == "none":
        return {}
    tokens = s.split()
    if len(tokens) % 2:
        raise ValueError(
            f"{key}: pair each word with an amount (like {EXAMPLE}), or type none"
        )
    got: dict[str, float] = {}
    for word, raw in zip(tokens[::2], tokens[1::2], strict=True):
        word = word.lower()
        if word not in WORDS:
            raise ValueError(f"{key}: {word!r} is not a word here ({', '.join(WORDS)})")
        if word in got:
            raise ValueError(f"{key}: {word} is typed twice")
        try:
            v = float(raw.replace(",", "").lstrip("$"))
        except ValueError:
            raise ValueError(f"{key}: {word} {raw!r} is not a number") from None
        if not 0 <= v <= MONEY_MAX:
            raise ValueError(f"{key}: {word} {raw} is out of range")
        got[word] = round(v, 2)
    if "basis" not in got:
        raise ValueError(f"{key}: needs basis, 0 if none before this year ({EXAMPLE})")
    if got.get("late", 0.0) > got.get("nondeductible", 0.0):
        raise ValueError(f"{key}: late is part of nondeductible, so not more than it")
    return got


def figure(who: str, e: dict[str, float], line7: float, line8: float) -> Form8606:
    """Parts I and II from the typed words, this year's taxable IRA
    distributions (line 7) and the conversion (line 8). Raises ValueError when
    the share needs the year-end value and it was not typed."""
    f = Form8606(who)
    put = f.lines.__setitem__
    put("1", e.get("nondeductible", 0.0))
    put("2", e["basis"])
    put("3", round(f.lines["1"] + f.lines["2"], 2))
    if not (line7 or line8):
        put("14", f.lines["3"])
        return f
    if "value" not in e:
        raise ValueError(
            f"ira_basis: with distributions or a conversion, the share needs value "
            f"(line 6: all traditional IRAs on December 31) ({EXAMPLE})"
        )
    put("4", e.get("late", 0.0))
    put("5", round(f.lines["3"] - f.lines["4"], 2))
    put("6", e["value"])
    put("7", round(line7, 2))
    put("8", round(line8, 2))
    put("9", round(f.lines["6"] + line7 + line8, 2))
    share = min(round(f.lines["5"] / f.lines["9"], PLACES), 1.0)
    put("10", share)
    put("11", round(line8 * share, 2))
    put("12", round(line7 * share, 2))
    put("13", round(f.lines["11"] + f.lines["12"], 2))
    put("14", round(f.lines["3"] - f.lines["13"], 2))
    if line7:
        put("15a", round(line7 - f.lines["12"], 2))
        put("15c", f.lines["15a"])
    if line8:
        put("16", f.lines["8"])
        put("17", f.lines["11"])
        put("18", round(line8 - f.lines["11"], 2))
    return f
