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

`planner facts --year 2025 --form 1099-DIV` lists what the ledger holds. OCR confirm
for scanned pages lands in Phase 2e.

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
