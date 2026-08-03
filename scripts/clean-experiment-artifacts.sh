#!/usr/bin/env bash
# Soft-clean CRSBench experiment-data bulk while preserving audit-critical files.
#
# Removes rebuildable bulk:
#   - oss-crs-workdir/   (OSS-Fuzz BUILD_OUT, coverage, compose run caches)
#   - staged/            (staged benchmark copy)
#   - agent pollution under registered log dirs (rebuild_out, coverage builds, etc.)
#   - Codex plugin cache under .codex/.tmp
#
# Preserves for organizer audit / anti-tamper review:
#   - LLM trajectories (claude_stream.jsonl, claude_stdout.log, Codex sessions/*.jsonl)
#   - agent prompts/roles (agent_*.txt, agent_*.md)
#   - POVs, patches, metadata.json, llm-usage.json, verification JSON, worker.log
#   - output/logs/services and docker-compose logs
#
# Usage:
#   ./scripts/clean-experiment-artifacts.sh --dry-run PATH...
#   ./scripts/clean-experiment-artifacts.sh --yes PATH...
#
# PATH may be:
#   - a trial directory (contains metadata.json)
#   - an experiment-data directory
#   - a team results directory (…/results or …/results/experiment-data)
#   - a team root (…/team-01) containing results/experiment-data
#
# Examples:
#   ./scripts/clean-experiment-artifacts.sh --dry-run .run/sanity/team-01
#   ./scripts/clean-experiment-artifacts.sh --yes .run/trivial/team-01/results/experiment-data

set -euo pipefail

DRY_RUN=0
ASSUME_YES=0
PATHS=()

usage() {
  sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage 0 ;;
    --) shift; PATHS+=("$@"); break ;;
    -*)
      echo "Unknown option: $1" >&2
      usage 2
      ;;
    *) PATHS+=("$1"); shift ;;
  esac
done

