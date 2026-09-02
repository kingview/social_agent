from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from mcp import StdioServerParameters
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .plugin_host import (
    PluginEndpoint,
    PluginHost,
    PluginHostError,
    default_plugin_host,
    invalidate_default_plugin,
)


PLUGIN_ARCHIVE_SUFFIX = ".socialtool"
PLUGIN_SCHEMA_VERSION = 1


class PluginError(RuntimeError):
    pass


class PluginTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = ""


class PythonRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$")
    gui_module: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$")
    install_extras: list[str] = Field(default_factory=list)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = PLUGIN_SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9.-]{2,79}$")
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    description: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=100)
    platforms: list[Literal["macos-arm64", "macos-x64", "windows-x64", "linux-x64"]]
    runtime: PythonRuntime
    tools: list[PluginTool] = Field(min_length=1)
    permissions: list[
        Literal[
            "network",
            "browser-session",
            "read-agent-output",
            "write-agent-output",
            "local-model",
            "social-content-write",
        ]
    ] = Field(default_factory=list)
    package_sha256: dict[str, str] = Field(default_factory=dict)

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, value: list[PluginTool]) -> list[PluginTool]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("plugin tool names must be unique")
        return value


class PluginRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: PluginManifest
    root: Path
    enabled: bool = True
    installed_at: str
    shared_runtime_bytes: int = 0

    @property
    def python(self) -> Path:
        relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        return self.root / ".venv" / relative


