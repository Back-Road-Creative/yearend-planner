"""Schedule F (Form 1040), profit or loss from farming, from the typed farms
answer (unit 3e-5).

Lines, labels and rules follow the 2025 Schedule F and its instructions. One
entry is one farm business, one Schedule F. The cash method fills Part I
(lines 1a-9); ``accrual`` fills Part III (lines 37-50) and carries line 50 to
line 9. Part II (lines 10-33) is the same for both. Line 12, conservation
expenses, is capped at 25% of gross income from farming (line 9 here), with
the excess carried forward (a note). Line 34 is the net profit or loss after
the passive activity loss rules: a farm you did not materially participate in
(``nonmaterial``, line E No) is a Form 8582 activity, and line 34 takes what
Form 8582 allows, with its prior-year unallowed loss (``prior``, "PAL"). Line
34 goes to Schedule 1 line 6 and the owner's Schedule SE line 1a. Not handled,
and named: the at-risk rules (Form 6198, ``notatrisk`` on line 36b), the
excess business loss (Form 461), farm income averaging (Schedule J), the
Schedule SE farm optional method, crop-share rent (Form 4835) and a former
passive activity's prior-year loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from planner.taxprep import carries, f8582

FORM = "Sch F"
EXAMPLE = (
    "farm raised 60000 program 2000 feed 12000 fertilizer 4000 fuel 3000 "
    "depreciation 8000"
)
FLAGS = ("spouse", "accrual", "nonmaterial", "notatrisk")
# Income words: (cash line, accrual line, label). None: not on that method.
INCOME = {
    "resale": ("1a", None, "Sales of purchased livestock and other resale items"),
    "basis": ("1b", None, "Cost or other basis of the items on line 1a"),
    "raised": (
        "2",
        None,
        "Sales of livestock, produce, grains and other products you raised",
    ),
    "sales": (None, "37", "Sales of livestock, produce, grains and other products"),
    "coop": ("3a", "38a", "Cooperative distributions (Form(s) 1099-PATR)"),
    "cooptaxable": ("3b", "38b", "Cooperative distributions, taxable amount"),
    "program": ("4a", "39a", "Agricultural program payments"),
    "programtaxable": ("4b", "39b", "Agricultural program payments, taxable amount"),
    "cccelected": ("5a", "40a", "CCC loans reported under election"),
    "cccforfeited": ("5b", "40b", "CCC loans forfeited"),
    "ccctaxable": ("5c", "40c", "CCC loans forfeited, taxable amount"),
    "cropins": (
        "6a",
        "41",
        "Crop insurance proceeds and federal crop disaster payments",
    ),
    "cropinstaxable": ("6b", None, "Crop insurance proceeds, taxable amount"),
    "deferredin": ("6d", None, "Crop insurance amount deferred from 2024"),
    "custom": ("7", "42", "Custom hire (machine work) income"),
    "otherincome": ("8", "43", "Other income"),
    "begin": (None, "45", "Inventory at beginning of the year"),
    "purchased": (
        None,
        "46",
        "Cost of livestock, produce, grains and other products purchased",
    ),
    "end": (None, "48", "Inventory at end of year"),
}
# (gross word, taxable word, the taxable line's default when not typed)
TAXABLE = (
    ("coop", "cooptaxable"),
    ("program", "programtaxable"),
    ("cccforfeited", "ccctaxable"),
    ("cropins", "cropinstaxable"),
)
EXPENSES = {
    "car": ("10", "Car and truck expenses"),
    "chemicals": ("11", "Chemicals"),
    "conservation": ("12", "Conservation expenses"),
    "customhire": ("13", "Custom hire (machine work)"),
    "depreciation": ("14", "Depreciation and section 179 expense"),
    "benefits": ("15", "Employee benefit programs other than on line 23"),
    "feed": ("16", "Feed"),
    "fertilizer": ("17", "Fertilizers and lime"),
    "freight": ("18", "Freight and trucking"),
    "fuel": ("19", "Gasoline, fuel, and oil"),
    "insurance": ("20", "Insurance (other than health)"),
    "mortgage": ("21a", "Interest: mortgage (paid to banks, etc.)"),
    "interest": ("21b", "Interest: other"),
    "labor": ("22", "Labor hired (less employment credits)"),
    "pension": ("23", "Pension and profit-sharing plans"),
    "rentequipment": ("24a", "Rent or lease: vehicles, machinery, equipment"),
    "rent": ("24b", "Rent or lease: other (land, animals, etc.)"),
    "repairs": ("25", "Repairs and maintenance"),
    "seeds": ("26", "Seeds and plants"),
    "storage": ("27", "Storage and warehousing"),
    "supplies": ("28", "Supplies"),
    "taxes": ("29", "Taxes"),
    "utilities": ("30", "Utilities"),
    "vet": ("31", "Veterinary, breeding, and medicine"),
    "other": ("32a", "Other expenses"),
}
EXTRA = ("conservationcarry", "prior")
CASH_ONLY = tuple(w for w, (c, a, _) in INCOME.items() if a is None)
ACCRUAL_ONLY = tuple(w for w, (c, a, _) in INCOME.items() if c is None)
WORDS = (*INCOME, *EXPENSES, *EXTRA)
LETTERS = "ABCDEF"
MONEY_MAX = 100_000_000
CONSERVATION_SHARE = 0.25  # line 12: 25% of gross income from farming
GROSS_CASH = ("1c", "2", "3b", "4b", "5a", "5c", "6b", "6d", "7", "8")
GROSS_ACCRUAL = ("37", "38b", "39b", "40a", "40c", "41", "42", "43")


def parse(key: str, s: str) -> list[dict[str, Any]]:
    """Each farm as a dict of its typed words and flags, or [] for ``none``."""
    if s.strip().lower() == "none":
        return []
    out: list[dict[str, Any]] = []
    for entry in (e.strip() for e in s.split(";") if e.strip()):
        tokens = entry.split()
        if tokens[0].lower() != "farm":
            raise ValueError(
                f"{key}: {entry!r} starts with farm (like {EXAMPLE}), or type none"
            )
        got: dict[str, Any] = {}
        rest = tokens[1:]
        while rest and rest[0].lower() in FLAGS:
            got[rest.pop(0).lower()] = True
        if len(rest) % 2:
            raise ValueError(f"{key}: {entry!r} pairs each word with an amount")
        for word, amount in zip(rest[::2], rest[1::2], strict=True):
            word = word.lower()
            if word not in WORDS:
                raise ValueError(
                    f"{key}: {word!r} is not a farm word ({', '.join(WORDS)}); "
                    f"flags {', '.join(FLAGS)} come right after farm"
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
        _check(key, entry, got)
        out.append(got)
    if len(out) > len(LETTERS):
        raise ValueError(f"{key}: at most {len(LETTERS)} farms")
    return out


def _check(key: str, entry: str, got: dict[str, Any]) -> None:
    accrual = got.get("accrual", False)
    wrong = [w for w in (CASH_ONLY if accrual else ACCRUAL_ONLY) if w in got]
    if wrong:
        method = "accrual" if accrual else "cash"
        raise ValueError(
            f"{key}: {', '.join(wrong)} is not on the {method} method in {entry!r}"
        )
    for gross, taxable in TAXABLE:
        if accrual and taxable == "cropinstaxable":
            continue
        cap = got.get(gross, 0)
        if got.get(taxable, 0) > cap:
            raise ValueError(f"{key}: {taxable} is more than {gross} in {entry!r}")
    if "prior" in got and not got.get("nonmaterial"):
        raise ValueError(
            f"{key}: prior (a prior-year unallowed passive loss) goes with "
            f"nonmaterial; a former passive activity's loss is not handled ({entry!r})"
        )
    if not any(w in got for w in WORDS):
        raise ValueError(f"{key}: {entry!r} has no amounts")


@dataclass
class Farm:
    """One Schedule F, before the passive-loss limit."""

    letter: str
    owner: str  # you or spouse
    entry: dict[str, Any]
    lines: dict[str, float] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)
    carry: float = 0.0  # conservation expenses over the 25% limit

    @property
    def name(self) -> str:
        return f"farm {self.letter}"

    @property
    def form(self) -> str:
        return FORM if self.letter == "A" else f"{FORM} ({self.letter})"

    @property
    def passive(self) -> bool:
        return bool(self.entry.get("nonmaterial"))

    @property
    def prior(self) -> float:
        return float(self.entry.get("prior", 0))

    @property
    def net(self) -> float:
        """Line 9 less line 33."""
        return self.lines["9"] - self.lines["33"]


def _put(f: Farm, line: str, value: float, src: str) -> float:
    f.lines[line] = value
    f.source[line] = src
    return value


def farms(entries: list[dict[str, Any]]) -> list[Farm]:
    """Each farm's lines 1-33, line 9 from Part I or Part III."""
    out = []
    for letter, e in zip(LETTERS, entries, strict=False):
        f = Farm(letter, "spouse" if e.get("spouse") else "you", e)
        accrual = bool(e.get("accrual"))
        col = 1 if accrual else 0
        for word, spec in INCOME.items():
            line = spec[col]
            if line is None:
                continue
            typed = word in e
            if not typed:
                default = _taxable_default(word, e)
                if default is None:
                    continue
                _put(f, line, default[0], default[1])
                continue
            _put(f, line, float(e[word]), "typed")
        if accrual:
            l44 = _put(
                f, "44", sum(f.lines.get(k, 0.0) for k in GROSS_ACCRUAL), "37-43"
            )
            l47 = _put(
                f, "47", f.lines.get("45", 0.0) + f.lines.get("46", 0.0), "45 + 46"
            )
            end = f.lines.get("48", 0.0)
            if end > l47:  # the line 49 footnote
                l49 = _put(f, "49", end - l47, "48 - 47 (line 48 is larger)")
                l50 = _put(f, "50", l44 + l49, "44 + 49")
            else:
                l49 = _put(f, "49", l47 - end, "47 - 48")
                l50 = _put(f, "50", l44 - l49, "44 - 49")
            _put(f, "9", l50, "Part III line 50 (accrual)")
        else:
            if "1a" in f.lines or "1b" in f.lines:
                _put(
                    f, "1c", f.lines.get("1a", 0.0) - f.lines.get("1b", 0.0), "1a - 1b"
                )
            _put(f, "9", sum(f.lines.get(k, 0.0) for k in GROSS_CASH), "1c, 2, 3b-8")
        for word, (line, _) in EXPENSES.items():
            if word in e and word != "conservation":
                _put(f, line, float(e[word]), "typed")
        _conservation(f)
        _put(
            f,
            "33",
            sum(f.lines.get(ln, 0.0) for ln, _ in EXPENSES.values()),
            "10-32",
        )
        out.append(f)
    return out


