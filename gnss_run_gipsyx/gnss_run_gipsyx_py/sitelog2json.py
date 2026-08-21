#!/usr/bin/env python3
"""
sitelog2json.py

Python translation of the bash script sitelog2json.
Reads a sitelog file (or directory of sitelogs) and returns site metadata
(receiver, antenna, position, observer, agency) valid at a given date, as JSON.

Author: F. Beauducel <beauducel@ipgp.fr> (original bash)
"""

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone


def sed_range(lines, start_regex, end_regex):
    """Equivalent of `sed -n "/start/,/end/p"`: returns the concatenation of
    all line ranges starting at a line matching start_regex and ending at
    the next line matching end_regex (inclusive)."""
    out = []
    in_range = False
    start_re = re.compile(start_regex)
    end_re = re.compile(end_regex)
    for line in lines:
        if not in_range and start_re.search(line):
            in_range = True
            out.append(line)
        elif in_range:
            out.append(line)
            if end_re.search(line):
                in_range = False
    return out


def strip_after_colon(line):
    """Equivalent of `sed 's/^.*: //g'`: keeps only the text after the last ': '."""
    return re.sub(r"^.*: ", "", line).rstrip("\r\n")


def group_records(values, group_size):
    """Equivalent of `paste -d '\\t' - - - ...`: groups consecutive values
    into fixed-size records, dropping any incomplete trailing group."""
    return [values[i:i + group_size] for i in range(0, len(values) - group_size + 1, group_size)]


def find_sitelog_file(path, site):
    """Selects the most recent sitelog file for the given site code (last one
    once sorted by the date suffix in the filename)."""
    if os.path.isdir(path):
        candidates = sorted(glob.glob(os.path.join(path, f"{site}*_????????.log")))
    else:
        candidates = sorted(glob.glob(path))
    if not candidates:
        return None
    # sort like `sort -t '_' -k 2` (sort by the field following the first '_')
    def sort_key(f):
        base = os.path.basename(f)
        parts = base.split("_")
        return parts[1] if len(parts) > 1 else base
    candidates.sort(key=sort_key)
    return candidates[-1]


def sitelog2json(file_arg, site_arg, date_arg=None):
    """Returns a dict of site metadata valid at date_arg, or None if the
    sitelog / site code could not be found (mirrors the bash script's
    silent exit)."""
    if date_arg:
        date_str = date_arg
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    site = site_arg.lower()[:4]
    site_upper = site_arg.upper()[:4]

    sitelog_file = find_sitelog_file(file_arg, site)
    if not sitelog_file or not os.path.isfile(sitelog_file):
        return None

    with open(sitelog_file, "r", errors="ignore") as f:
        lines = f.readlines()

    if not any(re.search(rf"(Four|Nine) Character ID.*: {re.escape(site_upper)}", l) for l in lines):
        return None

    result = {}

    # site monument code (4 or 9 characters)
    section = sed_range(lines, r"^1\. ", r"Character ID")
    if section:
        result["mo"] = strip_after_colon(section[-1])

    # site name
    section = sed_range(lines, r"^1\. ", r"Site Name")
    if section:
        result["sn"] = strip_after_colon(section[-1])

    # approximate position (WGS84 XYZ, m)
    section = sed_range(lines, r"^2\. ", r"Z coordinate")
    if section:
        result["px"] = "\n".join(strip_after_colon(l) for l in section[-3:])

    # receiver: 7 fields per record; keep record whose install/removal dates bracket date_str
    section = sed_range(lines, r"^3\.[1-9]\. ", r"Date Removed")
    values = [strip_after_colon(l) for l in section]
    for rec in group_records(values, 7):
        rt, rv, rn, _, _, installed, removed = rec
        if installed <= date_str <= removed:
            result["rt"] = rt
            result["rv"] = rv
            result["rn"] = rn
            break

    # antenna: 13 fields per record
    section = sed_range(lines, r"^4\.[1-9]\. ", r"Date Removed")
    values = [re.sub(r"\s*NONE", "", strip_after_colon(l)) for l in section]
    for rec in group_records(values, 13):
        at, an = rec[0], rec[1]
        height, east, north = rec[3], rec[4], rec[5]
        installed, removed = rec[11], rec[12]
        if installed <= date_str <= removed:
            result["at"] = at
            result["an"] = an
            try:
                result["pe"] = f"{float(height):1.4f} {float(east):1.4f} {float(north):1.4f}"
            except ValueError:
                result["pe"] = f"{height} {east} {north}"
            break

    # observer
    section = sed_range(lines, r"^11\. ", r"Abbreviation")
    if section:
        result["op"] = strip_after_colon(section[-1])

    # agency
    section = sed_range(lines, r"^12\. ", r"Abbreviation")
    if section:
        result["ag"] = strip_after_colon(section[-1])

    return result


def main(argv):
    if len(argv) < 2:
        print("      Syntax: sitelog2json FILE SITE [DATE]")
        print(" Description: reads sitelog file(s) and returns data as JSON")
        print("   Arguments: FILE = path or filename(s) of sitelog(s)")
        print("              SITE = site code (4 or 9 characters)")
        print("              DATE = date as YYYY-MM-DD[Thh:mm] (default is current time)")
        print("     Outputs: JSON string with following fields:")
        print("              mo = monument site code (4 characters)")
        print("              sn = site name")
        print("              px = approximate position (WGS84xyz,m)")
        print("              rt = receiver type")
        print("              rv = receiver version")
        print("              rn = receiver S/N")
        print("              at = antenna type")
        print("              an = antenna S/N")
        print("              pe = antenna Marker->ARP (hEN,m)")
        print("              op = operator")
        print("              ag = agency")
        print("")
        return 0

    file_arg = argv[0]
    site_arg = argv[1]
    date_arg = argv[2] if len(argv) > 2 else None

    result = sitelog2json(file_arg, site_arg, date_arg)
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
