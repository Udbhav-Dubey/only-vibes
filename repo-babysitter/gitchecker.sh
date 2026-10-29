#!/usr/bin/env bash
# gitchecker.sh - read-only check: which repos have work that isn't saved on the remote?
# usage: ./gitchecker.sh [path]    (default: current directory)
# Changes nothing. Exits with status 1 if anything is pending, so it works in other scripts too.

target_dir="${1:-.}"
found=0

for dir in "$target_dir"/*/; do
    dir="${dir%/}"
    [ -e "$dir/.git" ] || continue   # skip non-git folders

    dirty=$(git -C "$dir" status --porcelain)
    upstream=$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
    has_commits=0
    git -C "$dir" rev-parse -q --verify HEAD >/dev/null 2>&1 && has_commits=1

    ahead=0
    if [ -n "$upstream" ]; then
        ahead=$(git -C "$dir" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
    fi

    never_pushed=0
    [ -z "$upstream" ] && [ "$has_commits" -eq 1 ] && never_pushed=1

    if [ -n "$dirty" ] || [ "$ahead" -gt 0 ] || [ "$never_pushed" -eq 1 ]; then
        echo "=== $dir ==="
        [ -n "$dirty" ] && echo "  uncommitted changes"
        [ "$ahead" -gt 0 ] && echo "  $ahead commit(s) not pushed"
        [ "$never_pushed" -eq 1 ] && echo "  (no upstream branch set)"
        found=1
    fi
done

[ "$found" -eq 0 ] && echo "All repos clean and pushed."
exit "$found"
