"""The coverage gate (Phase 10, unit 2a). It runs right after intake, before any
plan or draft, and names each fact the planner cannot answer correctly: a
household shape the engine is not given, a state whose return is not drafted, a
document no template reads. Each gap carries what to do (a Needed line) and the
sections it touches, which are tagged "not handled"; the rest still runs. Every
other section is "verified" when its capability row is, else "estimated"."""

from __future__ import annotations

from dataclasses import dataclass

from planner.config import load_capabilities
from planner.paths import Layout

VERIFIED, ESTIMATED, NOT_HANDLED = "verified", "estimated", "not handled"
DRAFTED_STATES = ("NC",)  # the states whose return the draft lays out
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

# The planner models one person and no dependents. A status whose answer turns
# on a second person is priced as that one person and tagged, never passed off
# as a full plan (master plan unit 0b; lifted when unit 3a models the household).
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
}


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


def gate(lay: Layout, state: str | None, filing_status: str | None) -> list[Gap]:
    """Every gap for this household, in a fixed order: household, state, documents.
    ``filing_status`` is the engine's (SINGLE, JOINT, ...); None when not yet known."""
    out = []
    if filing_status in HOUSEHOLD:
        out.append(
            Gap(
                "household",
                HOUSEHOLD[filing_status],
                "have a preparer price the household, or plan as single until the "
                "household model (unit 3a) arrives",
                PRICED,
            )
        )
    if state and state not in DRAFTED_STATES:
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
