#!/usr/bin/env sh
# Thin wrapper: see setup.py for options.
exec python3 "$(dirname "$0")/setup.py" "$@"
