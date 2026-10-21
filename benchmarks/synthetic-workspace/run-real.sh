#!/usr/bin/env sh
# Real-agent benchmark entry point (explicit start only): see run_real.py. Refuses unless PICO_REAL_BENCHMARK=1.
exec python3 "$(dirname "$0")/run_real.py" "$@"
