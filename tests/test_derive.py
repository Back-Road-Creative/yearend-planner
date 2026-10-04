"""YTD facts derived from ledger rows (Phase 2c ``planner derive``)."""

from __future__ import annotations

import gzip
import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from planner.cli import app
from planner.ingest import ingest
from planner.ingest.derive import (
    CountyError,
    counties_for_zip,
    derive,
    derive_year,
    gaps,
    resolve_county,
)
from planner.ingest.needs import Status, enter, needed
from planner.ledger import db
from planner.paths import Layout
from tests.pdfgen import make_pdf
from tests.test_csv import BANK, FUND, REALIZED, VANGUARD_DOWNLOAD, drop
from tests.test_forms import F1040_FILED_P1, F1040_P2

runner = CliRunner()

INCOME = "\n".join(
    [
        "Account Number,Investment Name,Symbol,Income Type,Amount,Payment Date",
        f"12345678,{FUND},VTSAX,Dividend,412.33,03/14/2025",
        f"12345678,{FUND},VTSAX,Dividend,420.10,06/13/2025",
        "12345678,VANGUARD FEDERAL MONEY MARKET FUND,VMFXX,Interest,15.25,06/30/2025",
        f"12345678,{FUND},VTSAX,Capital Gain,150.00,12/20/2025",
        "",
    ]
)


@pytest.fixture
def lay(planner_home: Path) -> Layout:
    lay = Layout(planner_home)
    lay.ensure()
    return lay


def facts(conn: sqlite3.Connection, year: int = 2025) -> dict[tuple[str, str], float]:
    return {(f.issuer, f.box): f.value for f in db.facts_for(conn, year, "YTD")}


def test_ingest_derives_ytd_facts_from_rows(lay: Layout) -> None:
    drop(lay, "realized.csv", REALIZED)
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    drop(lay, "bank.csv", BANK)
    rep = ingest(lay)
    assert rep.derived == {2025: 6}  # the 2026 holdings snapshot yields none
    conn = db.connect(lay.data / "ledger" / "planner.db")
    got = facts(conn)
    assert got[("vanguard_realized", "lt_proceeds")] == 25000.0
    assert got[("vanguard_realized", "lt_basis")] == 16000.0
    assert got[("vanguard_realized", "lt_gain")] == 9000.0
    # the download's Dividend row counts; its Reinvestment row does not
    assert got[("vanguard_transactions", "dividends")] == 412.33
    assert got[("bank", "deposits")] == 2500.0 and got[("bank", "withdrawals")] == 45.10
    assert ("vanguard_realized", "st_proceeds") not in got


def test_income_export_wins_over_transactions_and_rerun_supersedes(lay: Layout) -> None:
    drop(lay, "ofxdownload.csv", VANGUARD_DOWNLOAD)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert facts(conn)[("vanguard_transactions", "dividends")] == 412.33
    drop(lay, "income.csv", INCOME)
    rep = ingest(lay)
    assert rep.imported[0].forms == ("vanguard_income 4 rows",)
    got = facts(conn)
    assert got[("vanguard_income", "dividends")] == 832.43
    assert got[("vanguard_income", "interest")] == 15.25
    assert got[("vanguard_income", "capital_gain_distributions")] == 150.0
    # the earlier transaction-based figure was superseded, not kept beside it
    assert ("vanguard_transactions", "dividends") not in got
    superseded = db.facts_for(conn, 2025, "YTD", status="superseded")
    assert [f.box for f in superseded] == ["dividends"]
    assert derive(conn, 2024) == 0


def test_derive_year_is_pure_and_bank_signs(lay: Layout) -> None:
    drop(lay, "bank.csv", BANK)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    rows = db.rows_for(conn, 2025)
    out = derive_year(rows, 2025)
    assert [(f.issuer, f.box, f.value) for f in out] == [
        ("bank", "deposits", 2500.0),
        ("bank", "withdrawals", 45.10),
    ]
    assert derive_year(rows, 2024) == []


