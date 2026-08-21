#!/usr/bin/env python3
"""
stationinfo2json.py

Python translation of the bash script stationinfo2json.
Reads a Gamit station.info file and returns, for a given site and date,
the matching receiver/antenna record as JSON.

Author: F. Beauducel <beauducel@ipgp.fr> (original bash)
"""

import json
import sys
from datetime import datetime, timezone

# column widths of the station.info fixed-width format (gawk FIELDWIDTHS)
FIELD_WIDTHS = [6, 18, 5, 4, 3, 3, 4, 5, 4, 3, 3, 4, 9, 7, 9, 9, 21, 22, 7, 22, 24, 21]


def slice_fixed_width(line, widths):
    """Splits a fixed-width line into fields according to the given widths."""
    fields = []
    pos = 0
    for w in widths:
        fields.append(line[pos:pos + w])
        pos += w
    return fields


def stationinfo2json(file_path, site, date_str=None):
    """Returns the list of matching station.info records (as dicts) for the
    given site and date (YYYY-DDD HH:MM:SS)."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%j %H:%M:%S")

    results = []
    with open(file_path, "r", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\r\n")
            fields = slice_fixed_width(line, FIELD_WIDTHS)
            if len(fields) < len(FIELD_WIDTHS):
                continue
            try:
                start = "{:4d}-{:03d} {:02d}:{:02d}:{:02d}".format(
                    int(fields[2]), int(fields[3]), int(fields[4]), int(fields[5]), int(fields[6])
                )
                stop = "{:4d}-{:03d} {:02d}:{:02d}:{:02d}".format(
                    int(fields[7]), int(fields[8]), int(fields[9]), int(fields[10]), int(fields[11])
                )
            except ValueError:
                continue

            if fields[0].strip() == site and start <= date_str <= stop:
                results.append({
                    "mo": site,
                    "sn": fields[1].strip(),
                    "tw": f"{start} {stop}",
                    "pe": f"{fields[12].strip()} {fields[14].strip()} {fields[15].strip()}",
                    "hc": fields[13].strip(),
                    "rt": fields[16].strip(),
                    "rs": fields[17].strip(),
                    "rv": fields[18].strip(),
                    "rn": fields[19].strip(),
                    "at": fields[20].strip(),
                    "an": fields[21].strip(),
                })
    return results


def main(argv):
    if len(argv) < 2:
        print("      Syntax: stationinfo2json FILE SITE [DATE]")
        print(" Description: reads station.info file and returns data as JSON")
        print("   Arguments: FILE = filename of station.info")
        print("              SITE = site code")
        print("              DATE = date as YYYY-DDD [HH:MM:SS] (default is current time)")
        print("")
        return 0

    file_path = argv[0]
    site = argv[1]
    date_str = argv[2] if len(argv) > 2 else None

    for record in stationinfo2json(file_path, site, date_str):
        print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
