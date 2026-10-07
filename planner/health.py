"""The local health summary (master plan unit 7a): five separate answers,
each "ok" or "attention" with its reasons, never folded into one score.

- App: the planner and tax engine versions, whether the engine publishes the
  year, the config files load, the data folder can be written, the last
  update check and the last backup.
- Data completeness: the Needed list, late forms, uncategorised bank rows and
  what was set aside (don't have, waived).
- Calculation: the engine's one-household self-check, whether the draft
  return builds, the CHECK notes it raises and the gaps the planner does not
  handle.
- Decision readiness: the dashboard's "Ready to act".
- Tax-pack readiness: the dashboard's "Ready for a preparer".

It reports only: nothing is imported, fetched, repaired or written, beyond the
portfolio's all-time high that every read of the portfolio records.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from planner import __version__
from planner.paths import Layout

OK = "ok"
ATTENTION = "attention"
NAMES = (
    "App",
    "Data completeness",
    "Calculation",
    "Decision readiness",
    "Tax-pack readiness",
)
BACKUP_DAYS = 30  # no backup for this long: the App answer says so


@dataclass(frozen=True)
class Answer:
    name: str
    status: str  # OK or ATTENTION
    facts: tuple[str, ...] = ()  # what was found, either way
    problems: tuple[str, ...] = ()  # why it is not ok

    @property
    def line(self) -> str:
        return f"{self.name}: {self.status}"


def _answer(name: str, facts: list[str], problems: list[str]) -> Answer:
    return Answer(name, ATTENTION if problems else OK, tuple(facts), tuple(problems))


def _backup(lay: Layout, today: date) -> tuple[str, str]:
    """(fact, problem) about the newest backup zip in out/backups/."""
    zips = sorted(
        (lay.out / "backups").glob("planner-backup-*.zip"),
        key=lambda p: p.stat().st_mtime,
    )
    if not zips:
        return "no backup yet", "no backup yet (planner backup)"
    made = date.fromtimestamp(zips[-1].stat().st_mtime)
    age = (today - made).days
    fact = f"last backup {made.isoformat()} ({zips[-1].name})"
    late = f"last backup {age} days ago (planner backup)" if age > BACKUP_DAYS else ""
    from planner import rehearsal

    proved = rehearsal.last(zips[-1])
    if proved is None:
        fact += "; never rehearsed (planner backup --rehearse)"
    elif proved.get("ok"):
        fact += (
            f"; rehearsed {proved.get('date')}: {proved.get('compared')} figures match"
        )
    else:
        failed = (
            f"the last backup's rehearsal on {proved.get('date')} found "
            f"{proved.get('differences')} figures that differ after a restore"
        )
        late = f"{failed}; {late}" if late else failed
    return fact, late


def app(
    lay: Layout,
    year: int,
    today: date,
    engine: str,
    years: list[int],
    update_check: str,
) -> Answer:
    from planner.config import load_assumptions, load_capabilities, load_thresholds

    facts = [f"planner {__version__}", f"tax engine policyengine-us {engine}"]
    problems: list[str] = []
    if years and year not in years:
        problems.append(f"the tax engine does not publish {year} (planner update)")
    try:
        load_assumptions(lay.config / "assumptions.example.yaml")
        load_thresholds(lay.config / "thresholds.yaml")
        load_capabilities(lay.config / "capabilities.yaml")
        facts.append("config files load")
    except Exception as exc:  # noqa: BLE001 - every config fault is reported
        problems.append(f"config does not load: {exc}")
    if not os.access(lay.data, os.W_OK):
        problems.append(f"the data folder cannot be written: {lay.data}")
    facts.append(update_check)
    fact, problem = _backup(lay, today)
    facts.append(fact)
    if problem:
        problems.append(problem)
    return _answer(NAMES[0], facts, problems)


def summary(lay: Layout, year: int, today: date) -> list[Answer]:
    """The five answers for ``year`` as of ``today``, in NAMES order."""
    from planner.dashboard import page
    from planner.engine.selfcheck import run

    pg = page.gather(lay, year, today)
    plan, act, prep = pg.readiness
    out = [app(lay, year, today, pg.engine, pg.years, pg.update_check)]

    problems = [
        *(f"needed: {s.need.label}" for s in pg.needed),
        *(f"late: {e.form} from {e.issuer}" for e in pg.late_forms),
        *(
            [f"{pg.loose_rows} bank rows with no Schedule C category"]
            if pg.loose_rows
            else []
        ),
    ]
    facts = [
        f"{len(pg.needed)} open on the Needed list",
        f"{len(pg.estimates)} standing as estimates",
        f"{pg.set_aside} set aside (don't have, waived)",
    ]
    if pg.stale_days is not None:
        problems.append(f"no document imported for {pg.stale_days} days")
    out.append(_answer(NAMES[1], facts, problems))

    facts, problems = [], []
    try:
        sc = run()
        if sc.income_tax > 0:
            facts.append(
                f"engine self-check: {sc.year} federal tax {sc.income_tax:,.2f}"
            )
        else:
            problems.append("engine self-check: tax is not positive")
    except Exception as exc:  # noqa: BLE001 - a broken engine is the answer
        problems.append(f"engine self-check failed: {exc}")
    if pg.draft is not None:
        checks = [n for n in pg.draft.notes if n.startswith("CHECK:")]
        facts.append(f"draft return builds; {len(checks)} CHECK note(s) for review")
    else:
        facts.append(f"draft return waits: {pg.draft_blocked}")
    problems += [g.reason for g in pg.coverage]
    out.append(_answer(NAMES[2], facts, problems))

    out.append(_answer(NAMES[3], [plan.line, act.line], list(act.blockers)))
    out.append(_answer(NAMES[4], [prep.line], list(prep.blockers)))
    return out


def render(year: int, today: date, answers: list[Answer]) -> str:
    lines = [
        f"Health for {year} as of {today.isoformat()} (reports only; repairs nothing)"
    ]
    for a in answers:
        lines.append(a.line)
        lines += [f"  {f}" for f in a.facts]
        lines += [f"  - {p}" for p in a.problems]
    return "\n".join(lines) + "\n"
