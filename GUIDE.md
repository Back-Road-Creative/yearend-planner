# Year-End Tax & Retirement Planner: the guide

How each part works, phase by phase. The one-page start is the [README](README.md).

**Not tax, legal or investment advice. Every figure is an estimate from the documents and answers you give it; have a tax preparer review the return before you file.** The same notice heads the page, the draft return and the tax pack.

## Engine (Phase 1)

`planner compute <household.yaml>` prints every figure for one household-year as JSON:
federal income tax after credits (line 22, which holds any excess advance premium credit
repaid when the household file gives `aptc`),
SE tax, total tax (1040 line 24: line 22 plus Schedule 2 line 21, so it holds SE tax, the
additional Medicare tax and NIIT; refundable credits are payments, not a cut in it), tax attributable to
qualified dividends and long-term gains, state tax (named by the household's state), AGI, ACA MAGI, taxable income, QBI
deduction, premium tax credit, FPL percentages, Medicaid eligibility, monthly Medicaid MAGI, and the headroom
left under the 0% capital-gains ceiling and the top of the 12% bracket. All figures come
from policyengine-us; the arithmetic written here is threshold minus taxable income and
the self-employed health insurance settlement below.
Two poverty guidelines are in play for one tax year, and both are printed: `fpg` is the
guideline of the tax year itself, which Medicaid's 138% line uses, and `aca_fpg` is the
guideline of the year before (Form 8962 line 4), which the premium tax credit's 400% cliff
and the 250% cost-sharing line use. `aca_fpl_pct` is measured against `aca_fpg`, so a 2026
single filer reaches 400% at $62,600 (4 x $15,650), not at 4 x the 2026 guideline.

**Self-employed health insurance and the premium tax credit (IRS Pub. 974).** Give
`se_health_premiums` as the year's premiums before any credit (1095-A column A) and
`aptc` as the advance credit paid (column C). The deduction is the premiums less the
credit allowed, no more than the profit less half the SE tax and any SEP/SIMPLE
contribution (Worksheet W); the credit depends on the income the deduction leaves
(Worksheet X). `compute` runs the Iterative Calculation Method on the engine: deduct
the premiums, price the credit, take it off the premiums, and repeat until neither
moves by $1 (about six runs, so a household with premiums takes a few seconds longer).
The result carries `se_health_deduction`, `aca_ptc` (the credit allowed, never more
than the premiums), `net_ptc` (Form 8962 line 26), `excess_aptc` and `aptc_repayment`
(lines 27 and 29, after the 2025 line 28 cap; none from 2026), and `se_health_converged`.
At the 400% cliff (2026 on) the credit ends as the deduction falls, so there is no
fixed point: the result is the two-pass figure with `se_health_converged` false, and
the draft says to check it with a preparer. Where the applicable percentage rises with
income, the whole-percent step of Form 8962 line 5 can leave two points that both get a
credit (about $13-15 apart); that is not the cliff, so the lower deduction is reported as
settled, with the deduction plus the credit within that step of the premiums. Assumed: one plan for the full year, all
premiums specified, one business; not modelled: partial-year coverage with a different
benchmark each month, nonspecified premiums (Worksheet P), and the passive-loss, IRA,
EE-bond and student-loan interactions of Pub. 974's special instructions.

`planner sweep <household.yaml> --variable taxable_roth_conversions --lo 0 --hi 100000 --step 5000`
runs the whole range in one engine call.

`planner verify data/private/returns/<year>.yaml` recomputes a filed year from the inputs
recorded in that file and prints each line as filed next to the engine's figure. The file
format is one case of `planner/engine/reference.yaml` (synthetic, `year`, `household`
and `filed`); `planner verify` with no file checks those shipped reference cases. Your own
returns live under `data/private/` and are never committed.

`planner update <release.zip> --sha256 <digest>` verifies the zip, extracts it to
`python-candidate/`, runs the candidate's own selfcheck and its regression against the
engine baseline, then swaps it in and keeps the old
install as `python-previous/`. `planner update --rollback` restores it. `data/` and `out/`
are never touched.

Reference cases (hand-worked, $1 tolerance) are in `tests/test_tax.py` and, shipped with
every release, in `planner/engine/reference.yaml`; the engine's
coverage of each rule the planner relies on is recorded in `config/capabilities.yaml`,
one record per capability: its historical `status`, its `evidence` (unreviewed,
implemented, independently validated, end-to-end accepted, partial, out of scope), the
`tests` that prove it, a `note` on what they leave out, a narrower `scope` where one
applies, and the independent `expected` source an evidence level above implemented must
name. `tests/test_config.py` fails a verified or partial record that lists no test, a
listed test that does not exist, and an unknown field or level. The release build
stamps each record's `proven` commit into the shipped copy once the suite has passed
there; the repository copy never carries one. The engine prices a household of more
than one person (unit 3a-1): `Household` takes a `spouse` (a joint return's second
person, with their own wages, self-employment income, IRA figures and Social Security)
and `dependents` (age, full-time student, disabled), and every person figure the planner
reads is the tax unit's sum; a sweep moves the first person's input only. A spouse on
any return but a joint one is refused. The poverty-line cases sit
on both sides of each line: 138% (Medicaid) and 400% (premium tax credit), with the credit
worked by hand from the Rev. Proc. 2025-25 table; they ship in `reference.yaml`, so
`planner update` holds a release whose engine moves any of them. One known engine
deviation, corrected by the planner: the engine rounds income over the poverty line down
to a whole percent and ends the premium tax credit at 400.00%. The statute keeps the
credit while income "does not exceed 400 percent" (IRC 36B(c)(1)(A)), and Form 8962's
instructions (Worksheet 2) enter 401 only when income is more than 4 times the poverty
line, in dollars. So the planner pays the credit at exactly 400.00% (3,365.04 for the
single $800-a-month benchmark case at $62,600, a filed line in `reference.yaml`) and
none a cent over it, in every figure, sweep and the draft Form 8962 (line 5 = 401). The
conversion sizer may size onto the line, never past it (less your margin).

## Intake (Phase 2a)

Drop files into `data/inbox/` (PDFs, or a ZIP of them) and run `planner ingest`. Each
file is fingerprinted, matched page by page against the templates in `templates/forms/`
(1099-INT, 1099-DIV, 1099-B totals, 1099-R, 1095-A), and its box values land in the
ledger (`data/ledger/planner.db`) with the source file and page. The originals move to
`data/archive/<year>/` only after the facts commit; a file the templates cannot read
moves to `data/inbox/UNMATCHED/` beside a `.reason.txt`. The same file dropped twice is
a no-op. A corrected form supersedes the earlier one by form, payer and year. No value
is ever inferred: a required box that does not parse unmatches the whole file.

A ZIP is unpacked in place, folders and ZIPs inside it included, and a folder of
statements dropped as it is gets the same reading. Nothing in the inbox is overwritten:
a file whose name is taken keeps both. A ZIP that cannot be read whole (corrupt,
password-protected, an entry that would write outside the inbox, or ZIPs nested more
than 5 deep) extracts nothing, moves to `data/inbox/UNMATCHED/` with a reason file, is
reported once, and does not hold back the other files. Mac resource-fork debris
(`__MACOSX`, dotted names) is ignored. A file in a folder is reported by its path, such
as `stmts/1099-div.pdf`, and the folders it leaves empty are removed.

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

Some boxes hold words, not dollars, and the ledger keeps them as text (`facts.value_text`,
schema version 4; an older ledger gains the column the first time it opens). A template box
has a `kind`: `amount` (the default), `text` (optionally limited to an `allowed` list) or
`check` (a set of check-mark options, one of which must be marked; two marked is a refusal,
never a guess). From the filed 1040 the planner reads the filing status (Single, Married
filing jointly, Married filing separately, Head of household), the state and the ZIP from the
address block; the Needed panel then shows filing status and state as *actual* from the
latest return filed for the plan year or earlier, and a typed answer still wins. The ZIP
also answers the county, which sets the ACA benchmark premium: `config/zip_county.csv.gz` is
the Census Bureau's 2020 ZIP-to-county file (source, URL and download date in
`config/zip_county.SOURCE.md`; `python scripts/refresh_zip_county.py` rebuilds it). A ZIP
that lies in one county of your state fills the county as *actual* and says where it came
from. A ZIP that spans counties (the Needed panel and `planner needed` list them, largest
first), is not in the file, or is in another state leaves the county missing with a note
saying why; type it (`planner enter county Macon --year 2026`) and the planner turns the name into the
engine's own (`MACON_COUNTY_NC`), refusing a name that fits two counties or none in your
state. Qualifying
surviving spouse is not read: type it. How a tax-software PDF renders its check boxes is not
confirmed on a real return (tests use synthetic marks), so a return with no readable mark
leaves filing status to be typed. Form 1099-R box 7 is stored as the distribution code
(`7`, `G`, `7D`...). Two consecutive years of Form 1098 from one lender give the mortgage's
monthly principal and interest: (box 1 interest + the drop in box 2 principal) / 12. The
Needed panel shows it as an *estimate* (escrow is not on a 1098; type the full payment to
replace it). `planner confirm --set 7=G` corrects a text box read from a scan.

