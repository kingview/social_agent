from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from social_ops_agent.plugin_host import PluginHost
from social_ops_agent.plugins import (
    PluginError,
    PluginInvoker,
    PluginManager,
    PluginManifest,
    build_plugin_bundle,
    current_platform_tag,
    python_abi_tag,
)


MANIFEST = {
    "schema_version": 1,
    "id": "com.example.test-tool",
    "name": "Test Tool",
    "version": "1.0.0",
    "description": "A test Tool plugin",
    "publisher": "Tests",
    "platforms": ["macos-arm64", "macos-x64", "windows-x64", "linux-x64"],
    "runtime": {"module": "test_tool.mcp", "install_extras": []},
    "tools": [{"name": "test_action", "description": "Run a test action"}],
    "permissions": ["read-agent-output"],
}


def _archive(path: Path, *, unsafe: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("plugin.json", json.dumps(MANIFEST))
        bundle.writestr("packages/test_tool-1.0.0-py3-none-any.whl", b"wheel")
        if unsafe:
            bundle.writestr("../escape", b"bad")
    return path


def test_manifest_rejects_duplicate_tool_names() -> None:
    payload = dict(MANIFEST)
    payload["tools"] = [MANIFEST["tools"][0], MANIFEST["tools"][0]]
    with pytest.raises(ValueError, match="unique"):
        PluginManifest.model_validate(payload)


def test_manager_installs_enables_and_uninstalls_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(tmp_path / "plugins")
    monkeypatch.setattr(manager, "_create_environment", lambda root, manifest: None)

    record = manager.install(_archive(tmp_path / "test.socialtool"))

    assert record.manifest.id == "com.example.test-tool"
    assert manager.find_tool("test_action").enabled
    assert manager.set_enabled(record.manifest.id, False).enabled is False
    with pytest.raises(PluginError, match="not installed or enabled"):
        manager.find_tool("test_action")
    manager.uninstall(record.manifest.id)
    assert manager.list() == []


def test_manager_rejects_zip_traversal(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "plugins")
    with pytest.raises(PluginError, match="unsafe path"):
        manager.install(_archive(tmp_path / "unsafe.socialtool", unsafe=True))


def test_bundle_records_and_verifies_wheel_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "plugin.json"
    manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    wheel = tmp_path / "test_tool-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel payload")
    archive = build_plugin_bundle(manifest_path, [wheel], tmp_path / "verified.socialtool")

    with zipfile.ZipFile(archive) as bundle:
        bundled_manifest = json.loads(bundle.read("plugin.json"))
        assert bundled_manifest["package_sha256"][wheel.name]

    manager = PluginManager(tmp_path / "plugins")
    monkeypatch.setattr(manager, "_create_environment", lambda root, manifest: None)
    monkeypatch.setattr(manager, "_deduplicate_environment", lambda root: 0)
    record = manager.install(archive)
    assert record.manifest.package_sha256[wheel.name]


def test_shared_package_store_hardlinks_identical_dependencies(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "plugins")
    first = tmp_path / "one" / ".venv" / "lib" / "python3.11" / "site-packages" / "same.bin"
    second = tmp_path / "two" / ".venv" / "lib" / "python3.11" / "site-packages" / "same.bin"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"identical dependency")
    second.write_bytes(b"identical dependency")

    manager._deduplicate_environment(tmp_path / "one" / ".venv")
    manager._deduplicate_environment(tmp_path / "two" / ".venv")

    assert first.stat().st_ino == second.stat().st_ino


def test_plugin_host_reuses_the_same_mcp_process(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins" / MANIFEST["id"]
    python = plugin_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    if os.name == "nt":
        pytest.skip("the lightweight executable fixture uses a POSIX symlink")
    python.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    manifest = {
        **MANIFEST,
        "runtime": {"module": "fake_mcp", "install_extras": []},
        "tools": [{"name": "process_identity"}],
    }
    (plugin_root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_root / "state.json").write_text(
        json.dumps({"enabled": True, "installed_at": "now"}), encoding="utf-8"
    )
    (plugin_root / "fake_mcp.py").write_text(
        """\
import os
from mcp.server.fastmcp import FastMCP
mcp = FastMCP('fake')
@mcp.tool()
def process_identity() -> dict[str, int]:
    return {'pid': os.getpid()}
if __name__ == '__main__':
    mcp.run(transport='stdio')
""",
        encoding="utf-8",
    )
    host = PluginHost()
    invoker = PluginInvoker(
        PluginManager(tmp_path / "plugins"),
        session_registry=tmp_path / "sessions.json",
        output_root=tmp_path / "output",
        state_root=tmp_path / "state",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="test",
        llm_api_key="test",
        host=host,
    )

    async def invoke_twice() -> tuple[object, object]:
        return (
            await invoker.call("process_identity", {}),
            await invoker.call("process_identity", {}),
        )

    try:
        first, second = __import__("asyncio").run(invoke_twice())
    finally:
        host.close()
    assert first["pid"] == second["pid"]


def test_plugin_gui_does_not_inherit_frozen_host_qt_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plugins" / MANIFEST["id"]
    python = plugin_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    manifest = {
        **MANIFEST,
        "runtime": {
            "module": "test_tool.mcp",
            "gui_module": "test_tool.desktop",
            "install_extras": [],
        },
    }
    (plugin_root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_root / "state.json").write_text(
        json.dumps({"enabled": True, "installed_at": "now"}), encoding="utf-8"
    )
    monkeypatch.setenv("QT_PLUGIN_PATH", "/frozen-app/Qt/plugins")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", "/frozen-app/Qt/plugins/platforms")
    monkeypatch.setenv("PYTHONPATH", "/frozen-app")
    captured: dict[str, object] = {}

    class FakeProcess:
        pass

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("social_ops_agent.plugins.subprocess.Popen", fake_popen)
    invoker = PluginInvoker(
        PluginManager(tmp_path / "plugins"),
        session_registry=tmp_path / "sessions.json",
        output_root=tmp_path / "output",
        state_root=tmp_path / "state",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="test",
        llm_api_key="test",
    )

    ready_file = tmp_path / "gui-ready"
    invoker.launch_gui(MANIFEST["id"], ["--manage-sessions-only"], ready_file=ready_file)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "QT_PLUGIN_PATH" not in environment
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["SOCIAL_AGENT_GUI_READY_FILE"] == str(ready_file.resolve())
    assert captured["stderr"] is subprocess.PIPE


def test_plugin_bootstrap_prefers_interpreter_matching_bundled_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from social_ops_agent import plugins

    current = Path(sys.executable).resolve()
    lock_directory = tmp_path / "locks"
    lock_directory.mkdir()
    lock = lock_directory / (
        f"requirements-{current_platform_tag()}-{python_abi_tag(current)}.lock"
    )
    lock.write_text("# test lock\n", encoding="utf-8")
    monkeypatch.delenv("SOCIAL_AGENT_PLUGIN_PYTHON", raising=False)

    assert plugins._plugin_bootstrap_python(lock_directory) == current
