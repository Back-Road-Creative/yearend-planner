"""The year-end plan on one page: every planner's headline, the Needed panel,
and the deadline calendar. A planner that cannot run yet (a required input is
still missing) reports what it needs instead of stopping the page; nothing
is estimated in its place."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from planner.engine.household import MissingInputError
from planner.ingest.needs import needed
from planner.ledger import db
from planner.paths import Layout
from planner.plan import (
    calendar,
    conversion,
    esttax,
    glidepath,
    levers,
    magi,
    spending,
    washsale,
    withdraw,
)
from planner.plan.inputs import Overrides

SECTIONS = (
    "needed",
    "magi",
    "conversion",
    "levers",
    "spending",
    "glide",
    "cash",
    "esttax",
    "washsales",
    "calendar",
)


@dataclass
class Section:
    name: str
    ok: bool  # False: the planner could not run; ``lines`` says what it needs
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class YearPlan:
    year: int
    as_of: str
    sections: list[Section] = field(default_factory=list)

    def section(self, name: str) -> Section:
        for s in self.sections:
            if s.name == name:
                return s
        raise KeyError(name)

    @property
    def blocked(self) -> list[str]:
        return [s.name for s in self.sections if not s.ok]


def _needed(lay: Layout, year: int, _today: date, _ov: Overrides) -> Section:
    rep = needed(lay, year)
    missing = rep.by_state("missing")
    estimates = rep.by_state("estimate")
    lines = [
        f"{len(missing)} missing, {len(estimates)} standing in from YTD, "
        f"{len(rep.by_state('actual'))} actual"
    ]
    lines += [f"missing  {s.need.key:30} {s.need.source}" for s in missing]
    lines += [f"estimate {s.need.key:30} {s.origin}" for s in estimates]
    return Section("needed", True, lines)


def _magi(lay: Layout, year: int, _today: date, ov: Overrides) -> Section:
    pj = magi.project(lay, year, ov)
    res = pj.result
    lines = [
        f"AGI {res.agi:,.2f}  taxable income {res.taxable_income:,.2f}  "
        f"ACA MAGI {res.aca_magi:,.2f} ({res.aca_fpl_pct:.0f}% FPL)",
        f"federal {res.fed_total_tax:,.2f}  NC {res.state_tax:,.2f}  "
        f"ACA credit {res.aca_ptc:,.2f}",
    ]
    for ln in pj.lines:
        state = "OVER" if ln.over else "under"
        lines.append(
            f"{ln.name:40} {state} by {abs(ln.room):>12,.2f}  ({ln.direction})"
        )
    notes = list(pj.inputs.notes)
    if pj.inputs.unknown:
        notes.append("unknown, left out (not zero): " + ", ".join(pj.inputs.unknown))
    return Section("magi", True, lines, notes)


def _conversion(lay: Layout, year: int, _today: date, ov: Overrides) -> Section:
    sz = conversion.size(
        lay,
        year,
        Overrides(
            ov.q4_dividend_estimate,
            ov.planned_st_sales,
            ov.planned_lt_sales,
            0.0,
            ov.planned_hsa,
        ),
    )
    rec = sz.recommendation
    lines = [
        f"already converted {sz.already:,.2f}; cap {sz.cap:,.2f}; "
        + (
            f"recommended {rec.name} {rec.amount:,.2f} (cash needed "
            f"{rec.cash_needed:,.2f}; {rec.note})"
            if rec
            else "no recommendation (set conversion_objective in the profile)"
        )
    ]
    for c in sz.candidates:
        lines.append(
            f"{c.name:15} {c.amount:>12,.2f}  federal +{c.fed_delta:,.2f}  "
            f"NC +{c.state_delta:,.2f}  ACA credit {c.ptc_delta:+,.2f}"
            f"{'  Medicaid month OVER' if c.medicaid_month_over else ''}"
        )
    return Section("conversion", True, lines, list(sz.notes))


def _levers(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    m = levers.menu(lay, year, today, ov)
    waiting = [
        f"{lv.key}: {lv.why}"
        for lv in m.levers
        if not lv.available and lv.why.startswith("needs")
    ]
    return Section("levers", True, levers.summary(m, levers.TOP), m.notes + waiting)


def _spending(lay: Layout, year: int, today: date, _ov: Overrides) -> Section:
    sp = spending.plan(lay, year, today, years=3)
    lines = [
        f"balance {sp.balance:,.2f}; peak {sp.peak:,.2f} ({sp.peak_date}); "
        f"spending {sp.spending:,.2f} (floor {sp.floor:,.2f}, ceiling "
        f"{sp.ceiling:,.2f})" + ("  DRAWDOWN" if sp.in_drawdown else "")
    ]
    return Section("spending", True, lines, list(sp.notes))


def _glide(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    g = glidepath.glide(lay, year, today, overrides=ov)
    lines = [
        f"accessible {g.accessible:,.2f} vs floor through {g.access_age:g} "
        f"{g.floor_needed:,.2f}: "
        + (f"SHORT by {g.floor_shortfall:,.2f}" if g.floor_shortfall else "covered")
    ]
    for s in g.stresses:
        end = f"runs out at {s.runs_out_age}" if s.runs_out_age else "lasts"
        lines.append(f"stress {s.name:22} {end}")
    if g.first_short_month:
        lines.append(f"cash line goes negative in {g.first_short_month}")
    return Section("glide", True, lines, list(g.notes))


def _cash(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    w = withdraw.pick(lay, year, as_of=today, overrides=ov)
    lines = [
        f"cash {w.cash_now:,.2f} vs target {w.target:,.2f}: "
        + (
            "covered"
            if not w.need
            else f"raise {w.need:,.2f} (gain ST {w.gain_st:,.2f}, LT "
            f"{w.gain_lt:,.2f}; ACA MAGI {w.magi_before:,.2f} -> {w.magi_after:,.2f})"
        )
    ]
    for s in w.sales:
        lines.append(
            f"sell {s.account} {s.symbol} {s.acquired} {s.quantity:g} "
            f"-> {s.proceeds:,.2f} (gain {s.gain:,.2f})"
        )
    return Section("cash", True, lines, list(w.notes))


def _esttax(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    et = esttax.estimate(lay, year, today, ov)
    lines = []
    for ag in et.agencies:
        nxt = (
            f"next {ag.next_amount:,.2f} due {ag.next_due}"
            if ag.next_due
            else "nothing further due"
        )
        short = [i for i in ag.installments if i.shortfall]
        lines.append(
            f"{ag.name}: required {ag.required:,.2f} ({ag.basis}); withheld "
            f"{ag.withheld:,.2f}; paid {sum(p.amount for p in ag.payments):,.2f}; "
            f"{nxt}"
            + (
                f"; short on installment {', '.join(str(i.n) for i in short)}"
                if short
                else ""
            )
        )
    notes = list(et.notes) + [n for ag in et.agencies for n in ag.notes]
    return Section("esttax", True, lines, notes)


def _washsales(lay: Layout, year: int, today: date, _ov: Overrides) -> Section:
    conn = db.connect(lay.data / "ledger" / "planner.db")
    try:
        flags = washsale.check(conn, year)
        windows = washsale.open_windows(conn, today)
    finally:
        conn.close()
    lines = [
        f"WASH {f.sale.symbol} loss {f.sale.loss:,.2f} sold {f.sale.date}; "
        f"bought {f.buy.date} in {f.buy.account} ({f.days:+d} days)"
        for f in flags
    ]
    lines += [
        f"open window {sym} until {until} (loss {loss:,.2f} at stake)"
        for sym, until, loss in windows
    ]
    return Section("washsales", True, lines or ["none"])


def _calendar(_lay: Layout, year: int, today: date, _ov: Overrides) -> Section:
    lines = []
    for d in calendar.deadlines(year):
        when = d.date if d.date == d.nominal else f"{d.date} (from {d.nominal})"
        past = "  done?" if date.fromisoformat(d.date) < today else ""
        lines.append(f"{when:28} {d.item}{past}")
    return Section("calendar", True, lines)


BUILDERS = {
    "needed": _needed,
    "magi": _magi,
    "conversion": _conversion,
    "levers": _levers,
    "spending": _spending,
    "glide": _glide,
    "cash": _cash,
    "esttax": _esttax,
    "washsales": _washsales,
    "calendar": _calendar,
}


def assemble(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
) -> YearPlan:
    today = as_of or date.today()
    ov = overrides or Overrides()
    plan = YearPlan(year, today.isoformat())
    for name in SECTIONS:
        try:
            plan.sections.append(BUILDERS[name](lay, year, today, ov))
        except MissingInputError as exc:
            plan.sections.append(Section(name, False, [f"needs: {exc}"]))
    return plan


def render(plan: YearPlan) -> str:
    out = [f"# Year-end plan {plan.year} (as of {plan.as_of})", ""]
    if plan.blocked:
        out.append(
            "blocked until the Needed panel is answered: " + ", ".join(plan.blocked)
        )
        out.append("")
    for s in plan.sections:
        out.append(f"## {s.name}" + ("" if s.ok else "  (not run)"))
        out += [f"  {ln}" for ln in s.lines]
        out += [f"  note: {n}" for n in s.notes]
        out.append("")
    return "\n".join(out)


def write(lay: Layout, plan: YearPlan) -> str:
    """Write the page to ``out/plan-<year>.md`` (personal, gitignored)."""
    lay.out.mkdir(parents=True, exist_ok=True)
    path = lay.out / f"plan-{plan.year}.md"
    path.write_text(render(plan), encoding="utf-8")
    return str(path)
