"""Test helper: the hand-written project descriptor of a fixture directory."""
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE / "factory"))


def legacy_descriptor(root):
    """The old `factory.project.yaml` of a test fixture directory, over engine/defaults.yaml, as a plain dict.
    The engine no longer loads that file: fixtures keep it as a hand-written project descriptor."""
    import yaml
    root = Path(root)
    defaults = yaml.safe_load((ENGINE / "engine/defaults.yaml").read_text()) or {}
    own = yaml.safe_load((root / "factory.project.yaml").read_text()) or {}
    return {**defaults, **{k: {**defaults[k], **v} if isinstance(defaults.get(k), dict) and isinstance(v, dict) else v
                           for k, v in own.items()}}
