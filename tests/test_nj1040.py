"""Unit 3d-13: a filed New Jersey NJ-1040 (2025), read from pages 1-3 of the
official form's text with synthetic amounts. Page 4 is not read: its only
printed year is line 69's next-year credit."""

from __future__ import annotations

from pathlib import Path

from tests.test_forms import one

PAGE1 = [
    "2025 NJ-1040",
    "New Jersey Resident",
    "Income Tax Return",
    "13. Total Exemption Amount (Add totals from the lines at 6 through 12) "
    "......... 13. 1,000.00",
]
PAGE2 = [
    "Name(s) as shown on Form NJ-1040 Your Social Security Number",
    "Page 2",
    "15. Wages, salaries, tips, and other employee compensation (State wages from",
    "Box 16 of enclosed W-2(s)) (See instructions) ........... 15. 61,250.00",
    "16a. Taxable interest income (Enclose federal Schedule B if over $1,500)",
    "(See instructions) ........................ 16a. 500.00",
    "17. Dividends ................................... 17. 0.00",
    "27. Total Income (Add lines 15, 16a, 17 through 20a, and 21 through 26) "
    "...... 27. 61,750.00",
    "29. New Jersey Gross Income (Subtract line 28c from line 27)",
    "(See instructions) .......................... 29. 61,750.00",
    "38. Total Exemptions and Deductions (Add lines 30 through 37c) ..... 38. 1,000.00",
    "39. Taxable Income (Subtract line 38 from line 29) ........ 39. 60,750.00",
    "40a. Total Property Taxes (18% of Rent) Paid (See instructions page 25) "
    ".... 40a. 8,000.00",
    "40b. Indicate your residency status during 2025 (fill in only one oval)",
    "41. Property Tax Deduction (From Worksheet H) (See instructions) "
    ".... 41. 8,000.00",
]
PAGE3 = [
    "Name(s) as shown on Form NJ-1040 Your Social Security Number",
    "Page 3",
    "42. New Jersey Taxable Income (Subtract line 41 from line 39) ..... 42. 52,750.00",
    "43. Tax on amount on line 42 (Tax Table page 54) ......... 43. 1,423.00",
    "50. Balance of Tax After Credits",
    "(Subtract line 49 from line 45) If zero or less, make no entry ..... 50. 1,423.00",
    "54. Total Tax Due (Add lines 50 through 53c) ........ 54. 1,423.00",
    "55. Total NJ Income Tax Withheld",
    "(Enclose Forms W-2 and 1099)(Part-year residents, see instr.) ..... 55. 1,500.00",
    "57. New Jersey Estimated Tax Payments/Credit from 2024 tax return .... 57. 100.00",
    "58. New Jersey Earned Income Tax Credit (See instructions) ...... 58. 0.00",
    "65. New Jersey Child Tax Credit (See instructions) ..... younger on 12/31/25 "
    "..... 65. 0.00",
    "66. Total Withholdings, Credits, and Payments (Add lines 55 through 65) "
    "..... 66. 1,600.00",
]


def test_a_filed_nj_1040_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "nj1040", [PAGE1, PAGE2, PAGE3])
    assert (f.form, f.issuer, f.tax_year) == ("NJ-1040", "NJ", 2025)
    got = {b: f.boxes[b][1] for b in ("13", "15", "16a", "17", "27", "29", "38")}
    assert got == {
        "13": 1000.0,
        "15": 61250.0,
        "16a": 500.0,
        "17": 0.0,
        "27": 61750.0,
        "29": 61750.0,
        "38": 1000.0,
    }
    got = {b: f.boxes[b][1] for b in ("39", "40a", "41", "42", "43", "50", "54")}
    assert got == {
        "39": 60750.0,
        "40a": 8000.0,
        "41": 8000.0,
        "42": 52750.0,
        "43": 1423.0,
        "50": 1423.0,
        "54": 1423.0,
    }
    got = {b: f.boxes[b][1] for b in ("55", "57", "58", "65", "66")}
    assert got == {"55": 1500.0, "57": 100.0, "58": 0.0, "65": 0.0, "66": 1600.0}
    assert "67" not in f.boxes
