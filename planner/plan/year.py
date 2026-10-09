"""The year-end plan on one page: every planner's headline, the Needed panel,
and the deadline calendar. A planner that cannot run yet (a required input is
still missing) reports what it needs instead of stopping the page; nothing
is estimated in its place."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    inputs,
    levers,
    magi,
    spending,
    washsale,
    withdraw,
)
from planner.plan.inputs import UNKNOWN, OverrideError, Overrides
from planner.taxprep import expected

SECTIONS = (
    "needed",
    "forms",
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


@dataclass(frozen=True)
class Table:
    """A table a section carries beside its lines: the dashboard draws it as a
    table, the text page as aligned columns. Cells are formatted strings; the
    first column is a label, the rest are numbers."""

    title: str
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Section:
    name: str
    ok: bool  # False: the planner could not run; ``lines`` says what it needs
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    # unknown inputs the figures leave out (not zero): never shown as actual
    rests_on: list[str] = field(default_factory=list)


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
    lines += [f"missing  {s.need.key:30} {s.need.where}" for s in missing]
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
    return Section("magi", True, lines, list(pj.inputs.notes))


SPILL = "WARNING: qualified dividends / long-term gains pushed into 15%"


def _conversion(
    lay: Layout,
    year: int,
    _today: date,
    ov: Overrides,
    sizing: conversion.Sizing | None = None,
) -> Section:
    # sized from the ledger year plus the other overrides; the conversion the
    # plan adopts (``ov.planned_conversion`` once resolved) is what is sized
    sz = sizing or conversion.size(
        lay, year, replace(ov, planned_conversion=0.0, conversion_target="manual")
    )
    rec = sz.recommendation
    lines = []
    notes = list(sz.notes)
    if ov.conversion_target == "auto" and rec:
        lines.append(
            f"adopted {rec.name} {rec.amount:,.2f} as the year's conversion "
            f"(+{ov.planned_conversion:,.2f} on the {sz.already:,.2f} already "
            "recorded); MAGI, levers, cash and estimated tax include it"
        )
    elif ov.conversion_target == "auto":
        notes.append("auto: no recommendation, nothing adopted")
    lines.append(
        f"already converted {sz.already:,.2f}; cap {sz.cap:,.2f}; "
        + (
            f"recommended {rec.name} {rec.amount:,.2f} (cash needed "
            f"{rec.cash_needed:,.2f}; {rec.note})"
            + (f"  {SPILL}" if rec.qualified_spill else "")
            if rec
            else "no recommendation (set conversion_objective in the profile)"
        )
    )
    for c in sz.candidates:
        lines.append(
            f"{c.name:15} {c.amount:>12,.2f}  federal +{c.fed_delta:,.2f}  "
            f"NC +{c.state_delta:,.2f}  ACA credit {c.ptc_delta:+,.2f}"
            f"{'  Medicaid month OVER' if c.medicaid_month_over else ''}"
            f"{'  ' + SPILL if c.qualified_spill else ''}"
        )
    return Section("conversion", True, lines, notes)


def _forms(lay: Layout, year: int, today: date, _ov: Overrides) -> Section:
    return Section("forms", True, expected.lines(expected.inventory(lay, year, today)))


def _levers(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    m = levers.menu(lay, year, today, ov)
    waiting = [
        f"{lv.key}: {lv.why}"
        for lv in m.levers
        if not lv.available and lv.why.startswith("needs")
    ]
    return Section("levers", True, levers.summary(m, levers.TOP), m.notes + waiting)


def _spending(lay: Layout, year: int, today: date, _ov: Overrides) -> Section:
    sp = spending.plan(lay, year, today, years=spending.BAND_YEARS)
    lines = [
        f"balance {sp.balance:,.2f}; peak {sp.peak:,.2f} ({sp.peak_date}); "
        f"spending {sp.spending:,.2f} (floor {sp.floor:,.2f}, ceiling "
        f"{sp.ceiling:,.2f})" + ("  DRAWDOWN" if sp.in_drawdown else "")
    ]
    band = Table(
        f"Return bands, {sp.rows[0].year} to {sp.rows[-1].year} (real dollars)",
        [
            "year",
            "age",
            "floor-return balance",
            "floor-return spend",
            "planning-return balance",
            "planning-return spend",
        ],
        [
            [
                str(rw.year),
                str(rw.age),
                f"{rw.balance_floor:,.2f}",
                f"{rw.spend_floor:,.2f}",
                f"{rw.balance_track:,.2f}",
                f"{rw.spend_track:,.2f}",
            ]
            for rw in sp.rows
        ],
    )
    return Section("spending", True, lines, list(sp.notes), [band])


def _glide(
    lay: Layout,
    year: int,
    today: date,
    ov: Overrides,
    g: glidepath.Glide | None = None,
) -> Section:
    g = g or glidepath.glide(lay, year, today, overrides=ov)
    lines = [
        f"accessible {g.accessible:,.2f} vs floor through {g.access_age:g} "
        f"{g.floor_needed:,.2f}: "
        + (f"SHORT by {g.floor_shortfall:,.2f}" if g.floor_shortfall else "covered"),
        f"band: {g.band} (spending {g.spending:,.2f})",
    ]
    for s in g.stresses:
        end = f"runs out at {s.runs_out_age}" if s.runs_out_age else "lasts"
        lines.append(f"stress {s.name:22} {end}")
    if g.first_short_month:
        lines.append(f"cash line falls under the target in {g.first_short_month}")
    table = Table(
        f"Age and year table, {g.rows[0].year} to {g.rows[-1].year} "
        "(on-track line beside the comfort-floor line)",
        [
            "year",
            "age",
            "on-track real",
            "on-track nominal",
            "SS",
            "spend",
            "withdraw",
            "comfort-floor real",
            "comfort-floor spend",
        ],
        [
            [
                str(rw.year),
                str(rw.age),
                f"{rw.balance_real:,.2f}",
                f"{rw.balance_nominal:,.2f}",
                f"{rw.ss:,.2f}",
                f"{rw.spend:,.2f}",
                f"{rw.withdrawal:,.2f}",
                f"{fl.balance_real:,.2f}",
                f"{fl.spend:,.2f}",
            ]
            for rw, fl in zip(g.rows, g.floor_rows, strict=True)
        ],
    )
    return Section("glide", True, lines, list(g.notes), [table])


def _month_table(g: glidepath.Glide, target: float) -> Table:
    """The monthly cash line for this year and next, each month against the
    cash target; ``*`` marks income columns read from ledger rows."""
    return Table(
        f"Monthly cash line, {g.months[0].year}-{g.months[0].month:02d} to "
        f"{g.months[-1].year}-{g.months[-1].month:02d} "
        "(* income from ledger rows; other months at the run-rate)",
        [
            "month",
            "SE",
            "other in",
            "div",
            "in",
            "sales",
            "living",
            "mortgage",
            "premiums",
            "est tax",
            "tax due",
            "irregular",
            "net",
            "cash",
            "vs target",
        ],
        [
            [
                f"{m.year}-{m.month:02d}{'*' if m.actual else ''}",
                f"{m.se:,.2f}",
                f"{m.other_in:,.2f}",
                f"{m.dividends:,.2f}",
                f"{m.cash_in:,.2f}",
                f"{m.planned_in:,.2f}",
                f"{m.living:,.2f}",
                f"{m.mortgage:,.2f}",
                f"{m.premiums:,.2f}",
                f"{m.est_tax:,.2f}",
                f"{m.balance_due:,.2f}",
                f"{m.irregular:,.2f}",
                f"{m.net:,.2f}",
                f"{m.cash:,.2f}",
                "UNDER" if m.cash < target else "ok",
            ]
            for m in g.months
        ],
    )


def _cash(
    lay: Layout,
    year: int,
    today: date,
    ov: Overrides,
    g: glidepath.Glide | None = None,
    why_no_line: str = "",
) -> Section:
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
    notes = list(w.notes)
    tables: list[Table] = []
    if g is not None:
        tables.append(_month_table(g, w.target))
        if g.first_short_month:
            lines.append(
                f"monthly cash line falls under the target in {g.first_short_month}"
            )
    else:
        notes.append(f"monthly cash line not drawn: {why_no_line}")
    return Section("cash", True, lines, notes, tables)


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


def _state(lay: Layout, year: int) -> str:
    """The household's state from the Needed panel ("" when not given)."""
    for s in needed(lay, year).items:
        if s.need.key == "state" and s.state in ("actual", "estimate"):
            return str(s.value)
    return ""


