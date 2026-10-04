"""Rebuild ``config/zip_county.csv.gz`` from the Census Bureau's 2020 ZCTA-to-county
relationship file, and rewrite ``config/zip_county.SOURCE.md`` beside it.

    python scripts/refresh_zip_county.py                 # download, reduce, write
    python scripts/refresh_zip_county.py --from-file tab20_zcta520_county20_natl.txt

The relationship file lists, for every ZIP Code Tabulation Area (ZCTA) and county
that overlap, the land and water area of the overlap. This keeps the land area only
and writes each ZCTA's share of it per county, so a ZIP that lies in one county has
one row at 1.0 and a ZIP that straddles two or more has a row per county. A ZCTA is
the Census approximation of a USPS ZIP Code area, not the ZIP itself; the planner
never treats a split ZCTA as one county (see ``planner/ingest/derive.py``).

Output is byte-for-byte reproducible for the same input (sorted rows, no gzip
timestamp), so a refresh that changes nothing produces no diff.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import sys
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
PAGE = (
    "https://www.census.gov/geographies/reference-files/time-series/geo/"
    "relationship-files.2020.html"
)
FILE_NAME = "tab20_zcta520_county20_natl.txt"
HEADER = ("zcta", "county_fips", "county_name", "state", "land_area_share")

# Census ANSI/FIPS state codes (the first two digits of a county FIPS code), from
# "ANSI/FIPS Codes for States and the District of Columbia" and the island-area
# codes, https://www.census.gov/library/reference/code-lists/ansi.html. The test
# suite checks every code against policyengine-us's own county table.
STATE_BY_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
    "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}  # fmt: skip

Row = tuple[str, str, str, str, float]


def reduce(text: str) -> list[Row]:
    """(zcta, county_fips, county_name, state, land_area_share) rows, sorted.

    Raises ``ValueError`` on a file whose columns are not the ones this reads or a
    county in a state this table does not know: a changed layout is an error to
    look at, never a silently wrong crosswalk."""
    lines = text.lstrip("﻿").splitlines()
    columns = lines[0].split("|") if lines else []
    want = ("GEOID_ZCTA5_20", "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20", "AREALAND_PART")
    if any(c not in columns for c in want):
        raise ValueError(f"expected columns {', '.join(want)}; got {columns}")
    at = {c: columns.index(c) for c in want}
    land: dict[str, dict[str, int]] = defaultdict(dict)
    names: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split("|")
        zcta = cells[at["GEOID_ZCTA5_20"]].strip()
        if not zcta:  # the part of a county that lies in no ZCTA
            continue
        fips = cells[at["GEOID_COUNTY_20"]].strip()
        if fips[:2] not in STATE_BY_FIPS:
            raise ValueError(f"county {fips}: no state for FIPS prefix {fips[:2]}")
        area = int(cells[at["AREALAND_PART"]] or 0)
        if area > 0:  # a sliver of water is not where anyone lives
            land[zcta][fips] = area
            names[fips] = cells[at["NAMELSAD_COUNTY_20"]].strip()
    rows: list[Row] = []
    for zcta in sorted(land):
        total = sum(land[zcta].values())
        for fips in sorted(land[zcta]):
            share = round(land[zcta][fips] / total, 6)
            rows.append((zcta, fips, names[fips], STATE_BY_FIPS[fips[:2]], share))
    return rows


def write_crosswalk(rows: list[Row], path: Path) -> None:
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    out.writerow(HEADER)
    out.writerows(rows)
    with path.open("wb") as raw:
        # mtime 0 and no stored name: the same rows always give the same bytes
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(buf.getvalue().encode("utf-8"))


def source_note(downloaded: str, sha256: str, rows: int) -> str:
    return f"""# config/zip_county.csv.gz: where it comes from

Source: U.S. Census Bureau, 2020 Census ZIP Code Tabulation Area (ZCTA) to County
Relationship File, `{FILE_NAME}`.
URL: {URL}
Index page: {PAGE}
Downloaded {downloaded} (SHA-256 of the file as downloaded: `{sha256}`).

Reduced by `scripts/refresh_zip_county.py` to {rows:,} rows with the columns
`zcta,county_fips,county_name,state,land_area_share`: the land area of each
ZCTA-county overlap as a share of the ZCTA's land area, water-only overlaps
dropped. Rows are sorted and the gzip carries no timestamp, so a refresh that
finds the same file changes nothing.

A ZCTA approximates a USPS ZIP Code area; a ZIP with no ZCTA (a PO Box or
single-building ZIP) is not in the file. The planner treats a ZCTA with more than
one county as a question for the household, never a guess.

To refresh: `python scripts/refresh_zip_county.py`, review the diff, commit.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--from-file", type=Path, help=f"a local copy of {FILE_NAME}")
    ap.add_argument("--config", type=Path, default=CONFIG, help="output folder")
    args = ap.parse_args(argv)
    if args.from_file:
        raw = args.from_file.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=120) as resp:  # noqa: S310  (fixed https URL)
            raw = resp.read()
    rows = reduce(raw.decode("utf-8"))
    args.config.mkdir(parents=True, exist_ok=True)
    write_crosswalk(rows, args.config / "zip_county.csv.gz")
    today = datetime.now(UTC).date().isoformat()
    note = source_note(today, hashlib.sha256(raw).hexdigest(), len(rows))
    (args.config / "zip_county.SOURCE.md").write_text(note, encoding="utf-8")
    print(f"wrote {len(rows):,} rows for {len({r[0] for r in rows}):,} ZCTAs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