def test_cli_derive_and_facts(lay: Layout) -> None:
    drop(lay, "realized.csv", REALIZED)
    r = runner.invoke(app, ["ingest"])
    assert r.exit_code == 0 and "derived   2025: 3 YTD facts from rows" in r.output
    r = runner.invoke(app, ["derive", "--year", "2025"])
    assert (
        r.exit_code == 0 and r.output.strip() == "derived 2025: 3 YTD facts from rows"
    )
    r = runner.invoke(app, ["facts", "--year", "2025", "--form", "YTD"])
    assert r.exit_code == 0 and "9,000.00" in r.output and "3 facts" in r.output


# ZIP-to-county crosswalk (unit 9h). Real ZCTAs from the Census file, made-up
# returns: 27601 lies in Wake County only; 28734 straddles Clay and Macon.


def _return_with_zip(lay: Layout, zip5: str, state: str = "NC") -> None:
    page = [
        line.replace("Anytown NC 27000", f"Anytown {state} {zip5}")
        for line in F1040_FILED_P1
    ]
    make_pdf(lay.data / "inbox" / "return.pdf", [page, F1040_P2])
    ingest(lay)


def _county(lay: Layout) -> Status:
    return {s.need.key: s for s in needed(lay, 2026).items}["county"]


def test_county_from_return_zip(lay: Layout) -> None:
    _return_with_zip(lay, "27601")
    st = _county(lay)
    assert (st.state, st.value) == ("actual", "Wake County")
    assert "ZIP 27601" in st.origin and "1040 2025 self" in st.origin
    enter(lay, 2026, "county", "Macon")  # a typed answer still wins
    st = _county(lay)
    assert (st.value, st.origin) == ("Macon", "profile")


def test_a_zip_split_across_counties_stays_a_typed_question(lay: Layout) -> None:
    _return_with_zip(lay, "28734")
    st = _county(lay)
    assert st.state == "missing" and st.value is None
    assert st.origin.index("Macon County") < st.origin.index("Clay County")
    assert "28734" in st.origin
    r = runner.invoke(app, ["needed", "--year", "2026"])
    assert r.exit_code == 0 and "Macon County" in r.output and "Clay County" in r.output
    enter(lay, 2026, "county", "Clay")
    assert _county(lay).state == "actual"


def test_a_zip_outside_the_crosswalk_or_the_state_is_not_guessed(lay: Layout) -> None:
    _return_with_zip(lay, "27000")  # not a real ZIP area
    st = _county(lay)
    assert st.state == "missing" and "27000" in st.origin
    _return_with_zip(lay, "27601")
    assert _county(lay).state == "actual"
    enter(lay, 2026, "state", "VA")  # the return's ZIP is in another state
    st = _county(lay)
    assert st.state == "missing" and "NC" in st.origin and "VA" in st.origin


def test_no_return_means_no_county_note(lay: Layout) -> None:
    st = _county(lay)
    assert (st.state, st.origin) == ("missing", "")


def test_counties_for_zip_lists_every_land_county_largest_first(lay: Layout) -> None:
    got = counties_for_zip(lay.config, "28741")
    assert [(c.name, c.state, c.fips) for c in got] == [
        ("Macon County", "NC", "37113"),
        ("Jackson County", "NC", "37099"),
    ]
    assert 0.8 < got[0].share < 0.9 and abs(sum(c.share for c in got) - 1) < 1e-3
    assert [c.key for c in counties_for_zip(lay.config, "27601")] == ["WAKE_COUNTY_NC"]
    assert counties_for_zip(lay.config, "27000") == ()
    assert counties_for_zip(lay.config, "2760") == ()