def _calendar(lay: Layout, year: int, today: date, ov: Overrides) -> Section:
    notes: list[str] = []
    owing: dict[int, list[str]] | None = None
    why_none: str | None = None
    state = _state(lay, year)
    try:
        et = esttax.estimate(lay, year, today, ov)
    except (MissingInputError, OverrideError) as exc:
        notes.append(f"Q2 and Q3 left as 'if required': {exc}")
    else:
        # June and September fall in the next year, which has no projection of
        # its own: this year's tax repeated stands in for it, by the same figure
        # the cash line pays installments from (esttax.next_year_required)
        owe = [
            ag.name for ag in et.agencies if esttax.next_year_required(ag, et.agi) > 0
        ]
        owing = {n: list(owe) for n in (2, 3)}
        if not owe:
            # two reasons leave nothing to pay: the de minimis test, or
            # withholding that already covers the safe harbor (IRC 6654(d)(1)(B))
            reasons = {
                ag.name: (
                    "tax after withholding under the de minimis"
                    if ag.de_minimis
                    else "withholding covers the safe harbor"
                )
                for ag in et.agencies
            }
            if len(set(reasons.values())) == 1:
                why_none = next(iter(reasons.values()))
            else:
                why_none = "; ".join(
                    f"{calendar.agency_name(a)}: {t}" for a, t in reasons.items()
                )
        notes.append(
            f"Q2 and Q3 fall in {year + 1}; whether they are required rests on "
            f"{year}'s projected tax repeated, less withholding (planner esttax)"
        )
    lines = []
    for d in calendar.deadlines(year, state, owing, why_none):
        when = d.date if d.date == d.nominal else f"{d.date} (from {d.nominal})"
        past = "  done?" if date.fromisoformat(d.date) < today else ""
        lines.append(f"{when:28} {d.item}{past}")
    return Section("calendar", True, lines, notes)


