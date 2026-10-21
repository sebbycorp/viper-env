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


def test_single_py_runs_help_and_loads_resources(tmp_path):
    out = tmp_path / "dist"
    subprocess.run(["bash", str(BUILD), str(out)], check=True, capture_output=True, text=True)
    script = out / "td-traffic.py"
    assert script.is_file() and script.read_text().startswith("#!/usr/bin/env python3")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(script, elsewhere / "td-traffic.py")
    env = {**os.environ, "TD_TRAFFIC_CACHE": str(tmp_path / "cache")}
    for args, needle in ((["--help"], "demo"), (["selfcheck"], "selfcheck ok")):
        r = subprocess.run([sys.executable, "td-traffic.py", *args], cwd=elsewhere, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert needle in r.stdout


def _load_entry():
    import importlib.util
    spec = importlib.util.spec_from_file_location("td_entry", REPO / "demos" / "td-traffic" / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cache_root_must_be_private(tmp_path):
    entry = _load_entry()
    ok = tmp_path / "ok"
    assert entry._private_root(ok) == ok and (ok.stat().st_mode & 0o777) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    assert entry._private_root(shared) is None


def test_no_fixed_shared_tmp_fallback():
    for name in ("__main__.py", "single_file_stub.py"):
        src = (REPO / "demos" / "td-traffic" / name).read_text()
        assert "td-traffic-cache" not in src and "mkdtemp(prefix=\"td-traffic-\")" in src
