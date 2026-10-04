"""Filed-return and other-form templates on synthetic PDFs (Phase 2c)."""
# ruff: noqa: E501  (fixture lines mirror the printed forms verbatim)

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.pdf import Unmatched, load_templates, parse_pdf
from tests.pdfgen import make_pdf

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = load_templates(ROOT / "templates" / "forms")

F1040_P1 = [
    "Form 1040 (2025) U.S. Individual Income Tax Return",
    "1z Add lines 1a through 1h . . . . . . . . 1z 85,000.00",
    "2a Tax-exempt interest . . 2a 0.00  b Taxable interest . . . . . 2b 1,234.56",
    "3a Qualified dividends . . 3a 8,100.00  b Ordinary dividends . . . 3b 9,800.00",
    "4a IRA distributions . . 4a 0.00  b Taxable amount . . . . . 4b 0.00",
    "5a Pensions and annuities . 5a 0.00  b Taxable amount . . . . . 5b 0.00",
    "6a Social security benefits 6a 0.00  b Taxable amount . . . . . 6b 0.00",
    "7 Capital gain or (loss). Attach Schedule D if required . . . 7 19,500.00",
    "8 Additional income from Schedule 1, line 10 . . . . . 8 12,000.00",
    "9 Add lines 1z, 2b, 3b, 4b, 5b, 6b, 7, and 8. This is your total income 9 127,534.56",
    "10 Adjustments to income from Schedule 1, line 26 . . . . 10 7,000.00",
    "11 Subtract line 10 from line 9. This is your adjusted gross income 11 120,534.56",
    "12 Standard deduction or itemized deductions (from Schedule A) . 12 15,000.00",
    "13 Qualified business income deduction from Form 8995 or Form 8995-A 13 1,000.00",
    "15 Subtract line 14 from line 11. This is your taxable income . 15 104,534.56",
]
F1040_P2 = [
    "Form 1040 (2025) Page 2",
    "16 Tax (see instructions). Check if any from Form(s): . . . 16 17,000.00",
    "22 Subtract line 21 from line 18. If zero or less, enter -0- . 22 17,000.00",
    "23 Other taxes, including self-employment tax, from Schedule 2, line 21 23 1,696.00",
    "24 Add lines 22 and 23. This is your total tax . . . . . 24 18,696.00",
    "25d Add lines 25a through 25c . . . . . . . . 25d 14,000.00",
    "26 2025 estimated tax payments and amount applied from 2024 return 26 4,000.00",
    "33 Add lines 25d, 26, and 32. These are your total payments . 33 18,000.00",
    "37 Subtract line 33 from line 24. This is the amount you owe . 37 696.00",
]
SCH1 = [
    "Schedule 1 (Form 1040) 2025 Additional Income and Adjustments to Income",
    "3 Business income or (loss). Attach Schedule C . . . . . 3 12,000.00",
    "10 Combine lines 1 through 7 and 9. Enter here and on Form 1040 10 12,000.00",
    "15 Deductible part of self-employment tax. Attach Schedule SE . 15 848.00",
    "16 Self-employed SEP, SIMPLE, and qualified plans . . . 16 6,152.00",
    "26 Add lines 11 through 23 and 25. These are your adjustments to income 26 7,000.00",
]
SCH2 = [
    "Schedule 2 (Form 1040) 2025 Additional Taxes",
    "4 Self-employment tax. Attach Schedule SE . . . . . . 4 1,696.00",
    "21 Add lines 4, 7 through 16, 18, and 19. These are your total other taxes 21 1,696.00",
]
SCH3 = [
    "Schedule 3 (Form 1040) 2025 Additional Credits and Payments",
    "1 Foreign tax credit. Attach Form 1116 if required . . . . 1 120.00",
    "8 Add lines 6z and 7. Enter here and on Form 1040 . . . . 8 120.00",
]
SCHC = [
    "Schedule C (Form 1040) 2025 Profit or Loss From Business",
    "1 Gross receipts or sales. See instructions . . . . . . 1 20,000.00",
    "7 Gross income. Add lines 5 and 6 . . . . . . . . 7 20,000.00",
    "28 Total expenses before expenses for business use of home. Add lines 8 through 27b 28 8,000.00",
    "31 Net profit or (loss). Subtract line 30 from line 29 . . . 31 12,000.00",
]
SCHD = [
    "Schedule D (Form 1040) 2025 Capital Gains and Losses",
    "7 Net short-term capital gain or (loss). Combine lines 1a through 6 7 (500.00)",
    "15 Net long-term capital gain or (loss). Combine lines 8a through 14 15 20,000.00",
    "16 Combine lines 7 and 15 and enter the result . . . . . 16 19,500.00",
]
SCHSE = [
    "Schedule SE (Form 1040) 2025 Self-Employment Tax",
    "12 Self-employment tax. Add lines 10 and 11 . . . . . 12 1,696.00",
    "13 Deduction for one-half of self-employment tax . . . . 13 848.00",
]
D400 = [
    "D-400 2025 Individual Income Tax Return North Carolina Department of Revenue",
    "For calendar year 2025",
    "6. Federal Adjusted Gross Income . . . . . . . . 6. 120,534.00",
    "12b. North Carolina Taxable Income . . . . . . . 12b. 108,000.00",
    "15. North Carolina Income Tax . . . . . . . . . 15. 4,590.00",
    "20a. North Carolina Income Tax Withheld . . . . . . 20a. 3,800.00",
    "23. Total Payments . . . . . . . . . . . . 23. 3,800.00",
    "26a. Pay This Amount . . . . . . . . . . . . 26a. 790.00",
]
SSA = [
    "Your Social Security Statement",
    "March 12, 2026",
    "Retirement benefits: monthly amount if you start at",
    "age 62 ................ $1,850",
    "age 67 ................ $2,640",
    "age 70 ................ $3,270",
]
NEC = [
    "Form 1099-NEC Nonemployee Compensation",
    "Tax year 2025",
    "PAYER'S name: Example Client LLC (synthetic)",
    "1 Nonemployee compensation $ 20,000.00",
    "4 Federal income tax withheld $ 0.00",
]
W2 = [
    "Form W-2 Wage and Tax Statement 2025",
    "c Employer's name, address, and ZIP code: Example Employer Inc (synthetic)",
    "1 Wages, tips, other compensation $ 30,000.00",
    "2 Federal income tax withheld $ 2,400.00",
    "12a W $ 1,200.00",
    "16 State wages, tips, etc. $ 30,000.00",
    "17 State income tax $ 1,100.00",
]
K = [
    "Form 1099-K Payment Card and Third Party Network Transactions",
    "Tax year 2025",
    "FILER'S name: Example Processor (synthetic)",
    "1a Gross amount of payment card/third party network transactions $ 5,400.00",
]
M1098 = [
    "Form 1098 Mortgage Interest Statement",
    "Tax year 2025",
    "RECIPIENT'S/LENDER'S name: Example Mortgage Co (synthetic)",
    "1 Mortgage interest received from payer(s)/borrower(s) $ 6,500.00",
    "2 Outstanding mortgage principal $ 180,000.00",
]
F5498 = [
    "Form 5498 IRA Contribution Information",
    "Tax year 2025",
    "TRUSTEE'S or ISSUER'S name: Example Brokerage (synthetic)",
    "1 IRA contributions (other than amounts in boxes 2-4, 8-10, 13a, and 14a) $ 7,000.00",
    "3 Roth IRA conversion amount $ 25,000.00",
    "5 Fair market value of account $ 310,000.00",
    "10 Roth IRA contributions $ 0.00",
]
F5498SA = [
    "Form 5498-SA HSA, Archer MSA, or Medicare Advantage MSA Information",
    "Tax year 2025",
    "TRUSTEE'S name: Example HSA Bank (synthetic)",
    "2 Total contributions made in 2025 $ 4,300.00",
    "5 Fair market value of HSA, Archer MSA, or MA MSA $ 12,000.00",
]
F1099SA = [
    "Form 1099-SA Distributions From an HSA, Archer MSA, or Medicare Advantage MSA",
    "Tax year 2025",
    "PAYER'S name: Example HSA Bank (synthetic)",
    "1 Gross distribution $ 900.00",
]


