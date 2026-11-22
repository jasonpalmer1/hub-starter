#!/usr/bin/env bash
# hub-starter installer. Copies patterns into $HUBKIT_HOME (default ~/.claude/hubkit).
# Never overwrites a file, never writes settings.json (it prints a snippet for you to merge).
#   ./install.sh                  core patterns
#   ./install.sh --only ledger,deadman
#   ./install.sh --all
#   ./install.sh --dry-run        show what would happen, write nothing
#   ./install.sh --prefix hk-     prefix agent and command names you copy into ~/.claude
case "$(uname -s 2>/dev/null)" in   # Git Bash / MSYS / Cygwin: native Windows. WSL2 reports Linux and is the supported route.
  MINGW*|MSYS*|CYGWIN*) echo "This is native Windows. Use WSL2: see docs/WINDOWS.md" >&2; exit 2 ;;
esac
command -v python3 >/dev/null || { echo "install: python3 not found (3.9 or newer needed)"; exit 2; }
exec python3 "$(cd "$(dirname "$0")" && pwd)/tools/install.py" "$@"
