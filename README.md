
## Planners: raising cash and wash sales (Phase 4c)

`planner withdraw --year 2026 [--target 30000] [--budget 5000] [--lot 11111111:VTSAX:2019-01-15]`
raises the cash target (the profile's `cash_target` unless `--target` says
otherwise): the cash accounts first, then the taxable lots with the least gain
per dollar raised — loss lots, then the highest-basis long-term lots — never a
retirement account. A `--lot` list is specific-ID: those lots go first, in the
order given. `--budget` caps the realized gain; what the budget cannot raise is
reported as short, not quietly sold. Each run prices the sales through the
engine and prints ACA MAGI and federal + NC tax before and after. A loss lot
whose symbol was bought inside the last 30 days is flagged as a wash sale.

`planner washsales [--year 2026] [--as-of 2026-06-30]` lists every loss sale
with a buy of the same symbol within 30 days either side, across all accounts
including IRAs (a dividend reinvestment is a buy), and the symbols whose window
is still open as of the date.
