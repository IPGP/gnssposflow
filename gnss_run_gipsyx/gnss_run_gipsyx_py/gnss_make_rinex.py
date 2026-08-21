#!/usr/bin/env python3
"""
gnss_make_rinex.py

Python translation of the bash script gnss_make_rinex.
Generates daily GNSS RINEX files from raw data, optionally overwriting
RINEX headers (receiver/antenna/position/observer/agency) using a sitelog
directory, a Gamit station.info file, or local WebObs node parameters.

Author: Baptiste Camus (original bash)
"""

import os
import sys

import raw2rinex as raw2rinex_mod
import sitelog2json as sitelog2json_mod
import stationinfo2json as stationinfo2json_mod
from gnss_common import (build_day_list, day_of_year, expand_fmt,
                          get_node_param, get_nodes_from_grid, load_yaml_config)


def resolve_nodes(config, cli_stations=None):
    """Resolves the list of station codes to process, and the WebObs
    nodes table if a GRID is used."""
    if cli_stations:
        return cli_stations, {}, None

    nodes = config.get("nodes")
    grid = config.get("grid")
    noderoot = config.get("noderoot")

    if grid and not nodes:
        nodes, nodes_table = get_nodes_from_grid(grid, noderoot)
        proc_dir = noderoot if (noderoot and os.path.isdir(noderoot)) else f"/etc/webobs.d/GRIDS2NODES/{grid}."
        return nodes, nodes_table, proc_dir

    if isinstance(nodes, str):
        nodes = nodes.split()
    return nodes or [], {}, None


def get_site_info(infosrc, config, fid, ymd, grid, proc_dir, nodes_table, verbose=False):
    """Returns a dict of header overrides (receiver, anttype, antnum, posxyz,
    observer, agency) and an alert message, for the given site information
    source (WEBOBS, STATION_INFO or SITELOG)."""
    info = {}
    alertheader = ""
    day_label = f"{fid} {ymd} -"

    if infosrc == "WEBOBS":
        if grid and config.get("noderoot"):
            node_id = None
            for nid, node_fid in nodes_table.items():
                if node_fid == fid:
                    node_id = nid
                    break
            if node_id:
                info["receiver"] = get_node_param(grid, proc_dir, node_id, "FID_RECEIVER")
                info["anttype"] = get_node_param(grid, proc_dir, node_id, "FID_ANTENNA")
                alertheader = (f"   {day_label} WO ID = {node_id} - receiver = '{info.get('receiver', '')}' "
                               f"- antenna = '{info.get('anttype', '')}' (will use rinex header if empty)")
        else:
            print(f"   {day_label}   - Warning -   Webobs site information : GRID or NODEROOT is empty in config.")

    elif infosrc == "STATION_INFO":
        station_info = config.get("station_info")
        doy = day_of_year_from_ymd(ymd)
        yyyy = ymd[:4]
        if station_info and os.path.isfile(station_info) and os.path.getsize(station_info) > 0:
            records = stationinfo2json_mod.stationinfo2json(station_info, fid, f"{yyyy}-{doy}")
            if records:
                info["receiver"] = records[0].get("rt", "")
                info["anttype"] = records[0].get("at", "")
            alertheader = (f"   {day_label} station.info: {doy} - receiver = '{info.get('receiver', '')}' "
                           f"- antenna = '{info.get('anttype', '')}' (will use rinex header if empty)")
        else:
            print(f"   {day_label}   - Warning -   station.info : STATION_INFO is empty in config. ")

    elif infosrc == "SITELOG":
        sitelog = config.get("sitelog")
        if sitelog:
            result = sitelog2json_mod.sitelog2json(sitelog, fid, ymd)
            if result:
                info["receiver"] = result.get("rt", "")
                info["anttype"] = result.get("at", "")
                info["antnum"] = result.get("an", "")
                info["posxyz"] = result.get("px", "")
                info["observer"] = result.get("op", "")
                info["agency"] = result.get("ag", "")
            alertheader = (f"   {day_label} sitelog: receiver='{info.get('receiver', '')}' "
                           f"- antenna type='{info.get('anttype', '')}' - antenna s/n='{info.get('antnum', '')}' "
                           f"- observer='{info.get('observer', '')}' - agency='{info.get('agency', '')}' "
                           f"(will use rinex header if empty)")
        else:
            print(f"   {day_label}   - Warning -   sitelog : SITELOG is empty in config !!!")
    else:
        print(f"   {day_label}   - Warning -   bad site information source")

    return info, alertheader


def day_of_year_from_ymd(ymd):
    from gnss_common import parse_ymd
    return parse_ymd(ymd).strftime("%j")


