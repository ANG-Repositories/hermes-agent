"""Supervisor ownership is local to its interpreter, not inherited child interpreters."""
import os
from pathlib import Path
import subprocess
import sys
import venv


def test_foreign_child_drops_checkout_contract(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    parent = root / "venv"
    venv.EnvBuilder(with_pip=False).create(parent)
    child = tmp_path / "child"
    venv.EnvBuilder(with_pip=False).create(child)
    repo = Path(__file__).resolve().parents[2]
    code = (f"import sys, os; sys.path.insert(0, {str(repo)!r}); "
            "from pathlib import Path; from pm.environments import activate_dependencies; "
            f"activate_dependencies(Path({str(root)!r})); "
            "assert 'HERMES_CHECKOUT_VENV' not in os.environ; print('child started')")
    for python in (sys.executable, str(child / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))):
        result = subprocess.run([python, "-c", code],
                                env=dict(os.environ, HERMES_CHECKOUT_VENV=str(parent), HERMES_HOME=str(tmp_path / 'home')),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "child started"


def test_owned_interpreter_removes_inherited_checkout(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    owned = root / "venv"
    venv.EnvBuilder(with_pip=False).create(owned)
    foreign = tmp_path / "other-checkout"
    foreign.mkdir()
    site = foreign / "venv/lib/python3.14/site-packages"
    site.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[2]
    code = (f"import sys; sys.path.insert(0, {str(repo)!r}); "
            "from pathlib import Path; from pm.environments import activate_dependencies; "
            f"activate_dependencies(Path({str(root)!r})); "
            f"assert {str(foreign)!r} not in sys.path, sys.path; "
            f"assert {str(site)!r} not in sys.path, sys.path")
    result = subprocess.run([str(owned / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')), "-c", code],
                            env=dict(os.environ, HERMES_CHECKOUT_VENV=str(owned), PYTHONPATH=os.pathsep.join([str(foreign), str(site)])),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
