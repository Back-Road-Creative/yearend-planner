"""The state returns the draft lays out, one entry per state: the draft forms
it adds (each with its heading and capability row), the filed-return template
``planner close`` reads and the boxes it compares, the tax line next year's
safe harbor carries and the key it carries under, the typed Needed keys and
engine variables the return reads, and the function that lays its lines.

NC's D-400 is the first entry, CA's Form 540 the second (unit 3d-6). A
drafted state is a module beside d400.py, a template under templates/forms/
(``issuer`` is the state's code; one per page when the return runs to several)
and an entry here; the draft, ``planner close``, the rollover and the
coverage gate read this table, so nothing else names a state's return."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from planner.taxprep import ca540, d400


@dataclass(frozen=True)
class StateReturn:
    code: str
    forms: dict[str, tuple[str, str]]  # draft form -> (heading, capability row)
    form: str  # the return itself, among ``forms``
    template: str  # the filed return's template form id (templates/forms/)
    boxes: tuple[str, ...]  # template boxes; the draft form numbers them the same
    tax_line: str  # the state's income tax: what the safe harbor carries
    carry: str  # the rollover key it carries under (CARRY-EST)
    prior: tuple[str, ...]  # the filed return's lines whose sum the safe harbor reads
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
        # 540-ES 2026 worksheet line 19b: lines 48, 61 and 62 of the 2025 540;
        # line 64 adds line 63, which the draft leaves empty, so a filed 540
        # carries the three lines and the draft carries line 64.
        tax_line="64",
        carry="state_tax",  # prior_state_tax's estimate
        prior=("48", "61", "62"),
        keys=ca540.KEYS,
        engine=ca540.ENGINE,
        lay=ca540.lay_lines,
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


def get(code: str | None) -> StateReturn | None:
    """The drafted return for a state code (any case); None when the draft
    does not lay that state's return out."""
    return RETURNS.get((code or "").upper())
