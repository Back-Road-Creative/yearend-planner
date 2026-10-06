"""The state returns the draft lays out, one entry per state: the draft forms
it adds (each with its heading and capability row), the filed-return template
``planner close`` reads and the boxes it compares, the tax line next year's
safe harbor carries and the key it carries under, the typed Needed keys and
engine variables the return reads, and the function that lays its lines.

NC's D-400 is the first entry, CA's Form 540 the second (unit 3d-6), NY's
IT-201 the third (unit 3d-7), PA's PA-40 the fourth (unit 3d-8), IL's
IL-1040 the fifth (unit 3d-9), OH's IT 1040 the sixth (unit 3d-10). A
drafted state is a module beside d400.py, a template under templates/forms/
(``issuer`` is the state's code; one per page when the return runs to several)
and an entry here; the draft, ``planner close``, the rollover and the
coverage gate read this table, so nothing else names a state's return."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from planner.taxprep import ca540, d400, il1040, ny201, oh1040, pa40


@dataclass(frozen=True)
class StateReturn:
    code: str
    forms: dict[str, tuple[str, str]]  # draft form -> (heading, capability row)
    form: str  # the return itself, among ``forms``
    template: str  # the filed return's template form id (templates/forms/)
    boxes: tuple[str, ...]  # template boxes; the draft form numbers them the same
    tax_line: str  # the state's income tax: what the safe harbor carries
    carry: str  # the rollover key it carries under (CARRY-EST)
    # The lines next year's safe harbor sums, from the filed return or the
    # draft alike; a "-" before a line subtracts it (NY's refundable credits).
    prior: tuple[str, ...]
    keys: tuple[str, ...]  # typed Needed keys the return reads
    engine: tuple[str, ...]  # engine variables the return reads
    # (add, notes, values, facts, paid, year, agi, typed, status): lays the
    # lines; ``paid`` is (date, amount, origin) per estimated payment to the
    # state, ``status`` the filing status (Household.filing_status)
    lay: Callable[..., None]


RETURNS: dict[str, StateReturn] = {
    "NC": StateReturn(
        code="NC",
        forms={
            d400.FORM: ("NC Form D-400", "nc_d400_draft"),
            d400.SCHED: (
                "NC D-400 Schedule S (additions and deductions)",
                "nc_d400_draft",
            ),
        },
        form=d400.FORM,
        template="NC-D400",
        boxes=("6", "12b", "15", "20a", "21a", "23", "26a", "28", "34"),
        tax_line="15",
        carry="nc_tax",  # prior_nc_tax's estimate, kept from v0.1.0
        prior=("15",),
        keys=d400.KEYS,
        engine=d400.ENGINE,
        lay=d400.lay_lines,
    ),
    "CA": StateReturn(
        code="CA",
        forms={ca540.FORM: ("CA Form 540", "ca_540_draft")},
        form=ca540.FORM,
        template="CA-540",
        boxes=("13", "17", "19", "31", "48", "64", "71", "72", "78", "97", "99")
        + ("100", "111", "115"),
        # 540-ES 2026 worksheet line 19b: lines 48, 61 and 62 of the 2025 540
        # (line 64 adds line 63, which the safe harbor leaves out).
        tax_line="64",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("48", "61", "62"),
        keys=ca540.KEYS,
        engine=ca540.ENGINE,
        lay=ca540.lay_lines,
    ),
    "NY": StateReturn(
        code="NY",
        forms={ny201.FORM: ("NY Form IT-201", "ny_it201_draft")},
        form=ny201.FORM,
        template="NY-IT201",
        boxes=("19", "33", "37", "39", "46", "59", "61", "62", "72", "75", "76")
        + ("77", "78", "80"),
        # IT-2105.9-I line 16 worksheet: lines 46 and 58 of the 2025 IT-201 less
        # the credits on lines 63-71 (less a STAR credit check, not on the form).
        tax_line="46",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("46", "58", "-63", "-64", "-65", "-66", "-67", "-68", "-69")
        + ("-69a", "-70", "-70a", "-71"),
        keys=ny201.KEYS,
        engine=ny201.ENGINE,
        lay=ny201.lay_lines,
    ),
    "PA": StateReturn(
        code="PA",
        forms={
            pa40.FORM: ("PA Form PA-40", "pa_40_draft"),
            pa40.SP: ("PA Schedule SP (tax forgiveness)", "pa_40_draft"),
        },
        form=pa40.FORM,
        template="PA-PA40",
        boxes=("9", "11", "12", "13", "15", "21", "23", "24", "25", "26", "28")
        + ("29", "30"),
        # REV-1630 Part II: the prior year's line 12 tax less its line 21
        # forgiveness (the 2024 PA-40 line 11 at 3.07% less line 21).
        tax_line="12",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("12", "-21"),
        keys=pa40.KEYS,
        engine=pa40.ENGINE,
        lay=pa40.lay_lines,
    ),
    "IL": StateReturn(
        code="IL",
        forms={il1040.FORM: ("IL Form IL-1040", "il_1040_draft")},
        form=il1040.FORM,
        template="IL-IL1040",
        boxes=("1", "9", "10", "11", "14", "16", "18", "19", "21", "23", "25")
        + ("26", "29", "30", "31", "32", "33", "38", "41"),
        # IL-2210 Step 2 lines 1-2: the prior year's lines 14 and 22 less the
        # credits on lines 15, 16, 17, 28, 29 and 30.
        tax_line="14",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("14", "22", "-15", "-16", "-17", "-28", "-29", "-30"),
        keys=il1040.KEYS,
        engine=il1040.ENGINE,
        lay=il1040.lay_lines,
    ),
    "OH": StateReturn(
        code="OH",
        forms={
            oh1040.FORM: ("OH Form IT 1040", "oh_it1040_draft"),
            oh1040.ADJ: ("OH Schedule of Adjustments", "oh_it1040_draft"),
            oh1040.BUS: ("OH Schedule of Business Income", "oh_it1040_draft"),
            oh1040.CRED: ("OH Schedule of Credits", "oh_it1040_draft"),
        },
        form=oh1040.FORM,
        template="OH-IT1040",
        boxes=("1", "3", "4", "5", "6", "7", "8c", "9", "10", "12", "13", "14")
        + ("15", "16", "17", "20", "22", "23", "26"),
        # IT/SD 2210: the prior year's tax liability is IT 1040 line 10 less the
        # refundable credits on line 16.
        tax_line="10",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("10", "-16"),
        keys=oh1040.KEYS,
        engine=oh1040.ENGINE,
        lay=oh1040.lay_lines,
    ),
}


def prior_boxes(carry: str) -> tuple[tuple[str, str], ...]:
    """The filed-return (template, line) pairs the prior-year Needed item
    carried under ``carry`` sums: one state's return is on file a year."""
    return tuple(
        (r.template, line)
        for r in RETURNS.values()
        if r.carry == carry
        for line in r.prior
    )


def signed(line: str) -> tuple[str, float]:
    """A ``prior`` entry as (line, sign): "-63" subtracts line 63."""
    return (line[1:], -1.0) if line.startswith("-") else (line, 1.0)


def carried(get: Callable[[str, str], float | None], ret: StateReturn) -> float | None:
    """The safe-harbor tax a draft carries: the signed sum of the ``prior``
    lines it laid (``get`` is Draft.get), None when it laid none; the same
    lines a filed return is read for."""
    got = [(sign, get(ret.form, line)) for line, sign in map(signed, ret.prior)]
    if all(v is None for _, v in got):
        return None
    return round(sum(sign * v for sign, v in got if v is not None), 2)


def get(code: str | None) -> StateReturn | None:
    """The drafted return for a state code (any case); None when the draft
    does not lay that state's return out."""
    return RETURNS.get((code or "").upper())
