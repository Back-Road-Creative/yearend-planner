"""Form 4797, Sales of Business Property, for the sales you type and the
section 1231 boxes on your Schedules K-1 (unit 3e-6c).

Line numbers and rules follow the 2025 Form 4797 and its instructions:

- A sale of property held more than 1 year (sold after the anniversary of the
  day bought) goes to Part III when it is section 1245 or 1250 property sold at
  a gain; its gain (line 24) less the depreciation recaptured (line 25b, or
  26g) reaches Part I line 6. Land, and depreciable property sold at a loss,
  go to Part I line 2. Property held 1 year or less goes to Part II line 10.
- Section 1245 recapture (line 25b) is the smaller of the gain and the
  depreciation allowed. Section 1250 recapture (line 26b) is the additional
  depreciation (the excess over straight line); real property placed in
  service after 1986 is depreciated straight line under MACRS, so it is 0
  unless you type ``additional``. Lines 26c-26f (pre-1976 property and the
  corporate rules) are not drafted.
- The net section 1231 gain or loss of a partnership (1065 box 10) or an S
  corporation (1120-S box 9) goes to Part I line 2. A passive K-1's section
  1231 loss is a passive loss (Form 8582) and is not taken here.
- Line 7 nets Part I. A loss goes to Part II line 11; a gain is reduced by the
  nonrecaptured net section 1231 losses of the 5 years before (line 8), which
  are recaptured as ordinary income (line 12), and the rest (line 9) is a
  long-term capital gain on Schedule D line 11.
- Part II totals at line 17; line 18b (an individual's) goes to Schedule 1
  line 4.

The Unrecaptured Section 1250 Gain Worksheet lines 1-9 (Schedule D
instructions) come from here: the smaller of the depreciation and the gain on
each Part III section 1250 property, less its recapture, plus a partnership's
or S corporation's unrecaptured section 1250 gain (1065 box 9c, 1120-S box 8c),
not more than line 7's gain, less line 8.

Each sale is typed as ``1250 acquired 2015-03-01 sold 2025-06-30 price 300000
basis 200000 depreciation 50000``; the kind is 1245, 1250 or land, and
``additional N`` gives a section 1250 property's additional depreciation.
Separate the sales with semicolons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

FORM = "4797"
KINDS = ("1245", "1250", "land")
EXAMPLE = (
    "1250 acquired 2015-03-01 sold 2025-06-30 price 300000 basis 200000 "
    "depreciation 50000; 1245 acquired 2020-01-15 sold 2025-08-01 price 8000 "
    "basis 20000 depreciation 15000"
)
WORDS = ("price", "basis", "depreciation", "additional")
LETTERS = "ABCDEFGHI"
MONEY_MAX = 100_000_000
NOT_DRAFTED = (
    "Form 4797 leaves out lines 1a-1c (the proceeds on your Forms 1099-S and "
    "1099-B: check them), Forms 4684, 6252 and 8824 (lines 3-5 and 14-16), "
    "Part IV, lines 26c-26f and 27-29, line 18a, and the passive losses a "
    "disposition frees (Form 8582)"
)


def _date(key: str, s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{key}: {s!r} is not a date (YYYY-MM-DD)") from None


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each sale typed, validated; ``none`` is no sales."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";")):
        if not entry:
            continue
        toks = entry.lower().split()
        if not toks or toks[0] not in KINDS:
            raise ValueError(
                f"{key}: {entry!r} starts with the kind ({', '.join(KINDS)}), "
                f"like {EXAMPLE}"
            )
        sale: dict[str, Any] = {"kind": toks[0]}
        rest = toks[1:]
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} has a word with no value")
        for word, value in zip(rest[::2], rest[1::2], strict=True):
            if word in ("acquired", "sold"):
                sale[word] = _date(key, value).isoformat()
            elif word in WORDS:
                if not re.fullmatch(r"\d+(\.\d+)?", value.replace(",", "")):
                    raise ValueError(f"{key}: {word} {value!r} is not an amount")
                v = float(value.replace(",", ""))
                if v > MONEY_MAX:
                    raise ValueError(f"{key}: {word} over {MONEY_MAX:,}")
                sale[word] = v
            else:
                raise ValueError(
                    f"{key}: {word!r} is not acquired, sold or one of "
                    f"{', '.join(WORDS)}"
                )
        for need in ("acquired", "sold", "price", "basis"):
            if need not in sale:
                raise ValueError(f"{key}: {entry!r} has no {need} (like {EXAMPLE})")
        if sale["sold"] < sale["acquired"]:
            raise ValueError(f"{key}: {entry!r} is sold before it was acquired")
        if sale["kind"] == "land" and sale.get("depreciation"):
            raise ValueError(f"{key}: land is not depreciated ({entry!r})")
        if sale.get("additional") and sale["kind"] != "1250":
            raise ValueError(f"{key}: additional is for 1250 property ({entry!r})")
        if sale.get("additional", 0) > sale.get("depreciation", 0):
            raise ValueError(f"{key}: additional over depreciation ({entry!r})")
        out.append(sale)
    if len(out) > len(LETTERS):
        raise ValueError(f"{key}: at most {len(LETTERS)} sales")
    return out


def long_term(acquired: str, sold: str) -> bool:
    """Held more than 1 year: sold after the anniversary of the day bought."""
    a, s = date.fromisoformat(acquired), date.fromisoformat(sold)
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:  # bought Feb 29
        anniversary = date(a.year + 1, 3, 1)
    return s > anniversary


@dataclass
class Result:
    lines: list[tuple[str, str, float, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    schedule_d: float = 0.0  # line 9 (or 7): Schedule D line 11
    line18b: float = 0.0  # Schedule 1 line 4
    worksheet: float = 0.0  # Unrecaptured Section 1250 Gain Worksheet line 9
    worksheet_from: list[str] = field(default_factory=list)


def compute(
    sales: list[dict[str, Any]],
    k1s: list[dict[str, Any]],
    k1p: list[tuple[str, str, float, str]],
    lookback: float | None,
) -> Result | None:
    """The form, or None when there is nothing for it. ``k1s`` are the typed
    K-1s (for passive), ``k1p`` their ``k1.portfolio`` rows in the same order,
    ``lookback`` the typed line 8 (None when not given)."""
    r = Result()
    add = r.lines.append
    passive = [bool(e.get("passive")) for e in k1s if e.get("section1231")]
    k1_1231 = [i for i in k1p if i[0] == "section1231"]
    if not sales and not k1_1231:
        return None

    part1: list[tuple[str, float, str]] = []
    part2: list[tuple[str, float, str]] = []
    part3 = []
    for n, s in enumerate(sales, 1):
        dep = s.get("depreciation", 0.0)
        gain = round(s["price"] + dep - s["basis"], 2)
        what = f"sale {n}, section {s['kind']} property"
        if s["kind"] == "land":
            what = f"sale {n}, land"
        how = (
            f"{s['acquired']} to {s['sold']}: (d) {s['price']:,.2f} + (e) "
            f"{dep:,.2f} - (f) {s['basis']:,.2f}"
        )
        if not long_term(s["acquired"], s["sold"]):
            part2.append((what + " (held 1 year or less)", gain, how))
        elif s["kind"] == "land" or gain <= 0:
            part1.append((what, gain, how))
        else:
            part3.append((n, s, what))

    # Part III, one column a property
    l30 = l31 = 0.0
    w1_3: list[float] = []
    for letter, (n, s, what) in zip(LETTERS, part3, strict=False):
        dep = s.get("depreciation", 0.0)
        l23 = round(s["basis"] - dep, 2)
        l24 = round(s["price"] - l23, 2)
        add(
            (
                "19" + letter,
                f"Property {letter}: {what}",
                l24,
                s["acquired"] + " to " + s["sold"],
            )
        )
        add(("20" + letter, "Gross sales price", s["price"], f"sale {n} price"))
        add(
            (
                "21" + letter,
                "Cost or other basis plus expense of sale",
                s["basis"],
                f"sale {n} basis",
            )
        )
        add(
            (
                "22" + letter,
                "Depreciation allowed or allowable",
                dep,
                f"sale {n} depreciation",
            )
        )
        add(("23" + letter, "Adjusted basis", l23, "21 - 22"))
        add(("24" + letter, "Total gain", l24, "20 - 23"))
        if s["kind"] == "1245":
            add(("25a" + letter, "Depreciation allowed or allowable", dep, "22"))
            recapture = min(l24, dep)
            add(
                (
                    "25b" + letter,
                    "Section 1245 recapture",
                    recapture,
                    "smaller of 24 or 25a",
                )
            )
        else:
            extra = s.get("additional", 0.0)
            add(
                (
                    "26a" + letter,
                    "Additional depreciation after 1975",
                    extra,
                    f"sale {n} additional"
                    if extra
                    else "0: straight-line MACRS (real property after 1986)",
                )
            )
            l26b = min(l24, extra)
            add(
                (
                    "26b" + letter,
                    "Applicable percentage times the smaller of 24 or 26a",
                    l26b,
                    "100% x smaller of 24 or 26a",
                )
            )
            recapture = l26b
            add(
                (
                    "26g" + letter,
                    "Section 1250 recapture",
                    l26b,
                    "26b (26c-26f not drafted)",
                )
            )
            w1 = min(dep, l24)
            w1_3.append(round(w1 - l26b, 2))
            r.worksheet_from.append(
                f"Form 4797 property {letter} (worksheet lines 1-3: "
                f"{w1:,.2f} - {l26b:,.2f})"
            )
        l30 += l24
        l31 += recapture
    l30, l31 = round(l30, 2), round(l31, 2)
    l32 = round(l30 - l31, 2)
    if part3:
        add(("30", "Total gains for all properties", l30, "columns A-D, line 24"))
        add(
            ("31", "Add property columns A through D, lines 25b, 26g", l31, "25b + 26g")
        )
        add(("32", "Line 30 less line 31", l32, "30 - 31 (to line 6)"))

    # Part I
    for what, gain, how in part1:
        add(("2", what, gain, how))
    for (_, payer, amount, src), is_passive in zip(k1_1231, passive, strict=False):
        if is_passive and amount < 0:
            r.notes.append(
                f"{payer} {src}: a passive section 1231 loss of {-amount:,.2f} is "
                "a passive loss (Form 8582) and is not on Form 4797"
            )
            continue
        part1.append((f"{payer} {src}", amount, "net section 1231 gain or loss"))
        add(("2", f"{payer}, net section 1231 gain or loss", amount, src))
    if part3:
        add(("6", "Gain from line 32", l32, "line 32"))
    l7 = round(sum(g for _, g, _ in part1) + (l32 if part3 else 0.0), 2)
    add(("7", "Combine lines 2 through 6", l7, "2 + 6"))
    l11 = l12 = 0.0
    if l7 <= 0:
        l11 = l7
    else:
        if lookback is None:
            r.unknown.append(
                "section_1231_lookback: Form 4797 line 8 (net section 1231 losses "
                "of the 5 years before) not given, left out, not zero"
            )
        l8 = min(float(lookback or 0.0), l7)
        add(
            (
                "8",
                "Nonrecaptured net section 1231 losses from prior years",
                l8,
                "section_1231_lookback"
                + (" (typed)" if lookback is not None else ": not given"),
            )
        )
        l9 = round(l7 - l8, 2)
        add(("9", "Line 7 less line 8", l9, "7 - 8 (to Schedule D line 11)"))
        l12 = l8 if l9 > 0 else l7
        r.schedule_d = l9
        # Unrecaptured Section 1250 Gain Worksheet lines 1-9
        k1_1250 = [i for i in k1p if i[0] == "unrecaptured1250" and "1041" not in i[1]]
        w5 = sum(a for _, _, a, _ in k1_1250)
        r.worksheet_from += [f"{p} {s} (worksheet line 5)" for _, p, _, s in k1_1250]
        w7 = min(round(sum(w1_3) + w5, 2), l7)
        r.worksheet = max(round(w7 - l8, 2), 0.0)

    # Part II
    for what, gain, how in part2:
        add(("10", what, gain, how))
    if l11:
        add(("11", "Loss, if any, from line 7", l11, "line 7"))
    if l12:
        add(
            (
                "12",
                "Gain, if any, from line 7 or amount from line 8",
                l12,
                "line 8" if r.schedule_d else "line 7",
            )
        )
    if part3:
        add(("13", "Gain, if any, from line 31", l31, "line 31"))
    l17 = round(sum(g for _, g, _ in part2) + l11 + l12 + (l31 if part3 else 0), 2)
    add(("17", "Combine lines 10 through 16", l17, "10 + 11 + 12 + 13"))
    add(
        (
            "18b",
            "Ordinary gain or (loss)",
            l17,
            "17 (18a not drafted; to Schedule 1 line 4)",
        )
    )
    r.line18b = l17
    r.notes.append(NOT_DRAFTED)
    if l17 > 0 and part3:
        r.notes.append(
            "Form 8995: the ordinary gain on Form 4797 line 13 (depreciation "
            "recaptured) can be qualified business income; it is not added"
        )
    if r.schedule_d or l17:
        r.notes.append(
            "net investment income tax (Form 8960): the engine counts the Schedule "
            "D line 11 gain and leaves out line 18b; a business you materially "
            "participate in is not investment income, a rental's sale is"
        )
    return r