def test_the_bundled_crosswalk_is_well_formed(repo_root: Path) -> None:
    path = repo_root / "config" / "zip_county.csv.gz"
    lines = gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()
    assert lines[0] == "zcta,county_fips,county_name,state,land_area_share"
    shares: dict[str, float] = {}
    for line in lines[1:]:
        zcta, fips, name, state, share = line.split(",")
        assert len(zcta) == 5 and zcta.isdigit() and len(fips) == 5, line
        assert name and len(state) == 2 and 0 < float(share) <= 1, line
        shares[zcta] = shares.get(zcta, 0.0) + float(share)
    assert len(shares) > 30_000
    assert all(abs(total - 1) < 0.002 for total in shares.values())
    source = (repo_root / "config" / "zip_county.SOURCE.md").read_text("utf-8")
    assert "tab20_zcta520_county20_natl.txt" in source and "Downloaded" in source


@pytest.mark.parametrize(
    "text",
    ["Macon", "macon county", " Macon County, NC ", "MACON_COUNTY_NC", "macon, nc"],
)
def test_a_typed_county_becomes_the_engines_name(lay: Layout, text: str) -> None:
    assert resolve_county(lay.config, text, "NC") == "MACON_COUNTY_NC"


def test_resolve_county_handles_parishes_and_independent_cities(lay: Layout) -> None:
    cfg = lay.config
    assert resolve_county(cfg, "East Baton Rouge", "LA") == "EAST_BATON_ROUGE_PARISH_LA"
    assert resolve_county(cfg, "St. Louis city", "MO") == "ST_LOUIS_CITY_MO"
    assert resolve_county(cfg, "Baltimore city", "MD") == "BALTIMORE_CITY_MD"
    assert resolve_county(cfg, "Baltimore County", "MD") == "BALTIMORE_COUNTY_MD"


def test_resolve_county_refuses_what_it_cannot_pin_down(lay: Layout) -> None:
    with pytest.raises(CountyError, match="Baltimore County, Baltimore city"):
        resolve_county(lay.config, "Baltimore", "MD")
    with pytest.raises(CountyError, match="not a county of VA"):
        resolve_county(lay.config, "Wake", "VA")
    with pytest.raises(CountyError, match="not a county of NC"):
        resolve_county(lay.config, "MACON_COUNTY_TN", "NC")


@pytest.mark.engine
def test_every_north_carolina_county_is_a_county_the_engine_knows(
    lay: Layout,
) -> None:
    from policyengine_us.variables.household.demographic.geographic.county.county_enum import (  # noqa: E501
        County,
    )

    from planner.ingest.derive import counties_of_state

    nc = counties_of_state(lay.config, "NC")
    assert len(nc) == 100
    assert {c.key for c in nc} <= set(County._member_names_)


def _refresh_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "refresh_zip_county",
        Path(__file__).resolve().parent.parent / "scripts" / "refresh_zip_county.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CENSUS_HEADER = (
    "OID_ZCTA5_20|GEOID_ZCTA5_20|NAMELSAD_ZCTA5_20|AREALAND_ZCTA5_20|"
    "AREAWATER_ZCTA5_20|MTFCC_ZCTA5_20|CLASSFP_ZCTA5_20|FUNCSTAT_ZCTA5_20|"
    "OID_COUNTY_20|GEOID_COUNTY_20|NAMELSAD_COUNTY_20|AREALAND_COUNTY_20|"
    "AREAWATER_COUNTY_20|MTFCC_COUNTY_20|CLASSFP_COUNTY_20|FUNCSTAT_COUNTY_20|"
    "AREALAND_PART|AREAWATER_PART"
)


def _census_row(zcta: str, fips: str, name: str, land: int, water: int) -> str:
    return (
        f"1|{zcta}|ZCTA5 {zcta}|0|0|G6350|B5|S|2|{fips}|{name}|0|0|G4020|H1|A|"
        f"{land}|{water}"
    )


