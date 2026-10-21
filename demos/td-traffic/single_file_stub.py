#!/usr/bin/env python3
"""td-traffic: TD Bank synthetic AI-assistant traffic for the Solo Enterprise agentgateway UI.

Single-file build: the tool's code and data are embedded below. On first run they are unpacked
once per version to `$TD_TRAFFIC_CACHE` (default `~/.cache/td-traffic/<hash>`), then td.main() runs.
Usage: python3 td-traffic.py <command> --context <kube-context>   (see --help)
"""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"td-traffic needs Python 3.11+ (this is {sys.version.split()[0]}). With uv installed, run:\n"
             "  uv run --python 3.12 python td-traffic.py <command> ...")

import base64
import hashlib
import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

PAYLOAD = "".join("""
__PAYLOAD__
""".split())


def payload_dir() -> Path:
    try:
        root = Path(os.environ.get("TD_TRAFFIC_CACHE") or Path.home() / ".cache" / "td-traffic")
    except RuntimeError:                            # no HOME
        root = Path(tempfile.gettempdir()) / "td-traffic-cache"
    try:
        return _extract(base64.b64decode(PAYLOAD), root)
    except OSError as e:
        sys.exit(f"td-traffic: cannot unpack to {root} ({e}). Set TD_TRAFFIC_CACHE to a writable directory.")


def _extract(blob: bytes, root: Path) -> Path:
    dest = root / hashlib.sha256(blob).hexdigest()[:16]
    if not (dest / ".complete").exists():
        root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=root))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(tmp, [n for n in z.namelist() if n != "__main__.py"])
        (tmp / ".complete").touch()
        try:
            tmp.rename(dest)
        except OSError:                             # another run won the race
            shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main() -> int:
    sys.path.insert(0, str(payload_dir()))
    import td
    return td.main()


if __name__ == "__main__":
    sys.exit(main())
