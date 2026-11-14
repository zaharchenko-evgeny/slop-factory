"""Run the `ui/` app's own npm scripts from the Python tests."""
import shutil
import subprocess
from pathlib import Path

UI = Path(__file__).resolve().parents[3] / "ui"
ASSETS = Path(__file__).resolve().parents[2] / "ui" / "assets"
NPM = shutil.which("npm")


def npm(*args):
    """Run `npm --prefix ui <args>` (after `npm ci` when node_modules is missing); return the CompletedProcess."""
    if not (UI / "node_modules").is_dir():
        subprocess.run([NPM, "--prefix", str(UI), "ci", "--no-audit", "--no-fund"], check=True, capture_output=True, text=True)
    return subprocess.run([NPM, "--prefix", str(UI), *args], capture_output=True, text=True)
