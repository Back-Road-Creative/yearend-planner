"""Unit 4a: goals, debts, protected income lines and the target mix, typed in
data/profile/goals.yaml; refused when malformed, never guessed."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from planner import goals
from planner.cli import app
from planner.paths import Layout
from planner.plan import year
from tests.test_spending import AS_OF, lay  # noqa: F401

runner = CliRunner()
TODAY = date(2026, 10, 6)
GOOD = {
    "goals": [
        {"name": "Roof", "amount": 18000, "date": "2027-06-01", "rank": 2},
        {
            "name": "Car",
            "amount": 24000,
            "date": "2029-03-01",
            "rank": 1,
            "flexibility": "flexible",
            "kind": "purchase",
        },
        {"name": "Trip", "rank": 3, "flexibility": "optional"},
    ],
    "debts": [{"name": "Auto loan", "balance": 10000, "rate": 6, "payment": 200}],
    "protect": ["aca_cliff", "irmaa"],
    "target_mix": {"stocks": 60, "bonds": 30, "cash": 10},
}


@pytest.fixture
def home(planner_home: Path) -> Layout:
    lay_ = Layout(planner_home)
    lay_.ensure()
    return lay_


def write(lay_: Layout, data: dict[str, Any]) -> None:
    goals.path(lay_).parent.mkdir(parents=True, exist_ok=True)
    goals.path(lay_).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_no_file_is_no_goals(home: Layout) -> None:
    g = goals.load(home)
    assert g.goals == [] and g.debts == [] and g.target_mix is None
    lines, notes = goals.summary(g, TODAY, married=False)
    assert lines == []
    assert any("no goals entered" in n for n in notes)
    assert any("no target mix chosen" in n for n in notes)


def test_goals_rank_pace_and_the_year_ahead(home: Layout) -> None:
    write(home, GOOD)
    g = goals.load(home)
    assert [x.name for x in g.ranked()] == ["Car", "Roof", "Trip"]
    assert [x.name for x in g.near(TODAY)] == ["Roof"]
    lines, notes = goals.summary(g, TODAY, married=False)
    # 18,000 over the 8 months from October to June
    assert (
        "2. Roof (goal, self, fixed)  18,000.00 by 2027-06-01  "
        "2,250.00/month for 8 months" in lines
    )
    assert lines[0].startswith(
        "1. Car (purchase, self, flexible)  24,000.00 by 2029-03-01"
    )
    assert (
        "3. Trip (goal, self, optional)  amount not entered by date not entered"
        in lines
    )
    assert "dated inside 12 months: 18,000.00 (Roof)" in lines
    assert any(
        "goal Trip: amount not entered; it is not counted as 0" in n for n in notes
    )
    assert "target mix: stocks 60%, bonds 30%, cash 10%" in lines
    # no MAGI lines given: the protected lines say why their room is missing
    assert "protect ACA 400% FPL cliff: room not computed ()" in lines


@pytest.mark.parametrize(
    ("balance", "rate", "payment", "months"),
    [
        (10000, 6, 200, 58),  # -ln(1 - 0.005 * 10000 / 200) / ln(1.005) = 57.7
        (1200, 0, 100, 12),
        (10000, 6, 50, None),  # 50 is exactly the month's interest
        (10000, 6, 40, None),
        (500, 0, 0, None),
    ],
)
def test_debt_payoff_is_the_amortization_count(
    balance: float, rate: float, payment: float, months: int | None
) -> None:
    assert goals.Debt("d", balance, rate, payment).months() == months


def test_debt_lines_name_the_payoff_month_or_never(home: Layout) -> None:
    write(
        home,
        {
            "debts": [
                {"name": "Auto loan", "balance": 10000, "rate": 6, "payment": 200},
                {"name": "Card", "balance": 5000, "rate": 24, "payment": 90},
            ]
        },
    )
    lines, _ = goals.summary(goals.load(home), TODAY, married=False)
    assert (
        "debt Auto loan (self) 10,000.00 at 6%, 200.00/month: "
        "paid off 2031-08 (58 payments)" in lines
    )
    assert any(
        ln.startswith("debt Card")
        and "does not cover the interest: never paid off" in ln
        for ln in lines
    )


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (
            {"goals": [{"name": "A", "rank": 1, "color": "red"}]},
            "unknown field(s) color",
        ),
        ({"goals": [{"name": "A"}]}, "rank must be a whole number"),
        ({"goals": [{"name": "A", "rank": 0}]}, "rank must be a whole number"),
        ({"goals": [{"rank": 1}]}, "name is required"),
        (
            {"goals": [{"name": "A", "rank": 1}, {"name": "B", "rank": 1}]},
            "rank shared by two goals: 1",
        ),
        (
            {"goals": [{"name": "A", "rank": 1}, {"name": "A", "rank": 2}]},
            "goal named twice: A",
        ),
        (
            {"goals": [{"name": "A", "rank": 1, "amount": 0}]},
            "amount must be more than 0",
        ),
        (
            {"goals": [{"name": "A", "rank": 1, "amount": "lots"}]},
            "amount must be a number",
        ),
        (
            {"goals": [{"name": "A", "rank": 1, "date": "June"}]},
            "date must be YYYY-MM-DD",
        ),
        ({"goals": [{"name": "A", "rank": 1, "owner": "kid"}]}, "owner must be one of"),
        (
            {"goals": [{"name": "A", "rank": 1, "flexibility": "x"}]},
            "flexibility must be",
        ),
        ({"debts": [{"name": "D", "balance": 100, "rate": 5}]}, "payment required"),
        (
            {"debts": [{"name": "D", "balance": 100, "rate": 120, "payment": 5}]},
            "0 to 100",
        ),
        (
            {"debts": [{"name": "D", "balance": -1, "rate": 5, "payment": 5}]},
            "balance must be",
        ),
        ({"protect": ["everything"]}, "protect: unknown line(s) everything"),
        ({"target_mix": {"stocks": 60, "bonds": 30}}, "adds to 90, not 100"),
        ({"target_mix": {"gold": 100}}, "unknown class(es) gold"),
        ({"target_mix": {"stocks": 120, "bonds": -20}}, "must be 0 to 100"),
        ({"wishes": []}, "unknown section(s) wishes"),
        ({"goals": {"name": "A"}}, "goals: expected a list"),
    ],
)
def test_a_malformed_file_is_refused(
    home: Layout, data: dict[str, Any], said: str
) -> None:
    write(home, data)
    with pytest.raises(goals.GoalsError, match=None) as got:
        goals.load(home)
    assert said in str(got.value)
    assert str(goals.path(home)) in str(got.value)


def test_cli_adds_changes_lists_and_removes(home: Layout) -> None:
    r = runner.invoke(
        app, ["goal", "Roof", "--amount", "$18,000", "--date", "2027-06-01"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["goal", "Car", "--amount", "24000", "--purchase"])
    assert r.exit_code == 0, r.output
    # a new goal with no rank goes last; a change keeps the rank
    runner.invoke(app, ["goal", "Roof", "--flexibility", "flexible"])
    g = goals.load(home)
    assert [(x.name, x.rank, x.amount, x.kind) for x in g.ranked()] == [
        ("Roof", 1, 18000.0, "goal"),
        ("Car", 2, 24000.0, "purchase"),
    ]
    assert g.ranked()[0].flexibility == "flexible"
    listed = runner.invoke(app, ["goal"]).output
    assert "Roof" in listed and "Car" in listed
    r = runner.invoke(
        app,
        [
            "debt",
            "Auto loan",
            "--balance",
            "10,000",
            "--rate",
            "6%",
            "--payment",
            "200",
        ],
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["protect", "aca_cliff", "irmaa"]).exit_code == 0
    assert runner.invoke(app, ["mix", "stocks=60", "bonds=40"]).exit_code == 0
    g = goals.load(home)
    assert g.debts == [goals.Debt("Auto loan", 10000.0, 6.0, 200.0)]
    assert g.protect == ["aca_cliff", "irmaa"]
    assert g.target_mix == {"stocks": 60.0, "bonds": 40.0}
    assert "stocks 60%" in runner.invoke(app, ["mix"]).output
    assert runner.invoke(app, ["goal", "Car", "--remove"]).exit_code == 0
    assert runner.invoke(app, ["debt", "Auto loan", "--remove"]).exit_code == 0
    assert runner.invoke(app, ["mix", "--clear"]).exit_code == 0
    g = goals.load(home)
    assert [x.name for x in g.goals] == ["Roof"] and g.debts == []
    assert g.target_mix is None


@pytest.mark.parametrize(
    "args",
    [
        ["mix", "stocks=60", "bonds=30"],
        ["mix", "stocks"],
        ["protect", "everything"],
        ["goal", "Roof", "--rank", "1"],  # Car already holds rank 1
        ["goal", "Boat", "--date", "someday"],
        ["goal", "Nothing", "--remove"],
        ["debt", "Loan", "--balance", "100"],
    ],
)
def test_cli_refusal_leaves_the_file_as_it_was(home: Layout, args: list[str]) -> None:
    write(home, {"goals": [{"name": "Car", "rank": 1}], "target_mix": {"cash": 100}})
    before = goals.path(home).read_text(encoding="utf-8")
    r = runner.invoke(app, args)
    assert r.exit_code == 2, r.output
    assert "refused:" in r.output
    assert goals.path(home).read_text(encoding="utf-8") == before


def test_owner_spouse_on_a_single_return_is_named(home: Layout) -> None:
    write(home, {"goals": [{"name": "Gift", "rank": 1, "owner": "spouse"}]})
    _, notes = goals.summary(goals.load(home), TODAY, married=False)
    assert any("owner spouse, but the filing status is not married" in n for n in notes)
    _, notes = goals.summary(goals.load(home), TODAY, married=True)
    assert not any("owner spouse" in n for n in notes)


@pytest.mark.engine
def test_year_plan_goals_section_reads_the_magi_lines(lay: Layout) -> None:  # noqa: F811
    write(lay, GOOD)
    yp = year.assemble(lay, 2026, AS_OF)
    sec = yp.section("goals")
    assert sec.ok and sec.lines[0].startswith("1. Car")
    cliff = next(
        ln for ln in yp.section("magi").lines if ln.startswith("ACA 400% FPL cliff")
    )
    room = cliff.split(" by ")[1].split()[0]
    state = "OVER" if " OVER " in cliff else "under"
    assert f"protect ACA 400% FPL cliff: {state} by {room}" in sec.lines
    # the household is 55: Medicare's IRMAA line is not watched this year
    assert "protect IRMAA first tier: does not apply this year" in sec.lines


@pytest.mark.engine
def test_year_plan_names_a_refused_goals_file(lay: Layout) -> None:  # noqa: F811
    write(lay, {"target_mix": {"stocks": 50}})
    sec = year.assemble(lay, 2026, AS_OF).section("goals")
    assert not sec.ok
    assert sec.lines[0].startswith("goals refused:") and "adds to 50" in sec.lines[0]
