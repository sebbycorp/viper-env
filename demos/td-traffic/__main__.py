"""Entry point of td-traffic.pyz: unpack the bundle once per version, then run td.main().

The tool reads its resources (rates catalog, span template, dimensions values) as plain files next to
its code, so the archive is extracted to a per-version cache directory first
(`$TD_TRAFFIC_CACHE`, default `~/.cache/td-traffic/<sha256 of the archive>`).
"""
import sys

if sys.version_info < (3, 11):
    sys.exit(f"td-traffic needs Python 3.11+ (this is {sys.version.split()[0]}). With uv installed, run:\n"
             "  uv run --python 3.12 python td-traffic.pyz <command> ...")

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path


def payload_dir() -> Path:
    here = Path(__file__).resolve().parent          # the .pyz file itself, or a plain directory
    if not here.is_file():
        return here
    root = None
    try:
        root = _private_root(Path(os.environ.get("TD_TRAFFIC_CACHE") or Path.home() / ".cache" / "td-traffic"))
    except RuntimeError:                            # no HOME
        pass
    if root is None:                                # no safe shared cache: unpack privately for this run
        root = Path(tempfile.mkdtemp(prefix="td-traffic-"))
    try:
        return _extract(here, root)
    except OSError as e:
        sys.exit(f"td-traffic: cannot unpack to {root} ({e}). Set TD_TRAFFIC_CACHE to a private writable directory.")


def _private_root(root: Path) -> Path | None:
    """The cache root if it is ours alone (created 0700, owned by us, not group/other writable)."""
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = root.stat()
    except OSError:
        return None
    if hasattr(os, "getuid") and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        return None
    return root


def _extract(here: Path, root: Path) -> Path:
    dest = root / hashlib.sha256(here.read_bytes()).hexdigest()[:16]
    if not (dest / ".complete").exists():
        tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=root))
        with zipfile.ZipFile(here) as z:
            z.extractall(tmp, [n for n in z.namelist() if n != "__main__.py"])
        (tmp / ".complete").touch()
        try:
            tmp.rename(dest)
        except OSError:                             # another run won the race
            shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main() -> int:
    here = str(Path(__file__).resolve().parent)
    dest = payload_dir()
    sys.path[:] = [str(dest)] + [p for p in sys.path if p != here]
    import td
    return td.main()


if __name__ == "__main__":
    sys.exit(main())
