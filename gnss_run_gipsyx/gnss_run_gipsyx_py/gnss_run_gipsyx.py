#!/usr/bin/env python3
"""
gnss_run_gipsyx.py

Python translation of the bash script gnss_run_gipsyx.
Runs the automatic GNSS process from raw files to position solution with
GipsyX (gd2e.py), for one or several stations and days.

Dependencies:
    - teqc (3rd party binary, must be in $PATH)
    - raw2rinex.py, download_orbit.py, sitelog2json.py, stationinfo2json.py
      (companion modules, in the same directory)
    - GipsyX (gd2e.py, netApply.py, netApplyNonLinear.py), sourced through
      the gipsyx_rc script given in the YAML configuration
    - 7z to save full logs (fullog option)

Authors: Francois Beauducel, Edgar Lenhof, Patrice Boissier, Pierre Sakic
(original bash script)
"""

import argparse
import glob
import gzip
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timedelta

from gnss_common import (build_day_list, check_home_partition_usage,
                          expand_fmt, get_node_param, get_nodes_from_grid,
                          load_yaml_config, make_tmpdir, parse_ymd, run_cmd)

import raw2rinex as raw2rinex_mod
import sitelog2json as sitelog2json_mod
import stationinfo2json as stationinfo2json_mod
import download_orbit as download_orbit_mod

LOCKFILE = "/tmp/gnss_run_gipsyx.txt"


# --------------------------------------------------------------------------
# lock file management
# --------------------------------------------------------------------------

