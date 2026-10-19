# Where a hive keeps things, for the shell tools. Source it; the Python tools use hive_paths.py, with the same names.
# Each setting is an environment variable. The defaults are 1-hive's layout, so 1-hive sets none of them.
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this repository
: "${HIVE_ID:=1-hive}"
: "${HIVE_GATEWAY_PORT:=8470}"
: "${HIVE_URL:=http://127.0.0.1:$HIVE_GATEWAY_PORT}"
: "${HIVE_CONFIG_DIR:=$HOME/.config/hive}"                  # keys and secrets
: "${HIVE_WORK_DIR:=$HOME/work/1hive}"                      # task folders and tools' state
: "${HIVE_REPOS_DIR:=$HOME/repos}"                          # the hive's git remotes (<name>.git)
: "${HIVE_DEPLOY_DIR:=$HIVE_REPOS_DIR/1-hive/deploy}"       # the hive's declared configuration
: "${HIVE_WORKSPACE_REPO:=$HIVE_REPOS_DIR/hive-workspace.git}"
: "${HIVE_ROUTE_DIR:=$HIVE_REPOS_DIR/hive-route}"           # the router (hive-route) checkout
: "${HIVE_DEFAULT_REPO:=mtg-player}"                         # a task's code repository when its order names none
LOCAL_BIN=$HOME/.local/bin                                   # hive, uv, claude, codex