def one(tmp_path: Path, name: str, pages: list[list[str]]) -> list:  # type: ignore[type-arg]
    p = tmp_path / f"{name}.pdf"
    make_pdf(p, pages)
    return parse_pdf(p, TEMPLATES)


def test_1040_two_pages_merge_into_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "1040", [F1040_P1, F1040_P2])
    assert (f.form, f.tax_year, f.issuer) == ("1040", 2025, "self")
    assert f.boxes["11"][1] == 120534.56 and f.boxes["24"][1] == 18696.0
    assert f.boxes["7"][1] == 19500.0 and f.boxes["37"][1] == 696.0
    assert f.boxes["2b"][1] == 1234.56 and f.boxes["3a"][1] == 8100.0
    assert f.boxes["26"][1] == 4000.0 and f.boxes["25d"][1] == 14000.0


F1040_2025 = [
    "Form 1040 (2025) U.S. Individual Income Tax Return",
    "7a Capital gain or (loss). Attach Schedule D if required . . . 7a 2,500.00",
    "11a Subtract line 10 from line 9. This is your adjusted gross income 11a 62,161.13",
    "12e Standard deduction or itemized deductions (from Schedule A) . 12e 15,750.00",
    "13a Qualified business income deduction from Form 8995 or Form 8995-A 13a 8,112.23",
]
SSA1099 = [
    "FORM SSA-1099 - SOCIAL SECURITY BENEFIT STATEMENT",
    "Box 3. Benefits Paid in 2025 $ 24,000.00",
    "Box 4. Benefits Repaid to SSA in 2025 $ 0.00",
    "Box 5. Net Benefits for 2025 (Box 3 minus Box 4) $ 24,000.00",
    "Box 6. Voluntary Federal Income Tax Withholding $ 1,200.00",
]


