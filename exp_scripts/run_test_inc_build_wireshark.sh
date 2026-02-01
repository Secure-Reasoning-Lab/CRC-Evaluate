#!/bin/bash
exec "$(dirname "$0")/run_usenix_exp.sh" "$(dirname "$0")/../experiment-configs/test-inc-build-wireshark.yaml" "$@"
