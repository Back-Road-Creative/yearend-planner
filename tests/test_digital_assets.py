"""Unit 3e-9: digital assets from synthetic forms, by the 2025 Form 1099-DA and
its instructions, the Form 8949 and Schedule D instructions (boxes G-L, lines
2, 3, 9 and 10) and the Form 1040 instructions (the digital assets question)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from planner.ingest import ingest
from planner.ingest.needs import enter, need_for, need_values, needed
from planner.ingest.pdf import base_issuer, load_templates, parse_texts
from planner.ledger import db, portfolio
from planner.paths import Layout
from planner.taxprep import capgains, draft, expected
from tests.test_csv import drop
from tests.test_income_1099g import _doc, _lay

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = load_templates(ROOT / "templates" / "forms")
EXCHANGE = "Example Exchange LLC (synthetic)"
TAXABLE = "55556666"


def _page(
    asset: str = "Examplecoin",
    code: str = "XYZ",
    units: str = "2.5",
    acquired: str = "03/02/2024",
    sold: str = "02/10/2025",
    proceeds: str = "5,000.00",
    basis: str | None = "3,000.00",
    reported: bool = True,
    term: str = "Short-term",
    letter: str = "G",
    extra: tuple[str, ...] = (),
) -> str:
    return "\n".join(
        [
            "Form 1099-DA 2025 Digital Asset Proceeds From Broker Transactions",
            f"FILER'S name, street address: {EXCHANGE}",
            f"Applicable checkbox on Form 8949 {letter}",
            f"1a Code for digital asset {code}",
            f"1b Name of digital asset {asset}",
            f"1c Number of units {units}",
            f"1d Date acquired {acquired}",
            f"1e Date sold or disposed {sold}",
            f"1f Proceeds $ {proceeds}",
            *([f"1g Cost or other basis $ {basis}"] if basis else []),
            f"2 [{'X' if reported else ' '}] Check if basis reported to IRS",
            f"6 Gain or loss: [X] {term}",
            *extra,
        ]
    )


def _forms(lay: Layout, *pages: str) -> None:
    for n, page in enumerate(pages):
        _doc(
            lay,
            f"1099da-{n}-{len(page)}",
            [
                db.Fact(
                    f.form,
                    f.tax_year,
                    f.issuer,
                    box,
                    label,
                    0.0 if isinstance(v, str) else v,
                    1,
                    text=v if isinstance(v, str) else None,
                )
                for f in parse_texts([page], TEMPLATES)
                for box, (label, v) in f.boxes.items()
            ],
        )


def test_template_reads_one_sale_per_form() -> None:
    page = _page(extra=("4 Federal income tax withheld $ 120.00",))
    (f,) = parse_texts([page], TEMPLATES)
    assert (f.form, f.tax_year) == ("1099-DA", 2025)
    assert f.issuer == (
        f"{EXCHANGE} [asset Examplecoin, units 2.5, acquired 03/02/2024, "
        "sold 02/10/2025]"
    )
    assert base_issuer(f.issuer) == EXCHANGE
    got = {k: v[1] for k, v in f.boxes.items()}
    assert got == {
        "8949": "G",
        "1a": "XYZ",
        "1b": "Examplecoin",
        "1c": "2.5",
        "1d": "03/02/2024",
        "1e": "02/10/2025",
        "1f": 5000.0,
        "1g": 3000.0,
        "2": "yes",
        "4": 120.0,
        "6": "short",
    }
    # a second sale from the same broker is its own form; a corrected copy of
    # the same sale replaces it
    other = parse_texts([_page(sold="03/10/2025")], TEMPLATES)[0]
    fixed = parse_texts([_page(proceeds="5,100.00")], TEMPLATES)[0]
    assert other.issuer != f.issuer == fixed.issuer


LOTS = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Date Acquired,Date Sold,Shares,"
        "Proceeds,Cost Basis,Term",
        f"{TAXABLE},Examplecoin,XYZ,03/02/2024,02/10/2025,2.5,5000,3000,Short-term",
        f"{TAXABLE},Othercoin,OTC,01/05/2023,04/01/2025,10,800,1000,Long-term",
        f"{TAXABLE},Total Stock,VTSAX,03/02/2020,02/10/2025,100,15000,10000,Long-term",
        "",
    ]
)
BUYS = "\n".join(
    [
        "Account Number,Trade Date,Settlement Date,Transaction Type,Transaction "
        "Description,Investment Name,Symbol,Shares,Share Price,Principal Amount,"
        "Commissions and Fees,Net Amount,Accrued Interest,Account Type,",
        f"{TAXABLE},04/10/2025,04/11/2025,Buy,Buy,Othercoin,OTC,10,80,800,0,-800,0,"
        "CASH,",
        "",
    ]
)


def _lots(planner_home: Path) -> Layout:
    lay = _lay(planner_home)
    portfolio.save_account(lay, TAXABLE, type="taxable")
    drop(lay, "lots.csv", LOTS)
    drop(lay, "buys.csv", BUYS)
    ingest(lay)
    return lay


def _cg(lay: Layout) -> capgains.CapGains:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        return capgains.store(conn, lay, 2025, date(2026, 2, 1))
    finally:
        conn.close()


def test_lots_go_to_boxes_g_to_l(planner_home: Path) -> None:
    lay = _lots(planner_home)
    _forms(lay, _page())
    enter(lay, 2025, "digital_assets", "otc")
    cg = _cg(lay)
    boxes = {lt.description: (lt.box, lt.code, lt.adjustment) for lt in cg.lots}
    assert boxes == {
        "2.5 sh XYZ": ("G", "", 0.0),  # the 1099-DA's basis was reported
        "10 sh OTC": ("L", "", 0.0),  # no 1099-DA; not washed (not a security)
        "100 sh VTSAX": ("D", "", 0.0),
    }
    assert cg.lines["1b"] == 2000.0 and cg.sources["1b"] == "Form 8949 box G"
    assert cg.lines["8b"] == 5000.0 and cg.sources["8b"] == "Form 8949 box D"
    assert cg.lines["10"] == -200.0
    assert (cg.lines["7"], cg.lines["15"], cg.lines["16"]) == (2000.0, 4800.0, 6800.0)
    assert list(cg.lines)[:6] == ["1b", "7", "8b", "10", "15", "16"]
    text = capgains.render(cg)
    assert "Form 8949 box L (long-term digital assets, no 1099-DA)" in text


def test_basis_not_reported_and_the_broker_wash_sale(planner_home: Path) -> None:
    lay = _lots(planner_home)
    loss = _page(
        proceeds="2,000.00",
        reported=False,
        letter="H",
        extra=("1i Wash sales loss disallowed $ 400.00",),
    )
    _forms(lay, loss)
    (xyz,) = [lt for lt in _cg(lay).lots if "XYZ" in lt.description]
    # the lot's own basis, the 1099-DA's box H and its box 1i (code W)
    assert (xyz.box, xyz.code, xyz.adjustment, xyz.gain) == ("H", "W", 400.0, 2400.0)


def test_a_1099da_with_no_lot(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _forms(
        lay,
        _page(reported=False, term="Long-term", letter="K", acquired="01/02/2020"),
        _page(asset="Thirdcoin", code="TRC", basis=None, sold="05/05/2025"),
        _page(asset="Fourthcoin", code="FRC", term="", letter="X", sold="06/06/2025"),
    )
    cg = _cg(lay)
    (lot,) = cg.lots
    assert (lot.box, lot.description, lot.acquired, lot.sold) == (
        "K",
        "2.5 Examplecoin",
        "2020-01-02",
        "2025-02-10",
    )
    assert cg.lines["9"] == 2000.0 and cg.lines["15"] == 2000.0
    notes = " ".join(cg.notes)
    assert "box 1g was not reported to the IRS (box 2 blank)" in notes
    assert any("box 1g (basis) is blank" in u for u in cg.unknown)
    assert any("term is unknown" in u for u in cg.unknown)


def test_needed_panel_and_the_1040_question(planner_home: Path) -> None:
    lay = _lay(planner_home)
    keys = ("digital_asset_activity", "digital_assets")
    report = needed(lay, 2025)
    (st,) = [s for s in report.items if s.need.key == "digital_asset_activity"]
    assert st.state == "missing" and "only holding one" in st.origin
    assert "digital_assets" not in {s.need.key for s in report.items}
    d = draft.build(lay, 2025)
    assert "digital_asset_activity" in d.unknown
    assert "1040" not in {(ln.form) for ln in d.lines if ln.line == "DA"}

    _forms(lay, _page(extra=("4 Federal income tax withheld $ 120.00",)))
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        got = need_values(conn, lay, 2025, keys)
    finally:
        conn.close()
    assert got == {"digital_asset_activity": "yes", "digital_assets": None}
    d = draft.build(lay, 2025)
    lines = {(ln.form, ln.line): ln for ln in d.lines}
    assert lines[("1040", "DA")].label.endswith("Yes")
    assert lines[("1040", "25b")].value == 120.0
    assert "1099-DA box 4" in lines[("1040", "25b")].source
    assert lines[("Sch D", "1b")].value == 2000.0
    assert lines[("8949", "G1")].value == 2000.0

    enter(lay, 2025, "digital_asset_activity", "no")
    asked = need_for("digital_assets").asked
    assert asked is not None and not asked({"digital_asset_activity": "no"})


def test_expected_next_year_names_the_broker_once(planner_home: Path) -> None:
    lay = _lay(planner_home)
    _forms(lay, _page(), _page(sold="03/10/2025"))
    inv = expected.inventory(lay, 2026, date(2026, 6, 1))
    mine = [e for e in inv.items if e.form == "1099-DA"]
    assert [(e.issuer, e.due) for e in mine] == [(EXCHANGE, "2027-02-16")]
