"""The state-return registry (unit 3d-4): each drafted state is one entry the
draft, `planner close`, the rollover and the Needed panel read, so a new
state's return is a module and an entry, not edits across the planner.
Synthetic households only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from planner import coverage, states
from planner.ingest.needs import enter, need_for
from planner.paths import Layout
from planner.plan import rollover
from planner.taxprep import close, d400, draft, statereturn
from tests.test_d400 import _lay

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "forms"


def test_nc_is_the_first_entry() -> None:
    nc = statereturn.RETURNS["NC"]
    assert nc.form == d400.FORM and nc.template == "NC-D400"
    assert nc.tax_line == "15" and nc.carry == "nc_tax"
    assert statereturn.get("nc") is nc and statereturn.get("CA") is None
    assert statereturn.get(None) is None


@pytest.mark.parametrize("code", sorted(statereturn.RETURNS))
def test_each_entry_has_its_filed_template(code: str) -> None:
    ret = statereturn.RETURNS[code]
    assert states.get(code).income_tax
    tpl = yaml.safe_load((TEMPLATES / f"{ret.template.lower()}.yaml").read_text())
    assert tpl["form"] == ret.template and tpl["issuer"] == code
    assert {ret.tax_line, *ret.boxes} <= set(tpl["boxes"])
    assert tpl["boxes"][ret.tax_line].get("required") is True
    assert ret.form in ret.forms


def test_the_planner_reads_the_registry() -> None:
    assert tuple(coverage.DRAFTED_STATES) == tuple(statereturn.RETURNS)
    for code, ret in statereturn.RETURNS.items():
        assert code in close.FILERS
        assert close.REQUIRED[code] == (ret.template, ret.tax_line)
        for b in ret.boxes:
            assert close.MAP[(ret.template, b)] == (ret.form, b)
        for form, (heading, capability) in ret.forms.items():
            assert form in draft.ORDER
            assert draft.HEADINGS[form] == heading
            assert draft.FORM_CAPABILITY[form] == capability


def test_another_states_prior_tax_carries_as_an_estimate() -> None:
    need = need_for("prior_state_tax")
    assert need.estimate == (("CARRY-EST", "state_tax"),)
    assert "state_tax" in rollover.LABELS  # a drafted state's carry key


def _fake(calls: list[dict[str, Any]]) -> statereturn.StateReturn:
    def lay(
        add: Any,
        notes: list[str],
        v: Any,
        facts: Any,
        paid: Any,
        year: int,
        agi: float,
        typed: Any,
        married: bool,
    ) -> None:
        calls.append({"paid": paid, "typed": dict(typed), "married": married})
        add("CA-540", "13", "Federal AGI", agi, "1040 line 11a")
        add("CA-540", "64", "Total tax", 100.0, "test")

    return statereturn.StateReturn(
        code="CA",
        forms={"CA-540": ("CA Form 540", "draft_return")},
        form="CA-540",
        template="CA-540",
        boxes=("13", "64"),
        tax_line="64",
        carry="state_tax",
        keys=("state_withheld",),
        engine=(),
        lay=lay,
    )


def test_the_draft_lays_the_households_state_return(
    planner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setitem(statereturn.RETURNS, "CA", _fake(calls))
    lay: Layout = _lay(planner_home, 0.0)
    enter(lay, 2025, "state", "CA")
    enter(lay, 2025, "state_withheld", "250")
    d = draft.build(lay, 2025)
    assert d.get("CA-540", "13") == d.get("1040", "11a")
    assert d.get("CA-540", "64") == 100.0
    assert not [ln for ln in d.lines if ln.form in (d400.FORM, d400.SCHED)]
    assert calls and calls[0]["typed"]["state_withheld"] == 250.0
    assert calls[0]["married"] is False
    assert not any("the CA return is not drafted" in n for n in d.notes)
