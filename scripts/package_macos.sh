#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_path="$project_dir/dist/SocialAgent.app"
plugin_dir="$project_dir/dist/plugins"
output_path="${1:-$project_dir/dist/SocialAgent-macOS-arm64.zip}"

if [[ ! -d "$app_path" ]]; then
  echo "Missing $app_path; run scripts/build_macos.sh first." >&2
  exit 1
fi

staging_dir="$(mktemp -d)"
trap 'rm -rf "$staging_dir"' EXIT
package_root="$staging_dir/package"
mkdir -p "$package_root/Tool Plugins"
/usr/bin/ditto "$app_path" "$package_root/SocialAgent.app"

for plugin in "$plugin_dir"/*.socialtool; do
  [[ -f "$plugin" ]] || continue
  /usr/bin/ditto "$plugin" "$package_root/Tool Plugins/$(basename "$plugin")"
done

(
  cd "$package_root"
  /usr/bin/ditto -c -k --sequesterRsrc . "$output_path"
)

echo "Packaged: $output_path"
