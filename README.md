# Year-End Tax & Retirement Planner

A Windows program for year-end tax planning, tax prep and retirement planning.
You drop your tax documents into a folder. It reads them, computes your taxes with
[PolicyEngine US](https://github.com/PolicyEngine/policyengine-us), asks for what is
still missing, shows every tax move left this year, and at filing time hands you a
complete package for your preparer. It is deterministic and runs on your computer only:
no account, no cloud, no AI at runtime.

**Not tax, legal or investment advice. Every figure is an estimate from the documents and answers you give it; have a tax preparer review the return before you file.**

## Use

1. Download `yearend-planner-<version>-win64.zip`. Right-click it, choose Properties,
   tick **Unblock**, then OK.
2. Extract it anywhere local, such as `Documents\Planner`. Do not use OneDrive, Dropbox
   or another synced folder.
3. Double-click `planner.cmd`. The dashboard opens in your browser.
4. Drop every tax document you have on the page: W-2s, 1099s, brokerage CSV exports,
   last year's return, and statements (PDF, CSV or a phone photo).
5. Work the **Needed** panel. It groups what the plan still lacks by document: each group
   shows the exact download path, the outputs that document unlocks, and every figure it
   closes. Upload that document, or type the figure, or mark it as one you don't have. Repeat until the panel says *nothing needed*. The panel then answers three questions apart: ready to plan, ready to act, ready for a preparer. Items you marked as ones you don't have empty the list but keep the last two at *no*.

The rest of the page is the plan: projected income against each cliff, the Roth
conversion size, estimated tax due dates, wash-sale warnings, the levers with a what-if,
the draft 1040 and the NC D-400 or CA 540, and (once the year ends) **Build tax package** and **Roll
over to <next year>**.

Everything personal stays in `data/` and `out/` inside that folder. Nothing is sent
anywhere. One planner writes at a time: a second window, or a scheduled run while the
dashboard is open, stops with "another planner is running". To move to a new computer,
copy the folder or run `planner backup`.

## Who it handles

A single filer gets the full plan and draft. A joint filer is asked for the spouse's
birth date, and a joint or head-of-household filer for the dependents (birth dates, marked
`student` or `disabled`, or `none`); until they are named, the plan is priced as one
person and tagged **Not handled** first in the alerts, in the draft return's notes, in
`planner magi` and in the tax pack. Once they are named the household is priced as the
whole tax unit (the spouse's age, the child tax credit, the household size for the ACA
credit), and the draft return carries the spouse's senior deduction and a Schedule 8812
for the dependents, and each spouse's own Schedule SE. The spouse's documents go in
`data/inbox/spouse/` (or `planner owner <file> spouse`), so their wages, IRA and Social
Security are theirs, and so are their Schedule SE and Form 8889 (the family HSA limit
split between the spouses' HSAs).
A qualifying surviving spouse is priced at joint rates for the two years after the
year of death while a dependent child lives at home (`spouse_death_date`, unit 3b-1); a
spouse who died during the year stays on that year's joint return at their age at
death (unit 3b-3). A couple married during the year gets Form 8962's alternative
calculation for the year of marriage when it lowers the repayment (`marriage_date`,
unit 3b-4, Pub. 974).
A household with a child under 13 is asked for the care it paid and any dependent care
benefits (W-2 box 10); the draft carries Form 2441, its credit on Schedule 3 line 2 and
benefits above the care or the lower earner's income on Form 1040 line 1e (unit 3c-1).
Each student's tuition, the aid applied and the credit taken are typed per student; the
draft carries Form 8863, the American opportunity credit's refundable part on Form 1040
line 29 and the nonrefundable credits on Schedule 3 line 3 (unit 3c-2).
Roth IRA and ABLE contributions, W-2 box 12 elective deferrals and the testing period's
distributions are typed per spouse; the draft carries Form 8880, the saver's credit, on
Schedule 3 line 4 (unit 3c-3).
The draft works the earned income credit on 1040 line 27a by the EIC worksheet and the
EIC Table, with Schedule EIC for each qualifying child (unit 3c-4), and Schedule 8812's
additional child tax credit line by line, Part II-B included (unit 3c-5).
`planner separate` prices a joint couple's two married-filing-separately returns, each
on that spouse's own documents, against the joint return (unit 3b-2; not in a
community property state). Filing as married filing separately stays
**Not handled**. The state returns drafted are listed in one registry,
`planner/taxprep/statereturn.py` (NC's D-400, CA's Form 540, NY's IT-201, PA's PA-40, IL's IL-1040, OH's IT 1040, GA's Form 500, MI's MI-1040 and NJ's NJ-1040 today; units 3d-4, 3d-6, 3d-7, 3d-8, 3d-9, 3d-10, 3d-11, 3d-12, 3d-13); another state
gets the engine's estimate and a **Not handled** line saying to have a preparer draft it.
A move into or out of the state, income from another state and a city, county or
school district income tax are asked, and each is **Not handled** when it applies (unit 3d-5).
A document no template reads is named the same way. Each panel and drafted form is tagged
verified, estimated or not handled, and the tax pack's `coverage.csv` lists why.

## Commands

`planner.cmd` with no arguments is `planner run`. Each command's `--help` has details.
The full guide is [GUIDE.md](GUIDE.md), which ships in the zip.

| Command | What it does |
|---|---|
| `account` | Describe one account (type, name, date of death, yearly RMDs, typed balance) in data/profile/accounts.yaml; with no arguments, list them. |
| `backup` | Zip data/ and config/ into one file you can copy to a USB drive. |
| `categorize` | Schedule C from the bank rows: categorise them by a rule (a piece of the description) or one row at a time, then see the lines and what is left. |
| `check-config` | Load config/ and report; exit non-zero on a malformed file. |
| `close` | Close the year from the filed return: every filed line beside the draft's, and the filed figures kept as the year's record. |
| `compute` | Every tax figure for one household-year, as JSON, with the self-employed health deduction settled against the premium tax credit (IRS Pub. 974). |
| `confirm` | Values read by OCR wait here. |
| `conversions` | Size this year's Roth conversion: one engine sweep, a candidate per watched line, each with its federal and NC tax, ACA credit change, Medicaid effect and the cash needed from outside the IRA. |
| `convert` | Record a Roth conversion; the planner dates when its principal becomes penalty-free. |
| `dashboard` | Write the dashboard as a static page, out/index.html, for printing and backup: the Needed panel, every planner, the draft return and the alerts, each panel tagged actual, estimate or unavailable. The glide panel carries the age/year table, the cash panel the monthly cash line and the spending panel the return-band table. |
| `derive` | Recompute the YTD facts (realized gains, dividends, interest, bank flows) for one year from the imported rows; supersedes the previous run. |
| `dont-have` | Mark an item as not available; it leaves the Needed list and the plan shows it as unavailable instead of guessing. `--undo` puts it back. |
| `draft` | The draft return: Form 1040 with Schedules 1, 1-A, 2, 3, C, D and SE and the other forms, the NC D-400 with Schedule S, the CA Form 540, the NY Form IT-201, the PA Form PA-40 with Schedule SP, the IL Form IL-1040, the OH Form IT 1040 with its Schedules of Adjustments, Business Income and Credits, the GA Form 500 with Schedule 1, the MI-1040 with Schedule 1, and the NJ-1040, every line priced by the engine and naming its source. |
| `enter` | Type one answer the documents did not supply; profile answers go to data/profile/assumptions.yaml, year answers to data/manual/<year>.yaml. |
| `esttax` | The safe harbor and the four installments, federal and the household's state (none in a state without an income tax; CA NY PA IL OH GA NC MI NJ VA on their own rules, any other state on the federal ones, marked `Estimated:`): what was paid (bank rows to the IRS or NCDOR, plus `planner paid`), each due date's shortfall, and the next payment. |
| `facts` | List the accepted facts in the ledger, each with its source file and page. |
| `forms` | The forms the year should produce (from last year's issuers, the accounts and the Needed panel), which have arrived, and where to download the rest. |
| `gains` | Form 8949 and Schedule D: each closed lot in a taxable account, wash sales across every account (code W), 1099-DIV capital gain distributions and the loss carried in. |
| `glide` | The age/year table to 95 under the planning return, the accessible-bucket floor through the IRA access age, the comfort-floor line (the same rule at the floor return) beside it, three stress rows, and the month-by-month cash line for this year and next, with estimated payments from `esttax` and the planned sales and conversion tax (no `--cash-in` needed). |
| `hsa` | Form 8889 (and a joint spouse's own): the HSA limit for the coverage and months, employer money against it, the deduction, any excess, and distributions not spent on medical care. |
| `ingest` | Read every file in data/inbox/ into the ledger; archive or mark UNMATCHED. |
| `init` | Set up this folder: create data/ and out/ with every subfolder. Refuses a folder inside OneDrive, Dropbox, iCloud Drive or Google Drive (exit 2). Safe to repeat. |
| `levers` | Every move left this year that changes the tax bill or the ACA credit, sized from the ledger, priced through the engine and ranked: moves that get you under a line, and moves that use the room below the next one. |
| `magi` | Project the full year from the Needed panel plus overrides: every tax figure and the distance to each watched line. |
| `needed` | What the plan still lacks for a year: each missing item, why, and the document that supplies it, then every form past its due date that the return needs. |
| `paid` | Record an estimated payment the bank export does not show. |
| `paths` | Show where this planner keeps its folders, creating data/ and out/. |
| `plan` | The year-end plan on one page: the Needed panel, projected MAGI against every line, the conversion, the spending band with its return-band table, the glide path with its age/year table, cash to raise with the monthly cash line, estimated tax, wash sales and the deadline calendar. From a terminal it first asks for total income, Q4 dividends, planned sales and the conversion target (`--total-income`, `--q4-dividends`, `--sales-st`, `--sales-lt`, `--conversion-target manual\|auto`; `--no-ask` skips the questions). |
| `restore` | Check a backup (paths, size, every file against its manifest), then swap its data/ in. |
| `rollover` | Roll the year that ended into the next: carry AGI, total tax, NC tax (another state's is asked) and the capital loss carryforward (filed figures once closed, else the draft's), keep a snapshot of the ledger and the year's dashboard, make next year the active one, refresh its limits, report next year's spending band and glide path, print the checklist and ask for next year's figures (last year's actual spending included). |
| `rows` | List imported CSV rows (holdings, lots, transactions, income, bank lines). |
| `run` | The one command: read the inbox, run every planner and the draft return, write out/index.html, then serve the page on this computer and open it. |
| `schedule` | Register a monthly quiet run (`planner.cmd run --quiet`, the 1st at 09:00) with Windows Task Scheduler; `--remove` deletes it. |
| `selfcheck` | Run one real federal calculation through the tax engine and print it; --regression runs the shipped reference cases instead and prints the engine's figure for each. |
| `separate` | Price the year married filing jointly and as two separate returns, each spouse on their own lines, and show which costs less. |
| `spend` | The spending band: rate x balance clamped to the floor and ceiling, the drawdown rule against the inflation-adjusted peak, and the return-band table under the floor and planning returns (real dollars). |
| `status` | The portfolio today: every account, total, accessible and locked money, the Roth withdrawal order, the all-time peak, YTD income by type and its gap to the filed 1099s, unrealized gains and the carryforward. |
| `sweep` | Sweep one input across a range in a single engine run; one JSON row per step. |
| `taxpack` | Everything a preparer asks for in out/tax-<year>/: the draft return (text and printable HTML), Form 8949 CSV, Schedule C, carryforward and basis, estimated payments, the form inventory, and the originals ZIP. |
| `thresholds` | The sourced limits in config/thresholds.yaml for a year, checked against the engine's own parameters. |
| `update` | Swap in a newer release after its own selfcheck passes and its regression matches the engine baseline ($5 on dollars, 0.1 points on a share of poverty, exact on yes/no); --rollback undoes it; --check looks for one on the update feed now (the automatic check runs at most weekly); --allow-major installs a release that jumps a major version of the planner or policyengine-us. |
| `verify` | Recompute a filed year from its inputs; compare each line to what was filed. With no file it checks the shipped reference cases. |
| `version` | Print the planner version (the release's VERSION file when there is one). |
| `waive` | Take a late form that will not come (the issuer never sends one) off the Needed list; it stays in `forms`, marked waived. `--undo` puts it back. |
| `washsales` | Every loss sale with a buy of the same symbol within 30 days either side, across all accounts, and the symbols whose window is still open. |
| `whatif` | Recompute the full year with the chosen levers and show it before and after, side by side. |
| `withdraw` | Raise the cash target: cash accounts first, then the taxable lots with the least gain per dollar (specific-ID lots first); the MAGI and tax effect is priced through the engine. |

## If an update is held

The dashboard says **engine update held** when a downloaded release failed its own
selfcheck, or moved a tax figure past its limit from the engine you run now, or jumps a
major version. Nothing changed: you are still on the release you had.

1. Keep working; the held release is never retried on its own.
2. To look again later (after a fixed release is published), run
   `planner.cmd update --check`.
3. A major version waits for you: `planner.cmd update --allow-major` takes it when you
   are ready.
4. If a release that did go in looks wrong, `planner.cmd update --rollback` puts the
   previous one back.

`GUIDE.md` (Updates) explains the selfcheck and the regression limits in full.

## Develop

```
uv sync --frozen --extra dev
uv run pytest            # add -m "not engine" to skip the ~1 min engine case
uv run ruff check . && uv run ruff format --check . && uv run mypy planner tests
uv run --no-project python scripts/build_release.py   # dist/yearend-planner-<v>-win64.zip
uv run --no-project python scripts/refresh_zip_county.py   # rebuild config/zip_county.csv.gz from the Census file
```

CI runs the suite on Ubuntu and Windows and proves the release zip on a clean Windows
runner (`scripts/windows_proof.ps1`): real calculation, path with a space and non-ASCII
characters, moved folder, network blocked, standard user, cloud-sync folder refused.

## License

AGPL-3.0-or-later. PolicyEngine US is AGPL-3.0; this planner is distributed under the
same terms.
