#!/usr/bin/env bash
# Point this clone at tools/githooks so a commit or merge as a personal identity is refused (and set the kit identity here).
# Run once per clone, including scratch clones. Usage: bash tools/install-hooks.sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$ROOT" config core.hooksPath tools/githooks
git -C "$ROOT" config user.name hub-starter
git -C "$ROOT" config user.email hub-starter@users.noreply.github.com
echo "install-hooks: core.hooksPath=tools/githooks, identity set to hub-starter"