if [[ ${#PATHS[@]} -eq 0 ]]; then
  echo "Error: provide at least one path" >&2
  usage 2
fi

if [[ "$DRY_RUN" -eq 0 && "$ASSUME_YES" -eq 0 ]]; then
  echo "Error: refusing to delete without --yes (or pass --dry-run)" >&2
  exit 2
fi

# --- helpers -----------------------------------------------------------------

bytes_of() {
  # Portable du: prefer -sb (GNU), fall back to -sk * 1024.
  local p=$1
  if [[ ! -e "$p" ]]; then
    echo 0
    return
  fi
  if du -sb "$p" &>/dev/null; then
    du -sb "$p" 2>/dev/null | awk '{print $1}'
  else
    local k
    k=$(du -sk "$p" 2>/dev/null | awk '{print $1}')
    echo $((k * 1024))
  fi
}

fmt_bytes() {
  local n=${1:-0}
  awk -v n="$n" 'BEGIN {
    split("B K M G T", u, " ")
    i = 1
    while (n >= 1024 && i < 5) { n /= 1024; i++ }
    if (i == 1) printf "%d%s", n, u[i]
    else printf "%.1f%s", n, u[i]
  }'
}

is_trial_dir() {
  local d=$1
  [[ -d "$d" ]] || return 1
  [[ -f "$d/metadata.json" ]] || return 1
  local base
  base=$(basename "$d")
  [[ "$base" == trial-* ]] || return 1
  return 0
}

# Resolve user paths into trial directories (…/trial-N with metadata.json).
collect_trials() {
  local root=$1
  local -a found=()

  if is_trial_dir "$root"; then
    printf '%s\n' "$root"
    return
  fi

  # Prefer experiment-data if present under a team/results root.
  local search=$root
  if [[ -d "$root/results/experiment-data" ]]; then
    search="$root/results/experiment-data"
  elif [[ -d "$root/experiment-data" ]]; then
    search="$root/experiment-data"
  fi

  # Only match trial dirs whose immediate child is metadata.json (not nested
  # plugin copies of metadata.json under .codex/.tmp).
  while IFS= read -r -d '' meta; do
    local trial
    trial=$(dirname "$meta")
    if is_trial_dir "$trial"; then
      found+=("$trial")
    fi
  done < <(find "$search" -type f -name metadata.json -print0 2>/dev/null)

  if [[ ${#found[@]} -eq 0 ]]; then
    return
  fi
  printf '%s\n' "${found[@]}" | sort -u
}

# Paths we never delete (basename match for trajectory / prompts).
# Used when pruning agent workdirs.
is_protected_file() {
  local f=$1
  local base
  base=$(basename "$f")
  case "$base" in
    claude_stream.jsonl|claude_stdout.log|claude_stderr.log) return 0 ;;
    agent_prompt.txt|agent_role.md|agent_claude_md.md|agent_prompt.md) return 0 ;;
    libcrs-sidecar-metrics.jsonl) return 0 ;;
  esac
  # Codex session rollouts
  if [[ "$base" == rollout-*.jsonl ]]; then
    return 0
  fi
  return 1
}

# Plan entries: print "DELETE|path|bytes" or "SKIP|path|reason"
plan_trial_deletes() {
  local trial=$1
  local p

  for p in "$trial/oss-crs-workdir" "$trial/staged"; do
    if [[ -e "$p" ]]; then
      printf 'DELETE|%s|%s\n' "$p" "$(bytes_of "$p")"
    fi
  done

  # Known bulk pollution directory names anywhere under output/logs or under
  # remaining agent workdirs. Trajectories sit beside these, not inside them
  # (except pathological nested copies which we still drop as rebuild noise).
  local pollution_names=(
    rebuild_out
    rebuild_tight
    OSS_CRS_BUILD_OUT_DIR
    BUILD_OUT_DIR
    coverage
  )

  # Walk output/logs for pollution dirs (depth-limited name match via find).
  if [[ -d "$trial/output/logs" ]]; then
    local name
    for name in "${pollution_names[@]}"; do
      while IFS= read -r -d '' p; do
        printf 'DELETE|%s|%s\n' "$p" "$(bytes_of "$p")"
      done < <(find "$trial/output/logs" -type d -name "$name" -print0 2>/dev/null)
    done

    # Codex downloads plugin cache (not sessions).
    while IFS= read -r -d '' p; do
      printf 'DELETE|%s|%s\n' "$p" "$(bytes_of "$p")"
    done < <(find "$trial/output/logs" -type d \( -path '*/.codex/.tmp' -o -path '*/.codex/tmp' \) -print0 2>/dev/null)

    # Nested recursive LOG_DIR re-registration (agent mirrored log tree).
    # Keep top-level log_dir/agent; delete deeper OSS_CRS_LOG_DIR nests.
    while IFS= read -r -d '' p; do
      # only if path has OSS_CRS_LOG_DIR at least twice, or is under rebuild_out
      local rel=${p#"$trial/"}
      local count
      count=$(awk -F'OSS_CRS_LOG_DIR' '{print NF-1}' <<<"$rel")
      if [[ "$count" -ge 1 && "$rel" == *rebuild_out* ]]; then
        printf 'DELETE|%s|%s\n' "$p" "$(bytes_of "$p")"
      elif [[ "$count" -ge 2 ]]; then
        printf 'DELETE|%s|%s\n' "$p" "$(bytes_of "$p")"
      fi
    done < <(find "$trial/output/logs" -type d -name 'OSS_CRS_LOG_DIR' -print0 2>/dev/null)
  fi
}

dedupe_delete_plan() {
  # If parent is deleted, drop children. Input: DELETE|path|bytes lines.
  # Sort by path length ascending so parents come first; skip if ancestor marked.
  local -a lines=()
  local -a keep=()
  local line path bytes

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    lines+=("$line")
  done

  if [[ ${#lines[@]} -eq 0 ]]; then
    return
  fi

  # Sort by path length (shortest first = parents first)
  local sorted
  sorted=$(printf '%s\n' "${lines[@]}" | awk -F'|' '{print length($2), $0}' | sort -n | cut -d' ' -f2-)

  local -a parents=()
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    path=${line#DELETE|}
    path=${path%%|*}
    local skip=0 p
    for p in "${parents[@]+"${parents[@]}"}"; do
      if [[ "$path" == "$p"/* || "$path" == "$p" ]]; then
        skip=1
        break
      fi
    done
    if [[ "$skip" -eq 1 ]]; then
      continue
    fi
    parents+=("$path")
    printf '%s\n' "$line"
  done <<<"$sorted"
}

# --- main --------------------------------------------------------------------

declare -a ALL_TRIALS=()
for raw in "${PATHS[@]}"; do
  # resolve relative to cwd
  if [[ "$raw" != /* ]]; then
    raw=$(pwd)/$raw
  fi
  if [[ ! -e "$raw" ]]; then
    echo "Warning: path not found: $raw" >&2
    continue
  fi
  while IFS= read -r t; do
    [[ -n "$t" ]] || continue
    ALL_TRIALS+=("$t")
  done < <(collect_trials "$raw")
done

if [[ ${#ALL_TRIALS[@]} -eq 0 ]]; then
  echo "No trial directories found under: ${PATHS[*]}"
  exit 0
fi

# unique trials
mapfile -t ALL_TRIALS < <(printf '%s\n' "${ALL_TRIALS[@]}" | sort -u)

echo "Found ${#ALL_TRIALS[@]} trial(s)"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Mode: DRY-RUN (no deletions)"
else
  echo "Mode: DELETE"
fi
echo

TOTAL_BYTES=0
TOTAL_TARGETS=0
declare -a PLAN_LINES=()

for trial in "${ALL_TRIALS[@]}"; do
  before=$(bytes_of "$trial")
  mapfile -t trial_plan < <(plan_trial_deletes "$trial" | dedupe_delete_plan)
  if [[ ${#trial_plan[@]} -eq 0 ]]; then
    echo "== $trial"
    echo "   (nothing to clean; already thin)"
    echo "   size: $(fmt_bytes "$before")"
    echo
    continue
  fi

  echo "== $trial"
  echo "   before: $(fmt_bytes "$before")"
  trial_sum=0
  for line in "${trial_plan[@]}"; do
    path=${line#DELETE|}
    path=${path%%|*}
    bytes=${line##*|}
    trial_sum=$((trial_sum + bytes))
    TOTAL_BYTES=$((TOTAL_BYTES + bytes))
    TOTAL_TARGETS=$((TOTAL_TARGETS + 1))
    echo "   - $(fmt_bytes "$bytes")  $path"
    PLAN_LINES+=("$path")
  done
  echo "   reclaimable (this trial): $(fmt_bytes "$trial_sum")"
  echo
done

echo "--------------------------------------------------"
echo "Targets: $TOTAL_TARGETS paths across ${#ALL_TRIALS[@]} trial(s)"
echo "Estimated reclaim: $(fmt_bytes "$TOTAL_BYTES")"
echo "--------------------------------------------------"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry-run complete. Re-run with --yes to delete."
  exit 0
fi

# Verify trajectories exist before/after for reporting
check_trajectories() {
  local trial=$1
  local n=0
  # Claude
  n=$(find "$trial" \( -name 'claude_stream.jsonl' -o -name 'claude_stdout.log' \) \
        ! -path '*/rebuild_out/*' ! -path '*/OSS_CRS_BUILD_OUT_DIR/*' \
        2>/dev/null | wc -l)
  # Codex sessions
  local c
  c=$(find "$trial" -path '*/.codex/sessions/*' -name 'rollout-*.jsonl' 2>/dev/null | wc -l)
  echo $((n + c))
}

declare -a PRE_TRAJ=()
for trial in "${ALL_TRIALS[@]}"; do
  PRE_TRAJ+=("$(check_trajectories "$trial")")
done

echo "Deleting..."
for path in "${PLAN_LINES[@]+"${PLAN_LINES[@]}"}"; do
  if [[ -e "$path" ]]; then
    rm -rf -- "$path"
  fi
done

echo
echo "After:"
i=0
for trial in "${ALL_TRIALS[@]}"; do
  after=$(bytes_of "$trial")
  traj=${PRE_TRAJ[$i]}
  traj_after=$(check_trajectories "$trial")
  echo "  $(fmt_bytes "$after")  traj_files=${traj_after} (was ${traj})  $trial"
  if [[ "$traj" -gt 0 && "$traj_after" -eq 0 ]]; then
    echo "  WARNING: trajectory files missing after clean: $trial" >&2
  fi
  i=$((i + 1))
done

echo "Done."
