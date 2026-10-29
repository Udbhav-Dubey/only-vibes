#!/usr/bin/env bash
# gitadder.sh - find repos with uncommitted or unpushed work and back them up.
# usage: ./gitadder.sh [path]    (default: current directory)
#
# For each git repo directly inside [path]:
#   1. lists the ones with pending work
#   2. asks once before touching anything
#   3. per repo: shows changes, warns about junk (build/, node_modules/ ...),
#      commits, and pushes

target_dir="${1:-.}"
pending=()

# folders that almost always mean "you forgot a .gitignore"
risky_re='(^|/)(build|node_modules|target|dist|__pycache__|\.venv|venv|\.cache|CMakeFiles)/?$'

echo "Scanning $target_dir ..."
echo

for dir in "$target_dir"/*/; do
    dir="${dir%/}"
    [ -e "$dir/.git" ] || continue

    dirty=$(git -C "$dir" status --porcelain)
    upstream=$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
    has_commits=0
    git -C "$dir" rev-parse -q --verify HEAD >/dev/null 2>&1 && has_commits=1

    ahead=0
    if [ -n "$upstream" ]; then
        ahead=$(git -C "$dir" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
    fi

    # pending = dirty, ahead of upstream, or commits that were never pushed anywhere
    never_pushed=0
    [ -z "$upstream" ] && [ "$has_commits" -eq 1 ] && never_pushed=1

    if [ -n "$dirty" ] || [ "$ahead" -gt 0 ] || [ "$never_pushed" -eq 1 ]; then
        echo "=== $dir ==="
        [ -n "$dirty" ] && echo "  uncommitted changes"
        [ "$ahead" -gt 0 ] && echo "  $ahead commit(s) not pushed"
        [ "$never_pushed" -eq 1 ] && echo "  (no upstream branch set, will push with -u)"
        pending+=("$dir")
    fi
done

if [ ${#pending[@]} -eq 0 ]; then
    echo "Everything clean and pushed. Nothing to do."
    exit 0
fi

echo
read -rp "Back up these ${#pending[@]} repo(s)? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || exit 0

for dir in "${pending[@]}"; do
    echo
    echo "--- $dir ---"

    branch=$(git -C "$dir" symbolic-ref --short -q HEAD)
    if [ -z "$branch" ]; then
        echo "  detached HEAD, skipping (check out a branch first)"
        continue
    fi

    dirty=$(git -C "$dir" status --porcelain)
    if [ -n "$dirty" ]; then
        git -C "$dir" status --short

        # warn about untracked junk before "git add -A" sweeps it in
        junk=$(git -C "$dir" status --porcelain | sed -n 's/^?? //p' | grep -E "$risky_re")
        if [ -n "$junk" ]; then
            echo
            echo "  WARNING: these untracked paths look like build/dependency junk:"
            echo "$junk" | sed 's/^/    /'
            [ -f "$dir/.gitignore" ] || echo "  (this repo has no .gitignore)"
            read -rp "  [i] add them to .gitignore, [c] commit anyway, [s] skip repo (default i): " choice
            case "$choice" in
                s|S)
                    echo "  skipped"
                    continue
                    ;;
                c|C)
                    ;;
                *)
                    # make sure the file ends with a newline before appending
                    [ -s "$dir/.gitignore" ] && [ -n "$(tail -c1 "$dir/.gitignore")" ] && echo >> "$dir/.gitignore"
                    echo "$junk" >> "$dir/.gitignore"
                    echo "  added to .gitignore"
                    ;;
            esac
        fi

        read -rp "  Commit message (blank = 'regular backup', 's' = skip this repo): " msg
        if [ "$msg" = "s" ]; then
            echo "  skipped"
            continue
        fi
        [ -z "$msg" ] && msg="regular backup"

        git -C "$dir" add -A
        git -C "$dir" commit -m "$msg" || { echo "  commit failed, skipping push"; continue; }
    fi

    if git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
        push_cmd=(push origin "$branch")
    else
        push_cmd=(push -u origin "$branch")
    fi

    if git -C "$dir" "${push_cmd[@]}"; then
        echo "  -> pushed to origin/$branch"
    else
        echo "  push failed (remote ahead? no origin?). Fix it by hand in $dir"
    fi
done

echo
echo "Done."
