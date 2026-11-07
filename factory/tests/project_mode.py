"""The team root a test module runs against: a definition (ADR-0021) as it is, else the old descriptor team with
the engine template's roles and skills linked in."""
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE / "factory"))


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
    return link_runtime_files(root)
