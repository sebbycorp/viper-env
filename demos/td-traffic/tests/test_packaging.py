"""The single-file td-traffic.pyz runs from anywhere and carries every resource."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILD = REPO / "scripts" / "build-td-traffic.sh"


def test_pyz_runs_help_and_loads_resources(tmp_path):
    out = tmp_path / "dist"
    subprocess.run(["bash", str(BUILD), str(out)], check=True, capture_output=True, text=True)
    pyz = out / "td-traffic.pyz"
    assert pyz.is_file() and pyz.read_bytes().startswith(b"#!/usr/bin/env python3")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(pyz, elsewhere / "td-traffic.pyz")
    env = {**os.environ, "TD_TRAFFIC_CACHE": str(tmp_path / "cache")}
    for args, needle in ((["--help"], "demo"), (["selfcheck"], "selfcheck ok")):
        r = subprocess.run([sys.executable, "td-traffic.pyz", *args], cwd=elsewhere, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert needle in r.stdout
