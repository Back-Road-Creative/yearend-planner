from __future__ import annotations

import re
from pathlib import Path

import pytest

from planner.config import (
    ASSUMPTION_FIELDS,
    VALIDATED,
    ConfigError,
    load_assumptions,
    load_capabilities,
    load_records,
    load_thresholds,
    missing_assumptions,
)
from planner.engine.tax import CONFIG_PARAMS


def test_example_assumptions_cover_every_field_and_are_unknown(repo_root: Path) -> None:
    a = load_assumptions(repo_root / "config" / "assumptions.example.yaml")
    assert set(a) == set(ASSUMPTION_FIELDS)
    # The example ships no personal values: all but the statutory access age are null.
    assert set(missing_assumptions(a)) == set(ASSUMPTION_FIELDS) - {"ira_access_age"}


def test_unknown_assumption_field_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "a.yaml"
    p.write_text("birth_date: null\nnet_worth: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="net_worth"):
        load_assumptions(p)


def test_thresholds_every_value_has_a_source(repo_root: Path) -> None:
    t = load_thresholds(repo_root / "config" / "thresholds.yaml")
    assert 2026 in t
    assert t[2026]["std_deduction_single"]["value"] == 16100
    for name, row in t[2026].items():
        assert row["source"], name


# the hand rows the levers read for the year planned (planner/plan/levers.py
# _phase and the HSA lever); the engine carries none of them
LEVER_ROWS = {
    f"{kind}_phaseout_{who}_{part}"
    for kind in ("ira", "roth")
    for who in ("single", "joint")
    for part in ("start", "width")
} | {"hsa_limit_self", "hsa_limit_family", "hsa_catchup_55plus"}


def test_every_hand_year_lists_every_lever_row(repo_root: Path) -> None:
    """A year missing one of these fails mid-run, so the file itself must be
    complete. A 2025 return is prepared in 2026, so 2025 is listed too."""
    t = load_thresholds(repo_root / "config" / "thresholds.yaml", merged=False)
    assert {2025, 2026} <= set(t)
    assert not LEVER_ROWS & set(CONFIG_PARAMS)
    for year, rows in t.items():
        assert LEVER_ROWS <= set(rows), (year, sorted(LEVER_ROWS - set(rows)))


def test_threshold_without_source_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("2026:\n  x:\n    value: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="needs value and source"):
        load_thresholds(p)


def test_capabilities_statuses(repo_root: Path, tmp_path: Path) -> None:
    c = load_capabilities(repo_root / "config" / "capabilities.yaml")
    assert set(c.values()) <= {"verified", "partial", "unsupported"}
    bad = tmp_path / "c.yaml"
    bad.write_text("federal_income_tax: verified\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="federal_income_tax: a record"):
        load_capabilities(bad)


RECORD = (
    "x:\n  status: verified\n  evidence: implemented\n  tests: [tests/test_tax.py]\n"
)


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ("  evidence2: no\n", "unknown field evidence2"),
        ("  proven: nothex\n", "proven must be a commit"),
    ],
)
def test_a_record_with_an_unknown_field_is_refused(
    tmp_path: Path, extra: str, why: str
) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(RECORD + extra, encoding="utf-8")
    with pytest.raises(ConfigError, match=why):
        load_records(p)


def test_evidence_above_implemented_needs_an_independent_source(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(RECORD.replace("implemented", "independently_validated"), "utf-8")
    with pytest.raises(ConfigError, match="x: independently_validated needs expected"):
        load_records(p)
    p.write_text(
        RECORD.replace("implemented", "independently_validated")
        + "  expected: IRS Pub. 17 worked example\n",
        "utf-8",
    )
    assert load_records(p)["x"].expected == "IRS Pub. 17 worked example"
    p.write_text(RECORD.replace("implemented", "certified"), "utf-8")
    with pytest.raises(ConfigError, match="x: evidence must be one of"):
        load_records(p)


def test_every_capability_row_cites_an_existing_test(repo_root: Path) -> None:
    """The matrix is only auditable if each record says which test proves it: a
    verified or partial record lists tests/<file>.py or tests/<file>.py::<name>,
    and every one listed must exist; an unsupported record names the rule it
    replaces in its note instead."""
    records = load_records(repo_root / "config" / "capabilities.yaml")
    for name, rec in records.items():
        if rec.status == "unsupported":
            assert rec.note, f"{name} (unsupported) names no rule in its note"
            continue
        assert rec.tests, f"{name} ({rec.status}) lists no tests"
        for ref in rec.tests:
            file, _, test = ref.partition("::")
            src = repo_root / file
            assert src.is_file(), f"{name}: {file} does not exist"
            if test:
                assert re.search(rf"^def {test}\b", src.read_text("utf-8"), re.M), (
                    f"{name}: {file} has no test {test}"
                )


def test_no_status_promotes_evidence_and_proven_is_never_typed(repo_root: Path) -> None:
    """The historical status is kept, never read as acceptance (finding F13):
    nothing is independently validated until it names its source, and the
    commit a release proved it at is stamped into the shipped copy only."""
    records = load_records(repo_root / "config" / "capabilities.yaml")
    assert all(not r.proven for r in records.values())
    assert all(r.expected for r in records.values() if r.evidence in VALIDATED)


def test_the_release_build_stamps_the_proven_commit_only_from_a_release_run(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_privacy import _load_build_release

    br = _load_build_release()
    caps = tmp_path / "config" / "capabilities.yaml"
    caps.parent.mkdir()
    src = (repo_root / "config" / "capabilities.yaml").read_text("utf-8")
    caps.write_text(src, "utf-8")
    monkeypatch.delenv("PROVEN_COMMIT", raising=False)
    br.stamp_proven(tmp_path)
    assert caps.read_text("utf-8") == src  # a local build proves nothing
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("PROVEN_COMMIT", sha)
    br.stamp_proven(tmp_path)
    stamped = load_records(caps)
    assert (
        stamped.keys()
        == load_records(repo_root / "config" / "capabilities.yaml").keys()
    )
    assert {r.proven for r in stamped.values()} == {sha}


def test_yaml_is_never_executed(tmp_path: Path) -> None:
    p = tmp_path / "evil.yaml"
    p.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - any constructor error is the point
        load_assumptions(p)


def test_needs_work_requirement_year_matches_the_config(repo_root: Path) -> None:
    """The Needed panel asks for the SE-hours log from the year the Medicaid
    work requirement starts; that year is the config's."""
    from planner.ingest.needs import MEDICAID_WORK_FROM

    t = load_thresholds(repo_root / "config" / "thresholds.yaml", merged=False)
    starts = {
        str(rows["medicaid_work_requirement_start"]["value"])
        for rows in t.values()
        if "medicaid_work_requirement_start" in rows
    }
    assert starts == {f"{MEDICAID_WORK_FROM}-01-01"}
