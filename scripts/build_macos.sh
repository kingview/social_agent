#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

"$project_dir/scripts/install_harness.sh"
node_bin="${SOCIAL_AGENT_NODE:-/opt/homebrew/opt/node@24/bin/node}"
if [[ ! -x "$node_bin" ]]; then
  node_bin="$(command -v node)"
fi

python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "Missing .venv. Create it and install social_content_crawler plus this package first." >&2
  exit 1
fi

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name SocialAgent \
  --osx-bundle-identifier com.socialagent.client \
  --paths src \
  --add-data "$project_dir/harness:harness" \
  --add-binary "$node_bin:." \
  --exclude-module PIL \
  --exclude-module numpy \
  --collect-submodules social_ops_agent \
  desktop_main.py

app_path="$project_dir/dist/SocialAgent.app"
codesign --force --deep --sign - "$app_path"

echo "Built: $app_path"
