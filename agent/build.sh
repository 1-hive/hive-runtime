#!/usr/bin/env bash
# Build localhost/1hive-agent from the host's current claude, codex and uv binaries.
#   deploy/agent/build.sh [tag]     (default tag: the date)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
mkdir "$CTX/bin"
for b in claude uv; do cp -L "$HOME/.local/bin/$b" "$CTX/bin/$b"; done
# Codex is a package (its binary finds helpers such as codex-code-mode-host beside it).
cp -rL "$(dirname "$(dirname "$(readlink -f "$HOME/.local/bin/codex")")")" "$CTX/codex"
cp "$HERE/Containerfile" "$HERE/containers.conf" "$HERE/hive-entry" "$CTX/"
TAG=${1:-$(date +%Y%m%d)}
podman build -t "localhost/1hive-agent:$TAG" -t localhost/1hive-agent:latest "$CTX"
echo "built localhost/1hive-agent:$TAG (claude $(claude --version | cut -d' ' -f1), $(codex --version))"