def build_teqc_header_opts(info):
    """Builds the extra teqc options string for the resolved header overrides."""
    opts = ""
    if info.get("receiver"):
        opts += f' -O.rt "{info["receiver"]}"'
    if info.get("anttype"):
        opts += f' -O.at "{info["anttype"]}"'
    if info.get("antnum"):
        opts += f' -O.an "{info["antnum"]}"'
    if info.get("posxyz"):
        opts += f' -O.px {info["posxyz"]}'
    if info.get("observer"):
        opts += f' -O.op {info["observer"]}'
    if info.get("agency"):
        opts += f' -O.ag {info["agency"]}'
    return opts


def gnss_make_rinex(config, days, stations=None, start_dates=None, infosrc_override=None, verbose=False):
    grid = config.get("grid")
    fmt = config.get("fmt") or "$FROM/$FID/$yyyy/$mm/$dd"
    from_dir = config.get("from")
    dest = config.get("dest")
    teqcoptions = config.get("teqcoptions", "")
    infosrc = infosrc_override or config.get("infosrc")

    nodes, nodes_table, proc_dir = resolve_nodes(config, stations)
    day_list = build_day_list(days, start_dates)

    print(f"*** Rinex maker / WebObs {grid or ''} GNSS File Processing ***")

    for station in nodes:
        fid = station.strip()
        print("")
        print(f"*** Processing files from station {fid} for the last {days} days")

        for d in day_list:
            ymd = d.strftime("%Y/%m/%d")
            yyyy = d.strftime("%Y")
            mm = d.strftime("%m")
            dd = d.strftime("%d")
            doy = d.strftime("%j")
            sta = fid.lower()

            day_label = f"{fid} {ymd} -"
            rinex_out = os.path.join(dest, "GNSS", "rinex", "30s", fid, yyyy)

            variables = dict(FROM=from_dir, FID=fid, sta=sta, yyyy=yyyy, yy=yyyy[2:4], mm=mm, dd=dd, doy=doy)
            raw = expand_fmt(fmt, variables)

            has_raw = os.path.isdir(raw) and any(os.path.isfile(os.path.join(root, f))
                                                  for root, _d, files in os.walk(raw) for f in files)
            if not has_raw:
                print(f"   {day_label} no data to process in {raw}.")
                continue

            info = {}
            alertheader = ""
            if infosrc:
                info, alertheader = get_site_info(infosrc, config, fid, ymd, grid, proc_dir, nodes_table, verbose)

            if any(info.values()):
                print(alertheader)
            else:
                print(f"   {day_label}   - Warning -   No site information found in {infosrc}, "
                      f"Rinex will be generated without overwriting headers if empty")

            teqcopt = f"{teqcoptions} {build_teqc_header_opts(info)}"

            os.makedirs(rinex_out, exist_ok=True)
            rc = raw2rinex_mod.raw2rinex(raw, rinex_out, teqcopt=teqcopt, verbose=verbose)
            if verbose or rc != 0:
                print(f"   raw2rinex \"{raw}\" {rinex_out} {teqcopt}")

    print("*************************************")


def main(argv):
    if len(argv) < 2:
        print("      Syntax: gnss_make_rinex CONF DAYS [options]")
        print(" Description: genrate gnss rinex file from rawdata and some site information if needed")
        print("   Arguments:")
        print("       CONF = configuration filename (YAML), e.g. gnss_make_rinex_template.yml")
        print("       DAYS = number of days to process (from today)")
        print("     Options:")
        print('        -s "STA1 STA2..."')
        print("            station code or station list with double quotes")
        print("            default is the list of nodes defined in CONF")
        print('       -d "yyyy/mm/dd,yyyy/mm/dd"')
        print("            choose days to start process; the DAYS argument can still be used to")
        print("            process previous days from the selected ones")
        print("       -i GRID/SITELOG/STATION_INFO")
        print("            choose the site information source to overwrite missing headers")
        print("       -debug")
        print("            verbose mode")
        print("")
        return 0

    conf_path = argv[0]
    days = int(argv[1])

    config = load_yaml_config(conf_path)

    stations = None
    start_dates = None
    infosrc_override = None
    verbose = False

    i = 2
    while i < len(argv):
        opt = argv[i]
        if opt == "-debug":
            verbose = True
            print("Debug mode : processing with verbose log")
        elif opt == "-d":
            i += 1
            from gnss_common import parse_ymd
            start_dates = [parse_ymd(d) for d in argv[i].split(",")]
        elif opt == "-i":
            i += 1
            infosrc_override = argv[i]
            print(f"   Generating rinex with headers informations based on {infosrc_override} files")
        elif opt == "-s":
            i += 1
            stations = argv[i].split()
        i += 1

    gnss_make_rinex(config, days, stations=stations, start_dates=start_dates,
                     infosrc_override=infosrc_override, verbose=verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