def _taxable_default(word: str, e: dict[str, Any]) -> tuple[float, str] | None:
    """A taxable line not typed: the gross amount (line 5c: the forfeited
    loans unless loan proceeds were elected as income, line 5a)."""
    for gross, taxable in TAXABLE:
        if word != taxable or gross not in e:
            continue
        if taxable == "cropinstaxable" and e.get("accrual"):
            return None
        if taxable == "ccctaxable" and "cccelected" in e:
            return 0.0, "0: CCC loan proceeds elected as income (line 5a)"
        return float(e[gross]), f"{gross}: all taxable (type {taxable} if not)"
    return None


def _conservation(f: Farm) -> None:
    typed = float(f.entry.get("conservation", 0))
    carried = float(f.entry.get("conservationcarry", 0))
    if not typed and not carried:
        return
    cap = CONSERVATION_SHARE * max(f.lines["9"], 0.0)
    allowed = min(typed + carried, cap)
    f.carry = typed + carried - allowed
    _put(
        f,
        "12",
        allowed,
        f"conservation {typed:,.0f} + carried {carried:,.0f}, "
        f"at most 25% of line 9 ({cap:,.0f})",
    )


def activities(fs: list[Farm]) -> list[f8582.Activity]:
    """Each farm you did not materially participate in as a Form 8582 Part V
    activity."""
    return [
        f8582.Activity(
            f.name,
            f"{f.form}, line 34",
            income=max(f.net, 0.0),
            loss=max(-f.net, 0.0),
            prior=f.prior,
        )
        for f in fs
        if f.passive and (f.net or f.prior)
    ]


