#!/bin/bash
exec "$(dirname "$0")/run_usenix_exp.sh" "$(dirname "$0")/../experiment-configs/paper-eval/usenix_grapple_bug-fixing_atlantis-claude-code_crsbench-all.yaml" "$@"
