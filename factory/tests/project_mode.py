"""The team root a test module runs against. With FACTORY_TEST_PROJECT=1 it is a factory.project.yaml and
workflows/delivery.yaml migrated from the team's descriptor and engine/nodes.yaml (ADR-0016), so the same tests
prove the descriptor path: labels through factory/vocab.py, nodes through workflow.to_nodes."""
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


def link_runtime_files(root):
    root = Path(root)
    for rel in ("roles", "skills"):
        path = root / rel
        if path.is_symlink() and not path.exists():  # a link left by an older checkout layout
            path.unlink()
        if not path.exists():
            path.symlink_to(ENGINE / "engine/template-v1" / rel, target_is_directory=True)
    return root


def team(root):
    root = Path(root)
    import definition
    if definition.present(root):  # ADR-0021: the engine compiles it; nothing to convert
        return root
    if os.environ.get("FACTORY_TEST_PROJECT") != "1":
        return link_runtime_files(root)
    if (root / config.PROJECT_DESCRIPTOR).is_file():
        descriptor = yaml.safe_load((root / config.PROJECT_DESCRIPTOR).read_text()) or {}
        wf = workflow.load(root, descriptor)
    else:
        nodes = yaml.safe_load((ENGINE / config.NODES).read_text())
        legacy = yaml.safe_load((root / config.DESCRIPTOR).read_text())
        descriptor, wf = workflow.migrate(nodes, legacy)
    out = Path(tempfile.mkdtemp(prefix="factory-project-"))
    atexit.register(shutil.rmtree, out, True)
    (out / workflow.WORKFLOW_PATH).parent.mkdir(parents=True)
    (out / config.PROJECT_DESCRIPTOR).write_text(workflow.dump(descriptor))
    (out / workflow.WORKFLOW_PATH).write_text(workflow.dump(wf))
    return link_runtime_files(out)