def magi_part(fs: list[Farm]) -> float:
    """What the farms add to Form 8582 line 6 modified AGI: a material
    participant's line 34, a passive farm's net income, never its loss."""
    return sum(f.net if not f.passive else max(f.net, 0.0) for f in fs)


@dataclass
class Result:
    lines: list[tuple[str, str, str, float, str]]  # (form, line, label, value, source)
    by_owner: dict[str, float]  # line 34 summed: Schedule 1 line 6, Sch SE line 1a
    notes: list[str]
    carries: list[carries.Carry] = field(default_factory=list)  # unit 6c


def _labels() -> dict[str, str]:
    out = {spec[0]: spec[2] for spec in INCOME.values() if spec[0]}
    out |= {spec[1]: spec[2] for spec in INCOME.values() if spec[1]}
    out |= {line: label for line, label in EXPENSES.values()}
    out |= {
        "1c": "Subtract line 1b from line 1a",
        "9": "Gross income",
        "33": "Total expenses",
        "34": "Net farm profit or (loss)",
        "44": "Add lines 37 through 43",
        "47": "Add lines 45 and 46",
        "49": "Cost of livestock, produce, grains and other products sold",
        "50": "Gross income",
    }
    return out


LABELS = _labels()


def _order(line: str) -> tuple[int, str]:
    digits = "".join(ch for ch in line if ch.isdigit())
    return int(digits), line


