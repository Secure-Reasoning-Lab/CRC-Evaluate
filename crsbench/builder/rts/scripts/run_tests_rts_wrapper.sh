#!/bin/bash
set -euo pipefail

# RTS_ON and RTS_TOOL are provided via container env at runtime.
# When RTS_ON is unset, this wrapper is a transparent pass-through.
if [ -n "${RTS_ON:-}" ]; then
    if [ ! -f /rts_project_initialized ]; then
        echo "[RTS] Running project init with tool: ${RTS_TOOL:-unknown}..."
        python3 /rts_init_project.py "$(pwd)" --tool "${RTS_TOOL}"
        touch /rts_project_initialized
    else
        echo "[RTS] Project already initialized, skipping init."
    fi
fi

# Resolve and execute the project test script.
if [ -f /src/test.sh ]; then
    exec bash /src/test.sh
else
    echo "[RTS wrapper] No /src/test.sh found."
    exit 0
fi
