#!/usr/bin/env bash
# Print the table of files that ship (or --write / --check). See ship_list.py.
exec python3 "$(dirname "$0")/ship_list.py" "$@"
