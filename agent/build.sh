#!/usr/bin/env bash
# Build localhost/1hive-agent from the host's current claude, codex and uv binaries.
#   deploy/agent/build.sh [tag]     (default tag: the date)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
mkdir "$CTX/bin"
for b in claude codex uv; do cp -L "$HOME/.local/bin/$b" "$CTX/bin/$b"; done
cp "$HERE/Containerfile" "$HERE/containers.conf" "$CTX/"
TAG=${1:-$(date +%Y%m%d)}
podman build -t "localhost/1hive-agent:$TAG" -t localhost/1hive-agent:latest "$CTX"
echo "built localhost/1hive-agent:$TAG (claude $(claude --version | cut -d' ' -f1), $(codex --version))"
