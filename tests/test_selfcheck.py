"""The real-calculation proof. 2026 single, $50,000 wages, NC:
taxable 50,000 - 16,100 = 33,900; tax = 10% x 12,400 + 12% x 21,500 = 3,820
(Rev. Proc. 2025-32)."""

from __future__ import annotations

import pytest

from planner.engine.selfcheck import run


@pytest.mark.engine
def test_selfcheck_matches_hand_worked_2026_figure() -> None:
    r = run(2026, 50_000)
    assert r.income_tax == pytest.approx(3820.0, abs=1.0)
    assert r.engine_version
