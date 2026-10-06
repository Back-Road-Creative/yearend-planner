"""Phase 10, unit 2f: the limits notice (not tax, legal or investment advice;
a preparer reviews) is one string, and the docs carry it word for word as the
page and the tax pack do."""

from __future__ import annotations

from pathlib import Path

import pytest

from planner import NOTICE

ROOT = Path(__file__).resolve().parents[1]


def test_notice_names_the_limits() -> None:
    assert "Not tax, legal or investment advice" in NOTICE
    assert "preparer" in NOTICE


@pytest.mark.parametrize("doc", ["README.md", "GUIDE.md"])
def test_docs_carry_the_notice(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    assert NOTICE in text
