"""Married filing jointly against married filing separately (phase 10, unit 3b-2).

For a joint household with the spouse named, each spouse's separate return is
priced from their own lines: the per-person inputs (wages, SE income, IRA
distributions and the rest of PERSON_INPUTS) and their own Form 8889 line 13. The
household's other lines (interest, dividends, gains, deductions) go to whoever's
documents report them (``owner``, schema 5); what no document places (a typed
figure, a planned sale) is split equally, and a note says so.

The rules the pair is priced under:

- 2025 Form 1040 instructions, Married Filing Separately: "If your spouse
  itemizes deductions, you must also itemize". Both returns are priced standard
  and both itemized (the engine's ``separate_filer_itemizes`` zeroes the standard
  deduction), and the cheaper pair is shown.
- 2025 Form 8962 instructions, Allocation Situation 2: spouses married at year
  end, filing separately, on one policy, allocate 50% of the advance credit to
  each; without the domestic-abuse or abandonment exception neither takes the
  credit, so each repays their half against their own income (the engine
  denies the credit on a separate return).
- The children are claimed on one return; both ways are priced.
- Pub. 555: in a community property state each spouse reports half the
  community income, which the ledger cannot tell from separate income, so those
  states are not handled.

The cost of each return is levers.cost: line 24 plus the state tax, less
refundable credits and the premium credit paid out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from planner.engine.household import PERSON_INPUTS, Household, Person
from planner.engine.tax import TaxResult, compute, r
from planner.ingest.needs import NEEDS, _sum_boxes
from planner.ledger import db
from planner.paths import Layout
from planner.plan import inputs
from planner.plan.levers import cost
from planner.taxprep import hsa

# Pub. 555, Community Property: the nine community property states.
COMMUNITY_PROPERTY = ("AZ", "CA", "ID", "LA", "NV", "NM", "TX", "WA", "WI")
# Household lines split between the two returns, and the Needed lines whose
# documents place them (dividends: ordinary less qualified, per owner).
SPLIT: dict[str, tuple[str, ...]] = {
    "interest": ("interest",),
    "tax_exempt_interest": ("tax_exempt_interest",),
    "qualified_dividends": ("qualified_dividends",),
    "non_qualified_dividends": ("ordinary_dividends", "-qualified_dividends"),
    "short_term_gains": ("short_term_gains",),
    "long_term_gains": ("long_term_gains",),
    "car_loan_interest": ("car_loan_interest",),
    "charitable_cash": ("planned_giving",),
    "charitable_shares": (),
    "real_estate_taxes": ("real_estate_taxes",),
    "mortgage_interest": ("mortgage_interest",),
}
# Form 8962 Allocation Situation 2: the policy's premiums and advance, 50% each.
HALVED = ("se_health_premiums", "aptc")
WHO = ("you", "spouse")


class NotHandled(ValueError):
    """The comparison cannot be priced for this household."""


@dataclass
class Return:
    who: str
    household: Household
    result: TaxResult

    @property
    def cost(self) -> float:
        return cost(self.result)


@dataclass
class Comparison:
    year: int
    joint: TaxResult
    you: Return
    spouse: Return
    itemize: bool  # both separate returns itemize (else both take the standard)
    dependents_with: str | None  # whose return claims the children
    # every pair priced: itemize, or (itemize, dependents_with) with children
    priced: dict[Any, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # left out, not zero

    @property
    def joint_cost(self) -> float:
        return cost(self.joint)

    @property
    def separate_cost(self) -> float:
        return r(self.you.cost + self.spouse.cost)

    @property
    def saving(self) -> float:
        """What filing jointly saves over two separate returns (negative: the
        separate returns cost less)."""
        return r(self.separate_cost - self.joint_cost)


def _documented(lay: Layout, year: int) -> dict[str, dict[str, float]]:
    """Each split line's documented amount per owner."""
    by_key = {n.key: n for n in NEEDS}
    out: dict[str, dict[str, float]] = {}
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        for name, keys in SPLIT.items():
            out[name] = {}
            for who in WHO:
                total = 0.0
                for key in keys:
                    sign, key = (-1.0, key[1:]) if key.startswith("-") else (1.0, key)
                    need = by_key[key]
                    hit = (
                        _sum_boxes(conn, need.boxes, year, who, need.docs)
                        if need.boxes
                        else None
                    )
                    total += sign * (hit[0] if hit else 0.0)
                out[name][who] = total
        out["hsa_contribution"] = {}  # each one's own Form 8889 line 13
        for who, form in zip(WHO, ("8889", hsa.SPOUSE_FORM), strict=True):
            hit = _sum_boxes(conn, ((form, "13"),), year)
            out["hsa_contribution"][who] = hit[0] if hit else 0.0
    finally:
        conn.close()
    return out


