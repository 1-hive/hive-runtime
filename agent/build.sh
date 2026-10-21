#!/usr/bin/env bash
# Build localhost/1hive-agent from the host's current claude, codex and uv binaries.
#   agent/build.sh [tag]     (default tag: the date)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
mkdir "$CTX/bin"
for b in claude uv; do
  f=$HOME/.local/bin/$b; [ -x "$f" ] || f=$(command -v "$b") || { echo "no $b on this host" >&2; exit 2; }
  cp -L "$f" "$CTX/bin/$b"
done
# Codex is a package (its binary finds helpers such as codex-code-mode-host beside it). Optional:
# the chief of staff doesn't need it.
mkdir "$CTX/codex"
if c=$(command -v codex || ls "$HOME/.local/bin/codex" 2>/dev/null); then
  cp -rL "$(dirname "$(dirname "$(readlink -f "$c")")")"/. "$CTX/codex/"
fi
cp "$HERE/Containerfile" "$HERE/containers.conf" "$HERE/hive-entry" "$CTX/"
cp "$HERE/../hive-mcp.py" "$CTX/"
TAG=${1:-$(date +%Y%m%d)}
${HIVE_CONTAINER_RUNTIME:-podman} build --build-arg "HIVERECORD_REV=${HIVE_RECORD_TOOL_REF:-v1.1.0}" -t "localhost/1hive-agent:$TAG" -t localhost/1hive-agent:latest "$CTX"
echo "built localhost/1hive-agent:$TAG (claude $(claude --version | cut -d' ' -f1), $(codex --version 2>/dev/null || echo "no codex"))"