BUILDERS = {
    "needed": _needed,
    "magi": _magi,
    "conversion": _conversion,
    "levers": _levers,
    "forms": _forms,
    "spending": _spending,
    "glide": _glide,
    "cash": _cash,
    "esttax": _esttax,
    "washsales": _washsales,
    "calendar": _calendar,
}


# Sections priced from the household: each rests on the unknown tax inputs,
# plus the unknown items it reads itself.
PRICED = {
    "magi": (),
    "conversion": (),
    "levers": (),
    "glide": glidepath.MONTHLY,
    "cash": glidepath.MONTHLY,
    "esttax": (),  # the withholding of the household's own agencies, below
}
RESTS_ON = "rests on unknown (left out, not zero): "


def _rests_on(lay: Layout, year: int, ov: Overrides, plan: YearPlan) -> None:
    try:
        inp = inputs.build(lay, year, ov)
    except (MissingInputError, OverrideError):
        return  # the priced sections already say what they need
    for s in plan.sections:
        if not s.ok or s.name not in PRICED:
            continue
        keys = PRICED[s.name]
        if s.name == "esttax":
            keys = tuple(
                esttax.withheld_key(a) for a in esttax.agencies(inp.household.state)
            )
        own = [k for k in keys if inp.state(k) == UNKNOWN]
        s.rests_on = inp.tax_unknown + own
        if s.rests_on:
            s.notes.append(RESTS_ON + ", ".join(s.rests_on))


def assemble(
    lay: Layout,
    year: int,
    as_of: date | None = None,
    overrides: Overrides | None = None,
) -> YearPlan:
    today = as_of or date.today()
    ov = overrides or Overrides()
    # conversion_target=auto: one sizing, adopted by every section that follows
    ov, sizing = conversion.resolve(lay, year, ov)
    # the glide section and the cash section's monthly line read one glide run
    memo: dict[str, glidepath.Glide | MissingInputError | OverrideError] = {}

    def shared_glide(lay_: Layout, y: int, t: date, o: Overrides) -> glidepath.Glide:
        if "g" not in memo:
            try:
                memo["g"] = glidepath.glide(lay_, y, t, overrides=o)
            except (MissingInputError, OverrideError) as exc:
                memo["g"] = exc
        got = memo["g"]
        if isinstance(got, Exception):
            raise got
        return got

    def cash(lay_: Layout, y: int, t: date, o: Overrides) -> Section:
        try:
            g, why = shared_glide(lay_, y, t, o), ""
        except (MissingInputError, OverrideError) as exc:
            g, why = None, str(exc)
        return _cash(lay_, y, t, o, g, why)

    builders: dict[str, Callable[[Layout, int, date, Overrides], Section]] = {
        **BUILDERS,
        "conversion": lambda lay_, y, t, o: _conversion(lay_, y, t, o, sizing),
        "glide": lambda lay_, y, t, o: _glide(
            lay_, y, t, o, shared_glide(lay_, y, t, o)
        ),
        "cash": cash,
    }
    plan = YearPlan(year, today.isoformat())
    for name in SECTIONS:
        try:
            plan.sections.append(builders[name](lay, year, today, ov))
        except (MissingInputError, OverrideError) as exc:
            plan.sections.append(Section(name, False, [f"needs: {exc}"]))
    _rests_on(lay, year, ov, plan)
    return plan


def text_table(t: Table) -> list[str]:
    """Aligned columns: the label column left, the numbers right."""
    cols = list(zip(t.headers, *t.rows, strict=True))
    width = [max(len(c) for c in col) for col in cols]

    def line(cells: list[str]) -> str:
        first, rest = cells[0], cells[1:]
        return " ".join(
            [first.ljust(width[0])]
            + [c.rjust(w) for c, w in zip(rest, width[1:], strict=True)]
        ).rstrip()

    return [line(t.headers)] + [line(row) for row in t.rows]


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
        for t in s.tables:
            out += ["", f"  {t.title}"] + [f"  {ln}" for ln in text_table(t)]
        out.append("")
    return "\n".join(out)


def write(lay: Layout, plan: YearPlan) -> str:
    """Write the page to ``out/plan-<year>.md`` (personal, gitignored)."""
    lay.out.mkdir(parents=True, exist_ok=True)
    path = lay.out / f"plan-{plan.year}.md"
    path.write_text(render(plan), encoding="utf-8")
    return str(path)
