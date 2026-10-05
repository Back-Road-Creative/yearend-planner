# Capability audit, October 2026 (Phase 10, unit 0a)

Read-only audit of `main` at `cb2c00e` (v0.1.0). It records what each declared capability rests on, what the tax engine can compute beyond what the planner uses, and the defects that the next units fix test-first. All examples are synthetic.

## Summary

- `config/capabilities.yaml` declares 83 capabilities: 77 `verified`, 5 `partial`, 1 `unsupported`.
- **56 of the 83 cite a whole test file, not a named test.** The guard (`tests/test_config.py::test_every_capability_row_cites_an_existing_test`) checks that the file exists, not that any test in it covers the row. 26 rows name at least one test.
- **5 rows name a hand-worked or official source** (Rev. Proc., IRS publication, CMS table, worksheet). The rest are checked against the engine or against the planner's own figures.
- The release fits one household: one person, North Carolina, no dependents. The engine already models far more (below), so most breadth work is wiring and proof, not new tax rules.

"Verified" in v0.1.0 therefore means "a test exists and passes". Unit 2b turns each row into a record with its scope, its evidence, and the commit it was last proven on.

## Engine coverage the planner does not use yet

From `policyengine_us` in the project environment (`CountryTaxBenefitSystem().variables`):

- **States:** an income-tax variable for 44 jurisdictions (43 states plus DC), local taxes (`local_income_tax`, `nyc_income_tax`, Yonkers, Wilmington), plus the 7 states without a wage income tax. Every state can get an engine estimate.
- **Credits:** `eitc`, `ctc`, `cdcc`, `american_opportunity_credit`, `lifetime_learning_credit`, `savers_credit`, `elderly_disabled_credit`, `foreign_tax_credit`, `residential_clean_energy_credit`, `new_clean_vehicle_credit`, `state_eitc`, `state_ctc`.
- **Income types:** `rental_income`, `partnership_s_corp_income`, `farm_income`, `unemployment_compensation`, `alimony_income`, `pension_income`, `taxable_pension_income`.
- **Benefits:** `snap`, `wic`, `chip`, `medicaid`, `ssi`, `free_school_meals`, `reduced_price_school_meals`, `lifeline`, `acp`, `msp` (Medicare Savings Programs).
- **Partial or absent:** LIHEAP (only some state programs), property-tax and renter credits (state-specific variables only, e.g. AZ, CT, MN), Part D Extra Help, QCDs, gambling income, Form 2210 penalty. These stay "Not handled" or are built by the planner from the cited rule.

## Findings and the unit that fixes each

Each was read in source at `cb2c00e`. None has a failing test yet; the fixing unit writes it first.

| Rank | ID | Defect | Where | Unit |
|---|---|---|---|---|
| 1 | F02 | Joint and head-of-household are accepted; the engine is given one person | `planner/engine/household.py:16`, one-member tax unit in `situation()` | 0b guard, 3a |
| 2 | F06 | "Spend cash first" sets cash against the planned **gain**, not the sale proceeds | `planner/plan/levers.py` `_spend_basis` | 1b |
| 3 | F08 | Cash line starts at the as-of balance, then replays the year from January; every positive bank row counts as business income | `planner/plan/glidepath.py` `months`, `glide` | 1d |
| 4 | F04 | One income export in any account hides transaction income in every other account | `planner/ingest/derive.py` `derive_year` | 1a |
| 5 | F07 | `whatif` skips the menu's overlap rule; `Lever.resized` has no upper bound | `planner/plan/levers.py` `whatif`, `resized` | 1c |
| 6 | F01 | Unknown amounts are priced as zero once the three required fields exist; mortgage and premium "counted as zero" | `planner/plan/inputs.py` `build`; `glidepath.months` | 1h, 2a |
| 7 | F10 | One 5-dollar tolerance for every regression value, including yes/no flags | `planner/engine/verify.py` `drifts` | 1e |
| 8 | F09 | ACA credit at 400.00–400.99% of the poverty line is 0 in the engine; pinned as a strict xfail | `tests/test_tax.py` `test_aca_400_cliff` | 1f |
| 9 | F15 | The tagged release runs the Windows proof without the synthetic inbox | `.github/workflows/release.yml` vs `ci.yml` | 1g |
| 10 | F14 | Marking every question "don't have" reads as "nothing more is needed" | `tests/test_serve.py::test_needed_reaches_zero_from_page_only` | 1g |
| 11 | F03 | No rest-of-year forecast per income stream; a typed `total_income` puts the leftover into wages | `planner/plan/inputs.py` | 2c |
| 12 | F05 | Conversion sizing caps by profile cap and IRA balance only, not the cash reserve | `planner/plan/conversion.py` `size` | 2d |
| 13 | F11 | Profile has spending, cash and conversion settings; no goals | `planner/config.py` profile fields | 4a |
| 14 | F12 | Long-term path grows one balance; no per-account tax, RMDs or debts | `planner/plan/glidepath.py` `run` | 4e |
| 15 | F17 | Updates check a sha256 published by the same feed as the zip: integrity, not proof of who published it | `planner/engine/update.py`, `planner/engine/feed.py` | 7d |
| 16 | F13 | The capability guard proves a cited file exists, not that it covers the row | `tests/test_config.py` | 2b |

