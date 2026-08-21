#!/usr/bin/env python3
"""
download_orbit.py

Python translation of the bash script download_orbit.
Downloads the best available GNSS orbit product (Final, Rapid, Rapid_GE or
Ultra) from the JPL secured web server, for a range of days.

Needs the companion file download_orbit.yml (list of product file suffixes),
and the external `wget` binary.

Authors: Edgar Lenhof, Francois Beauducel, Pierre Sakic (original bash)
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta

from gnss_common import build_day_list, load_yaml_config, parse_ymd, run_cmd

VALID_ORBITS = ("Ultra", "Rapid", "Final", "Rapid_GE")
DEFAULT_ORBITS = ["Final", "Rapid", "Ultra", "Rapid_GE"]


def delete_old_orbits(dest, older_than_days, verbose=False):
    """Deletes orbit files older than older_than_days (equivalent to
    `find $DEST -type f -mtime +$nb_delete`)."""
    cutoff = time.time() - older_than_days * 86400
    for root, _dirs, fnames in os.walk(dest):
        for fn in fnames:
            path = os.path.join(root, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    if verbose:
                        print(f"removed {path}")
            except OSError:
                continue



def download_orbit(days, dest, orbits=None, start_dates=None, delete_older_than=None,
                    verbose=False, extensions_file=None):
    """Downloads orbit products for the given days/orbit types into dest."""
    orbits = orbits or list(DEFAULT_ORBITS)

    if delete_older_than is not None:
        delete_old_orbits(dest, delete_older_than, verbose=verbose)

    day_list = build_day_list(days, start_dates)

    extensions = None
    if extensions_file and os.path.isfile(extensions_file):
        config = load_yaml_config(extensions_file)
        extensions = config.get("extensions")

    quiet_flag = "" if verbose else "-q"

    for day in day_list:
        year = day.strftime("%Y")
        month = day.strftime("%m")
        dd = day.strftime("%d")

        for orbit in orbits:
            local = os.path.join(dest, orbit, year, f"{year}-{month}-{dd}.eo.gz")

            if not os.path.isfile(local) or orbit == "Ultra":
                print(f"   Downloading orbit {orbit} for {year}-{month}-{dd}...", end="")
                url = f"https://sideshow.jpl.nasa.gov/pub/JPL_GNSS_Products/{orbit}/{year}/"
                out_dir = os.path.join(dest, orbit, year)
                os.makedirs(out_dir, exist_ok=True)

                if extensions:
                    for ext in extensions:
                        rc = run_cmd(
                            f'wget --no-check-certificate {quiet_flag} -N -P "{out_dir}" -r -l1 -nd '
                            f'"{url}/{year}-{month}-{dd}{ext}"',
                            verbose=verbose,
                        )
                        if rc != 0:
                            break
                else:
                    run_cmd(
                        f'wget --no-check-certificate {quiet_flag} -N -P "{out_dir}" -r -l1 -nd '
                        f'"{url}" -A "{year}-{month}-{dd}*"',
                        verbose=verbose,
                    )

                if os.path.isfile(local):
                    print(" OK.")
                    break
                else:
                    print(" not yet available!")
            else:
                print(f"   Orbit {orbit} for {dd}-{month}-{year} is locally available.")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="download_orbit",
        description=(
            "Downloads best available orbit for chosen dates from the JPL secured "
            "web server (see https://sideshow.jpl.nasa.gov/pub/JPL_GNSS_Products)."
        ),
        epilog="Needs the companion file download_orbit.yml",
    )
    parser.add_argument("days", type=int, help="number of days to process (from start day)")
    parser.add_argument("dest", help="root directory where orbits shall be saved")
    parser.add_argument("-o", dest="orbit", choices=VALID_ORBITS,
                         help="type of orbit (Ultra, Rapid, Final, or Rapid_GE)")
    parser.add_argument("-d", dest="start_days", metavar="STARTDAY[,STARTDAY...]",
                         help="days to start retrieving (YYYY/mm/dd), comma separated")
    parser.add_argument("-r", dest="remove_older_than", type=int, metavar="DAYS",
                         help="remove orbit files older than DAYS days")
    parser.add_argument("-v", dest="verbose", action="store_true", help="verbose mode")
    return parser


def main(argv):
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    progdir = os.path.dirname(os.path.abspath(__file__))
    extensions_file = os.path.join(progdir, "download_orbit.yml")

    orbits = [args.orbit] if args.orbit else list(DEFAULT_ORBITS)
    start_dates = [parse_ymd(d) for d in args.start_days.split(",")] if args.start_days else None

    download_orbit(args.days, args.dest, orbits=orbits, start_dates=start_dates,
                    delete_older_than=args.remove_older_than, verbose=args.verbose,
                    extensions_file=extensions_file)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