def test_refresh_script_reduces_the_census_file() -> None:
    mod = _refresh_script()
    text = "\ufeff" + "\n".join(
        [
            CENSUS_HEADER,
            "||||||||2|01003|Baldwin County|0|0|G4020|H1|A|5|5",  # no ZCTA
            _census_row("28734", "37043", "Clay County", 100, 0),
            _census_row("28734", "37113", "Macon County", 300, 9),
            _census_row("28734", "37099", "Jackson County", 0, 50),  # water only
            _census_row("27601", "37183", "Wake County", 7, 0),
            _census_row("96799", "60010", "Eastern District", 4, 0),
            "",
        ]
    )
    assert mod.reduce(text) == [
        ("27601", "37183", "Wake County", "NC", 1.0),
        ("28734", "37043", "Clay County", "NC", 0.25),
        ("28734", "37113", "Macon County", "NC", 0.75),
        ("96799", "60010", "Eastern District", "AS", 1.0),
    ]
    with pytest.raises(ValueError, match="columns"):
        mod.reduce("a|b\n1|2\n")
    with pytest.raises(ValueError, match="state"):
        mod.reduce(
            CENSUS_HEADER + "\n" + _census_row("00001", "99001", "Nowhere", 1, 0)
        )


def test_refresh_script_writes_the_same_bytes_every_time(tmp_path: Path) -> None:
    mod = _refresh_script()
    rows = [("27601", "37183", "Wake County", "NC", 1.0)]
    a, b = tmp_path / "a.csv.gz", tmp_path / "b.csv.gz"
    mod.write_crosswalk(rows, a)
    mod.write_crosswalk(rows, b)
    assert a.read_bytes() == b.read_bytes()
    assert gzip.decompress(a.read_bytes()).decode() == (
        "zcta,county_fips,county_name,state,land_area_share\n"
        "27601,37183,Wake County,NC,1.0\n"
    )
    note = mod.source_note("2026-10-03", "ab" * 32, 1)
    assert mod.URL in note and "Downloaded 2026-10-03" in note and "ab" * 32 in note


def test_the_state_table_agrees_with_the_engines_county_list() -> None:
    import policyengine_us

    path = Path(policyengine_us.__file__).parent / "data" / "county_fips_2020.csv.gz"
    engine = {
        (row.split(",")[3][:2], row.split(",")[1])
        for row in gzip.decompress(path.read_bytes()).decode().splitlines()[1:]
    }
    mod = _refresh_script()
    # the engine also lists the US Minor Outlying Islands (74), which have no ZCTA
    assert engine - set(mod.STATE_BY_FIPS.items()) == {("74", "UM")}


def test_ytd_vs_1099_gap_reported(lay: Layout) -> None:
    """Once the 1099 is in it is the authority: each YTD figure it also reports
    is shown beside the form's figure with the gap, never added to it."""
    drop(lay, "income.csv", INCOME)
    ingest(lay)
    conn = db.connect(lay.data / "ledger" / "planner.db")
    assert gaps(conn, 2025) == []  # no form yet: nothing to compare
    db.add_document(
        conn,
        fingerprint="div-2025",
        file_name="1099-div.pdf",
        kind="pdf",
        pages=1,
        batch="b1",
        facts=[
            db.Fact("1099-DIV", 2025, "Vanguard", "1a", "Ordinary dividends", 850.0, 1),
            db.Fact("1099-DIV", 2025, "Vanguard", "2a", "Capital gain dist", 150.0, 1),
        ],
    )
    got = gaps(conn, 2025)
    assert [(g.box, g.form, g.ytd, g.reported, g.gap) for g in got] == [
        ("dividends", "1099-DIV 1a", 832.43, 850.0, 17.57),
        ("capital_gain_distributions", "1099-DIV 2a", 150.0, 150.0, 0.0),
    ]
    # the needs lookup reads the form, not the form plus the YTD figure
    from planner.ingest.needs import need_value

    assert need_value(conn, lay, 2025, "ordinary_dividends") == 850.0
    r = runner.invoke(
        app, ["status", "--year", "2025"], env={"PLANNER_HOME": str(lay.root)}
    )
    assert r.exit_code == 0, r.output
    assert "1099-DIV 1a" in r.output and "+17.57" in r.output
    assert "1099-DIV 2a" not in r.output  # no gap, no line
