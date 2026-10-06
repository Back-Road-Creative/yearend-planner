"""The state returns the draft lays out, one entry per state: the draft forms
it adds (each with its heading and capability row), the filed-return template
``planner close`` reads and the boxes it compares, the tax line next year's
safe harbor carries and the key it carries under, the typed Needed keys and
engine variables the return reads, and the function that lays its lines.

NC's D-400 is the first entry. A drafted state is a module beside d400.py, a
template under templates/forms/ (``issuer`` is the state's code) and an entry
here; the draft, ``planner close``, the rollover and the coverage gate read
this table, so nothing else names a state's return."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from planner.taxprep import d400


@dataclass(frozen=True)
class StateReturn:
    code: str
    forms: dict[str, tuple[str, str]]  # draft form -> (heading, capability row)
    form: str  # the return itself, among ``forms``
    template: str  # the filed return's template form id (templates/forms/)
    boxes: tuple[str, ...]  # template boxes; the draft form numbers them the same
    tax_line: str  # the state's income tax: what the safe harbor carries
    carry: str  # the rollover key it carries under (CARRY-EST)
    keys: tuple[str, ...]  # typed Needed keys the return reads
    engine: tuple[str, ...]  # engine variables the return reads
    # (add, notes, values, facts, paid, year, agi, typed, married): lays the
    # lines; ``paid`` is (date, amount, origin) per estimated payment to the state
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
        keys=d400.KEYS,
        engine=d400.ENGINE,
        lay=d400.lay_lines,
    ),
}


def get(code: str | None) -> StateReturn | None:
    """The drafted return for a state code (any case); None when the draft
    does not lay that state's return out."""
    return RETURNS.get((code or "").upper())
