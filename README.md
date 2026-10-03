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
