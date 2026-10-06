"""The coverage gate (Phase 10, unit 2a). It runs right after intake, before any
plan or draft, and names each fact the planner cannot answer correctly: a
household shape the engine is not given, a state whose return is not drafted, a
document no template reads. Each gap carries what to do (a Needed line) and the
sections it touches, which are tagged "not handled"; the rest still runs. Every
other section is "verified" when its capability row is, else "estimated"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from planner import states
from planner.config import load_capabilities
from planner.paths import Layout
from planner.taxprep import statereturn

VERIFIED, ESTIMATED, NOT_HANDLED = "verified", "estimated", "not handled"
DRAFTED_STATES = statereturn.RETURNS  # the states whose return the draft lays out
STATE_RETURN = "state return"  # what a state gap touches: that return alone
# Every section whose figure rests on the household's income and deductions;
# "draft" stands for each form of the draft return.
PRICED = (
    "glide",
    "spending",
    "magi",
    "levers",
    "conversion",
    "esttax",
    "cash",
    "draft",
)

# A status whose answer turns on a second person, until the profile names that
# person (unit 3a-2: the spouse's birth date, the dependents), is priced as the
# one person and tagged, never passed off as a full plan (master plan unit 0b).
# Separate returns stay tagged until unit 3b. A qualifying surviving spouse
# (unit 3b-1) is tagged without a dependent child or outside the two years
# after the year of death (2025 Form 1040 instructions). A joint return after
# the year the spouse died (unit 3b-3) is tagged the same way.
HOUSEHOLD = {
    "JOINT": (
        "Not handled: married filing jointly is priced for one person; the "
        "spouse's income, age, deductions and credits are left out, so every "
        "figure here is this person's share only, not the joint return"
    ),
    "SEPARATE": (
        "Not handled: married filing separately is priced from this person's "
        "figures alone; the spouse's choice to itemize (which binds this "
        "return), a community-property split and the spouse's figures are left out"
    ),
    "HEAD_OF_HOUSEHOLD": (
        "Not handled: head of household is priced with no qualifying person; "
        "dependents' credits (child tax credit, earned income credit with "
        "children, dependent care) and the larger household for the ACA credit "
        "and benefits are left out"
    ),
    "SURVIVING_SPOUSE": (
        "Not handled: qualifying surviving spouse is priced at joint rates, but "
        "the status needs a dependent child (a child or stepchild, not a foster "
        "child, who lived with you all year) and a spouse who died in either of "
        "the two years before the tax year; as typed the return does not qualify"
    ),
}


HOUSEHOLD_NEEDED = {
    "JOINT": "type the spouse's birth date (planner enter spouse_birth_date "
    "YYYY-MM-DD) and the dependents (planner enter dependents ..., or none)",
    "SEPARATE": "have a preparer price the household, or plan as single until "
    "separate returns arrive (unit 3b)",
    "HEAD_OF_HOUSEHOLD": "name the qualifying person (planner enter dependents "
    "YYYY-MM-DD [student|disabled], ...)",
    "SURVIVING_SPOUSE": "name the child (planner enter dependents YYYY-MM-DD, ...)",
}


def death_year(answer: object) -> int | None:
    """The year of a stored spouse_death_date; None when unset or "none"."""
    if not answer or answer == "none":
        return None
    return date.fromisoformat(str(answer)).year


def _died_joint(death_year: int, year: int) -> tuple[str, str]:
    """A joint return for a year after the spouse's death: the reason and what
    to file instead (2025 Form 1040 instructions: a joint return "if ... your
    spouse died in 2025"; qualifying surviving spouse for the two years after)."""
    return (
        f"Not handled: married filing jointly for {year}, but the spouse died in "
        f"{death_year}; a joint return is filed for the year of death at the latest",
        f"for {death_year + 1} and {death_year + 2} the status is "
        "qualifying_surviving_spouse while a dependent child lives at home, "
        "otherwise single or head_of_household (planner enter filing_status ...); "
        "check spouse_death_date",
    )


def _widowed_needed(death_year: int, year: int) -> str:
    """What a surviving spouse whose year of death falls outside the two years
    before ``year`` files instead (2025 Form 1040 instructions)."""
    if death_year >= year:
        return (
            f"a spouse who died in {year} files a joint return for {year} "
            "(planner enter filing_status married_joint); check spouse_death_date"
        )
    return (
        f"from {death_year + 3} the status is single or head_of_household "
        "(planner enter filing_status ...); check spouse_death_date"
    )


# Once the people are named nothing about them stays tagged: the spouse's own
# lines, Schedule SE and Form 8889 are theirs (units 3a-4, 3a-5 and 3a-7).


@dataclass(frozen=True)
class Gap:
    area: str  # household | state | document
    reason: str  # "Not handled: ...", the line every copy shows
    needed: str  # what to do about it, on the Needed list
    touches: tuple[str, ...]  # the sections it makes "not handled"


def _documents(lay: Layout) -> list[Gap]:
    folder = lay.data / "inbox" / "UNMATCHED"
    out = []
    for path in sorted(folder.glob("*")) if folder.is_dir() else []:
        if path.name.endswith(".reason.txt") or not path.is_file():
            continue
        note = path.with_name(path.name + ".reason.txt")
        why = note.read_text(encoding="utf-8").strip() if note.exists() else ""
        out.append(
            Gap(
                "document",
                f"Not handled: {path.name} was not read"
                + (f": {why}" if why else "")
                + "; any income, deduction or payment on it is left out",
                f"{path.name}: type its figures (planner enter), or move it out of "
                "data/inbox/UNMATCHED if it holds no tax figures",
                PRICED,
            )
        )
    return out


def gate(
    lay: Layout,
    state: str | None,
    filing_status: str | None,
    *,
    spouse: bool = False,
    dependents: int = 0,
    death_year: int | None = None,
    year: int | None = None,
) -> list[Gap]:
    """Every gap for this household, in a fixed order: household, state, documents.
    ``filing_status`` is the engine's (SINGLE, JOINT, ...); None when not yet known.
    ``spouse`` and ``dependents`` are the people the profile names; ``death_year``
    the spouse's year of death, checked against the tax ``year`` for a surviving
    spouse and a joint return."""
    out = []
    unnamed = (
        filing_status == "SEPARATE"
        or (filing_status == "JOINT" and not spouse)
        or (
            filing_status in ("HEAD_OF_HOUSEHOLD", "SURVIVING_SPOUSE")
            and not dependents
        )
    )
    widowed = (
        filing_status == "SURVIVING_SPOUSE"
        and death_year is not None
        and year is not None
        and int(death_year) not in (year - 2, year - 1)
    )
    if (unnamed or widowed) and filing_status:
        out.append(
            Gap(
                "household",
                HOUSEHOLD[filing_status],
                HOUSEHOLD_NEEDED[filing_status]
                if unnamed
                else _widowed_needed(int(death_year or 0), int(year or 0)),
                PRICED,
            )
        )
    elif (
        filing_status == "JOINT"
        and death_year is not None
        and year is not None
        and int(death_year) < year
    ):
        out.append(Gap("household", *_died_joint(int(death_year), year), PRICED))
    if state and state not in states.STATES:
        out.append(
            Gap(
                "state",
                f"Not handled: {state!r} is not a state's two-letter code, so no "
                "state income tax is priced",
                "type the state as its two-letter code (NC, CA, TX)",
                PRICED,
            )
        )
    elif state and states.get(state).income_tax and state not in DRAFTED_STATES:
        out.append(
            Gap(
                "state",
                f"Not handled: the {state} return is not drafted; the plan's {state} "
                "income tax is the engine's estimate",
                f"have a preparer draft the {state} return",
                (STATE_RETURN,),
            )
        )
    return out + _documents(lay)


def tag(gaps: list[Gap], section: str, status: str | None) -> str:
    """not handled when a gap touches ``section``; else verified when its
    capability row (``status``) is, else estimated."""
    if any(section in g.touches for g in gaps):
        return NOT_HANDLED
    return VERIFIED if status == "verified" else ESTIMATED


def statuses(lay: Layout) -> dict[str, str]:
    """The capability rows (``config/capabilities.yaml``); none when absent."""
    path = lay.config / "capabilities.yaml"
    return load_capabilities(path) if path.exists() else {}


# Readiness is three answers (unit 2a-2), never "no open questions": an empty
# Needed list reached by setting facts aside readies nothing (finding F14).
# The needs outputs a move rests on (planner.ingest.needs.OUTPUTS); a fact set
# aside that feeds one holds back acting on the plan.
ACTS = (
    "Glide path",
    "Spending band",
    "MAGI headroom",
    "Levers",
    "Roth conversion",
    "Withdrawal plan",
    "Estimated tax",
    "Cash buffer",
)
SHOWN = 5  # reasons each answer lists before "and N more"


@dataclass(frozen=True)
class Item:
    label: str
    unlocks: tuple[str, ...] = ()  # the needs outputs it feeds; () = every one


@dataclass(frozen=True)
class Answer:
    question: str  # "Ready to plan", "Ready to act", "Ready for a preparer"
    blockers: tuple[str, ...] = ()  # each reason it is not, in a fixed order

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def line(self) -> str:
        if self.ready:
            return f"{self.question}: yes"
        more = len(self.blockers) - SHOWN
        tail = f"; and {more} more" if more > 0 else ""
        return f"{self.question}: no: " + "; ".join(self.blockers[:SHOWN]) + tail


def _unique(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def readiness(
    gaps: list[Gap],
    *,
    open_items: int,
    set_aside: list[Item],
    estimates: list[Item],
    year: int,
    year_open: bool,
    draft_blocked: str = "",
) -> tuple[Answer, Answer, Answer]:
    """Ready to plan, to act, for a preparer. Plan: nothing open and no gap in a
    planner section (estimates are fine, they are tagged). Act: that, and no fact
    set aside that a move rests on. Preparer: nothing open, set aside or still an
    estimate, no gap at all, the year ended and the draft built."""
    opened = [f"{open_items} open on the Needed list"] if open_items else []
    planned = [g.reason for g in gaps if set(g.touches) & set(PRICED[:-1])]
    aside = [f"set aside, not on hand: {i.label}" for i in set_aside]
    moves = [
        f"set aside, not on hand: {i.label}"
        for i in set_aside
        if not i.unlocks or set(i.unlocks) & set(ACTS)
    ]
    prep = [
        *opened,
        *(g.reason for g in gaps),
        *aside,
        *(f"still an estimate: {i.label}" for i in estimates),
        *(
            [f"the {year} tax year is still open; its final forms arrive in January"]
            if year_open
            else []
        ),
        *([draft_blocked] if draft_blocked else []),
    ]
    return (
        Answer("Ready to plan", _unique(opened + planned)),
        Answer("Ready to act", _unique(opened + planned + moves)),
        Answer("Ready for a preparer", _unique(prep)),
    )