def test_1040_2025_line_numbers_and_ssa_1099(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "1040-2025", [F1040_2025])
    assert f.boxes["7"][1] == 2500.0 and f.boxes["11"][1] == 62161.13
    assert f.boxes["12"][1] == 15750.0 and f.boxes["13"][1] == 8112.23
    (s,) = one(tmp_path, "ssa1099", [SSA1099])
    assert (s.form, s.issuer, s.tax_year) == ("SSA-1099", "SSA", 2025)
    assert s.boxes["5"][1] == 24000.0 and s.boxes["6"][1] == 1200.0


def test_schedules_parse_with_dot_leaders(tmp_path: Path) -> None:
    forms = one(tmp_path, "sched", [SCH1, SCH2, SCH3, SCHC, SCHD, SCHSE])
    got = {f.form: f for f in forms}
    assert set(got) == {
        "1040-SCH1",
        "1040-SCH2",
        "1040-SCH3",
        "1040-SCHC",
        "1040-SCHD",
        "1040-SCHSE",
    }
    assert got["1040-SCH1"].boxes["16"][1] == 6152.0
    assert got["1040-SCH2"].boxes["4"][1] == 1696.0
    assert got["1040-SCH3"].boxes["1"][1] == 120.0
    assert got["1040-SCHC"].boxes["31"][1] == 12000.0
    assert got["1040-SCHD"].boxes["7"][1] == -500.0
    assert got["1040-SCHSE"].boxes["13"][1] == 848.0
    assert all(f.issuer == "self" and f.tax_year == 2025 for f in forms)


def test_nc_d400_and_ssa_statement(tmp_path: Path) -> None:
    (nc,) = one(tmp_path, "d400", [D400])
    assert (nc.form, nc.issuer, nc.tax_year) == ("NC-D400", "NC", 2025)
    assert nc.boxes["15"][1] == 4590.0 and nc.boxes["20a"][1] == 3800.0
    assert nc.boxes["26a"][1] == 790.0 and "28" not in nc.boxes
    (ssa,) = one(tmp_path, "ssa", [SSA])
    assert (ssa.form, ssa.issuer, ssa.tax_year) == ("SSA", "SSA", 2026)
    assert [ssa.boxes[k][1] for k in ("monthly_62", "monthly_67", "monthly_70")] == [
        1850.0,
        2640.0,
        3270.0,
    ]


def test_information_returns(tmp_path: Path) -> None:
    forms = one(tmp_path, "info", [NEC, W2, K, M1098, F5498, F5498SA, F1099SA])
    got = {f.form: f for f in forms}
    assert got["1099-NEC"].boxes["1"][1] == 20000.0
    assert got["1099-NEC"].issuer == "Example Client LLC (synthetic)"
    assert got["W-2"].boxes["2"][1] == 2400.0 and got["W-2"].boxes["17"][1] == 1100.0
    assert got["W-2"].boxes["12W"][1] == 1200.0
    assert got["W-2"].issuer == "Example Employer Inc (synthetic)"
    assert got["1099-K"].boxes["1a"][1] == 5400.0
    assert got["1099-K"].issuer == "Example Processor (synthetic)"
    assert got["1098"].boxes["1"][1] == 6500.0 and got["1098"].boxes["2"][1] == 180000.0
    assert got["1098"].issuer == "Example Mortgage Co (synthetic)"
    assert got["5498"].boxes["1"][1] == 7000.0 and got["5498"].boxes["3"][1] == 25000.0
    assert got["5498"].boxes["5"][1] == 310000.0
    assert got["5498"].issuer == "Example Brokerage (synthetic)"
    assert (
        got["5498-SA"].boxes["2"][1] == 4300.0
        and got["5498-SA"].boxes["5"][1] == 12000.0
    )
    assert got["1099-SA"].boxes["1"][1] == 900.0


def test_1040_without_agi_is_unmatched(tmp_path: Path) -> None:
    page = [line for line in F1040_P1 if not line.startswith("11 ")]
    with pytest.raises(Unmatched, match="11"):
        one(tmp_path, "bad1040", [page])
