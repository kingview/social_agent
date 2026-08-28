from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .plugins import PluginError, PluginManager, build_dependency_lock, build_plugin_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="social-agent-plugin")
    parser.add_argument("--root", type=Path, help="override the plugin installation directory")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list installed Tool plugins")
    install = commands.add_parser("install", help="install or upgrade a .socialtool bundle")
    install.add_argument("archive", type=Path)
    uninstall = commands.add_parser("uninstall", help="uninstall a Tool plugin")
    uninstall.add_argument("plugin_id")
    enable = commands.add_parser("enable", help="enable a Tool plugin")
    enable.add_argument("plugin_id")
    disable = commands.add_parser("disable", help="disable a Tool plugin")
    disable.add_argument("plugin_id")
    bundle = commands.add_parser("bundle", help="build a .socialtool archive")
    bundle.add_argument("--manifest", type=Path, required=True)
    bundle.add_argument("--wheel", type=Path, action="append", required=True)
    bundle.add_argument("--lock", type=Path, action="append")
    bundle.add_argument("--output", type=Path, required=True)
    lock = commands.add_parser("lock", help="resolve a platform-specific hashed dependency lock")
    lock.add_argument("--manifest", type=Path, required=True)
    lock.add_argument("--wheel", type=Path, action="append", required=True)
    lock.add_argument("--output-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "bundle":
            output = build_plugin_bundle(
                arguments.manifest,
                arguments.wheel,
                arguments.output,
                arguments.lock,
            )
            print(output)
            return 0
        if arguments.command == "lock":
            output = build_dependency_lock(
                arguments.manifest,
                arguments.wheel,
                arguments.output_directory,
            )
            print(output)
            return 0
        manager = PluginManager(arguments.root)
        if arguments.command == "list":
            print(json.dumps(manager.catalog(), ensure_ascii=False, indent=2))
        elif arguments.command == "install":
            record = manager.install(arguments.archive)
            print(f"installed {record.manifest.id} {record.manifest.version}")
        elif arguments.command == "uninstall":
            manager.uninstall(arguments.plugin_id)
            print(f"uninstalled {arguments.plugin_id}")
        elif arguments.command == "enable":
            manager.set_enabled(arguments.plugin_id, True)
            print(f"enabled {arguments.plugin_id}")
        elif arguments.command == "disable":
            manager.set_enabled(arguments.plugin_id, False)
            print(f"disabled {arguments.plugin_id}")
    except (OSError, PluginError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