def _split(
    hh: Household, documented: dict[str, dict[str, float]], notes: list[str]
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {who: {} for who in WHO}
    for name in (*SPLIT, "hsa_contribution"):
        total = int(getattr(hh, name))
        doc = documented.get(name, {})
        yours, theirs = doc.get("you", 0.0), doc.get("spouse", 0.0)
        rest = total - yours - theirs
        if name == "hsa_contribution" and rest:  # a planned figure, not a form's
            yours = theirs = 0.0
            rest = total
        if rest:
            notes.append(
                f"{name}: {rest:,.0f} that no document places split equally "
                "between the two returns"
            )
        mine = int(round(yours + rest / 2))
        out["you"][name], out["spouse"][name] = mine, total - mine
    for name in HALVED:
        total = int(getattr(hh, name))
        out["you"][name] = total // 2
        out["spouse"][name] = total - total // 2
    return out


def _alone(
    hh: Household, sp: Person, who: str, lines: Mapping[str, Any], itemize: bool
) -> Household:
    base = replace(
        hh,
        filing_status="SEPARATE",
        spouse=None,
        dependents=(),
        tax_unit_inputs={
            **hh.tax_unit_inputs,
            "tax_unit_itemizes": itemize,
            "separate_filer_itemizes": itemize,
        },
        **lines,
    )
    if who == "you":
        return base
    return replace(base, age=sp.age, **{k: getattr(sp, k) for k in PERSON_INPUTS})


def compare(
    lay: Layout, year: int, overrides: inputs.Overrides | None = None
) -> Comparison:
    inp = inputs.build(lay, year, overrides)
    hh = inp.household
    if hh.filing_status != "JOINT" or hh.spouse is None:
        raise NotHandled(
            "the comparison is for a married couple filing jointly with the "
            "spouse named (planner enter filing_status married_joint, then "
            "spouse_birth_date)"
        )
    if hh.state in COMMUNITY_PROPERTY:
        raise NotHandled(
            f"{hh.state} is a community property state (Pub. 555): each spouse "
            "reports half the community income, which the ledger does not tell "
            "from separate income; the separate returns are not priced"
        )
    notes = list(inp.notes)
    lines = _split(hh, _documented(lay, year), notes)
    sides = (None,) if not hh.dependents else WHO
    priced: dict[Any, float] = {}
    pairs: dict[Any, tuple[bool, str | None, list[Return]]] = {}
    for itemize in (False, True):
        for kids in sides:
            pair = [
                Return(
                    who,
                    h := replace(
                        _alone(hh, hh.spouse, who, lines[who], itemize),
                        dependents=hh.dependents if kids == who else (),
                    ),
                    compute(year, h),
                )
                for who in WHO
            ]
            total = r(pair[0].cost + pair[1].cost)
            key = itemize if kids is None else (itemize, kids)
            priced[key], pairs[key] = total, (itemize, kids, pair)
    itemize, kids, (you, spouse) = pairs[min(priced, key=priced.__getitem__)]
    notes.append(
        "the premium credit's policy is split 50% to each return and neither "
        "return takes the credit (Form 8962, Allocation Situation 2; the "
        "domestic-abuse and abandonment exceptions are not handled)"
        if hh.aptc or hh.se_health_premiums
        else ""
    )
    return Comparison(
        year,
        joint=compute(year, hh),
        you=you,
        spouse=spouse,
        itemize=itemize,
        dependents_with=kids,
        priced=priced,
        notes=[n for n in notes if n],
        unknown=inp.tax_unknown,
    )


def render(c: Comparison) -> str:
    def line(label: str, res: TaxResult, amount: float) -> str:
        return (
            f"  {label:24} AGI {res.agi:>12,.2f}  federal {res.fed_total_tax:>11,.2f}  "
            f"state {res.state_tax:>10,.2f}  cost {amount:>12,.2f}\n"
        )

    out = f"{c.year} married filing jointly against separately\n"
    out += line("married filing jointly", c.joint, c.joint_cost)
    out += line("you, separately", c.you.result, c.you.cost)
    out += line("spouse, separately", c.spouse.result, c.spouse.cost)
    how = "both itemize" if c.itemize else "both take the standard deduction"
    if c.dependents_with:
        whose = "your" if c.dependents_with == "you" else "the spouse's"
        how += f"; the children on {whose} return"
    out += f"  separate total {c.separate_cost:,.2f} ({how})\n"
    if c.saving >= 0:
        out += f"joint saves {c.saving:,.2f}\n"
    else:
        out += f"separate saves {-c.saving:,.2f}\n"
    for n in c.notes:
        out += f"note: {n}\n"
    if c.unknown:
        out += f"unknown (left out, not zero): {', '.join(c.unknown)}\n"
    return out
