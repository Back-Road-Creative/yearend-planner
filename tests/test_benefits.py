"""Master plan units 5a-5d: the benefits registry and its three-way
screen. The rule tests build Facts by hand; the engine tests use synthetic
households (single filers at 18,000 of wages in NC and TX, a head of household
with one child, a 67-year-old on a 15,000 pension, 67-year-olds on 12,000,
18,000, 20,500 and 22,000 of Social Security, a 63-year-old on 150,000 of
wages, a couple of 70 and 66 on a 250,000 pension; for food and cash, heads of
household on 25,000 with children of 3 and 8 in NC and CA, one on 50,000 with
a child of 8 in TX, a 70-year-old on 10,000 of Social Security; for the
utility and state rows, the NY family, a single filer on 40,000 in NC, a
70-year-old in MN on 30,000 with 3,000 of property taxes) and the
synthetic layout of test_spending."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest
from typer.testing import CliRunner

from planner import benefits as bn
from planner.cli import app
from planner.engine.household import Dependent, Household, Person
from planner.engine.tax import irmaa
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
        "medicare_irmaa": bn.NOT,
        "msp": bn.NOT,
        "extra_help": bn.NOT,
        "snap": bn.NOT,
        "wic": bn.NOT,
        "school_meals": bn.NOT,
        "tanf": bn.NOT,
        "ssi": bn.NOT,
        "lifeline": bn.NOT,
        "liheap": bn.POSSIBLE,  # no income on file: under 110%
        "state_eitc": bn.NOT,
        "state_ctc": bn.NOT,
        "property_tax_relief": bn.NOT,
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
    assert [p.key for p in bn.REGISTRY] == [
        "aca_ptc",
        "aca_csr",
        "medicaid",
        "chip",
        "medicare_irmaa",
        "msp",
        "extra_help",
        "snap",
        "wic",
        "school_meals",
        "tanf",
        "ssi",
        "lifeline",
        "liheap",
        "state_eitc",
        "state_ctc",
        "property_tax_relief",
    ]
    for p in bn.REGISTRY:
        for f in fields(p):
            assert getattr(p, f.name), (p.key, f.name)
        assert p.reviewed == bn.REVIEWED
    text = "\n".join(bn.program_lines())
    for label in ("place", "who", "dates", "look-back", "assets", "sources"):
        assert text.count(f"  {label}: ") == len(bn.REGISTRY)
    assert "45 CFR 156.420(a)(1)-(3)" in text and "42 CFR 457.310(b)" in text
    assert "20 CFR 418.1115" in text and "SSA POMS HI 03030.025" in text
    assert "7 USC 2014(g)(7)" in text and "20 CFR 416.1205" in text
    assert "47 CFR 54.409(a), (c)" in text and "42 USC 8624(b)(2)" in text
    res = runner.invoke(app, ["benefits", "--programs"])
    assert res.exit_code == 0 and "Premium tax credit (marketplace) [aca_ptc]" in (
        res.output
    )


OLD = bn.Member("you", 67, medicaid=False, chip=False, medicare=True, msp="QMB")
MEDICARE = replace(
    BASE,
    members=(OLD,),
    msp_income=12_000.0,
    msp_fpg=15_960.0,
    fpg=15_960.0,
    msp_limit=9_950.0,
)


def test_savings_programs_test_resources_where_the_state_does() -> None:
    assert bn.msp(MEDICARE).status == bn.UNKNOWN
    got = bn.msp(replace(MEDICARE, resources=9_950.0))
    assert got.status == bn.POSSIBLE and got.why[0].startswith("you: QMB at 75%")
    assert got.amount == round(irmaa(2026, "SINGLE", 0.0).base * 12, 2)
    over = bn.msp(replace(MEDICARE, resources=9_950.01))
    assert over.status == bn.NOT and over.amount is None
    waived = bn.msp(replace(MEDICARE, msp_limit=None))
    assert waived.status == bn.POSSIBLE and "no asset test" in waived.why[-1]
    none = bn.msp(replace(MEDICARE, members=(replace(OLD, msp=""),)))
    assert none.status == bn.NOT and "over QI's 135%" in none.why[0]
    assert bn.msp(BASE).status == bn.NOT


def test_extra_help_is_deemed_by_a_savings_program_or_tested() -> None:
    deemed = bn.screen(replace(MEDICARE, resources=1_000.0))
    assert [x.status for x in deemed[5:7]] == [bn.POSSIBLE, bn.POSSIBLE]
    assert "423.773(c)(1)(iii)" in deemed[6].why[0]
    # over the savings programs' limit; Extra Help's is higher (with burial)
    rich = replace(MEDICARE, resources=18_090.0)
    assert bn.msp(rich).status == bn.NOT
    assert bn.extra_help(rich, bn.msp(rich)).status == bn.POSSIBLE
    past = replace(rich, resources=18_090.01)
    assert bn.extra_help(past, bn.msp(past)).status == bn.NOT
    couple = replace(rich, filing_status="JOINT", resources=36_100.0)
    assert bn.extra_help(couple, bn.msp(couple)).status == bn.POSSIBLE
    edge = replace(MEDICARE, members=(replace(OLD, msp=""),), msp_income=23_940.0)
    got = bn.extra_help(edge, bn.msp(edge))
    assert got.status == bn.NOT and "at or over 150%" in got.why[0]
    unknown = replace(edge, msp_income=20_000.0)
    assert bn.extra_help(unknown, bn.msp(unknown)).status == bn.UNKNOWN
    later = replace(unknown, year=2031, resources=0.0)
    got = bn.extra_help(later, bn.msp(later))
    assert got.status == bn.UNKNOWN and "not in the registry" in got.why[-1]


def test_the_premium_reads_this_years_income_two_years_on() -> None:
    young = bn.premium(BASE)
    assert young.status == bn.NOT and "65 by 2028" in young.why[0]
    at63 = replace(BASE, members=(replace(ADULT, age=63),))
    plain = bn.premium(replace(at63, irmaa_magi=100_000.0))
    assert plain.status == bn.POSSIBLE and plain.amount is None
    assert "at the 2026 brackets" in plain.why[1]
    high = bn.premium(replace(at63, irmaa_magi=150_000.0))
    assert high.status == bn.NOT and high.amount and high.amount > 0
    assert "SSA-44" in high.why[-1]


@pytest.mark.engine
def test_engine_medicare_screen_on_synthetic_households() -> None:
    def run(resources: float | None = None, **kw: object) -> dict[str, bn.Result]:
        hh = Household(**kw)  # type: ignore[arg-type]
        return {x.key: x for x in bn.assess(2026, hh, resources=resources).results}

    qmb = run(5_000, age=67, filing_status="SINGLE", state="NC", social_security=12_000)
    assert qmb["msp"].status == bn.POSSIBLE and "QMB" in qmb["msp"].why[0]
    assert qmb["extra_help"].status == bn.POSSIBLE
    assert qmb["medicare_irmaa"].status == bn.POSSIBLE
    slmb = run(
        50_000, age=67, filing_status="SINGLE", state="NC", social_security=18_000
    )
    assert "SLMB" in slmb["msp"].why[0] and slmb["msp"].status == bn.NOT
    assert slmb["extra_help"].status == bn.NOT
    qi = run(age=67, filing_status="SINGLE", state="AL", social_security=20_500)
    assert qi["msp"].status == bn.POSSIBLE and "QI" in qi["msp"].why[0]
    tx = run(10_000, age=67, filing_status="SINGLE", state="TX", social_security=22_000)
    assert tx["msp"].status == bn.NOT and tx["extra_help"].status == bn.POSSIBLE
    work = run(age=63, filing_status="SINGLE", state="NC", wages=150_000)
    assert work["medicare_irmaa"].status == bn.NOT
    assert work["msp"].status == bn.NOT
    pair = run(
        age=70,
        filing_status="JOINT",
        state="NC",
        pension_income=250_000,
        spouse=Person(age=66),
    )
    got = pair["medicare_irmaa"]
    assert got.status == bn.NOT and "for you, spouse" in got.why[0]
    one = irmaa(2028, "JOINT", 250_000.0)
    assert got.amount == round((one.part_b + one.part_d) * 24, 2)


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


TODDLER = bn.Member("dependent 1", 3, medicaid=True, chip=False, wic=700.0)
PUPIL = bn.Member("dependent 2", 8, medicaid=True, chip=False)
FAMILY = replace(BASE, filing_status="HEAD_OF_HOUSEHOLD", state="NC")


def test_snap_tests_resources_only_without_categorical_eligibility() -> None:
    cat = bn.snap(replace(FAMILY, snap=4_000.0, snap_categorical=True))
    assert cat.status == bn.POSSIBLE and cat.amount == 4_000.0
    assert "273.2(j)(2)" in cat.why[0] and "273.9(d)(6)" in cat.why[1]
    tested = replace(FAMILY, snap=700.0, snap_gross=True, snap_limit=4_500.0)
    assert bn.snap(tested).status == bn.UNKNOWN  # no accounts on file
    poor = bn.snap(replace(tested, liquid=4_500.0))
    assert poor.status == bn.POSSIBLE and poor.amount == 700.0
    rich = bn.snap(replace(tested, liquid=4_501.0))
    assert rich.status == bn.NOT and rich.amount is None
    assert "accounts other than retirement 4,501, over the 4,500" in rich.why[1]
    assert bn.snap(replace(FAMILY, snap_gross=True)).status == bn.UNKNOWN
    assert bn.snap(FAMILY).status == bn.NOT


def test_wic_and_school_meals_read_each_childs_age() -> None:
    kids = replace(FAMILY, members=(ADULT, TODDLER, PUPIL))
    assert bn.wic(kids).status == bn.NOT  # income test fails
    got = bn.wic(replace(kids, wic_income=True))
    assert got.status == bn.POSSIBLE and got.amount == 700.0
    assert got.why[0] == "dependent 1 (age 3): income qualifies"
    assert bn.wic(replace(FAMILY, wic_income=True)).status == bn.NOT
    assert bn.meals(replace(FAMILY, members=(ADULT, TODDLER))).status == bn.NOT
    free = bn.meals(replace(kids, meals_free=1_100.0))
    assert free.status == bn.POSSIBLE and free.amount == 1_100.0
    assert free.why[0].startswith("free meals for dependent 2:")
    reduced = bn.meals(replace(kids, meals_reduced=300.0))
    assert reduced.why[0].startswith("reduced-price meals for dependent 2")
    universal = bn.meals(replace(kids, state="CA", meals_universal=True))
    assert universal.status == bn.POSSIBLE and "CA serves every" in universal.why[0]
    paid = bn.meals(kids)
    assert paid.status == bn.NOT and "1759a(a)(1)(F)" in paid.why[1]


def test_tanf_needs_a_child_and_a_modeled_state() -> None:
    kids = replace(FAMILY, members=(ADULT, PUPIL))
    assert bn.tanf(FAMILY).status == bn.NOT
    assert bn.tanf(kids).status == bn.UNKNOWN  # the engine does not model it
    assert bn.tanf(replace(kids, tanf_modeled=True)).status == bn.NOT
    got = bn.tanf(replace(kids, tanf_modeled=True, tanf=5_200.0))
    assert got.status == bn.POSSIBLE and got.amount == 5_200.0
    assert "608(a)(7)" in got.why[1]


def test_ssi_tests_resources_against_one_or_a_couple() -> None:
    old = replace(OLD, ssi=2_000.0)
    f = replace(BASE, members=(old,), ssi_limit=2_000.0)
    assert bn.ssi(f).status == bn.UNKNOWN  # no accounts on file
    assert bn.ssi(replace(f, resources=2_000.0)).amount == 2_000.0
    assert bn.ssi(replace(f, resources=2_001.0)).status == bn.NOT
    assert bn.ssi(replace(f, members=(OLD,), resources=0.0)).status == bn.NOT
    assert bn.ssi(BASE).why[0].startswith("no one 65 or older")
    assert bn.ssi_limit(2026, "SINGLE") == 2_000.0
    assert bn.ssi_limit(2026, "JOINT") == 3_000.0
    assert bn.snap_limit(2026, [40, 8]) == 3_000.0
    assert bn.snap_limit(2026, [61]) == 4_500.0


def test_lifeline_and_liheap_read_programs_then_income() -> None:
    assert bn.lifeline(BASE).status == bn.NOT
    got = bn.lifeline(replace(BASE, lifeline=True, lifeline_year=111.0))
    assert got.status == bn.POSSIBLE and got.amount == 111.0
    f = replace(BASE, state="MN", fpg=15_960.0, smi=70_000.0)
    food = bn.Result("snap", bn.POSSIBLE, ())
    via = bn.liheap(replace(f, gross=60_000.0), [food])
    assert via.status == bn.POSSIBLE and "SNAP (food assistance)" in via.why[0]
    floor = 1.10 * 15_960
    assert bn.liheap(replace(f, gross=floor), []).status == bn.POSSIBLE
    assert bn.liheap(replace(f, gross=floor + 1), []).status == bn.UNKNOWN
    top = max(1.50 * 15_960, 0.60 * 70_000)  # 60% of the state median here
    assert bn.liheap(replace(f, gross=top), []).status == bn.UNKNOWN
    over = bn.liheap(replace(f, gross=top + 1), [])
    assert over.status == bn.NOT and "42,000" in over.why[0]


def test_state_credits_tell_none_in_the_state_from_none_at_this_income() -> None:
    f = replace(BASE, state="NY", eitc_programs=("ny_eitc",))
    assert "NY has no child tax credit" in bn.screen(f)[15].why[0]
    assert (
        bn.screen(f)[14].why[0]
        == "NY's earned income credit is 0 at this income and family"
    )
    got = bn.screen(replace(f, state_eitc=500.0))[14]
    assert got.status == bn.POSSIBLE and got.amount == 500.0
    rel = replace(BASE, state="MN", property_programs=("mn_renters_credit",))
    assert bn.property_relief(rel).status == bn.UNKNOWN  # rent is not asked
    paid = bn.property_relief(replace(rel, property_taxes=3_000.0))
    assert paid.status == bn.NOT and "3,000 of property taxes" in paid.why[0]
    assert bn.property_relief(replace(rel, property_credit=900.0)).amount == 900.0
    assert bn.state_programs(2026, "MN", "state_property_tax_credits") == (
        "mn_homestead_credit_refund",
        "mn_renters_credit",
    )
    assert bn.state_programs(2026, "TX", "state_eitcs") == ()


@pytest.mark.engine
def test_engine_food_and_cash_screen_on_synthetic_households() -> None:
    def run(**kw: object) -> dict[str, bn.Result]:
        hh = Household(**kw)  # type: ignore[arg-type]
        return {x.key: x for x in bn.assess(2026, hh).results}

    kids = (Dependent(age=3), Dependent(age=8))
    nc = run(
        age=30,
        filing_status="HEAD_OF_HOUSEHOLD",
        state="NC",
        wages=25_000,
        dependents=kids,
    )
    assert nc["snap"].status == bn.POSSIBLE and (nc["snap"].amount or 0) > 0
    assert nc["wic"].status == bn.POSSIBLE and "dependent 1" in nc["wic"].why[0]
    assert nc["school_meals"].why[0].startswith("free meals for dependent 2")
    assert nc["tanf"].status == bn.NOT and nc["ssi"].status == bn.NOT
    ca = run(
        age=30,
        filing_status="HEAD_OF_HOUSEHOLD",
        state="CA",
        wages=25_000,
        dependents=kids,
    )
    assert ca["tanf"].status == bn.POSSIBLE and (ca["tanf"].amount or 0) > 0
    assert "CA serves every" in ca["school_meals"].why[0]
    tx = run(
        age=40,
        filing_status="HEAD_OF_HOUSEHOLD",
        state="TX",
        wages=50_000,
        dependents=(Dependent(age=8),),
    )
    assert [tx[k].status for k in ("snap", "wic", "school_meals", "tanf")] == [
        bn.NOT
    ] * 4
    old = run(age=70, filing_status="SINGLE", state="NC", social_security=10_000)
    assert old["ssi"].status == bn.UNKNOWN and "about" in old["ssi"].why[0]
    assert old["snap"].status == bn.POSSIBLE


@pytest.mark.engine
def test_engine_utility_and_state_rows_on_synthetic_households() -> None:
    def run(**kw: object) -> dict[str, bn.Result]:
        hh = Household(**kw)  # type: ignore[arg-type]
        return {x.key: x for x in bn.assess(2026, hh).results}

    ny = run(
        age=30,
        filing_status="HEAD_OF_HOUSEHOLD",
        state="NY",
        wages=25_000,
        dependents=(Dependent(age=3), Dependent(age=8)),
    )
    assert [
        ny[k].status for k in ("lifeline", "liheap", "state_eitc", "state_ctc")
    ] == [bn.POSSIBLE] * 4
    assert (ny["state_eitc"].amount or 0) > 0 and (ny["state_ctc"].amount or 0) > 0
    assert ny["property_tax_relief"].status == bn.UNKNOWN
    nc = run(age=40, filing_status="SINGLE", state="NC", wages=40_000)
    assert [nc[k].status for k in ("lifeline", "liheap", "state_eitc")] == [bn.NOT] * 3
    mn = run(
        age=70,
        filing_status="SINGLE",
        state="MN",
        social_security=20_000,
        pension_income=10_000,
        real_estate_taxes=3_000,
    )
    assert (
        mn["liheap"].status == bn.UNKNOWN and "MN's line decides" in mn["liheap"].why[0]
    )
    assert mn["property_tax_relief"].status == bn.POSSIBLE
    assert (mn["property_tax_relief"].amount or 0) > 0


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
    assert "SNAP resources: accounts other than retirement (1,600,000.00" in (
        res.output
    )
