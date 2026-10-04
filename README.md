# Year-End Tax & Retirement Planner

A Windows program that reads the tax documents you drop into a folder, computes your
taxes with [PolicyEngine US](https://github.com/PolicyEngine/policyengine-us), shows what
is still missing, surfaces every tax lever, and at filing time hands you a complete tax
package. Deterministic, local, no account, no cloud, no LLM at runtime.

Status: Phase 0 (foundations). See the build plan for the phases.

## Use (Windows)

1. Download `yearend-planner-<version>-win64.zip`, right-click → Properties → **Unblock**.
2. Extract it anywhere local (not OneDrive, Dropbox or another synced folder).
3. Double-click `planner.cmd`.

Everything personal stays under `data/` and `out/` inside that folder.

## Develop

```
uv sync --frozen --extra dev
uv run pytest            # add -m "not engine" to skip the ~1 min engine case
uv run ruff check . && uv run ruff format --check . && uv run mypy planner tests
uv run --no-project python scripts/build_release.py   # dist/yearend-planner-<v>-win64.zip
```

CI runs the suite on Ubuntu and Windows and proves the release zip on a clean Windows
runner (`scripts/windows_proof.ps1`): real calculation, path with a space and non-ASCII
characters, moved folder, network blocked, standard user, cloud-sync folder refused.

## License

AGPL-3.0-or-later. PolicyEngine US is AGPL-3.0; this planner is distributed under the
same terms.

## Engine (Phase 1)

`planner compute <household.yaml>` prints every figure for one household-year as JSON:
federal income tax after credits, SE tax, total tax (1040 line 24), tax attributable to
qualified dividends and long-term gains, NC tax, AGI, ACA MAGI, taxable income, QBI
deduction, premium tax credit, FPL percentages, monthly Medicaid MAGI, and the headroom
left under the 0% capital-gains ceiling and the top of the 12% bracket. All figures come
from policyengine-us; the only arithmetic here is threshold minus taxable income.

`planner sweep <household.yaml> --variable taxable_roth_conversions --lo 0 --hi 100000 --step 5000`
runs the whole range in one engine call.

`planner verify data/private/returns/<year>.yaml` recomputes a filed year from the inputs
recorded in that file and prints each line as filed next to the engine's figure. The file
format is `tests/fixtures/2025_return.yaml` (synthetic); your own lives under
`data/private/` and is never committed.

`planner update <release.zip> --sha256 <digest>` verifies the zip, extracts it to
`python-candidate/`, runs the candidate's own selfcheck, then swaps it in and keeps the old
install as `python-previous/`. `planner update --rollback` restores it. `data/` and `out/`
are never touched.

Reference cases (hand-worked, $1 tolerance) are in `tests/test_tax.py`; the engine's
coverage of each rule the planner relies on is recorded in `config/capabilities.yaml`.

## Intake (Phase 2a)

Drop files into `data/inbox/` (PDFs, or a ZIP of them) and run `planner ingest`. Each
file is fingerprinted, matched page by page against the templates in `templates/forms/`
(1099-INT, 1099-DIV, 1099-B totals, 1099-R, 1095-A), and its box values land in the
ledger (`data/ledger/planner.db`) with the source file and page. The originals move to
`data/archive/<year>/` only after the facts commit; a file the templates cannot read
moves to `data/inbox/UNMATCHED/` beside a `.reason.txt`. The same file dropped twice is
a no-op. A corrected form supersedes the earlier one by form, payer and year. No value
is ever inferred: a required box that does not parse unmatches the whole file.

`planner facts --year 2025 --form 1099-DIV` lists what the ledger holds. Scanned pages
and photos go through OCR with confirm (Phase 2e, below).

## CSV intake (Phase 2b)

CSV exports go in the same inbox. Each header-led block is matched against
`templates/csv/` (Vanguard download: holdings and transactions in one file; Vanguard
cost basis by lot; realized gains; dividends and interest; bank exports with a signed
Amount or Debit/Credit columns) and lands in the ledger's `rows` table: one row per
line, money in cents, the source line kept verbatim. A row's identity is the broker's
transaction ID when the export carries one, else a hash of the row plus file name and
line, so the same file twice is a no-op, an overlapping export does not double-count a
transaction ID, and two identical real trades on one day stay two rows. A holdings
export is a snapshot stamped with the import date. Headers the templates do not
recognise send the file to UNMATCHED with the headers listed, and the fix is a template
edit, never a code change. `planner rows --year 2025 --kind transaction` lists rows.

Vanguard's cost-basis, realized-gains and income exports are matched on the column
names in `templates/csv/`; they are verified against synthetic files in the test suite,
and your own export is the check that the names are right (the UNMATCHED reason shows
what differs).

## Filed returns and other forms (Phase 2c)

The same inbox reads last year's filed return and the rest of the year-end paperwork.
Templates in `templates/forms/` now cover Form 1040 (both pages merge into one form,
issuer `self`), Schedules 1, 2, 3, C, D and SE, NC Form D-400 (issuer `NC`), the Social
Security Statement (monthly estimates at 62, 67 and 70, tax year = statement year),
1099-NEC, 1099-K, 1098, 5498, 5498-SA and 1099-SA. A template may name a literal
`issuer` for the taxpayer's own documents instead of a payer regex, and the line
patterns tolerate the dot leaders the IRS prints. The D-400 template is verified on the
lines the planner relies on (6, 12b, 15, 20a); the payment lines follow the printed form
and are optional, so a layout difference there never unmatches the return.

Until the 1099s arrive, `planner ingest` folds the imported rows into year-to-date
facts under form `YTD` (issuer = the CSV source): short- and long-term proceeds, basis
and gain from realized rows, dividends, interest and capital-gain distributions from
income rows (or from Dividend/Interest transactions when no income export covers the
year), deposits and withdrawals from bank rows. Every ingest recomputes them and
supersedes the last run; `planner derive --year 2025` reruns one year by hand.
`planner facts --year 2025 --form YTD` shows them beside the forms, box for box
(the realized boxes use the 1099-B names), so the estimate and the statement can be
compared the day the form lands.

## The Needed panel (Phase 2d)

Intake is a loop: drop everything you have, run `planner needed --year 2026`, and the
planner lists only what is still missing, why the plan wants it, which document
supplies it (with where to download it), and the `planner enter` line that types it
instead. Every fact a planner or tax line relies on is declared once in
`planner/ingest/needs.py` with the form boxes that supply it; the report diffs that
registry against the ledger, the profile and the typed answers. A figure a form has not
supplied yet but the year-to-date rows cover (dividends, interest, realized gains) shows
as an *estimate* rather than a need; the day the 1099 lands it becomes *actual*.

`planner enter <item> <value> --year 2026` stores one typed answer, validated by type
(dates ISO, money with `$` and commas, fractions as `0.035` or `3.5%`, filing status
from the fixed list); a bad answer is refused, never guessed. Profile items (birth date,
filing status, state, county, spending band, cash target, return and conversion
assumptions, SS estimates) go to `data/profile/assumptions.yaml`, which is created from
`config/assumptions.example.yaml` on first use; year items go to
`data/manual/<year>.yaml`. `planner dont-have <item> --year 2026` takes an item off the
list and the plan shows it as unavailable. The loop is done when `planner needed` prints
`nothing needed`; `--all` shows the covered items with their source.

## Scanned pages and photos (Phase 2e)

A PDF with no text layer and any `.png`/`.jpg` in the inbox is read by OCR
(`rapidocr-onnxruntime`, offline, models inside the wheel; nothing is uploaded) and
matched by the same templates as a text PDF. OCR is never trusted on its own: the
values land in the ledger as *pending* and count for nothing until a person confirms
them. `planner import` reports such a file as `pending`, and `planner confirm` lists
every pending document with its boxes, page and value. `planner confirm --doc 3
--accept` takes a document's values in, after any corrections typed as `--set
1a=1234.56`; accepting supersedes an earlier accepted copy of the same form, as a fresh
drop would. `planner confirm --doc 3 --reject` drops the values, forgets the document
(so a clearer scan or the same file can be dropped again) and returns the file to
`data/inbox/UNMATCHED/` with a reason file. Without the OCR engine installed, scans and
photos go to `UNMATCHED` with that reason and the values can still be typed with
`planner enter`.

## The ledger as a portfolio (Phase 3)

Every account number the exports name is an account; the planner asks for each
one's kind exactly once through the Needed panel (`planner enter
account:12345678 taxable`, one of `taxable`, `trad_ira`, `inherited_ira`, `roth`,
`hsa`, `cash`) and, for an inherited IRA, the date of death that starts the
10-year clock. `planner account <number> --name ... --type ... --balance ...`
describes an account in `data/profile/accounts.yaml` directly; a typed balance
is for an account no export covers (a bank export has no balance column).
Balances are the newest holdings snapshot per account and lots the newest
cost-basis export, so re-dropping a download replaces the picture instead of
adding to it.

`planner status --year 2026` prints each account, the total, the money that is
*accessible* without a penalty (taxable and cash balances, lifetime Roth
contributions from `roth_basis_contributions` or the sum of Form 5498 box 10,
and conversions past their clock) and the money that is *locked* (traditional
and inherited IRAs, the HSA, Roth earnings, unseasoned conversions), the
all-time peak (persisted in the ledger so a drawdown is measured from it), each
inherited IRA's empty-by date, YTD income by type from the derived rows,
unrealized gains by lot split short/long, and the capital loss carried in
(`prior_capital_loss_carryforward`, from last year's carryover worksheet).
`planner convert 2026-06-01 25000 --from 33333333` records a Roth conversion and
dates when its principal is penalty-free: January 1 of the fifth year after the
conversion or the IRA access age, whichever comes first. An inherited IRA is
refused as a source; so is any account not typed `trad_ira`.

## Planners: MAGI and the conversion (Phase 4a)

Every planner prices the household the Needed panel has confirmed, through the
engine, never by formula. `planner magi --year 2026` projects the full year
(add `--q4-dividends`, `--sales-st`, `--sales-lt`, `--conversion`, `--hsa` for
the planning numbers no document supplies) and shows the distance to every
watched line: the standard deduction, the 0% LTCG ceiling, the 12% bracket
top, the Medicaid line (tested monthly), the ACA 250% and 400% lines and NIIT.
Unknown inputs are named and left out, never treated as zero; unknown
qualified dividends are priced as ordinary and flagged.

`planner conversions --year 2026` sizes this year's Roth conversion from a
traditional IRA in one engine sweep: a candidate per line (fill to the 0% LTCG
line, to the 12% top, under the ACA cliff, under or just over the Medicaid
line in the month it lands, and the hard cap), each with the federal and NC
tax it adds, the ACA credit it costs, a warning when qualified dividends spill
into 15%, and the cash needed from outside the IRA. The profile's
`conversion_margin` is kept below each line, `conversion_cap` is the hard cap
and `conversion_objective` picks the recommendation; the rest stay on the page.
Record the one you make with `planner convert`.

## Planners: spending and the glide path (Phase 4b)

`planner spend --year 2026` is the spending band: the profile's withdrawal
rate times the investable balance (the ledger's latest snapshot, or
`--balance` for the January 1 figure), clamped to `spending_floor` and
`spending_ceiling`. The drawdown rule holds spending at the floor while the
balance sits under 90% of the all-time peak, inflation-adjusted, which a
rollover never resets. The table runs the next ten years in real dollars under
`return_floor` and `return_track`.

`planner glide --year 2026` is the age/year table to 95 under the planning
return, real and nominal, with Social Security from `ss_claim_age` (the SSA
statement figure for that age, else the nearest lower one), the
accessible-bucket check (taxable plus cash plus Roth basis against the floor
through `ira_access_age`), three stress rows (a 30% drop in year one, 5%
inflation, floor returns), and the month-by-month cash line for this year and
next: SE deposits and dividends from the ledger's rows for the months already
run and their run-rate after, living cost at the band, `mortgage_monthly`,
`premium_monthly`, estimated payments as a quarter of the projected year's tax
on the four due dates, the profile's `irregular` items (`label`, `month`,
`amount`, optional `year`) and any `--cash-in 2026-11:25000`. The cash bucket
is carried month by month and the first month under `cash_target` is named.

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

## Planners: estimated tax (Phase 4d)

`planner esttax --year 2026 [--as-of …] [--conversion 40000 …]` shows, for the
IRS and NC separately: the projected tax, the safe harbor (the lesser of 90% of
this year's tax and 100% of last year's — 110% federal when last year's AGI
topped 150,000; 90% of this year's when the prior return is not in), the four
installments (April 15, June 15, September 15, January 15) with what was paid
by each due date, and the next payment. Payments come from bank rows whose
description names the IRS (`USATAXPYMT`, `EFTPS`) or NCDOR, plus anything
typed with `planner paid --year 2026 --agency fed --on 2026-04-10 --amount 1200`
(kept in the year's manual file). A payment counts toward the installment
whose window it falls in, so a late payment never cures an earlier shortfall.
Tax after withholding under 1,000 is de minimis (no payments required);
withholding comes from `fed_withheld` / `nc_withheld` on the Needed panel (a W-2
template now reads boxes 1, 2, 16 and 17).
Income with over half in one quarter, or a planned year-end lump, raises the
annualized-method flag (Schedule AI is not computed). The Form 2210 penalty is
reported as unavailable.

## The plan on one page (Phase 4e)

`planner plan --year 2026` is the year-end plan: the Needed panel (what is
still missing and what is standing in from YTD), projected MAGI against every
watched line, the Roth conversion candidates and the recommended one, the
spending band, the glide path with its stresses and the first month the cash
line goes negative, the cash to raise and the lots to sell, estimated tax by
agency with the next payment, wash-sale flags and open windows, and the
deadline calendar from October through next September. The same `--as-of`
and override options as the planners it composes. A planner whose required
input is still unknown reports what it needs and the rest of the page still
renders; nothing is estimated in its place. The page is also written to
`out/plan-<year>.md` (personal, gitignored; `--no-write` skips it).

Every date on the calendar moves off a weekend or a federal holiday by the
rule in `planner/plan/calendar.py` (a due date to the next business day, a
year-end cut-off to the one before, an opening not at all); the holidays are
computed, including DC Emancipation Day, which the IRS counts. Estimated-tax
installments use the same shift.

## Levers and what-if (Phase 4f)

`planner levers --year 2026` lists every move left this year that changes the
tax bill or the ACA credit, each sized from the ledger, priced through the
engine and shown beside its deadline and friction (automatic, a trade, a
trade inside a wash-sale window, needs outside cash, irreversible). Net is
federal tax (income + SE) plus NC tax minus the ACA credit, saved or spent
against doing nothing; friction is never folded into the number.

- **Get under a line**: a loss harvest, deferring a planned sale (`--st`,
  `--lt`), an HSA contribution, the SE health insurance deduction, a
  deductible traditional IRA contribution and last year's capital-loss
  carryforward. Each is worth what it adds inside the combined set (all of
  them, against all but this one), so overlapping moves are not double
  counted; a "together" row says whether the set reaches under the nearest
  line you are over. Medicaid tests income month by month when you apply, so
  it is never the year-end target.
- **Use the room**: a Roth conversion (the sizer's pick when an objective is
  set, otherwise filled to the next line), a 0% gain harvest, an inherited-IRA
  withdrawal, and a Roth contribution (no tax effect). They share the room
  before the next line, so each is priced alone and ranked by what each
  dollar costs now.

A lever missing an input names it: `hsa_coverage` (none, self or family) and
`workplace_plan` (W-2 box 13) are Needed-panel questions; limits, catch-ups
and IRA/Roth phase-outs come from `config/thresholds.yaml`, each with its
source. The plan page shows the top three of each menu.

`planner whatif --year 2026 --apply traditional_ira,hsa --set hsa=1000`
recomputes the full year with the chosen moves and prints it before and after,
with every watched line. `planner thresholds --year 2026` prints the sourced
limits and checks the ones the engine also carries; a mismatch (the engine's
2026 IRA limit is still 7,000 against Notice 2025-67's 7,500) means the engine
prices with its own value until policyengine-us updates. Engine runs are
memoized per household, so the page prices each household once.

## Tax prep: the forms the year should produce (Phase 4g)

`planner forms --year 2026` predicts every form the return needs and checks it
off as it arrives. A form is expected when last year's issuer sent one, when an
account shows the activity behind it (dividends, interest or sales in a taxable
account: the institution's consolidated 1099; a withdrawal or Roth conversion
from an IRA: a 1099-R; HSA money in or out: 5498-SA or 1099-SA), or when a
Needed-panel answer implies it (wages: W-2; SE income: a 1099-NEC from each
client who sends one; interest or dividends: 1099-INT, 1099-DIV; marketplace
premiums: 1095-A; a mortgage: 1098, needed
only when itemizing; a Social Security claim age reached: SSA-1099). A form
nobody predicted is listed as it arrives. Each line is received (with the file
it came from), superseded or expected, with the date the issuer owes it to you
moved to a business day: January 31, February 15 for a broker's consolidated
statement, May 31 for the 5498 forms, which come after filing and are never
needed to file.

A form past its due date that the return needs joins `planner needed` with
where to download it, so the intake loop is not done until it is in the inbox
(`--as-of` checks any date). The plan page lists the inventory under "forms".

## Schedule C from categorised bank rows (Phase 4h)

`planner categorize --year 2026` lists the year's bank rows that have no
category. A row gets one only from you: `--rule "CLIENT PAYMENT" --as receipts`
catches every row whose description contains that text (case ignored; the
first rule wins), and `--row bank:T-3 --as supplies` sets one row, which beats
any rule. Categories are the Schedule C lines (receipts, returns, advertising,
car, commissions, contract_labor, insurance, interest, legal_professional,
office, rent_equipment, rent_property, repairs, supplies, taxes_licenses,
travel, meals, utilities, wages, other) plus `personal` and `transfer`, which
are left out. Rules and row choices are kept in `data/profile/categories.yaml`
and apply to every later export.

Line 1 is the categorised receipts, or the 1099-NEC and 1099-K total when the
forms add up to more, with a note saying which. Meals count at half. Net
profit (line 31) becomes the plan's self-employment income; until a row is
categorised the 1099 forms stand in as an estimate. An uncategorised row is
listed and left out, never guessed into a line, and while any remain
`planner needed` asks for them. Depreciation and the home office are not built
from bank rows.

## The draft return (Phase 4i)

`planner draft --year 2025` lays the year onto Form 1040, Schedules 1, 2, 3 and
SE and Form 8962 (line numbers follow the 2025 forms), and every line names
where its figure came from: the form and box, a YTD estimate, a typed answer,
an engine variable, or the arithmetic of other lines. `--json` gives the same
lines for other tools. The engine prices the tax; the draft adds what it does
not see: withholding from W-2 box 2 and box 4 of the 1099s (box 6 of the
SSA-1099), the federal estimated payments recorded for the year, and tax-exempt
interest from 1099-INT box 8 and 1099-DIV box 12.

Form 8962 is reconciled month by month from the 1095-A (premium, benchmark and
advance columns summed across policies): each month's credit is the smaller of
the premium and the benchmark less the monthly contribution, and a shortfall
against the advance is repaid up to the 2025 cap (Rev. Proc. 2024-35; no cap
from 2026 under P.L. 119-21). Without a 1095-A no credit is claimed and the
draft says what the engine would allow. Four totals (gross income, AGI,
taxable income, income tax) are checked against the engine and any gap is
printed as `CHECK:`. Unknown inputs, YTD estimates and forms still to come are
listed under the lines. Social Security benefits now come from SSA-1099 box 5
(`planner needed` asks for them), and the 1040 template reads the 2025 form's
7a, 11a, 12e and 13a lines.

## Capital gains: Form 8949 and Schedule D (Phase 4j-1)

`planner gains --year 2025` builds Form 8949 and Schedule D, and `planner draft`
carries both.

- **Lots.** Each closed lot from the realized-lots CSV in a taxable account is
  one Form 8949 row: box A when short-term, box D when long-term. A sale inside
  an IRA, Roth or HSA is not reported. When the CSV has no Term column, the term
  comes from the two dates.
- **Wash sales.** A loss lot whose symbol was bought again within 30 days either
  side, in any account, has part of its loss disallowed (code W). The part is in
  proportion to the replacement shares, and each replacement share covers one
  loss share, oldest first. A replacement bought in an IRA makes the loss lost
  for good (Rev. Rul. 2008-5). The broker reports wash sales only inside its own
  account; when its 1099-B figure differs, the draft says so.
- **No lots on file.** The 1099-B summaries go straight onto lines 1a and 8a
  (basis reported, no adjustments).
- **Other lines.** Capital gain distributions (1099-DIV box 2a) go on line 13.
  The loss carried in from last year is typed as `st_loss_carryover` and
  `lt_loss_carryover` and goes on lines 6 and 14.
- **When the Needed panel uses it.** After the year ends, Schedule D lines 7 and
  15 are the short- and long-term gains in the Needed panel. Until then the
  year-to-date estimate stands in, and it now includes capital gain
  distributions.
- **A loss beyond the yearly limit.** The draft adds line 21 and the Capital Loss
  Carryover Worksheet figures for next year.
- **Checks.** A gap between lot proceeds and the 1099-B proceeds is named. A gap
  between 1040 line 7a and Schedule D is printed as `CHECK:`.
- **Assumption.** Boxes A and D assume the broker reported the basis to the IRS;
  a noncovered lot belongs in box B or E.

## Health savings accounts: Form 8889 (Phase 4j-2)

`planner hsa --year 2025` builds Form 8889, and `planner draft` carries it onto
Schedule 1 (line 13, and line 8f for taxable distributions) and Schedule 2
(line 17c).

- **The limit.** The limit for your coverage (`hsa_coverage`: self or family),
  pro-rated by the months covered on the 1st (`hsa_months`, 12 when not typed).
  At 55 or older it adds the $1,000 catch-up. The limits come from the yearly
  Rev. Proc. and are checked against `config/thresholds.yaml`.
- **Contributions.** The total is 5498-SA box 2 plus box 3 (`hsa_contributions`).
  Employer and payroll money (W-2 box 12 code W, `hsa_employer_contributions`)
  counts against the limit first and is never deducted again. The deduction
  (line 13) is the smaller of what you put in yourself and the room left.
- **Excess.** Money in over the limit is named, with the fix: take it out with its
  earnings before the filing deadline, or pay a 6% excise tax each year it stays.
- **Distributions.** 1099-SA box 1, less the qualified medical expenses paid from
  the HSA (`hsa_qualified_expenses`), is income. Before 65 it also carries a 20%
  additional tax. Until the expenses are typed, the distributions are taken as
  spent on medical care, and the draft says so.
- **When the Needed panel uses it.** After the year ends, line 13 is the panel's
  HSA deduction. Until then, the typed HSA contribution stands.
- **The HSA lever.** The year-end HSA lever now takes payroll money off the room
  it offers.
- **Not handled, and named.** Archer MSA contributions (line 4), a funding
  distribution from an IRA (line 10), rollovers (line 14b), and a family limit
  split with a spouse's own HSA (line 6).

## The NC return: D-400 and Schedule S (Phase 4k)

For a North Carolina resident (`state: NC`), `planner draft` adds Form D-400
and its Schedule S. It uses the same engine run as the federal return.

- **Lines.** Line numbers, the rate (4.25% for 2025, 3.99% for 2026), the
  standard deduction and the child deduction table follow the 2025 D-400
  instructions (D-401). A test checks the rate against the engine for each year
  on file.
- **Schedule S.** The planner fills these lines itself:
  - Line 18: interest on US obligations (1099-INT box 3).
  - Line 19: the Social Security taxed federally.
  - Line 16: additions you type (`nc_additions`).
  - Lines 20-40: other deductions you type (`nc_other_deductions`), such as a
    Bailey or uniformed services pension.
- **Tax and payments.**
  - Line 18 (use tax): `nc_use_tax` when typed; until then, the use tax table's
    estimate for your income, and the draft says so.
  - Line 20a: NC withholding from W-2 box 17 and 1099-R box 14.
  - Line 21a: the NC estimated payments from the bank export or typed.
  - The draft ends on line 26a/27 (owed) or line 28/34 (refund).
- **Check.** Line 14 is compared with the engine's NC taxable income, with the
  typed Schedule S items put back. A gap is printed as `CHECK:`.
- **Federal fix.** 1099-INT box 3 (US Treasury and savings bond interest) now
  counts as taxable interest on 1040 line 2b. Before, it was missed.
- **Not drafted, and named.** Part-year and nonresident returns (line 13,
  Schedule PN), D-400TC credits, penalties and interest (lines 26b-26e,
  Form D-422), and the amended-return lines.

## The tax pack: one folder for the preparer (Phase 4l)

`planner taxpack --year 2025` writes `out/tax-2025/`. Every file shows
something another command already prints:

| File | What it holds |
|---|---|
| `draft.txt` | the draft return (`planner draft`), every line with its source |
| `draft.html` | the same draft laid out to print; Print, then Save as PDF |
| `form-8949.csv` | Form 8949 rows by box, columns (a)-(h) and the account (`planner gains`) |
| `schedule-c.txt` | the Schedule C summary and any uncategorised rows (`planner categorize`) |
| `carryforward.csv` | the capital loss carried to next year |
| `basis.csv` | cost basis of each open lot, and each Roth conversion's basis and penalty-free date |
| `estimated-payments.csv` | federal and NC estimated payments, the installment, and where each came from |
| `forms.csv` | the expected forms, which arrived and their source files (`planner forms`) |
| `originals.zip` | the archived originals in `data/archive/<year>/` |

How a run behaves:

- A run replaces the files from the last run.
- A run blocked on a missing answer (for example, birth date) writes nothing
  and exits 2.
- The summary at the end names any form still to come and any `CHECK:` line
  to resolve first.

## Closing the year from the filed return (Phase 4m)

Once the return is filed, drop the filed 1040 (with its schedules) and the NC
D-400 into the inbox, run `planner ingest`, then `planner close --year 2025`.

- **The delta.** Each filed line read by the form templates is set beside
  the draft's line, and every line more than $1 apart is listed with the gap.
  - The templates follow the 2024 form numbers and the draft follows 2025's;
    the planner pairs the lines by what they hold. For example, filed 1040
    line 11 is draft line 11a.
  - Filed lines the draft has no line for are listed separately: line 5b,
    line 34 on a refund, and Schedule C, which `planner categorize` covers.
- **The record.** The filed figures are written to
  `data/private/returns/2025-closed.yaml`, and next year's rollover reads
  them. It is your own data: it stays in the private folder, is never in the
  repository, and is never in the tax pack.
- **Amended returns.** Drop the amended return the same way. Its figures
  replace the original's, and closing again writes version 2. The earlier
  version stays in the file.
- **Blocked or partial runs.**
  - With no filed 1040 (line 24) on file, nothing is written and the command
    exits 2.
  - A D-400 the draft expects but that is not on file is named.
  - Values read by OCR still waiting for `planner confirm` are named and left
    out of the record.
