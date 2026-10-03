# Templates

`forms/<form>.yaml`: one PDF parse template per form per layout. Each names the
form, the tax years the layout covers (`years`), the words that identify a page
(`match`, all must appear), optional `year_pattern` / `issuer_pattern` regexes,
and one regex per box with `AMOUNT` standing for a dollar figure (`group` picks
the capture when a row holds several amounts). A page is accepted only when
every `required` box parses; otherwise the file lands in `data/inbox/UNMATCHED/`
with the reason. A payer whose layout differs is a new template, not a code
change. Dashboard page templates land in Phase 5.
