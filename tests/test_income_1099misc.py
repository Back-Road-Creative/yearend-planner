"""Unit 3e-2a: Form 1099-MISC (Rev. April 2025). Box 3, other income, goes to
Schedule 1 line 8z; boxes 4 and 16 are federal and state withholding; boxes 1
and 2 (rents and royalties) are kept for Schedule E. Synthetic figures only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from planner.ingest.needs import enter, need_value
from planner.ingest.pdf import parse_texts
from planner.ledger import db
from planner.taxprep import draft, expected
from tests.test_forms import TEMPLATES
from tests.test_income_1099g import _doc, _lay

PAGE = "\n".join(
    [
        "Form 1099-MISC Miscellaneous Information",
        "For calendar year 2025",
        "PAYER'S name: Example Contest Sponsor (synthetic)",
        "1 Rents $ 2,400.00",
        "2 Royalties $ 310.00",
        "3 Other income $ 1,500.00",
        "4 Federal income tax withheld $ 150.00",
        "11 Fish purchased for resale $",
        "16 State tax withheld $ 60.00",
    ]
)


def _misc(boxes: dict[str, float]) -> list[db.Fact]:
    return [
        db.Fact("1099-MISC", 2025, "Contest Sponsor (synthetic)", b, "", v, 1)
        for b, v in boxes.items()
    ]


def test_1099misc_boxes_read() -> None:
    (f,) = parse_texts([PAGE], TEMPLATES)
    assert (f.form, f.tax_year) == ("1099-MISC", 2025)
    assert "Contest Sponsor" in f.issuer
    assert {b: v for b, (_, v, *_) in f.boxes.items()} == {
        "1": 2400.0,
        "2": 310.0,
        "3": 1500.0,
        "4": 150.0,
        "16": 60.0,
    }


def test_other_income_and_withholding_needs(planner_home: Path) -> None:
    lay = _lay(planner_home, "VA")
    _doc(lay, "misc", _misc({"3": 1500.0, "4": 150.0, "16": 60.0}))
    conn = db.connect(lay.data / "ledger" / "planner.db")
    got = {
        k: need_value(conn, lay, 2025, k)
        for k in ("other_income", "fed_withheld", "state_withheld")
    }
    conn.close()
    assert got == {
        "other_income": 1500.0,
        "fed_withheld": 3150.0,
        "state_withheld": 60.0,
    }


def test_nc_withholding_reads_box_16(planner_home: Path) -> None:
    lay = _lay(planner_home, "NC")
    _doc(lay, "misc", _misc({"16": 60.0}))
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert need_value(conn, lay, 2025, "nc_withheld") == 60.0
    conn.close()


def test_draft_schedule_1_line_8z(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _doc(lay, "misc", _misc({"3": 1500.0, "4": 150.0}))
    d = draft.build(lay, 2025)
    assert {n: d.get("Sch 1", n) for n in ("8z", "9", "10")} == {
        "8z": 1500.0,
        "9": 1500.0,
        "10": 1500.0,
    }
    assert d.get("1040", "8") == 1500.0
    assert d.get("1040", "25b") == 150.0
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_line_8z_with_canceled_debt(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "other_income", "700")
    enter(lay, 2025, "cancelled_debt", "300")
    d = draft.build(lay, 2025)
    assert d.get("Sch 1", "9") == 1000.0
    src = next(ln.source for ln in d.lines if (ln.form, ln.line) == ("Sch 1", "9"))
    assert src == "line 8c + line 8z"
    assert not [n for n in d.notes if n.startswith("CHECK")]


def test_expected_1099misc(planner_home: Path) -> None:
    lay = _lay(planner_home)
    enter(lay, 2025, "other_income", "500")
    inv = expected.inventory(lay, 2025, date(2026, 3, 1))
    by = {(e.form, e.issuer): e for e in inv.items}
    assert by[("1099-MISC", "each payer")].reason == "other income"