def is_already_running(lockfile=LOCKFILE):
    if not os.path.isfile(lockfile):
        return False
    try:
        with open(lockfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def write_lockfile(lockfile=LOCKFILE):
    with open(lockfile, "w") as f:
        f.write(str(os.getpid()))


def remove_lockfile(lockfile=LOCKFILE):
    if os.path.isfile(lockfile):
        os.remove(lockfile)


# --------------------------------------------------------------------------
# GipsyX command execution (through the sourced gipsyx_rc environment)
# --------------------------------------------------------------------------

def run_gipsyx_cmd(gipsyx_rc, cmd, log_file=None, debug=False):
    """Runs a GipsyX command (gd2e.py, netApply.py...) inside a bash
    subshell that has sourced gipsyx_rc first (equivalent of the bash
    script itself having done `source rc_GipsyX.sh`)."""
    full_cmd = f'source "{gipsyx_rc}" > /dev/null 2>&1; {cmd}'
    if debug:
        print(f"   {cmd}")
        return subprocess.run(["bash", "-c", full_cmd]).returncode
    if log_file:
        with open(log_file, "a") as lf:
            return subprocess.run(["bash", "-c", full_cmd], stdout=lf, stderr=subprocess.STDOUT).returncode
    return subprocess.run(["bash", "-c", full_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def gipsyx_env_var(gipsyx_rc, var_name):
    """Reads an environment variable (e.g. GOA_VAR) as set by gipsyx_rc."""
    full_cmd = f'source "{gipsyx_rc}" > /dev/null 2>&1; echo "${var_name}"'
    proc = subprocess.run(["bash", "-c", full_cmd], capture_output=True, text=True)
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# node / site-information resolution
# --------------------------------------------------------------------------

def resolve_nodes(config, cli_stations=None):
    if cli_stations:
        return cli_stations, {}, None

    nodes = config.get("nodes")
    grid = config.get("grid")
    noderoot = config.get("noderoot")

    if grid:
        nodes, nodes_table = get_nodes_from_grid(grid, noderoot)
        proc_dir = noderoot if (noderoot and os.path.isdir(noderoot)) else f"/etc/webobs.d/GRIDS2NODES/{grid}."
        return nodes, nodes_table, proc_dir

    if isinstance(nodes, str):
        nodes = nodes.split()
    return nodes or [], {}, None


def get_node_receiver_antenna(config, grid, proc_dir, nodes_table, fid):
    """Node-level (WebObs) receiver/antenna, used as the base header
    override before station_info/sitelog are applied."""
    receiver, antenna = "", ""
    if grid:
        node_id = None
        for nid, node_fid in nodes_table.items():
            if node_fid == fid:
                node_id = nid
                break
        if node_id:
            receiver = get_node_param(grid, proc_dir, node_id, "FID_RECEIVER")
            antenna = get_node_param(grid, proc_dir, node_id, "FID_ANTENNA")
            print(f"   WO ID = {node_id} - receiver = '{receiver}' - antenna = '{antenna}' "
                  f"(will use rinex header if empty)")
    return receiver, antenna


def apply_station_info_override(config, fid, yyyy, doy, header):
    station_info = config.get("station_info")
    if not (station_info and os.path.isfile(station_info) and os.path.getsize(station_info) > 0):
        return header, ""
    records = stationinfo2json_mod.stationinfo2json(station_info, fid, f"{yyyy}-{doy}")
    receiver = records[0].get("rt", "") if records else ""
    antenna = records[0].get("at", "") if records else ""
    header = dict(header, receiver=receiver, anttype=antenna)
    alert = (f"   station.info: {fid} @ {yyyy}-{doy} - receiver = '{receiver}' - antenna = '{antenna}' "
             f"(will use rinex header if empty)")
    return header, alert


def apply_sitelog_override(config, fid, ymd_dash, header):
    sitelog = config.get("sitelog")
    if not sitelog:
        return header, ""
    result = sitelog2json_mod.sitelog2json(sitelog, fid, ymd_dash) or {}
    header = dict(header, receiver=result.get("rt", ""), anttype=result.get("at", ""),
                  posxyz=result.get("px", ""))
    alert = (f"   sitelog: {fid} @ {ymd_dash} - receiver = '{header['receiver']}' "
             f"- antenna = '{header['anttype']}' (will use rinex header if empty)")
    return header, alert


def build_teqc_header_opts(header):
    opts = ""
    if header.get("receiver"):
        opts += f' -O.rt "{header["receiver"]}"'
    if header.get("anttype"):
        opts += f' -O.at "{header["anttype"]}"'
    if header.get("posxyz"):
        opts += f' -O.px {" ".join(header["posxyz"].split())}'
    return opts


# --------------------------------------------------------------------------
# CLI options
# --------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="gnss_run_gipsyx",
        description="Runs the automatic GNSS process from raw files to position solution.",
        epilog=(
            'Example: gnss_run_gipsyx CONF 1 -d 2017/03/17,2018/08/05\n'
            'will compute 2017/03/17, 2017/03/16, 2018/08/05 and 2018/08/04.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("conf", help="configuration filename (YAML, see gnss_run_gipsyx_template.yml)")
    parser.add_argument("days", type=int, help="number of days to process (from today)")
    parser.add_argument("-s", dest="stations", metavar='"STA1 STA2..."',
                         help="station code or station list, default is all nodes defined in CONF nodes:")
    parser.add_argument("-d", dest="start_days", metavar="yyyy/mm/dd[,yyyy/mm/dd]",
                         help="choose day(s) to start process; DAYS still applies to previous days")

    orbit_group = parser.add_mutually_exclusive_group()
    orbit_group.add_argument("-final", dest="orbit_choice", action="store_const", const="final",
                              help="use only final orbit")
    orbit_group.add_argument("-rapid", dest="orbit_choice", action="store_const", const="rapid",
                              help="use only rapid orbit")
    orbit_group.add_argument("-ultra", dest="orbit_choice", action="store_const", const="ultra",
                              help="use only ultra orbit")

    parser.add_argument("-force", dest="force", action="store_true",
                         help="forces the process despite existence of final results")
    parser.add_argument("-lock", dest="lock", action="store_true",
                         help="creates a lock file to prevent multiple process of gnss_run_gipsyx")
    parser.add_argument("-debug", dest="debug", action="store_true",
                         help="verbose mode & temporary folders will not be deleted")
    parser.add_argument("-fullog", dest="fullog", action="store_true",
                         help="full log record, temporary folders stored in a .fullog.7z file (7-zip needed)")
    return parser


ORBIT_CHOICE_TO_ORBITS = {"final": ["flinn"], "rapid": ["ql"], "ultra": ["ultra"]}
ORBIT_CHOICE_MESSAGE = {
    "final": "Will use only final orbit",
    "rapid": "Will use only rapid orbit",
    "ultra": "Will use only ultra orbit",
}


def parse_options(args):
    """Builds the opts dict used throughout the script from a parsed
    argparse.Namespace."""
    opts = dict(orbits=["flinn", "ql", "ultra"], force=args.force, debug=args.debug,
                fullog=args.fullog, stations=None, start_dates=None, lock=args.lock)

    if args.orbit_choice:
        opts["orbits"] = ORBIT_CHOICE_TO_ORBITS[args.orbit_choice]
        print(ORBIT_CHOICE_MESSAGE[args.orbit_choice])
    if args.force:
        print("Force computation despites final orbit results already exist")
    if args.debug:
        os.environ["VERBOSE"] = "1"
        print("Debug mode: temporary folders will NOT be deleted!")
    if args.fullog:
        print("Full log record: temporary folders will be stored in zip files")
    if args.stations:
        opts["stations"] = args.stations.split()
    if args.start_days:
        opts["start_dates"] = [parse_ymd(d) for d in args.start_days.split(",")]

    return opts


# --------------------------------------------------------------------------
# gd2e / orbit processing for a single station-day
# --------------------------------------------------------------------------

def orbit_product_name(orbit):
    return {"flinn": "Final", "ql": "Rapid", "ultra": "Ultra"}.get(orbit)


def try_orbits(config, opts, fid, ymd, rinex, tmpdir, gipsyres, log_path, verbose):
    """Loops through the requested orbit precisions (final -> rapid ->
    ultra), running gd2e.py until one succeeds. Writes gipsyres (or
    gipsyres.<orbit> for non-final orbits). Returns True on success."""
    gipsyx_rc = config.get("gipsyx_rc")
    orbitsdir = config.get("orbitsdir")
    download_orbit_enabled = config.get("download_orbit", True)
    download_options = config.get("download_options", "")
    gipsyoptions = config.get("gipsyoptions", "")
    nforb = bool(config.get("nforb", False))
    cm2cf = bool(config.get("cm2cf", False))
    antex = config.get("antex")
    trop_tdp = bool(config.get("trop_tdp", False))
    debug = opts["debug"]

    tdp = os.path.join(tmpdir, "smoothFinal.tdp")
    cov = os.path.join(tmpdir, "smoothFinal.gdcov")

    for orbit in opts["orbits"]:
        if orbit == "ql" and os.path.isfile(f"{gipsyres}.{orbit}") and os.path.getsize(f"{gipsyres}.{orbit}") > 0 \
                and not opts["force"]:
            print(f"   file {gipsyres} [{orbit}] already exists...")
            break

        nforb_loop = False
        orbit_opt = orbit
        orbit_ok = None

        if orbitsdir:
            product = orbit_product_name(orbit)
            if orbit == "flinn":
                nforb_loop = nforb

            if download_orbit_enabled:
                delete_older_than = None
                opt_parts = download_options.split()
                if "-r" in opt_parts:
                    delete_older_than = int(opt_parts[opt_parts.index("-r") + 1])
                download_orbit_mod.download_orbit(
                    0, orbitsdir, orbits=[product], start_dates=[ymd],
                    delete_older_than=delete_older_than, verbose=verbose,
                    extensions_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_orbit.yml"),
                )

            eo_path = os.path.join(orbitsdir, product, ymd.strftime("%Y"),
                                    f'{ymd.strftime("%Y-%m-%d")}.eo.gz')
            if os.path.isfile(eo_path):
                orbit_ok = True
            else:
                continue
            orbit_opt = os.path.join(orbitsdir, product)

        nforb_opts = "-prodTypeGNSS nf -gdCov" if nforb_loop else ""
        cm2cf_opts = "-gdCov" if (cm2cf and not nforb_loop) else ""
        atx_opts = f"-antexFile {antex}" if antex else ""

        cmd = f"gd2e.py -rnxFile {rinex} -GNSSproducts {orbit_opt} {gipsyoptions} {nforb_opts} {cm2cf_opts} {atx_opts}"
        print(f"   {cmd}")
        rc = run_gipsyx_cmd(gipsyx_rc, cmd, log_file=log_path, debug=debug)

        if rc != 0 or not (os.path.isfile(tdp) and os.path.getsize(tdp) > 0):
            print(f"   {cmd}")
            if orbit_ok is None and orbit != "ultra":
                print(f"   ** WARNING: Problem to process gd2e... May be orbit {orbit} not yet available?")
                continue
            else:
                print("   ** ERROR: Problem to process gd2e... Please check logs.")
                error_regex = config.get("error_regex_rinex", "REC #|ANT #|# / TYPES OF OBSERV|MARKER NAME")
                grep_rinex_errors(rinex, error_regex)
                return False

        label = ""
        cov_current = cov

        if cm2cf:
            print("   Apply Center-of-Mass > Center-of-Figure transformation")
            shutil.copy(cov, cov + "_cm")
            goa_var = gipsyx_env_var(gipsyx_rc, "GOA_VAR")
            cmd_cm2cf = f"netApplyNonLinear.py {cov} -cmFile {goa_var}/sta_info/IGS20.cm -reverse"
            run_gipsyx_cmd(gipsyx_rc, cmd_cm2cf, log_file=log_path, debug=debug)
            shutil.copy(cov, cov + "_cf")
            label += ".CF"

        if nforb_loop:
            print("   Apply Non-Fiducial > Fiducial Helmert transformation")
            product = orbit_product_name(orbit)
            trsprm = os.path.join(orbitsdir, product, ymd.strftime("%Y"), f'{ymd.strftime("%Y-%m-%d")}.x.gz')
            cmd_trans = f"netApply.py -t -r -s -i {cov} -o {cov}_trs -x {trsprm}"
            print(f"   {cmd_trans}")
            run_gipsyx_cmd(gipsyx_rc, cmd_trans, log_file=log_path, debug=debug)
            shutil.move(cov, cov + "_nf")
            shutil.copy(cov + "_trs", cov)
            label += ".NFtrs"

        for old in glob.glob(f"{gipsyres}.*"):
            os.remove(old)

        if not nforb_loop and not cm2cf:
            write_gipsyres_simple(tdp, gipsyres)
        else:
            write_gipsyres_from_cov(cov, gipsyres, label)

        if trop_tdp:
            append_trop_tdp(tdp, gipsyres)

        print(f"==> {gipsyres} [{orbit}] written.")
        if orbit != "flinn":
            shutil.move(gipsyres, f"{gipsyres}.{orbit}")
        return True

    return False


def grep_rinex_errors(rinex, error_regex):
    import re
    if not os.path.isfile(rinex):
        return
    pattern = re.compile(error_regex)
    with open(rinex, "r", errors="ignore") as f:
        for line in f:
            if pattern.search(line):
                print(line.rstrip("\n"))


def write_gipsyres_simple(tdp, gipsyres):
    """Simple case: no fiducial orbit nor CM2CF -> take the last X/Y/Z
    position lines directly from the .tdp file."""
    lines = []
    if os.path.isfile(tdp):
        with open(tdp, "r", errors="ignore") as f:
            for line in f:
                if ".State.Pos." in line and any(f".State.Pos.{ax}" in line for ax in "XYZ"):
                    lines.append(line)
    with open(gipsyres, "w") as f:
        f.writelines(lines[-3:])


def write_gipsyres_from_cov(cov, gipsyres, label):
    """Non-fiducial / CM2CF case: converts the .gdcov X/Y/Z lines into the
    same tdp-like text format."""
    out_lines = []
    if os.path.isfile(cov):
        with open(cov, "r", errors="ignore") as f:
            for line in f:
                if ".STA." in line and any(f".STA.{ax}" in line for ax in "XYZ"):
                    fields = line.split()
                    # fields: [0]=idx? mimic awk $2,$3,$4,$5 usage: index, value, sigma?, name
                    try:
                        idx = int(fields[2])
                        value = float(fields[3])
                        sigma = float(fields[4])
                        name = fields[1]
                    except (IndexError, ValueError):
                        continue
                    axis = name.split(".")[-1]
                    param_name = f".State.Pos.{axis}{label}"
                    out_lines.append(f"{idx:9d} {0:+22.15e} {value:+22.15e} {sigma:+22.15e} {param_name}\n")
    with open(gipsyres, "w") as f:
        f.writelines(out_lines)


def append_trop_tdp(tdp, gipsyres):
    if not os.path.isfile(tdp):
        return
    with open(tdp, "r", errors="ignore") as f, open(gipsyres, "a") as out:
        for line in f:
            if ".Trop." in line:
                out.write(line)


# --------------------------------------------------------------------------
# realtime 24h/30h window handling
# --------------------------------------------------------------------------

def build_realtime_window(config, fid, ymd, header_opts, teqcoptions, tmpdir, rinex_today, verbose):
    """Rebuilds a 30h-window RINEX from yesterday+2-days-ago+today, used
    for the realtime processing mode. Returns the merged rinex path."""
    from_dir = config.get("from")
    fmt = config.get("fmt") or "$FROM/$FID/$yyyy/$mm/$dd"
    data_delay = config.get("data_delay", "5 min")

    rinex1 = os.path.join(tmpdir, "rinex1")
    rinex2 = os.path.join(tmpdir, "rinex2")
    rinex3 = os.path.join(tmpdir, "rinex3")

    yesterday = ymd - timedelta(days=1)
    two_days_ago = ymd - timedelta(days=2)
    print(f"   Real-time case: Appending {two_days_ago.strftime('%Y/%m/%d')}, "
          f"{yesterday.strftime('%Y/%m/%d')} and {ymd.strftime('%Y/%m/%d')} to process 30h of data.")

    for day, out_rinex in ((yesterday, rinex2), (two_days_ago, rinex1)):
        variables = dict(FROM=from_dir, FID=fid, sta=fid.lower(), yyyy=day.strftime("%Y"),
                          yy=day.strftime("%y"), mm=day.strftime("%m"), dd=day.strftime("%d"),
                          doy=day.strftime("%j"))
        raw = expand_fmt(fmt, variables)
        print(raw)
        raw2rinex_mod.raw2rinex(raw, out_rinex, teqcopt=f"{teqcoptions} {header_opts}", verbose=verbose)

    shutil.move(rinex_today, rinex3)

    # data_delay like "5 min" -> parse into a timedelta and compute end window
    delay_minutes = 5
    parts = data_delay.split()
    if parts and parts[0].isdigit():
        delay_minutes = int(parts[0])
    end_window = (datetime.utcnow() - timedelta(minutes=delay_minutes)).strftime("%Y%m%d%H%M%S")

    merged = rinex_today
    run_cmd(f'teqc -phc +quiet -e {end_window} -dh 30 "{rinex1}" "{rinex2}" "{rinex3}" > "{merged}"', verbose=verbose)
    return merged


# --------------------------------------------------------------------------
# per station-day processing
# --------------------------------------------------------------------------

def process_day(config, opts, fid, ymd, tmpdir, base_header, verbose):
    from_dir = config.get("from")
    fmt = config.get("fmt") or "$FROM/$FID/$yyyy/$mm/$dd"
    dest = config.get("dest")
    teqcoptions = config.get("teqcoptions", "")
    realtime = bool(config.get("realtime", False))
    save_debug_tree = bool(config.get("save_debug_tree", False))
    fullog = opts["fullog"]
    force = opts["force"]

    yyyy = ymd.strftime("%Y")
    mm = ymd.strftime("%m")
    dd = ymd.strftime("%d")
    doy = ymd.strftime("%j")
    ymd_dash = ymd.strftime("%Y-%m-%d")
    sta = fid.lower()

    rinex = os.path.join(tmpdir, f"{sta}{yyyy}{doy}.rnx")

    res = os.path.join(dest, fid, yyyy, f"{yyyy}-{mm}-{dd}.{fid}")
    gipsyres = f"{res}.tdp"
    gipsylog = f"{res}.log"
    gipsyfullog = f"{res}.fullog.7z"

    if not force and os.path.isfile(gipsyres) and os.path.getsize(gipsyres) > 0:
        print(f"   file {gipsyres} [flinn] already exists...")
        return

    variables = dict(FROM=from_dir, FID=fid, sta=sta, yyyy=yyyy, yy=yyyy[2:4], mm=mm, dd=dd, doy=doy)
    raw = expand_fmt(fmt, variables)
    if not any(glob.glob(pattern) for pattern in raw):
        print(f"   no data to process in {raw}.")
        return

    header = dict(base_header)
    header, alert1 = apply_station_info_override(config, fid, yyyy, doy, header)
    header, alert2 = apply_sitelog_override(config, fid, ymd_dash, header)
    print(alert2 or alert1)

    header_opts = build_teqc_header_opts(header)

    for old in glob.glob(os.path.join(tmpdir, "*")):
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.remove(old)

    teqcopt = f'{teqcoptions} -O.mn "{fid}" -O.mo "{fid}" {header_opts}'
    rc = raw2rinex_mod.raw2rinex(raw, rinex, teqcopt=teqcopt, verbose=verbose)
    if verbose or rc != 0:
        print(f"   raw2rinex \"{raw}\" {rinex} {teqcopt}")

    today = datetime.utcnow().strftime("%Y/%m/%d")
    if realtime and ymd.strftime("%Y/%m/%d") == today:
        rinex = build_realtime_window(config, fid, ymd, header_opts, teqcoptions, tmpdir, rinex, verbose)
        opts["orbits"] = ["ultra"]

    log_path = os.path.join(tmpdir, "gd2e.log")
    ok = try_orbits(config, opts, fid, ymd, rinex, tmpdir, gipsyres, log_path, verbose)

    os.makedirs(os.path.join(dest, fid, yyyy), exist_ok=True)

    debug_tree_src = os.path.join(tmpdir, "debug.tree")
    if ok and save_debug_tree and os.path.isfile(debug_tree_src):
        shutil.copy(debug_tree_src, f"{res}.tree")

    if ok and os.path.isfile(log_path):
        with open(log_path, "rb") as src, gzip.open(f"{gipsylog}.gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(log_path)

    if fullog:
        entries = os.listdir(tmpdir)
        if len(entries) <= 1:
            print("   full log not saved (tmp folder contains only the input RINEX)")
        else:
            run_cmd(f'cd "{tmpdir}" && 7z a "{gipsyfullog}" * > /dev/null 2>&1', verbose=verbose)
            print(f"   full log saved in {gipsyfullog}")


def process_station(config, opts, fid, day_list, tmpdir, grid, proc_dir, nodes_table, verbose):
    print("")
    print(f"*** Processing files from station {fid} for the last {len(day_list) - 1} days")

    receiver, antenna = get_node_receiver_antenna(config, grid, proc_dir, nodes_table, fid)
    base_header = dict(receiver=receiver, anttype=antenna, posxyz="")

    for ymd in day_list:
        process_day(config, opts, fid, ymd, tmpdir, base_header, verbose)


# --------------------------------------------------------------------------
# main orchestration
# --------------------------------------------------------------------------

def gnss_run_gipsyx(config, days, opts):
    verbose = opts["debug"]

    if not check_home_partition_usage(98):
        return 2

    tmpdirmain = config.get("tmpdirmain") or "/tmp"
    os.environ["TMPDIRMAIN"] = tmpdirmain

    tmpdir = make_tmpdir(tmpdirmain, "gipsyx.")

    print("*** GipsyX / OVS GNSS File Processing ***")

    nodes, nodes_table, proc_dir = resolve_nodes(config, opts["stations"])
    grid = config.get("grid")

    day_list = build_day_list(days, opts["start_dates"])

    try:
        for station in nodes:
            fid = station.strip()
            process_station(config, opts, fid, day_list, tmpdir, grid, proc_dir, nodes_table, verbose)
    finally:
        print("*************************************")
        if not verbose and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

    return 0


def main(argv):
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    if is_already_running():
        print("already running")
        return 0

    opts = parse_options(args)
    config = load_yaml_config(args.conf)

    if opts["lock"]:
        write_lockfile()

    try:
        return gnss_run_gipsyx(config, args.days, opts)
    finally:
        if opts["lock"]:
            remove_lockfile()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
