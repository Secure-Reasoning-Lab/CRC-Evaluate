#!/bin/bash
# Cleanup failed trial directories marked with .fail files.
#
# Usage: ./scripts/cleanup-failed-trials.sh [path]

set -e

SEARCH_PATH="${1:-.}"

# Detect fd binary (fd on most systems, fdfind on Debian/Ubuntu)
if command -v fd &>/dev/null; then
    FD=fd
elif command -v fdfind &>/dev/null; then
    FD=fdfind
else
    echo "Error: fd is not installed (tried 'fd' and 'fdfind')" >&2
    exit 1
fi

# Find directories containing .fail files
DIRS=$("$FD" -H -g '.fail' "$SEARCH_PATH" | sed 's|/[^/]*$||' | sort -u)

if [ -z "$DIRS" ]; then
    echo "No failed trial directories found."
    exit 0
fi

# Display directories
echo "Directories to be deleted:"
while IFS= read -r dir; do
    SIZE=$(du -sh "$dir" 2>/dev/null | cut -f1)
    echo "  $dir ($SIZE)"
done <<< "$DIRS"

# Count and total size
COUNT=$(wc -l <<< "$DIRS")
TOTAL=$(xargs du -shc 2>/dev/null <<< "$DIRS" | tail -1 | cut -f1)
echo ""
echo "Total: $COUNT directories, $TOTAL"
echo ""

# Confirm
read -p "Delete these directories? [y/N] " CONFIRM
if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
    xargs rm -rf <<< "$DIRS"
    echo "Deleted $COUNT directories."
else
    echo "Aborted."
fi
