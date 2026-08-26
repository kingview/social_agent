#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
node_bin="${SOCIAL_AGENT_NODE:-}"
if [[ -z "$node_bin" && -x /opt/homebrew/opt/node@24/bin/node ]]; then
  node_bin=/opt/homebrew/opt/node@24/bin/node
fi
if [[ -z "$node_bin" ]]; then
  node_bin="$(command -v node || true)"
fi
if [[ -z "$node_bin" ]]; then
  echo "Node.js 24 is required. On macOS run: brew install node@24" >&2
  exit 1
fi

node_version="$($node_bin --version)"
node_major="${node_version#v}"
node_major="${node_major%%.*}"
node_minor="${node_version#v$node_major.}"
node_minor="${node_minor%%.*}"
if (( node_major < 22 || (node_major == 22 && node_minor < 19) )); then
  echo "DeepSeek Harness requires Node.js 22.19+ or 24+; found $node_version." >&2
  exit 1
fi

npm_bin="$(dirname "$node_bin")/npm"
if [[ ! -x "$npm_bin" ]]; then
  npm_bin="$(command -v npm || true)"
fi
if [[ -z "$npm_bin" ]]; then
  echo "npm was not found next to $node_bin." >&2
  exit 1
fi

PATH="$(dirname "$node_bin"):$PATH" "$npm_bin" --prefix "$project_dir/harness" ci
"$node_bin" --check "$project_dir/harness/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/bin.js"
echo "DeepSeek Harness dependencies are ready ($node_version)."