Until the 1099s arrive, `planner ingest` folds the imported rows into year-to-date
facts under form `YTD` (issuer = the CSV source): short- and long-term proceeds, basis
and gain from realized rows, dividends, interest and capital-gain distributions from
income rows (or from an account's Dividend/Interest transactions when no income export
covers that account for the year; an export never hides another account's), deposits and withdrawals from bank rows. Every ingest recomputes them and
supersedes the last run; `planner derive --year 2025` reruns one year by hand.
`planner facts --year 2025 --form YTD` shows them beside the forms, box for box
(the realized boxes use the 1099-B names), so the estimate and the statement can be
compared the day the form lands. Once a year's 1099 is in, it is the figure the
plan and the return use; the YTD figure is never added to it. `planner status`
and a dashboard alert show each gap (1099-B proceeds and basis, 1099-DIV 1a and
2a, 1099-INT 1 against the YTD box), so a missed export or a late correction is
visible instead of silently replaced.

## The Needed panel (Phase 2d)

Intake is a loop: drop everything you have, run `planner needed --year 2026`, and the
planner lists only what is still missing, why the plan wants it, which document
supplies it (with where to download it), and the `planner enter` line that types it
instead. Items are grouped by document, so one download closes several: `planner needed`
prints a `document` line (for example *Vanguard tax forms*) with its exact download path
(`Vanguard > My Accounts > Tax center > ...`, or `Vanguard > Cost basis > Realized
gains/losses > Export CSV` for realized gains), the plan outputs the whole group unlocks
(MAGI headroom, Draft 1040, ...), and then each item in it with where to look in the
document. Items no document supplies (birth date, spending band, return assumptions) come
last under *Typed answers*. The click paths describe each issuer's site as the planner
knows it; if a site has moved, the document's name is still right. Every fact a planner or tax line relies on is declared once in
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
`data/manual/<year>.yaml`. A YAML file edited by hand that gives one key twice is
refused with the repeated key and its line named, never read with the last one winning. `planner dont-have <item> --year 2026` takes an item off the
list and the plan shows it as unavailable; `--undo` puts it back. The loop is done when `planner needed` prints
`nothing needed`; `--all` shows the covered items with their source. A list emptied by
setting items aside (`dont-have`, or a waived late form) prints `nothing left to answer,
N set aside: not ready` instead: the figures that rest on those items are estimates.
An empty list is not readiness; the page gives three answers (see *Readiness* under
*Coverage gate*).

Every kind of item in the Needed panel can be closed on the live page. Every
don't-have and every waiver can be undone there; a row you categorised is changed
with `planner categorize`. A typed item takes an answer or **I don't have
this**. A form past its due date (see *Expected forms*) takes **It will not come: take
it off the list**, which is `planner waive --year 2026 --form 1099-INT --issuer "First
Example Bank"` on the command line: the form must be one `planner forms` lists and has
not received, the waiver holds for that year only and is kept in
`data/profile/forms_waived.yaml`, and the form stays in `planner forms` marked
`waived`. The uncategorised bank rows take a piece of description text and a category
for every row it matches, or a category for one row at a time (the first 40 rows are
listed; the text box catches the rest), with `personal` and `transfer` for what is not
business; this is `planner categorize` with the Schedule C rebuilt after each choice.
Items marked don't-have and forms waived are listed under *Set aside* below the panel,
each with a button that puts it back. An account's kind and an inherited IRA's date of
death take **I don't have this** like any other item.

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

On the live page (`planner run`) each pending value sits beside a picture of the line
of the scan it was read from, so you check the number against the source before it
counts. The value is in a box you can retype; **Accept** takes in what the boxes
hold, and the line at the top of the page says how many you corrected. **Reject**
drops the document as above. The pictures are cut from the scan when it is read and
saved under `data/crops/<document>/` (on this computer only; the page serves them
only with the run's key, and only while the value is pending). Rejecting a document
deletes its pictures. A value whose line the engine could not place has no picture
and says so, and `planner ingest` prints a `note`. The printed copy
(`planner dashboard`) shows the values without pictures; use `planner confirm` there.
A box that two forms in one scan share (for example box 4 of a 1099-INT and a
1099-DIV on one page) cannot be corrected on the page or with `--set`, because the
correction would not say which form it means.

A PDF that has text on some pages and none on others (a consolidated 1099 with a
scanned page) is read both ways: the text pages from the text layer, as accepted, and
only the bare pages by OCR, as pending. The file shows as `pending` until the scanned
values are confirmed; accepting them leaves the text pages' values as they were. If the
two disagree on a box, the file goes to `UNMATCHED`. Without the OCR engine the text
pages are still imported and `planner ingest` prints a `note` naming the pages that
were not read; if OCR reads nothing from a bare page, the same note says so (or, if
no text page matched a form either, the `UNMATCHED` reason does). Rejecting such a file at
`planner confirm` drops only the scanned pages' values and keeps the document, its
text-page values (already in the ledger, and possibly superseding an earlier copy) and its
archived copy, so nothing already counted is lost; type the scanned values with
`planner enter`.

## The ledger as a portfolio (Phase 3)

Every account number the exports name is an account; the planner asks for each
one's kind exactly once through the Needed panel (`planner enter
account:12345678 taxable`, one of `taxable`, `trad_ira`, `inherited_ira`, `roth`,
`hsa`, `cash`) and, for an inherited IRA, the date of death that starts the
10-year clock. An inherited IRA whose owner had already begun RMDs also owes a
yearly RMD inside the 10 years: `planner account <number> --annual-rmd` (or
`--no-annual-rmd`) records it, and `planner status` notes an inherited IRA
whose answer is missing. `planner account <number> --name ... --type ... --balance ...`
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
inherited IRA's empty-by date and yearly-RMD answer, the Roth in withdrawal
order (contributions, then each conversion oldest first with its penalty-free
date, then earnings: Pub. 590-B; the balance caps it), YTD income by type from
the derived rows and its gap to the filed 1099s,
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
top, the Medicaid line (tested monthly), the ACA 250% and 400% lines, NIIT
and next year's 110% safe harbor (AGI over $150,000). The household adds its
own: IRMAA's first tier from age 63 (Medicare premiums two years on), the
Social Security 50% and 85% taxability lines on provisional income when
benefits are typed, NC's scheduled rate step (3.99% to 3.49% in 2027; the
conversion lever names what waiting would save on the NC side), and from
2027, with the Medicaid objective, the 80-hour monthly work requirement
against the thinnest month in the `se_hours` log (`1:85, 2:60`).
Every value is in one of four states: known (a typed or documented zero
included), an estimate (a year-to-date figure standing in), unknown, or not
applicable (an item another answer makes moot, so it is never asked). Unknown
inputs are named and left out, never treated as zero; unknown
qualified dividends are priced as ordinary and flagged. Tax-exempt interest
(1099-INT box 8, 1099-DIV box 12, or a typed answer) is one of those inputs:
it is not income, but ACA MAGI adds it back, so the room to the 250% and 400%
lines, the Medicaid line, the conversion sizes and the levers all shrink by it.

`planner conversions --year 2026` sizes this year's Roth conversion from a
traditional IRA in one engine sweep: a candidate per line (fill to the 0% LTCG
line, to the 12% top, under the ACA cliff, under or just over the Medicaid
line in the month it lands, and the hard cap), each with the federal and state
tax it adds, the ACA credit it costs, a warning when qualified dividends spill
into 15%, and the cash needed from outside the IRA. The profile's
`conversion_margin` is kept below each line, `conversion_cap` is the hard cap
and `conversion_objective` picks the recommendation; the rest stay on the page.
The tax a candidate adds must come out of cash on hand above `cash_target`: a
candidate that would dip into the reserve is cut to the largest amount whose tax
fits, with a note naming both numbers, or dropped when nothing fits (Phase 10,
2d). With no account typed `cash` the check is skipped and the page says so.
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
next: SE deposits (bank deposits categorised `receipts`), wages paid in
(`pay`), other deposits and dividends from the ledger's rows for the months
already run, and the run-rate of receipts and pay after. A deposit categorised
`transfer`, `refund` or `loan` is not counted as income; one with no category
counts once in the `other` column, and a note names it so you can give it one
(`planner categorize`). Living cost at the band, `mortgage_monthly`,
`premium_monthly`, estimated payments from `planner esttax` (payments already
made, in the month they were paid; each later installment at what its safe-harbor
figure still lacks, so a missed quarter is made up at the next due date; next
year's April, June and September installments at 90% of this year's tax less
withholding; and the tax the installments leave unpaid in the month the return
is due, as `tax due`), the planned sales and conversion (below), the profile's
`irregular` items (`label`, `month`,
`amount`, optional `year`) and any `--cash-in 2026-11:25000`. A planned sale
(`--sales-st`, `--sales-lt`) brings its proceeds into the `sales` column in
December (the plan's year-end cut-off), priced from the taxable lots with the
most gain per dollar, which is the least cash for that gain; a gain no lot can
supply counts nothing and is named in the notes. A planned conversion moves no
cash itself; its tax is in the estimated payments and the `tax due` month, and
a note gives the tax it adds. None of this needs `--cash-in`. The cash bucket
is carried month by month and the first month under `cash_target` is named.
It starts from the cash accounts' balance on their `balance_date`: that
month's flows after the date run forward from it, later months add their net,
and earlier months are worked back from it, so a deposit the balance already
holds is not counted twice. A balance with no date (or dated outside the year)
starts the line on January 1, and a note says so.

The glide path also runs the comfort-floor line: the same spending rule at
`return_floor` every year, printed beside the on-track line in the `comfort
floor` column. `planner glide` names which band this year's spending sits in
(inside the band, at the floor, at the ceiling, or held at the floor by the
drawdown rule).

## Planners: raising cash and wash sales (Phase 4c)

`planner withdraw --year 2026 [--target 30000] [--budget 5000] [--lot 11111111:VTSAX:2019-01-15]`
raises the cash target (the profile's `cash_target` unless `--target` says
otherwise): the cash accounts first, then the taxable lots with the least gain
per dollar raised — loss lots, then the highest-basis long-term lots — never a
retirement account. A `--lot` list is specific-ID: those lots go first, in the
order given. `--budget` caps the realized gain; what the budget cannot raise is
reported as short, not quietly sold. Each run prices the sales through the
engine and prints ACA MAGI and federal + state tax before and after. A loss lot
whose symbol was bought inside the last 30 days is flagged as a wash sale.

`planner washsales [--year 2026] [--as-of 2026-06-30]` lists every loss sale
with a buy of the same symbol within 30 days either side, across all accounts
including IRAs (a dividend reinvestment is a buy), and the symbols whose window
is still open as of the date.

## Planners: estimated tax (Phase 4d)

`planner esttax --year 2026 [--as-of …] [--conversion 40000 …]` shows, for the
IRS and the state you live in separately (no state line in the nine states
without an income tax: AK, FL, NV, NH, SD, TN, TX, WA, WY): the projected tax, the safe harbor (the lesser of 90% of
this year's tax and 100% of last year's — 110% federal when last year's AGI
topped 150,000; 90% of this year's when the prior return is not in), the four
installments (April 15, June 15, September 15, January 15 federal; each state's
own, below) with what was paid
by each due date, and the next payment. Payments come from bank rows whose
description names the IRS (`USATAXPYMT`, `EFTPS`) or NCDOR, plus anything
typed with `planner paid --year 2026 --agency fed --on 2026-04-10 --amount 1200`
(`--agency` is `fed` or the state's code in lowercase, `nc`, `ca`; kept in the
year's manual file; another state's payments are typed, and its line says so).
The federal 110% step starts at 75,000 of last year's AGI when married filing
separately. Ten states' rules are their own, from each state's 2026
estimated-tax instructions and underpayment form:

| State | Due | Shares | No payments when the tax after withholding is | Safe harbor |
|---|---|---|---|---|
| CA | federal dates, none in September | 30/70/70/100% | under 500 (250 separately) | 90%, or 100% / 110% over 150,000 of last year's AGI; this year's AGI at 1,000,000 (500,000 separately): 90% only |
| NY | federal | 25% each | under 300 (NYC and Yonkers tax counts, not priced) | 90%, or 100% / 110% over 150,000 |
| PA | federal | 25% each | under 430 | 90%, or 100% of last year's |
| IL | federal | 25% each | not over 1,000 | 90%, or 100% |
| OH | federal | 25% each | not over 500 | 90%, or 100% |
| GA | federal | 25% each | not over 0 (the 500-ES test is income, not applied) | 70%, or 100% |
| NC | federal | 25% each | under 1,000 | 90%, or 100% (no 110% step) |
| MI | federal | 25% each | not over 500 | 90%, or 100% / 110% over 150,000 |
| NJ | federal | 25% each | not over 400 | 80%, or 100% |
| VA | May 1, then federal | 25% each | not over 1,000 | 90%, or 100% |

A state's AGI is taken as the federal AGI, and the line says so. Every other
taxing state's rules are not in the planner: the federal dates, shares, de
minimis and safe harbor stand in, and the state's line says `Estimated:`. A
payment counts toward the installment
whose window it falls in, so a late payment never cures an earlier shortfall.
Federal tax after withholding under 1,000 is de minimis (no payments required);
withholding comes from `fed_withheld` and `nc_withheld`, or `state_withheld`
for another state, on the Needed panel (W-2 box 17, 1099-R box 14); last
year's state tax is `prior_nc_tax` or `prior_state_tax`. Each is asked only
for the state it belongs to, and the state itself is typed as its two-letter
code.
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
deadline calendar from October through next September (the June and September
estimated payments, federal and your state's on one line when the dates
match, are marked "if required" and name the agencies whose
`planner esttax` result owes them, resting on this year's tax after withholding). The same `--as-of`
and override options as the planners it composes. A planner whose required
input is still unknown reports what it needs and the rest of the page still
renders; nothing is estimated in its place. A section priced from the
household (MAGI, conversion, levers, glide path, cash, estimated tax) that
rests on an unknown input says so ("rests on unknown (left out, not zero): …")
and the dashboard tags it an estimate even after the year is over; an unknown
mortgage or premium leaves the cash line running high, and unknown withholding
makes the estimated-tax amounts the most that could be due. A draft return
that rests on an unknown (an income item, or a sale with no cost basis) opens
with "NOT READY for a preparer", and so do its tax-pack page and notes. The page is also written to
`out/plan-<year>.md` (personal, gitignored; `--no-write` skips it).

Each income stream is carried to December 31 (Phase 10, unit 2c). A
statement's year-to-date figure counts only through its last row's date, and
the plan's notes say so when nothing covers the rest of the year. `planner
forecast <stream> --year <year>` types the rest: a pay schedule (`--cadence
biweekly --amount 2,400 --next 2026-10-09`; weekly, semimonthly, monthly,
quarterly, annual and once work the same way), a `--remaining` amount, or a
`--full-year` figure, which replaces the forecast instead of adding to it. A pay
stub with no statement behind it starts a stream with `--ytd 40,000 --through
2026-09-30`; a schedule with no amount so far leaves the stream unknown, never
zero. `--low` and `--high` bound an uncertain stream and the notes show the
full-year range. A stream whose annual form is in (a W-2, a 1099, a typed value)
keeps it and its forecast is not used. `planner forecast --year <year>` lists
every stream: owner, kind of income, so far, through, rest of year, full year
and range. One owner per line until the spouse is a full person; a spouse's
stream needs a joint return.

From a terminal `planner plan` first asks the few typed fields, and each has an
option so a script or Task Scheduler never waits (`--no-ask` skips the
questions; Enter skips one):

- **Total income** (`--total-income`): your own full-year figure when the YTD
  ledger lags. Wages (or the line `--total-income-line` names: se_income,
  interest, non_qualified_dividends, ira_distributions) become the total less
  the other income the ledger counts
  (business, interest, dividends, gains, IRA distributions and the conversions
  recorded so far; Social Security is left to the engine). Gains and losses
  count as one net figure, and a net loss only up to 3,000 (1,500 married
  filing separately), as on Form 1040 line 7. The Q4 dividends,
  planned sales and conversion below are added on top. A total below the other
  income is refused and says by how much, and so is a line that has its own
  forecast. The Q4 dividends are refused beside a dividend forecast: the same
  dividends would count twice.
- **Q4 dividends, planned short-term and long-term sales**
  (`--q4-dividends`, `--sales-st`, `--sales-lt`): added to the year. A loss may
  be typed in parentheses.
- **Conversion target** (`--conversion-target manual|auto`). `manual` (the
  default) uses `--conversion` as typed. `auto` adopts the recommended
  conversion (the one your `conversion_objective` picks) as the year's
  conversion, so MAGI, levers, cash and estimated tax all include it; the
  conversion section starts with an `adopted` line. With no objective or no
  candidate that meets it, nothing is adopted and a note says so. `auto` with a
  typed `--conversion` is refused: pick one.

Each conversion candidate, and the recommendation, carries `WARNING: qualified
dividends / long-term gains pushed into 15%` only when the conversion adds gains
tax the year would not owe without it. Gains already taxed at 15% before the
conversion do not trip it.

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
federal tax (Form 1040 line 24, which holds any repayment of an advance ACA
credit above the credit allowed) plus state tax, less refundable credits and the
ACA credit paid out above the advance, saved or spent against doing nothing; friction is never folded into the number.

- **Get under a line**: pairing losses against the year's realized gains
  (only enough loss to net the gain to 0; a lot can be sold in part, lots
  clear of a wash-sale window first), a loss harvest of the rest, spending
  cash or Roth basis (contributions and seasoned conversions) instead of a
  planned sale (no MAGI; the cash replaces sale proceeds, not gain, so the
  gain avoided is the gain in the lots the sale would have drawn first, up to
  the cash on hand; a planned gain no taxable lot supplies avoids nothing and
  is named), deferring a planned
  sale (`--st`, `--lt`; it moves the same gain as spending basis, so it is
  priced alone, not stacked), an HSA contribution, the SE health insurance deduction, a
  deductible traditional IRA contribution, last year's capital-loss
  carryforward, giving long-held shares instead of the cash gift
  (`planned_giving`: same deduction, the gain never taxed) and bunching next
  year's gift into a donor-advised fund this year (worth what the engine says
  only once Schedule A, with `real_estate_taxes` and `mortgage_interest`,
  beats the standard deduction; the lever says which wins). Each is worth what it adds inside the combined set (all of
  them, against all but this one), so overlapping moves are not double
  counted; a "together" row says whether the set reaches under the nearest
  line you are over. Medicaid tests income month by month when you apply, so
  it is never the year-end target.
- **Use the room**: a Roth conversion (the sizer's pick when an objective is
  set, otherwise filled to the next line), a 0% gain harvest, an inherited-IRA
  withdrawal, and a Roth contribution (no tax effect). They share the room
  before the next line, so each is priced alone and ranked by what each
  dollar costs now.
- **Income targeting** (your choice; never picked for you): with a
  traditional IRA, the conversion that keeps a month's income under the
  Medicaid line, the one that lands just over it (the marketplace with the
  largest credit) and the one just under the ACA 400% cliff sit side by side,
  each with its tax, the credit it costs and how many of the 12 months stay
  under the Medicaid line. Medicaid vs the marketplace is a coverage choice,
  so the optimizer never makes it.
- **Tax-efficient swaps** (next year's dividends, not this year's tax): a
  bond, REIT, income or Treasury fund in a taxable account whose gain is
  within 1% of its value is listed to sell for about no tax and hold in the
  IRA instead; one with a real gain is named with "hold it, or move new money
  into a broad index fund".

A lever missing an input names it: `hsa_coverage` (none, self or family) and
`workplace_plan` (W-2 box 13) are Needed-panel questions; limits, catch-ups
and IRA/Roth phase-outs come from `config/thresholds.yaml`, each with its
source. The plan page shows the top three of each menu.

`planner whatif --year 2026 --apply traditional_ira,hsa --set hsa=1000`
recomputes the full year with the chosen moves and prints it before and after,
with every watched line. It refuses what the menu would never stack: two moves
that move the same dollars (apply one), a key listed twice, a `--set` size at or
under 0, and a size past what the move can move (a lowering move's sized amount;
for a conversion the IRA balance, for a gain harvest the long-term gain held, for
an inherited-IRA withdrawal its balance). One feasibility check (Phase 10, 2d)
backs the menu, `whatif` and conversion sizing: moves that give more shares
held over a year than the taxable account holds, a gain harvest past the gain
left once the gifts are made, or traditional and Roth contributions past the one
IRA limit they share are refused by `whatif` and priced alone on the menu. A
set that takes more cash by its deadline (contributions paid in, tax added)
than is on hand above `cash_target` is shown, not refused: `whatif` prints a
`cash:` line and the menu a note. `planner thresholds --year 2026` prints the sourced
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
travel, meals, utilities, wages, other) plus `personal`, `transfer`, `pay`
(wages paid to you), `refund` and `loan`, which
are left out. Rules and row choices are kept in `data/profile/categories.yaml`
and apply to every later export.

Line 1 is the categorised receipts, or the 1099-NEC and 1099-K total when the
forms add up to more, with a note saying which. Meals count at half. Net
profit (line 31) becomes the plan's self-employment income; until a row is
categorised the 1099 forms stand in as an estimate. An uncategorised row is
listed and left out, never guessed into a line, and while any remain
`planner needed` asks for them. Depreciation and the home office are not built
from bank rows.

`planner draft` carries the result as its own Schedule C sheet: lines 1, 2, 3,
7, the expense lines you categorised, 28, 29 and 31, each naming how many bank
rows stand behind it. Schedule 1 line 3 and Schedule SE line 2 then read line 31
("Sch C line 31"); a typed self-employment figure that differs from line 31 by
more than a dollar is kept and flagged with a `CHECK:`.

## The draft return (Phase 4i)

`planner draft --year 2025` lays the year onto Form 1040, Schedules 1, 1-A, 2, 3,
C and SE and Form 8962 (line numbers follow the 2025 forms), and every line names
where its figure came from: the form and box, a YTD estimate, a typed answer,
an engine variable, or the arithmetic of other lines. `--json` gives the same
lines for other tools. The engine prices the tax; the draft adds what it does
not see: withholding from W-2 box 2 and box 4 of the 1099s (box 6 of the
SSA-1099), and the federal estimated payments recorded for the year. Tax-exempt
interest (1099-INT box 8, 1099-DIV box 12) comes from the Needed panel, the
same figure the plan prices.

Schedule 1 line 17 is the settled deduction above (its source line says so), and the
draft checks that it and Form 8962 line 24 add up to the premiums (`CHECK:` if not).
Form 8962 is reconciled month by month from the 1095-A (premium, benchmark and
advance columns summed across policies): each month's credit is the smaller of
the premium and the benchmark less the monthly contribution, and a shortfall
against the advance is repaid up to the 2025 cap (Rev. Proc. 2024-35; no cap
from 2026 under P.L. 119-21). Without a 1095-A no credit is claimed and the
draft says what the engine would allow. Four totals (gross income, AGI,
taxable income, income tax) are checked against the engine and any gap is
printed as `CHECK:`. Unknown inputs, YTD estimates and forms still to come are
listed under the lines. Schedule SE is drawn line by line (2, 3, 4a, 4c, 6, 7, 8a, 8d, 9, 10, 11, 12,
13): the wage base, the 12.4% and 2.9% rates and the $400 floor are the
engine's own parameters, W-2 wages (boxes 3 and 7 when a W-2 gives them, else
box 1 as a stand-in, said on the line) use up the wage base, and net earnings under
$400 owe nothing. On a joint return each spouse with self-employment income has
their own Schedule SE ("Sch SE (spouse)" for the spouse, from `spouse_se_income`),
with their own wage base used up by their own W-2s (unit 3a-5). Line 12 is checked
against the engine's tax for that person; the line 12s add to Schedule 2 line 4 and
the line 13s to Schedule 1 line 15 (checked against the engine's deduction), and
Schedule 1 line 3 carries both profits.

Line 16 follows the Tax Table, as the filed return does: under $100,000 of
taxable income the tax is the table row's (the tax on the row's midpoint,
rounded to the dollar), not the rate schedule's exact figure, so the draft can
be a few dollars off the engine and a note gives the gap. With qualified
dividends or capital gains only the ordinary part is priced by the table (the
Qualified Dividends and Capital Gain Tax Worksheet). Every line drawn from a
document names it to the page: form, box, issuer, file name and page, for
example `W-2 box 2 (Employer; w2-2025.pdf p.1)`.

Schedule 1-A (tax years 2025 to 2028) is its own sheet, and its line 38 is
1040 line 13b. Part I takes AGI from line 11b. Part V, the $6,000 senior
deduction, is drawn for a filer or a joint return's spouse 65 by year end (6%
of the AGI over $75,000, $150,000 joint, off each $6,000): line 36a is the
filer's, 36b the spouse's, 37 their sum; a joint return with no
`spouse_birth_date` on file draws no 36b and a note says so. A person born
January 1 counts as 65 for the year before (Pub. 501: 65 on the day before the
birthday), which also gives the extra standard deduction. Parts II to IV (tips, overtime, car loan interest) are
drawn from four Needed-panel items, each naming its document: `qualified_tips`
with its Treasury `tipped_occupation_code`, `qualified_overtime` and
`car_loan_interest` (`planner enter` takes 0 for none). An item still unanswered
is listed under the draft's unknowns and a note says so; tips without a code
carry no deduction. The form
cuts the tips and overtime deductions by $100 for each whole $1,000 of income
over the start and the car loan interest by $200 for each $1,000 or part of
one; the engine cuts tips and overtime smoothly, so the draft follows the form
and a note gives the gap (up to $100).

Schedule 8812 is drawn when dependents are named (tax years 2025 and 2026,
whose amounts are on file: $2,200 a child under 17 at year end, $1,700 of it
refundable; $500 another dependent). Lines 4 to 12 count them and cut $50 for
each $1,000 or part of one over $200,000 ($400,000 joint). Line 13, the tax
limit (Credit Limit Worksheet A), is 1040 line 18 less Schedule 3 line 8, line
14 the smaller of 12 and 13, and it is 1040 line 19. Part II-A draws 16a, 16b
and 17, then line 18a by the Earned Income Chart (the EIC's earned income when
the EIC is taken, else the Earned Income Worksheet: 1040 line 1z plus Schedule C
line 31 less Schedule 1 line 15), and lines 19 and 20 (15% over $2,500 for 2025).
With three or more children and line 20 under line 17, Part II-B draws lines 21
to 26: W-2 boxes 4 and 6 (both spouses'; the engine's payroll tax on the wages
when no box is on file), Schedule 1 line 15, less 1040 line 27a. Line 27 (unit
3c-5), 1040 line 28, is checked against the engine. Forms 4137 and 8919, excess
Social Security tax withheld (Schedule 3 line 11), the Additional Medicare Tax
and RRTA Tax Worksheet, combat pay and the optional methods are not drafted.
Line 12 is checked against the engine's
credit, line 4 against its child count. The notes list each dependent's age and
credit for the 1040 dependents table; the names, SSNs and relationships are
typed from the cards (each SSN is assumed valid).

Schedule B is drawn when taxable interest or ordinary dividends are over
$1,500, and left out (with a note saying so) when both are $1,500 or less.
Line 1 takes a row per payer from 1099-INT boxes 1 and 3, line 5 a row per payer
from 1099-DIV box 1a; a figure typed with no form behind it is one more row,
asking for the payer's name. Lines 4 and 6 must equal 1040 lines 2b and 3b, or a
`CHECK:` says by how much. Line 3 is 0: Form 8815 (savings bond interest spent
on tuition) is not drafted. Part III is asked, never assumed: while Schedule B is
required, `planner needed` lists `foreign_accounts` (yes or no), and lines 7a
and 8 carry the answer; a yes leaves the FBAR question and the country to you.

Social Security benefits now come from SSA-1099 box 5
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
- **Each spouse's own (unit 3a-7).** On a joint return a spouse with an HSA has
  their own Form 8889 (`8889 (spouse)` in the draft), from the documents marked
  theirs (`data/inbox/spouse/` or `planner owner`) and their own answers
  (`spouse_hsa_coverage`, `spouse_hsa_months`, `spouse_hsa_qualified_expenses`).
  If either spouse has family coverage, both are treated as having it. When each
  has an HSA the family limit is split on line 6, equally unless
  `hsa_family_share` (your percent) says otherwise, and each spouse 55 or older
  adds their own $1,000 on line 7. Schedule 1 line 13 adds both line 13s (2025
  Instructions for Form 8889, Part I and lines 6-7).
- **Not handled, and named.** Archer MSA contributions (line 4), a funding
  distribution from an IRA (line 10) and rollovers (line 14b).

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
  - Line 20a: NC withholding from W-2 box 17 and 1099-R box 14. On a joint return
    line 20a is yours and line 20b the spouse's (2025 D-401 p. 15), from the documents
    marked theirs (`data/inbox/spouse/` or `planner owner`; unit 3a-6); with none
    marked and withholding on file, a note says how to mark them.
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
| `schedule-b.csv` | Schedule B by part, line and payer, written only when Schedule B is required (else a note says why) |
| `carryforward.csv` | the capital loss carried to next year |
| `basis.csv` | cost basis of each open lot, and each Roth conversion's basis and penalty-free date |
| `estimated-payments.csv` | federal and state estimated payments, the installment, and where each came from |
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
D-400 on the page (or into the inbox before `planner run`). A filed 1040 with
line 24 for a year that has ended closes that year on its own: the page shows
a Filed return panel with every line that differs from the draft, and next
year's prior-year figures switch to the filed ones. A filed return read by
OCR closes the year once its values are confirmed. Dropping it again changes
nothing; an amended return closes the year again as a new version. In a
terminal, `planner ingest` then `planner close --year 2025` does the same.

- **The delta.** Each filed line read by the form templates is set beside
  the draft's line, and every line more than $1 apart is listed with the gap.
  - The templates follow the 2024 form numbers and the draft follows 2025's;
    the planner pairs the lines by what they hold. For example, filed 1040
    line 11 is draft line 11a.
  - Filed lines the draft has no line for are listed separately: line 5b,
    and line 34 on a refund. Filed Schedule C lines 1, 7, 28 and 31 are paired
    with the draft's Schedule C sheet once bank rows are categorised.
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

## The dashboard page (Phase 5a)

`planner dashboard --year 2026` writes `out/index.html`, one page with the
whole year. It is a static copy for printing and backup. `planner run` (Phase
5b) serves the same page live, with the forms that answer it.

- **Top to bottom:**
  1. Needed: missing items grouped by the document that supplies them, each
     group with its download path and the outputs it unlocks, then every item
     with why it is needed; forms past their due date, and uncategorised bank rows, each
     with the form that closes it (an answer, don't-have, waive or a category). Inputs
     standing in from year-to-date figures are listed under it, then the items set
     aside, each with its undo.
  2. OCR values awaiting confirm, if any.
  3. The planners: glide path, spending band, MAGI headroom, levers, the
     conversion, tax-prep forms with the draft return, estimated tax, cash
     buffer, wash sales and deadlines. Three panels carry a table under
     their summary: the glide path shows which band you are in and the
     age/year table to 95 (the on-track line beside the comfort-floor line,
     real and nominal), the spending band the return-band table for the next
     ten years, and the cash buffer the monthly cash line for this year and
     next with each month marked `ok` or `UNDER` the cash target. `planner
     plan` prints the same tables as aligned text.
  4. Alerts.
- **Every panel is tagged.**
  - *Estimate*: a projection made before the year ends, or one resting on
    year-to-date stand-ins.
  - *Unavailable*: the planner could not run, and the panel says what it
    needs.
  - *Actual*: everything else.
- **Alerts:**
  - files the intake could not read (with the reason)
  - wash sales
  - OCR values to confirm
  - no document imported yet, or none in the last 90 days
  - limits for the year (or, from October, next year) that are projected or
    carried rather than published, and any hand limit the engine disagrees
    with (Phase 6a)
  - any planner that could not run
- **Self-contained.** Everything the page shows is escaped, including issuer
  names, file names and OCR text. It loads nothing from the internet. It
  prints cleanly, with the forms hidden.

## The one command: `planner run` (Phase 5b)

`planner run` is the program's front door.

1. It reads whatever is in `data/inbox/`.
2. It runs every planner and the draft return, and writes `out/index.html`.
3. It opens the same page live in your browser.

On the live page:

- **Drop files** (PDFs, CSVs, scans) anywhere on the page, or pick them with
  the button. Each one is read straight away. The page reloads with the
  Needed list shorter and a line saying what was imported, what waits for
  you to confirm, what was already in the ledger, and what could not be
  read, and why.
- **Answer** a Needed item in its box, or press *Don't have*. The item leaves
  the list, and the panels that waited on it recompute.
- **Confirm or reject** values read from scans, each beside the scan line it
  came from, correcting a wrong number in place before accepting it.
- **Build the tax pack** from the forms panel.

Repeat until the Needed list is empty. Press Ctrl+C in the window to stop.

- **Private by construction.**
  - The page is served only to this computer (127.0.0.1), on a port the
    system picks.
  - The address carries a one-time key made fresh on each run. A request
    without it, or addressed to any other host name, is refused.
  - Nothing is logged, and the page loads nothing from the internet.
- **Options.**
  - `--year` picks the plan year; it defaults to this year.
  - `--no-open` serves the page without opening a browser.
  - `--port` fixes the port.
  - `--quiet` stops after writing `out/index.html` (no server, no browser),
    for Windows Task Scheduler. `planner schedule --monthly` registers that run
    (the 1st of each month, 09:00, as you, task "Year-End Planner") with
    `schtasks`; `planner schedule --remove` deletes it. Off Windows it prints the
    `schtasks` command and changes nothing.

## Limits kept current on each launch (Phase 6a)

Each `planner run` and `planner dashboard` first refreshes the year's limits
(standard deduction, bracket tops, IRA limits, NC rate and deduction) for this
year and next from the installed policyengine-us. It needs no network.

- **Your sourced rows win.** `config/thresholds.yaml` holds the limits entered
  by hand, each with its IRS or NC source. The refresh writes
  `config/thresholds.engine.yaml` beside it only for what that file lacks.
  Never edit the engine file; it is rewritten on every launch and is not
  shipped.
- **Published, projected or carried.** An engine row is *published* when the
  engine's own parameter file lists that year. An inflation-indexed limit the
  IRS has not announced yet is *projected* by the engine's index and labelled
  so. For a year with no hand rows, the other limits (HSA, phase-outs, poverty
  line) are *carried* from the latest hand year and marked "confirm".
- **Disagreements are shown.** Where a hand row and the engine differ (the
  engine projects the 2026 IRA limit at 7,000; IRS Notice 2025-67 says 7,500),
  the hand row stays in the plan and the dashboard says the engine prices the
  tax with its own figure until policyengine-us is updated.
- **The page header** shows the engine version and where each year's limits
  came from, for example `limits 2026 (config), 2027 (engine projection +
  carried)`.

## Updates (Phase 6b)

- **Automatic check.** Each `planner run` starts with a look for a newer
  release on the feed named in `config/update.yaml` (GitHub's "latest
  release" address), at most once every 7 days: the result is written to
  `data/update/last-check.json` and shown in the page header ("update check
  2026-10-01: up to date"). It sends one plain request and no personal data.
  Offline, or with no newer release, it says nothing; an unreachable feed is
  not counted, so the next launch tries again. `planner update --check`
  looks right now, whatever the last check said. `--no-update-check` or
  `PLANNER_UPDATE_FEED=off` turns it off (the header then says so).
- **Checked before it is used.** A newer release is downloaded, its sha256
  checked against the `.sha256` file published beside it, and unpacked into
  `python-candidate/`. It must pass its own selfcheck there, with its own
  Python. Limits you typed into `config/thresholds.yaml` that the new release
  lacks are copied into it.
- **Swapped in on the next start.** `planner run` swaps the ready release in,
  keeps the old one in `python-previous/`, and starts again on the new one.
  On Windows, `planner.cmd` does the move after Python exits, because a
  running python.exe locks its folder; if a file is in use (another planner
  window), everything is put back and nothing changes.
- **Regression against the engine baseline.** The first `planner run` records what the
  engine you run computes on the reference cases in `planner/engine/reference.yaml`
  (synthetic households with hand-worked figures) in `data/engine-baseline.json`, keyed
  by the policyengine-us version, and prints how far the engine is from the filed lines:
  the reference cases, then each return you filed under `data/private/returns/`. That gap
  is reported, never a failure; a known variance (say, the self-employed health
  insurance deduction) does not stop updates. The first engine recorded is the baseline.
  A candidate release runs `planner selfcheck --regression` inside `python-candidate/`
  with its own Python, and every figure must land within its limit of the baseline or
  the release is held: $5 on a dollar figure, 0.1 points on a share of the poverty
  line, and no change at all in a yes/no (Medicaid eligible, itemizes, whether the
  SE health deduction settled), so an eligibility flip with the same dollars is caught.
  A release that prints no regression figures is held too. A later engine within the
  limits is recorded beside the baseline; one that is not is named on each run. Delete `data/engine-baseline.json` only to start a new baseline from the
  engine you run now.
- **Held.** A release that fails its selfcheck or its regression is never swapped in. The
  dashboard shows "engine update held" with the reason, and the same release
  is not tried again.
- **Major versions are held for you.** A release whose policyengine-us (read
  from its own `dist-info`) or planner is a new major version over the
  installed one can change results, so it is never staged on its own. The
  dashboard shows it as held with `planner update --allow-major`; run that
  (alone, to fetch the feed's release, or with a zip and `--sha256`) when you
  want it.
- **Tax years covered.** The release's selfcheck prints the tax years its
  engine publishes parameters for. Staging reports them, and says so when
  next year is missing ("2027 is not modelled yet"). The page header shows
  the same for the engine you run, for example `tax years 2015, 2018-2026
  (2027 is not published)`.
- **By hand.** `planner update <zip> --sha256 <hash>` installs a downloaded
  release the same way, `planner update --rollback` goes back to
  `python-previous/`, and `planner update --check` looks for one now. `data/`
  and `out/` are never touched.
- **An older `data/` opens in a newer release** (Phase 10, 2e). The first
  writing command after an update copies the ledger to
  `data/ledger/planner.db.schemaN.bak` (N is the old schema) and then brings it
  up to date one step at a time. A ledger written by a *newer* release (say
  after `--rollback`, or restoring a newer backup) is refused with "written by
  a newer planner" and left exactly as it was: use that release again, or
  restore a backup this release made. Every release's tests open the v0.1.0
  `data/` folder in `tests/fixtures/data-v0.1.0` (synthetic, made by the
  v0.1.0 tag with `make.py` beside it) and run it end to end.
- `scripts/build_release.py` writes the `.sha256` file beside the zip.

## Rolling over to the new year (Phase 7)

When a year ends, the dashboard shows "2026 has ended" with a **Roll over to
2027** button. From the command line, run `planner rollover` (it rolls last
year by default). Rolling over:

- **Carries forward what next year needs.** AGI, total tax and NC tax feed
  the safe-harbor estimates, and the capital loss carried forward lands on
  next year's Schedule D. Until the filed return closes the year,
  these come from the draft and show as estimates. After that, they come
  from the filed figures.
- **Keeps a snapshot** of the ledger and of that year's dashboard in
  `data/snapshots/<year>-v<n>/`. The all-time peak and older snapshots are
  never changed.
- **Switches to the new year.** The dashboard and `planner dashboard` plan
  next year from then on, refresh its limits, and list any Roth conversions
  that become penalty-free during it.
- **Writes a checklist** to `out/rollover-<next>.txt` and prints it:
  - update the engine
  - confirm the new limits
  - re-check the form templates
  - re-enter the ACA plan
  - turn dividend reinvestment off
  - use specific-ID cost basis
  - answer anything still missing
- **Reports next year's spending band and glide path**, recomputed from the
  balance the ledger holds now: the year's spend and where it sits in the
  band, what you can reach before the IRA opens against what the floor
  needs, and the age the money runs out (or lasts through). Until the
  spending profile is filled in, it says which answer is still missing.
- **Asks for next year's figures** when run in a console (`--ask`), such as
  the new Social Security estimates, ending with what the household really
  spent in the year that ended (`spending_actual`). Pressing Enter keeps the
  value shown. Once typed, the rollover report and `planner spend` show it
  beside the band: "2025 spending was 58,000.00: 2,000.00 above the 2026
  band's 56,000.00".

Running it again changes nothing. If a corrected form arrives later, or the
filed return is closed, the next `planner run` (or `planner rollover`)
reopens the year and records it as a new version with a fresh snapshot. The
new year's plan then reads the new figures, and the earlier snapshots stay.

## Backup and restore (Phase 8a)

`planner backup` writes one zip of `data/` and `config/` to
`out/backups/planner-backup-<date>.zip` (or a path you give it, such as a USB
drive). It asks for a password twice and never shows it. With a password, the
zip is AES-256 encrypted. If you press Enter instead, the zip is not encrypted
and the command says so. `--plain` skips the question. The ledger is copied
safely even while the dashboard is open.

`planner restore <zip>` asks for the password if the zip has one. It checks
the whole archive before touching anything:

- every entry sits under `data/` or `config/` (no absolute paths, drive
  letters, `..` or links)
- the unpacked size is under 4 GiB
- every file matches the sha256 recorded at backup time
- the ledger passes sqlite's integrity check

Only then does it swap the backup's `data/` in. Your current `data/` is kept
as `data-previous/` until the next clean `planner run`. Until then,
`planner restore --undo` puts it back. A second restore is refused while
`data-previous/` exists.

`config/` belongs to the release you are running. Only limits you typed into
the backed-up `thresholds.yaml` that the current one lacks are copied over.
If a planner window has `data/` open, the restore stops and asks you to
close it.

## Typed answers are checked; old data is flagged (Phase 8b)

Every typed answer is checked before it is stored, on the Needed panel and in
`planner enter`. A bad answer is refused with what was expected, and nothing is
saved:

- **Amounts** may carry `$`, commas or (parentheses) for a negative. They must be finite,
  at most 100,000,000, and not negative except gains and self-employment income.
- **Whole numbers** have their ranges (Social Security claim age 62-70, HSA months 0-12, `hsa_family_share` 0-100;
  a spouse's `spouse_*` answer takes the same range).
- **Fractions** run from 0 to 1, or take a percent such as `4%`.
- **Dates** are YYYY-MM-DD, between 1900 and today.
- **Choices** must be one of the listed options.
- **Text** is at most 200 characters, with no control characters.

On the command line, years must fall between 1990 and 2100, and amounts, ports
and steps have bounds too. Click rejects a bad value with "Invalid value".

More than 90 days after the last document import, a banner atop the page says
how old the figures are. The plan still runs.

## Releases and the privacy audit (Phase 8c)

To publish a release, set `__version__` in `planner/__init__.py`, then push a
tag `vX.Y.Z` that matches it. `.github/workflows/release.yml` then runs these
steps on a clean Windows runner:

1. checks that the tag matches the version
2. runs the suite
3. builds the zip
4. proves it with `scripts/windows_proof.ps1`
5. attaches the zip and its `.sha256` to a **draft** release

Publishing the draft is a person's decision. The update check reads published
releases only.

Every push and pull request also runs the `scan` job in `ci.yml` (Phase 10,
unit 2f): `pip-audit` checks every locked dependency in `uv.lock` against the
known-vulnerability databases, and `gitleaks` scans the whole git history for
keys and tokens (the binary is pinned by checksum and findings are redacted).
The Linux test run measures coverage of `planner/` and fails below the floor in
`ci.yml`. Dependabot proposes weekly updates for the lockfile and the CI
actions.

The release can only contain files git tracks. `scripts/build_release.py`
stages its contents from `git ls-files`, so a developer's own `data/`, `out/`
or untracked notes cannot ship. Before zipping, `audit()` refuses a stage that
holds a private folder or a database. `tests/test_privacy.py` plants a sentinel
string in `data/`, `out/`, an untracked config file and an untracked module,
then builds a release from that tree and checks that no entry carries the
sentinel. It also checks that git tracks nothing under `data/`, `out/` or
`dist/`.

`tests/test_end_to_end.py` is the done test:

1. synthetic 1099 PDFs dropped in the inbox reach the dashboard
2. a fresh copy of the release's `config/` plus only the `data/` folder (a new
   computer, or a restored backup) renders a byte-identical page

## One planner at a time (Phase 9a)

Two planner processes writing the ledger together could corrupt it, so every
command that writes takes a lock on `data/planner.lock` when it starts and
lets go when it ends. A second one stops at once with exit code 2 and says
"another planner is running". The usual cause is the dashboard open in one
window while a scheduled `planner run --quiet` or a second `planner` window
starts; close the first or wait for it.

- The lock is held by the operating system (`fcntl.flock` on Linux and macOS,
  `msvcrt.locking` on Windows), not by the file existing. If planner crashes
  or the computer loses power, the file stays but the lock is gone, so the
  next run starts normally. There is nothing to delete.
- Commands that only read `data/` or never open it do not lock, so they run
  beside the dashboard (`facts` and `rows` open the ledger read-only: they
  never create or change it, and with no ledger yet they list nothing): `backup`, `facts`, `rows`, `paths`, `init`, `version`,
  `selfcheck`, `check-config`, `thresholds`, `compute`, `sweep` and `verify`.
  `--help` never locks.
- `planner restore` and `restore --undo` rename `data/`, which Windows will
  not do while a file inside is open. They let go of the lock for the rename
  and take it again afterward. An update that ends the process with exit
  code 75 for the launcher to swap folders releases the lock when the process
  exits, so the rerun starts clean.

`planner init` creates `data/` and `out/` with every subfolder and prints the
inbox path. It is safe to repeat. If the folder is inside OneDrive, Dropbox,
iCloud Drive or Google Drive it creates nothing and exits 2 with the reason,
because the sync client would copy your ledger to the cloud. `planner.cmd`
checks its own folder first with `findstr` and prints the same warning before
it downloads or runs anything, so a double-click in a synced folder explains
itself in the window it keeps open.

## Scope guard and the household's people (Phase 10, units 0b, 3a-2 to 3a-7 and 3b-1 to 3b-4)

The profile names the household's people: `spouse_birth_date` (asked when the filing
status is married_joint), `spouse_death_date` (a date or `none`; asked for married_joint
and qualifying_surviving_spouse) and `dependents` (asked for married_joint, head_of_household and
qualifying_surviving_spouse), typed as birth dates with `student` (full-time this year) or
`disabled` after one, or `none`:

    planner enter spouse_birth_date 1974-05-02
    planner enter dependents "2018-03-02, 2006-07-01 student"

`inputs.build` puts them on the household (a spouse only on a joint return; an old
answer under another status is kept but not priced) and the engine prices the tax unit.
The spouse's age for the draft counts the day before the birthday, as the filer's does.
Until they are named, `planner.coverage.HOUSEHOLD` holds one line per status:

- Married filing jointly: the spouse's income, age, deductions and credits are left out,
  so every figure is this person's share, not the joint return.
- Married filing separately: the spouse's choice to itemize (which binds this return), a
  community-property split and the spouse's figures are left out. This stays until
  separate returns arrive (unit 3b).
- Head of household: no qualifying person is entered, so dependents' credits and the
  larger household for the ACA credit and benefits are left out.
- Qualifying surviving spouse (unit 3b-1): joint rates (the joint brackets, standard
  deduction and IRA and Roth phase-outs, Pub. 590-A) with no spouse in the tax unit,
  for the two years after the year of death while a dependent child lives at home
  (2025 Form 1040 instructions). With no child named, or a `spouse_death_date` outside
  those two years, the line says what to file instead: married_joint for the year of
  death, single or head_of_household after the second year. Medicare's IRMAA tiers and
  the Social Security taxation thresholds are the single ones, as the law sets them.
- Married filing jointly after the year the spouse died (unit 3b-3): a joint return is
  filed for the year of death at the latest, so a later year names what to file instead
  (qualifying_surviving_spouse for the two years after, else single or head_of_household).

A spouse who died during the year (unit 3b-3) stays on the joint return, with their
income to the date of death and the survivor's for the whole year (2025 Form 1040
instructions, Married Filing Jointly). The spouse is priced at their age at death and
counts as 65 only if 65 then, reached the day before the 65th birthday (Pub. 501:
born February 14, 1960 and died February 13, 2025 is 65; died February 12 is not).
The draft's notes say to check the spouse's Deceased box with the date and to sign
"Filing as surviving spouse". A death after the year ends, before the return is filed,
leaves that year's joint return as it is. The date is the current spouse's: after a
remarriage in the year the joint return is with the new spouse (`none`), and the
deceased spouse's own return, married filing separately, is not drafted.

A couple married during the year (unit 3b-4, `marriage_date`: a date or `none`, asked
for married_joint) may repay less excess advance premium credit through Form 8962's
alternative calculation for the year of marriage (2025 Form 8962 instructions, Table 4
and Worksheet 3; Pub. 974, Worksheets I-V). When dependents are named,
`spouse_premarriage_dependents` says how many were the spouse's before the marriage
(a child the spouse could claim; one either of you could claim may go on either side);
more than there are dependents is cut to the number of dependents, with a note. The
draft runs it when the return is joint, each of you was unmarried on January 1,
someone had marketplace coverage before the first full month of marriage, and the
regular calculation leaves an excess advance. Each spouse's family before the marriage
(you or the spouse, plus the dependents on that side) gets half the household income
over the poverty line for its own size (`planner.engine.tax.poverty_line`), its own
applicable figure (`applicable_figure`, i8962 Table 2) and monthly contribution, run
against that spouse's own 1095-A (documents are each person's, unit 3a-4) from the
first month of coverage to the month of marriage. The draft elects it only when
Worksheet V's total is more than the regular credit for those months: then the
pre-marriage months' columns (c) and (e) are the worksheets', lines 35-36 hold each
side's family size, contribution and months, line 26 is zero and the notes say to check
Yes on line 9 and No on line 10, with the repayment before and after. Otherwise a note
says it does not lower the excess and Part V stays blank. The tests run Pub. 974's own
example (Paulette Oak and Quentin Cedar: line 24 $7,021, line 29 $1,402 against $2,942).
A policy covering both spouses in the month of marriage counts on its owner's side, and
the calculation runs only when the household is eligible for the credit at all.

The line leads the dashboard's alerts (kind `scope`), reaches the draft return's notes,
`planner magi` and the tax pack's notes. Once the people are named it goes: the draft lays out the spouse's Schedule 1-A line 36b and the dependents'
Schedule 8812 (unit 3a-3), each spouse's own Schedule SE (unit 3a-5) and Form 8889
(unit 3a-7).

A married couple filing jointly can price the other way to file (unit 3b-2):

    planner separate --year 2026

prices the joint return and each spouse's married-filing-separately return, and says
which costs less (cost as in `planner levers`). Each separate return takes that
spouse's own lines: their wages, SE income, IRA, Social Security, tips and overtime,
their own Form 8889 line 13, and the interest, dividends, gains and deductions on
their own documents. What no document places (a typed mortgage interest figure, a
planned sale) is split equally, and a note names it. The rules applied:

- If one spouse itemizes the other must (2025 Form 1040 instructions); both returns
  are priced with the standard deduction and both itemized, and the cheaper pair is shown.
- The health plan's premiums and advance credit go 50% to each return and neither
  takes the premium credit (2025 Form 8962 instructions, Allocation Situation 2), so
  each repays its half against its own income. The domestic-abuse and abandonment
  exceptions are not handled.
- The children are claimed on one return, priced both ways.
- A community property state (AZ, CA, ID, LA, NV, NM, TX, WA, WI; Pub. 555) is refused:
  each spouse reports half the community income, which the ledger cannot tell apart.

Each document is one person's (unit 3a-4). Drop the spouse's W-2, 1099-R, SSA-1099,
5498 and 1099-NEC in `data/inbox/spouse/` (any case); everything else is yours. A
document dropped in the wrong place is moved with

    planner owner theirs-w2.pdf spouse

(its file name from `planner facts`, or its archived path when two share a name). A
corrected form replaces only its owner's copy, so two W-2s from one employer both
count. On a joint return the Needed panel then asks for the spouse's own lines
(`spouse_wages`, `spouse_se_income`, `spouse_ira_distributions`,
`spouse_roth_conversion`, `spouse_social_security`,
`spouse_traditional_ira_contribution`, `spouse_qualified_tips`,
`spouse_tipped_occupation_code` only with their tips, `spouse_qualified_overtime`),
each summed from the spouse's documents or typed, and the head's from the head's
alone, and the engine prices each as that person's.
A typed `total_income` is the couple's: the head's wages are what is left after the
spouse's own income. The ledger is schema 5 (`documents.owner`); an older one is
copied aside and brought up as the head's.

## Child and dependent care (Phase 10, unit 3c-1)

A household with dependents is asked for `care_expenses` (care paid in the year for a
child under 13 so you, and a joint spouse, could work) and `dependent_care_benefits`
(W-2 box 10; a joint spouse's own as `spouse_dependent_care_benefits`). With benefits it
also asks `dependent_care_grace` (last year's benefits used in this year's grace period,
Form 2441 line 13) and `dependent_care_forfeited` (forfeited or carried to next year,
line 14). Type 0 for none.

The draft lays out Form 2441 (2025 Form 2441 and instructions):

- Part III (lines 12-31), when there are benefits. The exclusion (line 25) is the
  smallest of the benefits less forfeitures, the care incurred, each earner's income
  without the benefits, and $5,000 ($2,500 married filing separately). The rest
  (line 26) is wages on Form 1040 line 1e. The excluded amount lowers the credit's
  $3,000/$6,000 limit (line 29), and care paid with benefits leaves the credit (line 30).
- Part II (lines 2-11): the qualifying children, the care (line 3), each earner's
  income (lines 4-5, which now include any line 1e), the line 8 decimal (35% down to 20%
  as AGI passes $15,000 to $43,000), and the credit, capped at the tax (line 10 = 1040
  line 18). The credit goes on Schedule 3 line 2.

The engine excludes benefits but neither taxes the excess nor drops the care paid with
them, so `planner.engine.tax.dependent_care` works Part III out first and prices the
household that results: the exclusion pinned to line 25, the care cut to line 30, line 26
added to the wages of whoever received the benefits. The plan, the draft and the
planners all price that household. A sweep (`planner levers` curves) holds Part III at
the base household's figures.

Not drafted: Part I (each provider's name, address and ID, from their receipts), line 9b
(last year's care paid this year), benefits from your own business (lines 22 and 24),
and a dependent 13 or over, or a spouse, who cannot care for themselves (they qualify;
the draft does not count them, so see a preparer). A married person filing separately
gets no credit unless they lived apart from their spouse the last six months of the year;
the draft takes none and says so.

## Education credits (Phase 10, unit 3c-2)

Every household is asked `education`: each student on the return, what was paid for
their qualified tuition and related expenses (Form 1098-T box 1, or the school's
statement), the tax-free aid applied to them (scholarships and grants, box 5, used for
those expenses) and the credit, as `dependent 1 6500 aid 1500 aotc; you 3000 llc`, or
`none`. A student is `you`, `spouse` (joint) or `dependent N` (the N-th dependent as
typed). `aotc` is the American opportunity credit (a student in the first four years of
college, at least half time, toward a credential, with no felony drug conviction and
not claimed for four earlier years); `llc` is the lifetime learning credit. Each student
takes one. A filer who is not married filing jointly and claims the American opportunity
credit is also asked `aotc_refundable_barred`: yes when all of Form 8863's line 7
conditions apply (under 18 at the end of the year, or 18, or a full-time student over 18
and under 24, with earned income under half their support; a parent alive; not filing
jointly), which makes the whole credit nonrefundable.

The draft lays out Form 8863 (2025 Form 8863 and instructions):

- Part III per student: the adjusted expenses (paid less aid) up to $4,000 (line 27), all
  of the first $2,000 and a quarter of the next $2,000 (lines 28-30), or the lifetime
  learning expenses (line 31).
- Part I: the phase-out share, modified AGI from $90,000 down to $80,000 ($180,000 to
  $160,000 joint; lines 2-6), and the refundable 40% (line 8), on Form 1040 line 29.
- Part II: the lifetime learning credit, 20% of up to $10,000 of expenses (lines 10-12),
  phased out the same way (lines 13-18), then the Credit Limit Worksheet: the
  nonrefundable credits, capped at the tax less Schedule 3 lines 1 and 2, on Schedule 3
  line 3 (line 19).

Not drafted: Part III lines 20-26 (each student's school, its EIN and the yes-or-no
answers; filled in from the 1098-T), recapture of an earlier year's credit after a
refund, and expenses paid by someone other than you, your spouse or your dependent.
A married person filing separately takes neither credit; the draft takes none and says
so. A student's 1098-T is expected by January 31 (Expected forms) when `education` names
one, and it reads as form `1098-T` (boxes 1 and 5).

## Saver's credit (Phase 10, unit 3c-3)

Every household is asked `roth_ira_contribution` (Form 5498 box 10, counting what goes in
by the filing deadline; not rollovers or conversions; ABLE contributions as the
beneficiary go here too) and `elective_deferrals` (W-2 box 12 codes D, E, F, G, H, S, AA,
BB and EE, Roth deferrals included, plus voluntary after-tax contributions to a workplace
plan). A joint spouse is asked their own (`spouse_roth_ira_contribution`,
`spouse_elective_deferrals`). Anyone with a contribution the form counts (these two or the
traditional IRA contribution) is also asked:

- `savers_distributions`: every distribution from an IRA, Roth IRA, ABLE account or
  workplace plan in the testing period (the two years before this one, this year, and
  next year up to the filing deadline). On a joint return both spouses' go on each line,
  except a spouse's from a year you did not file jointly. Leave out rollovers,
  trustee-to-trustee transfers, Roth conversions, plan loans, returned excess
  contributions, 404(k) dividends, military retirement and an inherited IRA's. The draft
  never takes less than the year's own IRA distributions.
- `savers_barred`: yes when someone else claims that person on their return, or they were
  a full-time student during some part of five calendar months of the year.

Someone born after January 1, 18 years back (after January 1, 2008, for 2025) cannot take
the credit; the birth date decides it.

The draft lays out Form 8880 (2025): each person's column, lines 1 and 2 less line 4
(line 5), capped at $2,000 (line 6); both columns (line 7); AGI (line 8, Form 1040 line
11a); the decimal from the line 9 table (0.5, 0.2 or 0.1 by AGI and filing status, and 0
above $79,000 joint, $59,250 head of household or $39,500 otherwise, for 2025); the
credit before the limit (line 10); the Credit Limit Worksheet (line 11: the tax on 1040
line 18 less Schedule 3 lines 1-3); and the credit (line 12) on Schedule 3 line 4. The
table comes from the engine's own parameters for the year and the tests pin its 2025
edges to the printed form.

## Earned income credit (Phase 10, unit 3c-4)

The draft works Form 1040 line 27a by the instructions' EIC worksheet (2025): Worksheet A,
or Worksheet B when you or a joint spouse had Schedule C profit (Part 1: Schedule SE line 3
less line 13, then line 1z). The EIC Table gives the credit on earned income and, when AGI
is different and at or past the phase-out start ($10,620, or $17,730 joint, with no
qualifying child; $23,350, or $30,470 joint, with one or more), on AGI too; the smaller
is the credit. The table is priced from the engine's parameters for the year, and the
tests pin it to rows and footnotes of the printed 2025 table.

The draft also applies Steps 1-4 where it knows the answer: no credit with investment
income over $11,950 (1040 lines 2a + 2b + 3b + 7a, a loss as zero), none married filing
separately without a qualifying child, and without one the filer (or a joint spouse) must
be 25 to 64 at the end of the year (25 the day before the birthday). A qualifying child is
a dependent under 19, or under 24 and a full-time student, and younger than you (or your
spouse), or permanently and totally disabled. Schedule EIC carries each child's year of
birth and lines 4a-4b; type the name, SSN, relationship and months lived with you in the
United States. Each child is taken to have a valid SSN and to have lived with you more
than half the year; married filing separately with a child, the draft notes the special
rule for separated spouses. Rental or passive income (Pub. 596 Worksheet 1), clergy pay,
excluded Medicaid waiver payments and nontaxable combat pay are not drafted: a CHECK note
appears when the engine's credit differs.

## Coverage gate (Phase 10, unit 2a)

`planner.coverage.gate` runs right after intake, before any plan or draft, and lists
every fact the planner cannot answer correctly. Each gap has a reason (starting
`Not handled:`), a Needed line saying what to do, and the sections it touches:

- **Household**: the lines above. An unnamed spouse or qualifying person touches every
  priced panel and the draft; once named, only the draft and the estimated tax.
- **State**: a state other than NC that taxes income. The plan's state income tax is
  the engine's estimate; the state return is not drafted (have a preparer draft it).
  Touches only the state return, so the federal draft stays ready. A state with no
  income tax has no gap; a value that is not a state's code is one that touches every
  priced panel.
- **Document**: each file in `data/inbox/UNMATCHED/`. Anything on it is left out, so it
  touches every priced panel and the draft until its figures are typed with
  `planner enter` or the file is moved out because it holds no tax figures.

Every panel and every drafted form carries a coverage tag: **not handled** when a gap
touches it, else **verified** when its row in `config/capabilities.yaml` is verified,
else **estimated**. The dashboard lists the gaps at the top of Needed (each counts as
one open item); `planner draft` prints the tag beside each form heading and a gap that
touches the draft makes it NOT READY for a preparer; the tax pack writes
`coverage.csv`, a row per drafted form (tag, why, what to do) and a row per gap.

### Readiness (unit 2a-2)

The top of Needed, and `planner dashboard`, give three answers, each `yes` or `no` with
its reasons (the first five, then a count):

- **Ready to plan**: nothing open on the Needed list and no gap in a planner section
  (household or an unread document). Estimates are fine here; each is tagged.
- **Ready to act**: that, and no item set aside that a move rests on (the glide path,
  spending, MAGI, levers, conversion, withdrawals, estimated tax, cash buffer). A waived
  form holds back every move: the income on it may be missing.
- **Ready for a preparer**: nothing open, nothing set aside, nothing still an estimate,
  no gap of any kind (a state return not drafted included), the tax year ended and the
  draft built.

Marking an item *don't have* or waiving a form empties the list but never makes the plan
ready to act or to hand to a preparer.

