#!/usr/bin/env python3
"""
gnss_common.py

Shared helper functions used by all the Python translations of the
gnss_run_gipsyx bash scripts (raw2rinex, download_orbit, gnss_make_rinex,
gnss_run_gipsyx, sitelog2json, stationinfo2json).

Purely procedural module: no classes, only functions.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta

try:
    import yaml
except ImportError:
    yaml = None


def load_yaml_config(path):
    """Loads a .yml configuration file and returns a dict of parameters.

    Missing/empty values in the yaml file are returned as None so callers
    can use the same "if not config.get('X')" checks as the original .rc
    bash scripts using empty variables.
    """
    if yaml is None:
        print("ERROR: PyYAML is required (pip install pyyaml). Abort.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(path):
        print(f"cannot read configuration file {path}. Abort.", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}
    return expand_environment_variables(config)


def expand_environment_variables(value):
    """Expands ``$VAR`` and ``${VAR}`` in YAML strings recursively.

    Unknown variables are preserved, which allows runtime format variables
    such as ``$FROM`` and ``$FID`` to be expanded later by ``expand_fmt``.
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_environment_variables(item) for item in value]
    if isinstance(value, dict):
        return {
            expand_environment_variables(key): expand_environment_variables(item)
            for key, item in value.items()
        }
    return value


def run_cmd(cmd, log_file=None, verbose=True, check=False, cwd=None, env=None):
    """Runs a shell command string (equivalent of bash's eval "$cmd").

    If log_file is given, stdout/stderr are appended to it (like "> log 2>&1").
    Returns the process return code.
    """
    if verbose:
        print(f"   {cmd}")
    if log_file:
        with open(log_file, "a") as lf:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, env=env, stdout=lf, stderr=subprocess.STDOUT)
    else:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, env=env)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return proc.returncode


def expand_braces(pattern):
    """Expands Bash-style brace alternatives recursively."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]

    expanded = []
    for alternative in match.group(1).split(","):
        replacement = pattern[:match.start()] + alternative + pattern[match.end():]
        expanded.extend(expand_braces(replacement))
    return expanded


def expand_fmt(fmt_template, variables):
    """Expands variables and Bash-style brace alternatives in an input fmt.

    The return value is a list because one format can represent several
    glob patterns, such as ``{$FID,$sta}*{crx,d\\.}*``.
    """
    def repl(match):
        name = match.group(1) or match.group(2)
        return str(variables.get(name, ""))
    substituted = re.sub(r"\$(\w+)|\$\{(\w+)\}", repl, fmt_template)
    expanded = expand_braces(substituted)
    return [re.sub(r"\\+([.])", r"\1", pattern) for pattern in expanded]


def build_day_list(days, start_dates=None, utc=True):
    """Builds the sorted, de-duplicated list of dates to process.

    Equivalent of the bash DAYLIST computation:
      - with no start date: today back to (today - days)
      - with start date(s) (-d option): each start date back to (start - days)
    Returns a list of datetime.date objects, oldest first.
    """
    if start_dates is None or len(start_dates) == 0:
        start_dates = [date.today()]

    all_days = set()
    for start in start_dates:
        for d in range(days, -1, -1):
            all_days.add(start - timedelta(days=d))
    return sorted(all_days)


def parse_ymd(text):
    """Parses a 'yyyy/mm/dd' string into a datetime.date."""
    y, m, d = text.strip().split("/")
    return date(int(y), int(m), int(d))


def format_ymd(d):
    """Formats a datetime.date as 'yyyy/mm/dd'."""
    return d.strftime("%Y/%m/%d")


def day_of_year(d):
    """Returns the 3-digit day-of-year string, e.g. '027'."""
    return d.strftime("%j")


def get_nodes_from_grid(grid, noderoot=None):
    """Replicates the WebObs GRID->NODES lookup done with grep/sed on the
    node .cnf files. Returns (nodes, nodes_table) where:
      - nodes: list of station codes (FID)
      - nodes_table: dict {node_id: FID}
    """
    if noderoot and os.path.isdir(noderoot):
        proc_dir = noderoot
    else:
        proc_dir = f"/etc/webobs.d/GRIDS2NODES/{grid}."

    cnf_files = sorted(glob.glob(os.path.join(proc_dir + "*", "*.cnf")))
    fid_re = re.compile(rf"^{re.escape(grid)}\.FID\|(.*)$")

    nodes = []
    nodes_table = {}
    for cnf in cnf_files:
        node_id = os.path.splitext(os.path.basename(cnf))[0]
        with open(cnf, "r", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\r\n")
                m = fid_re.match(line)
                if m:
                    fid = m.group(1).strip()
                    nodes.append(fid)
                    nodes_table[node_id] = fid
    return nodes, nodes_table


def get_node_param(grid, proc_dir, node_id, param_name):
    """Reads a single GRID.<param_name> value from a node's .cnf file
    (e.g. FID_RECEIVER, FID_ANTENNA)."""
    cnf = os.path.join(proc_dir + node_id, f"{node_id}.cnf")
    if not os.path.isfile(cnf):
        return ""
    prefix = f"{grid}.{param_name}|"
    with open(cnf, "r", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return ""


def make_tmpdir(main_dir, prefix):
    """Creates a temporary working directory under main_dir, falling back
    to the system temp dir if main_dir is not usable."""
    if not main_dir or not os.path.isdir(main_dir):
        if main_dir:
            print(f"WARN: given main temp dir {main_dir} does not exists, defaut /tmp will be used")
        main_dir = "/tmp"
    return tempfile.mkdtemp(prefix=prefix, dir=main_dir)


def check_home_partition_usage(max_percent=98):
    """Safety check equivalent to the 'df -h $HOME' check in gnss_run_gipsyx."""
    home = os.environ.get("HOME", "/")
    try:
        usage = shutil.disk_usage(home)
        percent = round(usage.used / usage.total * 100)
    except OSError:
        return True
    if percent >= max_percent:
        print(f"WARN: Home partition is {percent}% full, I refuse to continue")
        return False
    return True


def quote_opt(flag, value):
    """Builds a ' -flag "value"' teqc-style option string, or '' if value is empty."""
    if value:
        return f' {flag} "{value}"'
    return ""
