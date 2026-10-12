"""Master plan unit 5a: the benefits registry and its three-way screen. The
rule tests build Facts by hand; the engine tests use synthetic households
(single filers at 18,000 of wages in NC and TX, a head of household with one
child, a 67-year-old on a 15,000 pension) and the synthetic layout of
test_spending."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest
from typer.testing import CliRunner

from planner import benefits as bn
from planner.cli import app
from planner.engine.household import Dependent, Household
from planner.paths import Layout
from planner.plan import year
from tests.test_spending import AS_OF, lay  # noqa: F401

runner = CliRunner()
ADULT = bn.Member("you", 40, medicaid=False, chip=False)
BASE = bn.Facts(2026, "SINGLE", 2.0, 200.0, True, 0.0, (ADULT,))


def at(ratio: float, **kw: object) -> bn.Facts:
    return replace(BASE, ratio=ratio, **kw)  # type: ignore[arg-type]


def status(f: bn.Facts) -> dict[str, str]:
    return {x.key: x.status for x in bn.screen(f)}


@pytest.mark.parametrize(
    ("ratio", "av"),
    [(1.0, 94), (1.5, 94), (1.5001, 87), (2.0, 87), (2.0001, 73), (2.5, 73)],
)
def test_csr_bands_at_their_edges(ratio: float, av: int) -> None:
    got = bn.screen(at(ratio))[1]
    assert got.status == bn.POSSIBLE and f"at {av}% actuarial value" in got.why[0]


def test_csr_ends_past_250_and_the_credit_past_400_in_a_capped_year() -> None:
    assert status(at(2.5001))["aca_csr"] == bn.NOT
    assert status(at(4.0))["aca_ptc"] == bn.POSSIBLE
    assert status(at(4.0001))["aca_ptc"] == bn.NOT
    assert status(at(5.0, capped=False))["aca_ptc"] == bn.POSSIBLE
    assert status(at(0.9999)) == {
        "aca_ptc": bn.NOT,
        "aca_csr": bn.NOT,
        "medicaid": bn.NOT,
        "chip": bn.NOT,
    }


def test_separate_returns_and_medicare_age_end_the_credit() -> None:
    sep = bn.screen(at(2.0, filing_status="SEPARATE"))
    assert sep[0].status == bn.NOT and "36B(c)(1)(C)" in sep[0].why[0]
    assert sep[1].status == bn.NOT
    old = bn.Member("you", 66, medicaid=False, chip=False)
    assert status(at(2.0, members=(old,)))["aca_ptc"] == bn.UNKNOWN
    mixed = bn.screen(at(2.0, members=(ADULT, replace(old, label="spouse"))))[0]
    assert mixed.status == bn.POSSIBLE
    assert "not for spouse once on Medicare (65 or older)" in mixed.why


def test_medicaid_waits_on_assets_at_65_and_rolls_up_by_member() -> None:
    old = bn.Member("you", 67, medicaid=True, chip=False)
    got = bn.medicaid(at(0.9, members=(old,)))
    assert got.status == bn.UNKNOWN and "435.603(j)" in got.why[0]
    kid = bn.Member("dependent 1", 8, medicaid=True, chip=False)
    both = bn.medicaid(at(2.1, members=(ADULT, kid)))
    assert both.status == bn.POSSIBLE
    assert both.why[1] == "dependent 1 (age 8): in a MAGI group at 200% of the line"
    credit = bn.ptc(at(2.1, members=(ADULT, kid)))
    assert "not for dependent 1: Medicaid or CHIP instead (36B(c)(2)(B))" in credit.why


def test_chip_needs_a_child_outside_medicaid() -> None:
    assert bn.chip(BASE).status == bn.NOT
    kid = bn.Member("dependent 1", 18, medicaid=False, chip=True)
    assert bn.chip(at(2.5, members=(ADULT, kid))).status == bn.POSSIBLE
    on_medicaid = replace(kid, medicaid=True, chip=False)
    got = bn.chip(at(1.2, members=(ADULT, on_medicaid)))
    assert got.status == bn.NOT and "457.310(b)(2)(i)" in got.why[0]
    adult_child = bn.Member("dependent 1", 19, medicaid=False, chip=False)
    assert bn.chip(at(2.0, members=(ADULT, adult_child))).status == bn.NOT


def test_every_program_fills_every_registry_field() -> None:
    assert [p.key for p in bn.REGISTRY] == ["aca_ptc", "aca_csr", "medicaid", "chip"]
    for p in bn.REGISTRY:
        for f in fields(p):
            assert getattr(p, f.name), (p.key, f.name)
        assert p.reviewed == bn.REVIEWED
    text = "\n".join(bn.program_lines())
    for label in ("place", "who", "dates", "look-back", "assets", "sources"):
        assert text.count(f"  {label}: ") == len(bn.REGISTRY)
    assert "45 CFR 156.420(a)(1)-(3)" in text and "42 CFR 457.310(b)" in text
    res = runner.invoke(app, ["benefits", "--programs"])
    assert res.exit_code == 0 and "Premium tax credit (marketplace) [aca_ptc]" in (
        res.output
    )


@pytest.mark.engine
def test_engine_screen_across_states_and_members() -> None:
    def run(**kw: object) -> dict[str, bn.Result]:
        hh = Household(**kw)  # type: ignore[arg-type]
        return {x.key: x for x in bn.assess(2026, hh).results}

    tx = run(age=40, filing_status="SINGLE", state="TX", wages=18_000)
    assert tx["aca_ptc"].status == bn.POSSIBLE and (tx["aca_ptc"].amount or 0) > 0
    assert "94% actuarial value" in tx["aca_csr"].why[0]
    assert tx["medicaid"].status == bn.NOT  # Texas has not expanded Medicaid
    nc = run(age=40, filing_status="SINGLE", state="NC", wages=18_000)
    assert nc["medicaid"].status == bn.POSSIBLE and nc["aca_ptc"].status == bn.NOT
    fam = run(
        age=35,
        filing_status="HEAD_OF_HOUSEHOLD",
        state="NC",
        wages=45_000,
        dependents=(Dependent(age=8),),
    )
    assert fam["medicaid"].status == bn.POSSIBLE
    assert fam["medicaid"].why[0].startswith("you (age 35): not in a Medicaid group")
    assert (
        fam["chip"].status == bn.NOT
        and "Medicaid-eligible instead" in (fam["chip"].why[0])
    )
    assert fam["aca_ptc"].status == bn.POSSIBLE
    old = run(age=67, filing_status="SINGLE", state="NC", pension_income=15_000)
    assert old["medicaid"].status == bn.UNKNOWN
    assert old["aca_ptc"].status == bn.UNKNOWN


@pytest.mark.engine
def test_benefits_section_and_command_on_the_layout(lay: Layout) -> None:  # noqa: F811
    section = year.assemble(lay, 2026, AS_OF).section("benefits")
    assert section is not None and section.ok
    assert section.lines[0].startswith("Premium tax credit (marketplace): ")
    assert any("a screen, not a decision" in ln for ln in section.lines)
    assert runner.invoke(app, ["benefits"]).exit_code == 2
    res = runner.invoke(app, ["benefits", "--year", "2026"])
    assert res.exit_code == 0, res.output
    assert "Medicaid: " in res.output and "apply: " in res.output
    assert "note: the screen reads the projected full-year income" in res.output
