# config/zip_county.csv.gz: where it comes from

Source: U.S. Census Bureau, 2020 Census ZIP Code Tabulation Area (ZCTA) to County
Relationship File, `tab20_zcta520_county20_natl.txt`.
URL: https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt
Index page: https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.2020.html
Downloaded 2026-10-03 (SHA-256 of the file as downloaded: `3ed41278d637dc249e0323306f68be8a6c234e3090f4de88ef328dee71aeaaaf`).

Reduced by `scripts/refresh_zip_county.py` to 46,953 rows with the columns
`zcta,county_fips,county_name,state,land_area_share`: the land area of each
ZCTA-county overlap as a share of the ZCTA's land area, water-only overlaps
dropped. Rows are sorted and the gzip carries no timestamp, so a refresh that
finds the same file changes nothing.

A ZCTA approximates a USPS ZIP Code area; a ZIP with no ZCTA (a PO Box or
single-building ZIP) is not in the file. The planner treats a ZCTA with more than
one county as a question for the household, never a guess.

To refresh: `python scripts/refresh_zip_county.py`, review the diff, commit.
