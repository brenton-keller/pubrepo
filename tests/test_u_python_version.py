"""On a Python older than 3.11, pubrepo.py stops before its first 3.11-only
import (tomllib) with one plain line and exit 1, instead of a traceback.
The old interpreter is simulated by patching sys.version_info in a child
process before the module runs."""
import subprocess
import sys

from conftest import SCRIPT_PATH

LOAD = ("import importlib.machinery, importlib.util, sys\n"
        "{patch}"
        f"loader = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
        "spec = importlib.util.spec_from_loader('pubrepo', loader)\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['pubrepo'] = m\n"
        "loader.exec_module(m)\n"
        "print(m.__version__)\n")


def _load(patch, tmp_path):
    return subprocess.run([sys.executable, "-c", LOAD.format(patch=patch)],
                          capture_output=True, text=True, cwd=tmp_path)


def test_python_3_9_gets_one_line_and_exit_1(tmp_path):
    r = _load("sys.version_info = (3, 9, 6, 'final', 0)\n", tmp_path)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stderr == f"pubrepo requires Python 3.11+; this is 3.9 at {sys.executable}\n"
    assert r.stdout == ""


def test_python_3_11_and_later_load_normally(tmp_path):
    for patch in ("sys.version_info = (3, 11, 0, 'final', 0)\n", ""):
        r = _load(patch, tmp_path)
        assert r.returncode == 0, (patch, r.stderr)
        assert r.stdout.strip() and "requires Python" not in r.stderr
