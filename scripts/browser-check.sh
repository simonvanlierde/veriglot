#!/usr/bin/env bash
# Serve fixture copies, then run the browser smoke test and axe scan in the Playwright image.
# Usage: scripts/browser-check.sh [screenshot-dir]
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(realpath "${1:-/tmp/veriglot-web}")
mkdir -p "$out/nl" "$out/en"
cp tests/fixtures/broken/nl/messages.po "$out/nl/messages.po"
cp tests/fixtures/credit-matrix/en/messages.po "$out/en/messages.po"
# One entry whose translation holds a bidi control character (U+202E) and trailing whitespace.
printf '\nmsgid "bidiTest"\nmsgstr "Hello"\n' >>"$out/en/messages.po"
printf '\nmsgid "bidiTest"\nmsgstr "Hallo\xe2\x80\xae x "\n' >>"$out/nl/messages.po"
: >"$out/url"
uv run veriglot review "$out/en/messages.po" "$out/nl/messages.po" --no-browser --port 8765 >"$out/url" &
server=$!
trap 'kill $server' EXIT
until [ -s "$out/url" ]; do sleep 0.2; done
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -v "$out:/out" -w /w \
    mcr.microsoft.com/playwright:v1.64.1-noble bash -c '
    npm i --silent --prefix /tmp/axe @axe-core/playwright playwright@1.64.1 >/dev/null
    cp scripts/browser-check.mjs /tmp/axe/ && node /tmp/axe/browser-check.mjs "$0" "$1"
  ' "$(cat "$out/url")" /out/review.png
