#!/usr/bin/env python3
"""
raw2rinex.py

Python translation of the bash script raw2rinex.
Converts GNSS raw data files (Leica, Trimble, RINEX2, RINEX3, Hatanaka...)
into a single daily RINEX file, using teqc / ConvertoCPP / CRX2RNX / runpkr00.

Dependencies (external binaries, must be in $PATH):
    teqc, unzip, gunzip, CRX2RNX, runpkr00, cnvrnx3-rnx2-v3, ConvertoCPP (optional)

Author: F. Beauducel / DOMERAPI <beauducel@ipgp.fr> (original bash)
"""

import argparse
import fnmatch
import glob
import os
import shutil
import subprocess
import sys

from gnss_common import make_tmpdir, run_cmd


def has_converto():
    return shutil.which("ConvertoCPP") is not None


def resolve_raw_files(rawdir):
    """Resolves the input raw file list, equivalent to:
    `[ -d "$rawdir" ] && find $rawdir -type f || ls $rawdir`"""
    if os.path.isdir(rawdir):
        files = []
        for root, _dirs, fnames in os.walk(rawdir):
            for fn in fnames:
                files.append(os.path.join(root, fn))
        return sorted(files)
    files = []
    for pattern in rawdir.split():
        files.extend(sorted(glob.glob(pattern)))
    return [f for f in files if os.path.isfile(f)]


def stage_files(files, tmpdir, verbose=False):
    """Copies raw files into tmpdir, unzipping/gunzipping as needed."""
    for f in files:
        if f.endswith(".zip"):
            run_cmd(f'unzip -oq "{f}" -d "{tmpdir}"', verbose=verbose)
        else:
            shutil.copy(f, tmpdir, follow_symlinks=True)
            staged = os.path.join(tmpdir, os.path.basename(f))
            if staged.endswith(".gz"):
                run_cmd(f'gunzip -f "{staged}"', verbose=verbose)


def find_iname(tmpdir, *patterns):
    """Equivalent of `find $tmpdir -iname "pattern1" -o -iname "pattern2" ...`"""
    matches = []
    for root, _dirs, fnames in os.walk(tmpdir):
        for fn in fnames:
            if any(fnmatch.fnmatch(fn.lower(), p.lower()) for p in patterns):
                matches.append(os.path.join(root, fn))
    return sorted(matches)


def select_conversion_inputs(tmpdir, verbose=False):
    """Selects the raw/obs/nav files and the teqc/Converto conversion mode,
    mirroring the cascading `if [ ! -z "$rf" ]` blocks of the bash script.
    Each later block overwrites the selection if it finds matching files."""
    sel = dict(rawfiles=None, obsfiles=None, navfiles=None, teqcfmt="",
               skipteqc=None, skipconverto=None)

    # ------ RINEX2 ------
    rf = find_iname(tmpdir, "*.??o")
    if rf:
        print("   Found some RINEX2 files: proceeding...")
        for f in rf:
            with open(f, "r", errors="ignore") as fh:
                first_token = fh.readline().split()[:1]
            if first_token == ["3.01"]:
                run_cmd(f'cnvrnx3-rnx2-v3 "{f}" +0 g', verbose=verbose)
                gps_rnx2 = f + ".gps.rnx2"
                if os.path.isfile(gps_rnx2) and os.path.getsize(gps_rnx2) > 0:
                    with open(gps_rnx2, "r", errors="ignore") as gh, open(f, "w") as fh:
                        for line in gh:
                            if "PHASE SHIFTS" not in line:
                                fh.write(line)
        sel.update(rawfiles=rf, obsfiles=find_iname(tmpdir, "*.??o"),
                    navfiles=find_iname(tmpdir, "*.??n"), teqcfmt="",
                    skipteqc=0, skipconverto=1)

    # --- RINEX2 Hatanaka ---
    rf = find_iname(tmpdir, "*.??d.Z", "*.??d.gz", "*.??d")
    if rf:
        print("   Found some RINEX2 Hatanaka files: proceeding...")
        for f in rf:
            if f.endswith(".Z") or f.endswith(".gz"):
                run_cmd(f'gunzip -f "{f}"', verbose=verbose)
                f = os.path.splitext(f)[0]
            run_cmd(f'CRX2RNX "{f}" -f', verbose=verbose)
        rawfiles = find_iname(tmpdir, "*.??o")
        sel.update(rawfiles=rawfiles, obsfiles=rawfiles, navfiles=[], teqcfmt="",
                    skipteqc=0, skipconverto=1)

    # ------ RINEX3 ------
    rf = find_iname(tmpdir, "*.rnx")
    if rf:
        print("   Found some RINEX3 files: proceeding...")
        sel.update(rawfiles=rf, obsfiles=rf, teqcfmt="", skipteqc=1, skipconverto=0)

    # --- RINEX3 Hatanaka ---
    rf = find_iname(tmpdir, "*.crx.gz", "*.crx")
    if rf:
        print("   Found some RINEX3 Hatanaka files: proceeding...")
        for f in rf:
            if f.endswith(".gz"):
                run_cmd(f'gunzip -f "{f}"', verbose=verbose)
                f = os.path.splitext(f)[0]
            f2 = os.path.splitext(f)[0] + ".rnx"
            run_cmd(f'CRX2RNX < "{f}" > "{f2}"', verbose=verbose)
        sel.update(rawfiles=find_iname(tmpdir, "*.crx"),
                    obsfiles=find_iname(tmpdir, "*.rnx"), navfiles=[],
                    teqcfmt="", skipteqc=1, skipconverto=0)

    # ------ RAW manufacturer files ------
    rf = find_iname(tmpdir, "*.m??")
    if rf:
        print("   Found some Leica files: proceeding...")
        obsfiles = find_iname(tmpdir, "*.m??")
        sel.update(rawfiles=rf, obsfiles=obsfiles, navfiles=rf, teqcfmt="-lei mdb",
                    skipteqc=0, skipconverto=1)

    rf = find_iname(tmpdir, "*.T02")
    if rf:
        print("   Found some Trimble files: proceeding...")
        for f in rf:
            run_cmd(f'runpkr00 -g -d "{f}"', verbose=verbose)
        rawfiles = find_iname(tmpdir, "*.tgd")
        sel.update(rawfiles=rawfiles, obsfiles=rawfiles, navfiles=rawfiles,
                    teqcfmt="-tr d", skipteqc=0, skipconverto=1)

    return sel