def schedule(fs: list[Farm], allowed: dict[str, float]) -> Result:
    """Every farm's lines, line 34 after Form 8582 (``allowed``: each passive
    farm's allowed loss by its name, planner.taxprep.f8582.Result.allowed)."""
    lines: list[tuple[str, str, str, float, str]] = []
    by_owner = {"you": 0.0, "spouse": 0.0}
    out: list[carries.Carry] = []
    notes: list[str] = []
    for f in fs:
        if f.passive and (f.net < 0 or f.prior):
            l34 = max(f.net, 0.0) - allowed.get(f.name, 0.0)
            src = "9 - 33, after Form 8582" + (" (PAL)" if f.prior else "")
        else:
            l34, src = f.net, "9 - 33"
        for ln in sorted(f.lines, key=_order):
            lines.append((f.form, ln, LABELS[ln], f.lines[ln], f.source[ln]))
        lines.append((f.form, "34", LABELS["34"], l34, src))
        by_owner[f.owner] += l34
        what = "accrual" if f.entry.get("accrual") else "cash"
        notes.append(
            f"{f.form} is drafted on the {what} method; line E (material "
            f"participation) is {'No' if f.passive else 'Yes'}"
        )
        if f.carry:
            out.append(
                carries.Carry(
                    f"{f.form} conservation expenses over the 25% limit",
                    round(f.carry, 2),
                    f"{f.form} line 12",
                    "the farm's word conservationcarry",
                    "Schedule F instructions, line 12",
                )
            )
            notes.append(
                f"{f.form} line 12: {f.carry:,.2f} of conservation expenses is over "
                "25% of gross income from farming and carries to next year "
                "(type it as conservationcarry next year)"
            )
        if f.entry.get("notatrisk") and l34 < 0:
            notes.append(
                f"{f.form} line 36b: some investment is not at risk, so Form 6198 "
                "may limit the loss; it is not drafted and line 34 takes the "
                "loss before the at-risk rules"
            )
    notes.append(
        "Schedule F: farm income averaging (Schedule J), the Schedule SE farm "
        "optional method, the excess business loss (Form 461) and crop-share "
        "rent (Form 4835) are not drafted"
    )
    return Result(lines, by_owner, notes, out)
