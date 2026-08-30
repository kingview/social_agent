#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
tools_dir="$(cd "$project_dir/../tools" && pwd)"
python_bin="$project_dir/.venv/bin/python"
plugin_python="${SOCIAL_AGENT_PLUGIN_PYTHON:-}"
output_dir="${1:-$project_dir/dist/plugins}"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing $python_bin" >&2
  exit 1
fi

if [[ -z "$plugin_python" ]]; then
  for candidate in \
    /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.12 \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$python_bin"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      plugin_python="$candidate"
      break
    fi
  done
fi
if [[ -z "$plugin_python" || ! -x "$plugin_python" ]]; then
  echo "Missing compatible plugin Python (3.12 recommended)" >&2
  exit 1
fi

mkdir -p "$output_dir"
temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

build_bundle() {
  local source_dir="$1"
  local output_name="$2"
  local wheel_dir="$temp_dir/$output_name"
  mkdir -p "$wheel_dir"
  "$python_bin" -m pip wheel --no-deps --wheel-dir "$wheel_dir" "$source_dir"
  local wheel
  wheel="$(find "$wheel_dir" -maxdepth 1 -name '*.whl' -print -quit)"
  local lock
  lock="$("$python_bin" -m social_ops_agent.plugin_cli lock \
    --manifest "$source_dir/plugin/plugin.json" \
    --wheel "$wheel" \
    --python "$plugin_python" \
    --output-directory "$wheel_dir")"
  "$python_bin" -m social_ops_agent.plugin_cli bundle \
    --manifest "$source_dir/plugin/plugin.json" \
    --wheel "$wheel" \
    --lock "$lock" \
    --output "$output_dir/$output_name.socialtool"
}

build_bundle "$tools_dir/social_content_crawler" "social-content"
build_bundle "$tools_dir/media_content_analyzer" "media-content"

echo "Built Tool plugins in $output_dir"
