#!/usr/bin/env bash
# Build a Chrome Web Store upload: dist/corpus-chrome-<version>.zip containing
# only the files the extension needs (no tests, scripts or package.json).
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(python3 -c 'import json;print(json.load(open("manifest.json"))["version"])')
mkdir -p dist
out="dist/corpus-chrome-$version.zip"
rm -f "$out"
zip -r -X "$out" manifest.json background icons lib options popup
echo "Wrote $out"
