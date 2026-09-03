"""Synchronize generated Tool diagnostics from the Agent's canonical sources.

Standalone Tool repositories ship these files; they do not depend on Agent at
runtime or need its checkout to build. Workspace plugin builds synchronize first.
"""
from __future__ import annotations

import argparse
from pathlib import Path

FILES = ("diagnostics.py", "diagnostic_mcp.py")
PACKAGES = ("media_content_analyzer", "social_content_crawler")


def sync(source: Path, tools_root: Path, *, check: bool = False) -> list[Path]:
    # Validate all targets before changing either independently packaged Tool.
    targets = [tools_root / package / "src" / package for package in PACKAGES]
    for target in targets:
        if not target.is_dir() or not (target.parents[1] / "pyproject.toml").is_file():
            raise ValueError(f"Not a Tool source directory: {target}")
    contents = {name: (source / name).read_bytes() for name in FILES}
    changed = []
    for target in targets:
        for name, content in contents.items():
            path = target / name
            if not path.is_file() or path.read_bytes() != content:
                changed.append(path)
                if not check:
                    path.write_bytes(content)
    return changed


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, default=project.parent / "tools")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = sync(project / "src/social_ops_agent", args.tools_root, check=args.check)
    for path in changed:
        print(f"{'Stale' if args.check else 'Updated'}: {path}")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
