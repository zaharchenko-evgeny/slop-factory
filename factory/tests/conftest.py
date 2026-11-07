"""pytest only: run every test against the fixture team, as CI does (quality-gates.yml sets FACTORY_TEAM).

A bare `pytest factory/tests` in an agent run then gives CI's result, whatever FACTORY_TEAM the run has."""
import os
from pathlib import Path

os.environ["FACTORY_TEAM"] = str(Path(__file__).parent / "fixtures" / "team")