## Drafted state returns (decision 1)

The 10 most populous states with a wage income tax are CA, NY, PA, IL, OH, GA, NC, MI, NJ and VA. NC is drafted already, so nine are new. Confirm the order against the current Census estimate when unit 3d-n starts.

## Capability evidence, row by row

| Capability | Declared | Evidence cited | Hand-worked source named |
|---|---|---|---|
| `federal_income_tax` | verified | whole file(s) only: test_tax.py, test_verify.py | no |
| `federal_ltcg_rates` | verified | whole file(s) only: test_tax.py | no |
| `self_employment_tax` | verified | 1 named test(s) | no |
| `qbi_deduction` | verified | 1 named test(s) | no |
| `nc_income_tax` | verified | whole file(s) only: test_tax.py, test_verify.py | no |
| `federal_total_tax_line_24` | verified | 1 named test(s) | no |
| `aca_magi_tax_exempt_interest` | verified | 1 named test(s) | no |
| `aca_premium_tax_credit` | partial | 3 named test(s) | yes |
| `aca_slcsp_county` | verified | 1 named test(s) | yes |
| `medicaid_expansion_magi` | verified | 1 named test(s) | no |
| `se_health_insurance_deduction` | verified | 2 named test(s) | yes |
| `pdf_text_1099_int_div_b_r_1095a` | verified | whole file(s) only: test_ingest.py | no |
| `pdf_filed_1040_and_schedules_1_2_3_c_d_se` | verified | whole file(s) only: test_forms.py | no |
| `pdf_nc_d400` | partial | 1 named test(s) | no |
| `pdf_ssa_statement` | verified | whole file(s) only: test_forms.py | no |
| `pdf_1099_nec_k_1098_5498_5498sa_1099sa` | verified | whole file(s) only: test_forms.py | no |
| `pdf_1099_r_distribution_code` | verified | 1 named test(s) | no |
| `pdf_1040_filing_status_state_zip` | partial | whole file(s) only: test_needs.py | no |
| `zip_to_county_crosswalk` | verified | whole file(s) only: test_derive.py | no |
| `mortgage_pi_from_two_1098s` | verified | 1 named test(s) | no |
| `ytd_facts_from_rows` | verified | whole file(s) only: test_derive.py | no |
| `pdf_scanned_ocr_confirm` | verified | whole file(s) only: test_ocr.py | no |
| `ocr_crop_beside_value_edit_before_accept` | verified | 2 named test(s) | no |
| `pdf_mixed_text_and_scanned_pages` | verified | 1 named test(s) | no |
| `zip_subfolders_nested_corrupt` | verified | 1 named test(s) | no |
| `needed_panel_registry_diff` | verified | whole file(s) only: test_needs.py | no |
| `needed_grouped_by_document` | partial | 2 named test(s) | no |
| `csv_vanguard_download` | verified | whole file(s) only: test_csv.py | no |
| `csv_vanguard_cost_basis_realized_income` | partial | 1 named test(s) | no |
| `csv_bank` | verified | whole file(s) only: test_csv.py | no |
| `ledger_accounts_balances_lots` | verified | whole file(s) only: test_portfolio.py | no |
| `roth_conversion_clock` | verified | whole file(s) only: test_portfolio.py | no |
| `magi_projection_lines` | verified | whole file(s) only: test_plan.py | no |
| `magi_watch_lines` | verified | 1 named test(s) | yes |
| `roth_conversion_candidates` | verified | whole file(s) only: test_plan.py | no |
| `spending_band` | verified | 1 named test(s) | no |
| `glide_path_and_monthly_cash` | verified | 1 named test(s) | no |
| `glide_cash_line_estimated_tax` | verified | whole file(s) only: test_esttax.py | no |
| `glide_cash_line_planned_flows` | verified | whole file(s) only: test_year.py | no |
| `withdraw_lots` | verified | 1 named test(s) | no |
| `wash_sales` | verified | 1 named test(s) | no |
| `estimated_tax` | verified | 1 named test(s) | no |
| `underpayment_penalty` | unsupported | rule the planner implements itself | no |
| `deadline_calendar` | verified | whole file(s) only: test_year.py | no |
| `year_end_plan_page` | verified | whole file(s) only: test_year.py | no |
| `plan_typed_fields` | verified | whole file(s) only: test_year.py | no |
| `conversion_spill_warning` | verified | whole file(s) only: test_plan.py | no |
| `lever_optimizer` | verified | whole file(s) only: test_levers.py | no |
| `whatif` | verified | whole file(s) only: test_levers.py | no |
| `threshold_drift` | verified | whole file(s) only: test_levers.py | no |
| `expected_forms` | verified | whole file(s) only: test_expected.py | no |
| `schedule_c` | verified | whole file(s) only: test_draft.py, test_schedule_c.py | no |
| `draft_return` | verified | whole file(s) only: test_draft.py | no |
| `schedule_1a` | verified | whole file(s) only: test_draft.py | no |
| `schedule_b` | verified | 1 named test(s) | no |
| `schedule_d` | verified | whole file(s) only: test_capgains.py | no |
| `form_8889` | verified | whole file(s) only: test_hsa.py | no |
| `nc_d400_draft` | verified | whole file(s) only: test_d400.py | no |
| `taxpack` | verified | whole file(s) only: test_package.py | no |
| `close_year` | verified | 1 named test(s) | yes |
| `dashboard_page` | verified | whole file(s) only: test_dashboard.py | no |
| `dashboard_glide_cash_band_tables` | verified | whole file(s) only: test_dashboard.py | no |
| `limits_refresh` | verified | whole file(s) only: test_limits.py | no |
| `update_check` | verified | whole file(s) only: test_feed.py | no |
| `engine_baseline` | verified | whole file(s) only: test_verify.py | no |
| `candidate_regression` | verified | whole file(s) only: test_privacy.py, test_update.py | no |
| `rollover` | verified | whole file(s) only: test_rollover.py | no |
| `planner_run` | verified | whole file(s) only: test_serve.py | no |
| `needed_items_closable_on_page` | verified | 1 named test(s) | no |
| `backup_restore` | verified | whole file(s) only: test_backup.py | no |
| `input_validation` | verified | whole file(s) only: test_validation.py | no |
| `stale_banner` | verified | whole file(s) only: test_validation.py | no |
| `release` | verified | whole file(s) only: test_privacy.py | no |
| `privacy_audit` | verified | whole file(s) only: test_privacy.py | no |
| `release_end_to_end` | verified | whole file(s) only: test_privacy.py | no |
| `scheduled_run` | verified | 1 named test(s) | no |
| `end_to_end` | verified | whole file(s) only: test_end_to_end.py | no |
| `double_click_run` | verified | whole file(s) only: test_launch.py | no |
| `single_writer_lock` | verified | whole file(s) only: test_paths.py | no |
| `planner_init` | verified | whole file(s) only: test_cli.py | no |
| `launcher_cloud_sync_warning` | verified | whole file(s) only: test_launch.py | no |
| `update_major_hold` | verified | whole file(s) only: test_feed.py, test_update.py | no |
| `update_tax_years_report` | verified | whole file(s) only: test_dashboard.py, test_update.py | no |

## How this was produced

- `config/capabilities.yaml` parsed line by line; test citations matched with the guard's own pattern (`tests/<file>.py[::<name>]`).
- Engine variables listed from `policyengine_us.CountryTaxBenefitSystem` in the project's locked environment.
- Findings read from source at `cb2c00e`; no test was run for this audit.
