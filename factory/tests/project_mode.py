"""The team root a test module runs against. With FACTORY_TEST_PROJECT=1 it is a factory.project.yaml and
workflows/delivery.yaml migrated from the team's descriptor and nodes.yaml (ADR-0016), so the same tests prove the
descriptor path: labels through factory/vocab.py, nodes through workflow.to_nodes."""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE / "factory"))
import config  # noqa: E402
import workflow  # noqa: E402


def team(root):
    root = Path(root)
    if os.environ.get("FACTORY_TEST_PROJECT") != "1":
        return root
    nodes_path = root / "workflows/nodes.yaml"
    nodes = yaml.safe_load((nodes_path if nodes_path.is_file() else ENGINE / "workflows/nodes.yaml").read_text())
    descriptor = yaml.safe_load((root / config.DESCRIPTOR).read_text())
    project, wf = workflow.migrate(nodes, descriptor)
    out = Path(tempfile.mkdtemp(prefix="factory-project-"))
    atexit.register(shutil.rmtree, out, True)
    (out / workflow.WORKFLOW_PATH).parent.mkdir(parents=True)
    (out / config.PROJECT_DESCRIPTOR).write_text(workflow.dump(project))
    (out / workflow.WORKFLOW_PATH).write_text(workflow.dump(wf))
    return out
