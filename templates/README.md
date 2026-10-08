# Templates

`forms/<form>.yaml`: one PDF parse template per form per layout. Each names the
form, the tax years the layout covers (`years`), the words that identify a page
(`match`, all must appear), optional `year_pattern` / `issuer_pattern` regexes,
and one regex per box with `AMOUNT` standing for a dollar figure (`group` picks
the capture when a row holds several amounts). A page is accepted only when
every `required` box parses; otherwise the file lands in `data/inbox/UNMATCHED/`
with the reason. A payer whose layout differs is a new template, not a code
change. A template may set a literal `issuer` (the filed 1040 is `self`, the D-400
is `NC`, the 540 `CA`, the IT-201 `NY`, the PA-40 `PA`, the IL-1040 `IL`, the IT 1040 `OH`, Form 500 `GA`, the MI-1040 `MI`, the NJ-1040 `NJ`, Form 760 `VA`) instead of relying on `issuer_pattern`. Pages of one form that a template
splits (1040 page 1 and page 2, the 540's Sides 2-5, the IT-201's pages 2-4, the PA-40's Sides 1-2, the IL-1040's front and back, the IT 1040's pages 1 and 2, Form 500's pages 2-4, the MI-1040's pages 1-3, the NJ-1040's pages 1-3, Form 760's pages 1 and 2) merge when they share form, year and issuer. Dashboard
page templates land in Phase 5.

## CSV templates (`templates/csv/`)

One YAML per export layout. `match` lists the header names that must all be
present (case-insensitive) for the template to claim a block; `columns` maps the
planner's field names (`account`, `date`, `type`, `description`, `symbol`,
`quantity`, `price`, `amount`, `basis`, `acquired`, `term`, `txn_id`, or
`debit`/`credit` in place of `amount`) to the export's column names.
`date_format` defaults to `%m/%d/%Y`. A file with several header-led blocks
(Vanguard's download) matches each block on its own; a blank line followed by a data
row (a number, amount or date in some cell) carries on the block above, a blank line
followed by a header row starts a new one. Every template has a real-layout fixture
in `tests/fixtures/real/csv/` cited in its `layouts.yaml`, or an `unsourced:` entry
there saying why not. A line with one filled cell is a title or a note: the last one
above a header is the block's `[title]`, usable as a column (`account: "[title]"`), and
`title:` is an optional regex whose first group is kept ("for account (.+?) as of").
`skip:` maps a column to values whose rows are totals, not data (`Symbol: [Account
Total]`). "--", "n/a", "na" and "Incomplete" read as no value. A date "A as of B"
reads as B. When several templates claim a block, the one with the longest `match`
wins. A block no template claims
sends the file to UNMATCHED with the headers it found: add or edit a template,
drop the file again.