class PluginManager:
    """Installs trusted local Tool bundles outside the immutable application."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_plugin_root()).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self, *, enabled_only: bool = False) -> list[PluginRecord]:
        records: list[PluginRecord] = []
        for manifest_path in sorted(self.root.glob("*/plugin.json")):
            try:
                manifest = PluginManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
                state = self._read_state(manifest_path.parent)
                record = PluginRecord(
                    manifest=manifest,
                    root=manifest_path.parent,
                    enabled=bool(state.get("enabled", True)),
                    installed_at=str(state.get("installed_at") or ""),
                    shared_runtime_bytes=int(state.get("shared_runtime_bytes") or 0),
                )
            except (OSError, ValueError):
                continue
            if not enabled_only or record.enabled:
                records.append(record)
        return records

    def get(self, plugin_id: str, *, require_enabled: bool = False) -> PluginRecord:
        for record in self.list(enabled_only=require_enabled):
            if record.manifest.id == plugin_id:
                return record
        qualifier = "enabled " if require_enabled else ""
        raise PluginError(f"{qualifier}plugin is not installed: {plugin_id}")

    def find_tool(self, tool_name: str) -> PluginRecord:
        matches = [
            record
            for record in self.list(enabled_only=True)
            if tool_name in {item.name for item in record.manifest.tools}
        ]
        if not matches:
            raise PluginError(f"required Tool plugin is not installed or enabled: {tool_name}")
        if len(matches) > 1:
            ids = ", ".join(item.manifest.id for item in matches)
            raise PluginError(f"Tool name {tool_name!r} is provided by multiple plugins: {ids}")
        return matches[0]

    def install(self, archive: Path) -> PluginRecord:
        archive = archive.expanduser().resolve(strict=True)
        if archive.suffix != PLUGIN_ARCHIVE_SUFFIX:
            raise PluginError(f"plugin bundle must use {PLUGIN_ARCHIVE_SUFFIX}")
        staging_parent = Path(tempfile.mkdtemp(prefix="social-agent-plugin-", dir=self.root))
        extracted = staging_parent / "payload"
        extracted.mkdir()
        backup: Path | None = None
        try:
            self._safe_extract(archive, extracted)
            manifest_path = extracted / "plugin.json"
            if not manifest_path.is_file():
                raise PluginError("plugin.json is missing from the bundle root")
            manifest = PluginManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            self._require_current_platform(manifest)
            self._verify_packages(extracted, manifest)
            self._create_environment(extracted, manifest)
            shared_bytes = self._deduplicate_environment(extracted / ".venv")
            state = {
                "enabled": True,
                "installed_at": datetime.now(UTC).isoformat(),
                "shared_runtime_bytes": shared_bytes,
            }
            (extracted / "state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            target = self.root / manifest.id
            invalidate_default_plugin(manifest.id)
            if target.exists():
                backup = self.root / f".{manifest.id}.backup"
                if backup.exists():
                    shutil.rmtree(backup)
                target.replace(backup)
            extracted.replace(target)
            if backup is not None:
                shutil.rmtree(backup)
            self._garbage_collect_package_store()
            return self.get(manifest.id)
        except Exception:
            if backup is not None and backup.exists():
                target = self.root / backup.name.removeprefix(".").removesuffix(".backup")
                if not target.exists():
                    backup.replace(target)
            raise
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    def uninstall(self, plugin_id: str) -> None:
        record = self.get(plugin_id)
        if record.root.parent != self.root or record.root.name != plugin_id:
            raise PluginError("refusing to remove a plugin outside the plugin root")
        invalidate_default_plugin(plugin_id)
        shutil.rmtree(record.root)
        self._garbage_collect_package_store()

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRecord:
        record = self.get(plugin_id)
        invalidate_default_plugin(plugin_id)
        state = self._read_state(record.root)
        state["enabled"] = bool(enabled)
        state.setdefault("installed_at", record.installed_at or datetime.now(UTC).isoformat())
        (record.root / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.get(plugin_id)

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": record.manifest.id,
                "name": record.manifest.name,
                "version": record.manifest.version,
                "description": record.manifest.description,
                "enabled": record.enabled,
                "tools": [item.model_dump() for item in record.manifest.tools],
                "permissions": record.manifest.permissions,
                "has_gui": record.manifest.runtime.gui_module is not None,
                "shared_runtime_bytes": record.shared_runtime_bytes,
            }
            for record in self.list()
        ]

    @staticmethod
    def _read_state(root: Path) -> dict[str, Any]:
        try:
            payload = json.loads((root / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _safe_extract(archive: Path, target: Path) -> None:
        with zipfile.ZipFile(archive) as bundle:
            total = 0
            for member in bundle.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise PluginError("plugin bundle contains an unsafe path")
                total += member.file_size
                if total > 4 * 1024 * 1024 * 1024:
                    raise PluginError("plugin bundle expands beyond the 4 GB safety limit")
            bundle.extractall(target)

    @staticmethod
    def _create_environment(root: Path, manifest: PluginManifest) -> None:
        wheels = sorted((root / "packages").glob("*.whl"))
        if not wheels:
            raise PluginError("plugin bundle contains no Python wheel in packages/")
        bootstrap_python = _plugin_bootstrap_python(root / "locks")
        subprocess.run([str(bootstrap_python), "-m", "venv", str(root / ".venv")], check=True)
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        lock = root / "locks" / dependency_lock_filename(python)
        if lock.is_file():
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--require-hashes",
                    "--find-links",
                    str(root / "packages"),
                    "--requirement",
                    str(lock),
                ],
                check=True,
            )
            return
        requirements = [str(wheel) for wheel in wheels]
        if manifest.runtime.install_extras:
            requirements[0] += "[" + ",".join(manifest.runtime.install_extras) + "]"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *requirements,
            ],
            check=True,
        )

    @staticmethod
    def _verify_packages(root: Path, manifest: PluginManifest) -> None:
        wheels = sorted((root / "packages").glob("*.whl"))
        if not wheels:
            raise PluginError("plugin bundle contains no Python wheel in packages/")
        if not manifest.package_sha256:
            return
        actual_names = {wheel.name for wheel in wheels}
        expected_names = set(manifest.package_sha256)
        if actual_names != expected_names:
            raise PluginError("plugin wheel set does not match package_sha256")
        for wheel in wheels:
            expected = manifest.package_sha256[wheel.name].lower()
            actual = _sha256_file(wheel)
            if actual != expected:
                raise PluginError(f"plugin wheel checksum mismatch: {wheel.name}")

    def _deduplicate_environment(self, environment: Path) -> int:
        """Hard-link identical dependency files through a user-level content store."""
        store = self.root.parent / "runtimes" / "package-store-v1"
        store.mkdir(parents=True, exist_ok=True)
        shared_bytes = 0
        candidates = [
            path
            for path in environment.rglob("*")
            if path.is_file()
            and "site-packages" in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ]
        for path in candidates:
            try:
                digest = _sha256_file(path)
                blob = store / digest[:2] / digest[2:]
                blob.parent.mkdir(parents=True, exist_ok=True)
                if not blob.exists():
                    temporary = blob.with_suffix(f".{os.getpid()}.tmp")
                    shutil.copy2(path, temporary)
                    try:
                        temporary.replace(blob)
                    except FileExistsError:
                        temporary.unlink(missing_ok=True)
                replacement = path.with_name(f".{path.name}.shared")
                replacement.unlink(missing_ok=True)
                os.link(blob, replacement)
                replacement.replace(path)
                shared_bytes += path.stat().st_size
            except OSError:
                continue
        return shared_bytes

    def _garbage_collect_package_store(self) -> None:
        store = self.root.parent / "runtimes" / "package-store-v1"
        if not store.exists():
            return
        for path in store.rglob("*"):
            if path.is_file() and path.stat().st_nlink <= 1:
                path.unlink(missing_ok=True)
        for path in sorted(store.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _require_current_platform(manifest: PluginManifest) -> None:
        current = current_platform_tag()
        if current not in manifest.platforms:
            raise PluginError(
                f"plugin {manifest.id} does not support {current}; "
                f"supported: {', '.join(manifest.platforms)}"
            )


class PluginInvoker:
    def __init__(
        self,
        manager: PluginManager,
        *,
        session_registry: Path,
        output_root: Path,
        state_root: Path,
        llm_base_url: str,
        llm_model: str,
        llm_api_key: str,
        host: PluginHost | None = None,
    ) -> None:
        self.manager = manager
        self.session_registry = session_registry
        self.output_root = output_root
        self.state_root = state_root
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.host = host or default_plugin_host()

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        record = self.manager.find_tool(tool_name)
        self._require_runtime(record)
        try:
            return await self.host.call(self._endpoint(record), tool_name, arguments)
        except PluginHostError as exc:
            raise PluginError(str(exc)) from exc

    async def live_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for record in self.manager.list(enabled_only=True):
            self._require_runtime(record)
            try:
                tools = await self.host.list_tools(self._endpoint(record))
            except PluginHostError as exc:
                raise PluginError(str(exc)) from exc
            catalog.append(
                {
                    "plugin_id": record.manifest.id,
                    "plugin_name": record.manifest.name,
                    "version": record.manifest.version,
                    "tools": tools,
                }
            )
        return catalog

    def launch_gui(
        self,
        plugin_id: str,
        extra_args: list[str] | None = None,
        *,
        ready_file: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        record = self.manager.get(plugin_id, require_enabled=True)
        module = record.manifest.runtime.gui_module
        if not module:
            raise PluginError(f"plugin has no standalone GUI: {plugin_id}")
        self._require_runtime(record)
        environment = self._environment()
        # A frozen Qt host sets these paths to the libraries inside its own app
        # bundle.  Passing them to a plugin venv makes the child load two
        # different Qt builds and macOS aborts while initializing Cocoa.
        for name in (
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QT_QPA_PLATFORM",
            "QML2_IMPORT_PATH",
            "QML_IMPORT_PATH",
            "PYTHONHOME",
            "PYTHONPATH",
            "DYLD_LIBRARY_PATH",
            "DYLD_FRAMEWORK_PATH",
        ):
            environment.pop(name, None)
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        # A unique per-launch path lets the child acknowledge an exposed window.
        # Never propagate an unrelated parent GUI's readiness channel.
        environment.pop("SOCIAL_AGENT_GUI_READY_FILE", None)
        if ready_file is not None:
            environment["SOCIAL_AGENT_GUI_READY_FILE"] = str(ready_file.resolve())
        try:
            return subprocess.Popen(
                [str(record.python), "-m", module, *(extra_args or [])],
                cwd=record.root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise PluginError(f"无法启动插件界面：{exc}") from exc

    def _environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "SOCIAL_AGENT_SESSION_REGISTRY": str(self.session_registry),
            "POSTDROP_SESSION_REGISTRY": str(self.session_registry),
            "SOCIAL_AGENT_OUTPUT_ROOT": str(self.output_root),
            "SOCIAL_AGENT_STATE_ROOT": str(self.state_root),
            "SOCIAL_AGENT_LLM_BASE_URL": self.llm_base_url,
            "SOCIAL_AGENT_LLM_MODEL": self.llm_model,
            "SOCIAL_AGENT_LLM_API_KEY": self.llm_api_key,
        }

    def _endpoint(self, record: PluginRecord) -> PluginEndpoint:
        return PluginEndpoint(
            plugin_id=record.manifest.id,
            version=record.manifest.version,
            parameters=StdioServerParameters(
                command=str(record.python),
                args=["-m", record.manifest.runtime.module],
                cwd=record.root,
                env=self._environment(),
            ),
            expected_tools=tuple(tool.name for tool in record.manifest.tools),
        )

    @staticmethod
    def _require_runtime(record: PluginRecord) -> None:
        if not record.python.is_file():
            raise PluginError(f"plugin runtime is incomplete: {record.manifest.id}")


def build_plugin_bundle(
    manifest_path: Path,
    wheels: list[Path],
    output_path: Path,
    lock_paths: list[Path] | None = None,
) -> Path:
    manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if not wheels:
        raise PluginError("at least one wheel is required")
    output_path = output_path.expanduser().resolve()
    if output_path.suffix != PLUGIN_ARCHIVE_SUFFIX:
        output_path = output_path.with_suffix(PLUGIN_ARCHIVE_SUFFIX)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_wheels = [wheel.expanduser().resolve(strict=True) for wheel in wheels]
    manifest = manifest.model_copy(
        update={
            "package_sha256": {
                wheel.name: _sha256_file(wheel) for wheel in resolved_wheels
            }
        }
    )
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        manifest_payload = manifest.model_dump(mode="json")
        for tool in manifest_payload["tools"]:
            if not tool.get("description"):
                tool.pop("description", None)
        bundle.writestr(
            "plugin.json",
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
        )
        for wheel in resolved_wheels:
            bundle.write(wheel, f"packages/{wheel.name}")
        for lock in lock_paths or []:
            lock = lock.expanduser().resolve(strict=True)
            bundle.write(lock, f"locks/{lock.name}")
    return output_path


def build_dependency_lock(
    manifest_path: Path,
    wheels: list[Path],
    output_directory: Path,
    resolver_python: Path | None = None,
) -> Path:
    """Resolve a hash-pinned lock for a selected platform-compatible Python ABI."""
    manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if not wheels:
        raise PluginError("at least one wheel is required")
    resolved_wheels = [wheel.expanduser().resolve(strict=True) for wheel in wheels]
    requirements = [str(wheel) for wheel in resolved_wheels]
    if manifest.runtime.install_extras:
        requirements[0] += "[" + ",".join(manifest.runtime.install_extras) + "]"
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    resolver = (resolver_python or Path(sys.executable)).expanduser().resolve()
    if not resolver.is_file():
        raise PluginError(f"dependency resolver Python does not exist: {resolver}")
    if not _is_supported_plugin_python(resolver):
        raise PluginError(f"plugin dependency resolver requires Python 3.11+: {resolver}")
    with tempfile.TemporaryDirectory(prefix="social-agent-lock-") as temporary:
        report = Path(temporary) / "report.json"
        subprocess.run(
            [
                str(resolver),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--dry-run",
                "--ignore-installed",
                "--report",
                str(report),
                *requirements,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
    lines = [
        f"# Social Agent dependency lock: {current_platform_tag()} / {python_abi_tag(resolver)}",
        "# Generated from pip's resolver; do not edit by hand.",
    ]
    resolved: list[tuple[str, str, str]] = []
    for item in payload.get("install", []):
        metadata = item.get("metadata") or {}
        archive = (item.get("download_info") or {}).get("archive_info") or {}
        hashes = archive.get("hashes") or {}
        digest = hashes.get("sha256")
        name = str(metadata.get("name") or "").strip()
        version = str(metadata.get("version") or "").strip()
        if not name or not version or not digest:
            raise PluginError(f"dependency resolver returned an unhashed artifact: {name or 'unknown'}")
        normalized_name = name.lower().replace("_", "-").replace(".", "-")
        resolved.append((normalized_name, version, str(digest).lower()))
    for name, version, digest in sorted(resolved):
        lines.append(f"{name}=={version} --hash=sha256:{digest}")
    output = output_directory / dependency_lock_filename(resolver)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def default_plugin_root() -> Path:
    configured = os.getenv("SOCIAL_AGENT_PLUGIN_ROOT")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "SocialAgent"
    elif os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SocialAgent"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "social-agent"
    return base / "plugins"


def current_platform_tag() -> str:
    import platform

    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x64"
    if os.name == "nt":
        return "windows-x64"
    return "linux-x64"


def python_abi_tag(python: Path) -> str:
    result = subprocess.run(
        [str(python), "-c", "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def dependency_lock_filename(python: Path) -> str:
    return f"requirements-{current_platform_tag()}-{python_abi_tag(python)}.lock"


def _is_supported_plugin_python(candidate: Path) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), "-c", "import sys; print(int(sys.version_info >= (3, 11)))"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return result.stdout.strip() == "1"


def _plugin_bootstrap_python(lock_directory: Path | None = None) -> Path:
    configured = os.getenv("SOCIAL_AGENT_PLUGIN_PYTHON", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and _is_supported_plugin_python(candidate):
            return candidate
        raise PluginError(
            "SOCIAL_AGENT_PLUGIN_PYTHON must point to an existing Python 3.11+ "
            f"interpreter: {candidate}"
        )
    names = (
        ["python3.12.exe", "python3.13.exe", "python3.11.exe", "python3.exe", "python.exe"]
        if os.name == "nt"
        else ["python3.12", "python3.13", "python3.11", "python3"]
    )
    raw_candidates = [
        *(Path(value) for value in (shutil.which(name) for name in names) if value),
        Path(sys.executable),
        Path("/opt/homebrew/bin/python3.12"),
        Path("/opt/homebrew/bin/python3.13"),
        Path("/opt/homebrew/bin/python3.11"),
        Path("/opt/homebrew/bin/python3"),
        Path("/usr/local/bin/python3.12"),
        Path("/usr/local/bin/python3.13"),
        Path("/usr/local/bin/python3.11"),
        Path("/usr/local/bin/python3"),
        Path("/usr/bin/python3"),
    ]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate.is_file():
            continue
        candidate = raw_candidate.resolve()
        if candidate in seen or not _is_supported_plugin_python(candidate):
            continue
        seen.add(candidate)
        candidates.append(candidate)
    if lock_directory is not None:
        for candidate in candidates:
            if (lock_directory / dependency_lock_filename(candidate)).is_file():
                return candidate
    if candidates:
        return candidates[0]
    raise PluginError(
        "未找到用于创建隔离插件环境的 Python 3.11+。"
        "请安装 Python，或通过 SOCIAL_AGENT_PLUGIN_PYTHON 指定解释器。"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