def convert_to_rinex(tmpdir, sel, teqcopt, verbose=False):
    """Runs teqc or ConvertoCPP (or a plain copy) to produce the spliced
    RINEX file $tmpdir/rinex."""
    tmprnx = os.path.join(tmpdir, "rinex")
    obsfiles = " ".join(f'"{f}"' for f in (sel["obsfiles"] or []))

    if sel["skipteqc"] == 0:
        cmd = f'teqc {sel["teqcfmt"]} {teqcopt} {obsfiles} > "{tmprnx}"'
        run_cmd(cmd, verbose=verbose)
    elif has_converto() and sel["skipconverto"] == 0:
        cmd = f'ConvertoCPP {teqcopt} -cat -i {obsfiles} -o "{tmprnx}"'
        run_cmd(cmd, verbose=verbose)
    else:
        obsfiles_1st = (sel["obsfiles"] or [None])[0]
        if verbose:
            print("   RNX3: no teqc possible & no Converto available.")
            print("     > splicing/modding skipped, assuming header's ant/rec are correct!")
            print(f"     > 1st file only is kept: {obsfiles_1st}")
        if obsfiles_1st:
            shutil.copy(obsfiles_1st, tmprnx)
    return tmprnx


def write_output(outdir, sel, tmprnx, tmpdir, teqcopt, verbose=False):
    """Writes the resulting RINEX (and nav file if any) either as archived
    8.3 short-named files in a directory (case A), or as the exact given
    filename (case B, used by gnss_run_gipsyx)."""
    if os.path.isdir(outdir):
        if verbose:
            print("  case A: output **directory** specified")
        code = None
        rinex_base = None
        for f in sel["rawfiles"] or []:
            filebn = os.path.basename(f)
            code = filebn[:4]
            rinex_base = os.path.join(outdir, filebn[:8])
        if code is None:
            return
        code = code.upper()
        meta = subprocess.run(f'teqc +meta "{tmprnx}"', shell=True, capture_output=True, text=True).stdout
        starttime = ""
        for line in meta.splitlines():
            if "start date" in line:
                starttime = line.split(":", 1)[1].strip()
                break
        yy = starttime[2:4] if len(starttime) >= 4 else "00"
        target = f"{rinex_base}.{yy}o"
        print(f"   cp -f {tmprnx} {target}")
        shutil.copy(tmprnx, target)
        if sel["navfiles"]:
            tmpnav = os.path.join(tmpdir, "nav")
            navfiles = " ".join(f'"{f}"' for f in sel["navfiles"])
            run_cmd(f'teqc {sel["teqcfmt"]}n {teqcopt} {navfiles} > "{tmpnav}"', verbose=verbose)
            target_nav = f"{rinex_base}.{yy}n"
            if verbose:
                print(f"   cp -f {tmpnav} {target_nav}")
            shutil.copy(tmpnav, target_nav)
    else:
        if verbose:
            print("  case B: output **file** specified")
            print(f"   cp -f {tmprnx} {outdir}")
        shutil.copy(tmprnx, outdir)


def raw2rinex(rawdir, outdir, teqcopt="", verbose=False, tmpdirmain=None):
    """Main conversion routine, equivalent to running the raw2rinex script."""
    if has_converto():
        if verbose:
            print("INFO: ConvertoCPP found, RINEX3 files can be spliced")
    else:
        if verbose:
            print("WARN: ConvertoCPP not found, RINEX3 files can't be spliced")

    tmpdir = make_tmpdir(tmpdirmain or os.environ.get("TMPDIRMAIN"), "raw2rinex.")
    try:
        files = resolve_raw_files(rawdir)
        stage_files(files, tmpdir, verbose=verbose)

        sel = select_conversion_inputs(tmpdir, verbose=verbose)
        if not sel["rawfiles"]:
            print(f"No valid GNSS file found in {rawdir}... abort.")
            return 1

        tmprnx = convert_to_rinex(tmpdir, sel, teqcopt, verbose=verbose)
        write_output(outdir, sel, tmprnx, tmpdir, teqcopt, verbose=verbose)
        return 0
    finally:
        if not verbose:
            shutil.rmtree(tmpdir, ignore_errors=True)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="raw2rinex",
        description="GNSS raw data files convertion to daily RINEX 2.11.",
        epilog='NB1: multiple inputs: "input1 input2 ... inputN" (between double quotes)',
    )
    parser.add_argument("input", help="directory or filename of raw data (Leica, Trimble, Rinex), compressed or not")
    parser.add_argument("output", help="output directory (must exist) or output rinex filename")
    parser.add_argument("teqcoptions", nargs=argparse.REMAINDER,
                         help='any options to add to teqc (example: -O.dec 30 -O.rt "receiver" -O.at "antenna")')
    return parser


def main(argv):
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    teqcopt = " ".join(args.teqcoptions)
    verbose = bool(os.environ.get("VERBOSE"))

    return raw2rinex(args.input, args.output, teqcopt=teqcopt, verbose=verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
