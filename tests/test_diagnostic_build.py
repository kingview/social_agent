import importlib.util
from pathlib import Path

import pytest


def test_build_sync_updates_both_tools_and_check_mode_does_not_write(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/sync_diagnostics.py"
    spec = importlib.util.spec_from_file_location("diagnostic_build_sync", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    for name in module.FILES:
        (source / name).write_text(f"# canonical {name}\n")
    for package in module.PACKAGES:
        target = tmp_path / "tools" / package
        (target / "src" / package).mkdir(parents=True)
        (target / "pyproject.toml").write_text("[project]\n")
    changed = module.sync(source, tmp_path / "tools", check=True)
    assert len(changed) == 4 and not any(p.exists() for p in changed)
    assert module.sync(source, tmp_path / "tools") == changed
    assert module.sync(source, tmp_path / "tools", check=True) == []
    with pytest.raises(ValueError):
        module.sync(source, tmp_path / "invalid")
