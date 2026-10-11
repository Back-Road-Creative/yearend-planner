"""Unit 3d-14: Virginia Form 760, read from a filed return. Line numbers and
labels follow the 2025 Form 760 (Virginia Department of Taxation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.ingest.pdf import Unmatched
from tests.test_forms import one

PAGE1 = [
    "2025",
    "Virginia Form 760",
    "Resident Income Tax Return",
    "1. Adjusted Gross Income from federal return - Not federal taxable income "
    "........ 1 61,050.00",
    "2. Additions from enclosed Schedule ADJ, Line 3. ........ 2 0.00",
    "3. Add Lines 1 and 2 ........ 3 61,050.00",
    "You 0.00 + Spouse 0.00 = 4 0.00",
    "7. Subtractions from enclosed Schedule ADJ, Line 7 ........ 7 300.00",
    "8. Add Lines 4, 5, 6, and 7 ........ 8 300.00",
    "9. Virginia Adjusted Gross Income (VAGI) - Subtract Line 8 from Line 3.",
    "Note: If less than $11,950 for Filing Status 1 or 3; or $23,900 for Filing "
    "Status 2, your tax is $0.00 ....9 60,750.00",
    "11. If you do not claim itemized deductions on Line 10, enter standard "
    "deduction. See instructions. ...... 11 8,750.00",
    "12. Exemptions. Sum of total from Exemption Section A plus Exemption Section "
    "B ........ 12 930.00",
    "14. Add Lines 10, 11, 12, and 13 ........ 14 9,680.00",
    "15. Virginia Taxable Income - Subtract Line 14 from Line 9 ........ 15 51,070.00",
]
PAGE2 = [
    "Social Security Number",
    "2025 Form 760",
    "16. Amount of Tax from Tax Table or Tax Rate Schedule (round to whole "
    "dollars) ........ 16 2,679.00",
    "17. Spouse Tax Adjustment (STA). Filing Status 2",
    "and STA amount on Line 17. 17 0.00",
    "18. Net Amount of Tax - Subtract Line 17 from Line 16 ........ 18 2,679.00",
    "19a. Your Virginia withholding ........ 19a 2,800.00",
    "20. Estimated tax payments for taxable year 2025 (from Form 760ES) "
    "........ 20 100.00",
    "23. Tax Credit for Low-Income Individuals or Earned Income Credit from Sch. "
    "ADJ, Line 17 ...... 23 0.00",
    "26. Add Lines 19a through 25 ........ 26 2,900.00",
    "28. If Line 18 is less than Line 26, subtract Line 18 from Line 26. This is "
    "Your Tax Overpayment ........ 28 221.00",
    "36. If Line 28 is greater than Line 34, subtract Line 34 from Line 28 "
    "........YOUR REFUND ........ 36 221.00",
]


def test_a_filed_form_760_reads_as_one_form(tmp_path: Path) -> None:
    (f,) = one(tmp_path, "va760", [PAGE1, PAGE2])
    assert (f.form, f.issuer, f.tax_year) == ("VA-760", "VA", 2025)
    got = {b: f.boxes[b][1] for b in ("1", "2", "3", "4", "7", "8", "9", "11")}
    assert got == {
        "1": 61050.0,
        "2": 0.0,
        "3": 61050.0,
        "4": 0.0,
        "7": 300.0,
        "8": 300.0,
        "9": 60750.0,
        "11": 8750.0,
    }
    got = {b: f.boxes[b][1] for b in ("12", "14", "15", "16", "17", "18", "19a")}
    assert got == {
        "12": 930.0,
        "14": 9680.0,
        "15": 51070.0,
        "16": 2679.0,
        "17": 0.0,
        "18": 2679.0,
        "19a": 2800.0,
    }
    got = {b: f.boxes[b][1] for b in ("20", "23", "26", "28", "36")}
    assert got == {"20": 100.0, "23": 0.0, "26": 2900.0, "28": 221.0, "36": 221.0}
    assert "27" not in f.boxes and "10" not in f.boxes


def test_a_part_year_form_760py_is_not_read_as_form_760(tmp_path: Path) -> None:
    page = [line.replace("Virginia Form 760", "Virginia Form 760PY") for line in PAGE1]
    with pytest.raises(Unmatched, match="no tax year"):
        one(tmp_path, "va760py", [page])
