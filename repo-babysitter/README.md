# repo-babysitter

Two small bash scripts that watch over my repos so I don't lose work by forgetting to commit or push.

Like everything else in this repo, these are vibe coded.

## The scripts

### `gitchecker.sh`: just look

Read-only. Scans every git repo directly inside a folder and tells you which ones have:

- uncommitted changes (staged, unstaged, or untracked)
- commits that haven't been pushed
- no upstream branch set

It changes nothing. It exits with status `1` if anything is pending and `0` if everything is clean, so you can use it in other scripts too.

```bash
./gitchecker.sh ~/dev
```

### `gitadder.sh`: look, then back it all up

Does the same scan, then asks once: "Back up these repos?" If you say yes, for each repo it:

1. shows what changed
2. warns if untracked paths look like junk (`build/`, `node_modules/`, `target/`, `CMakeFiles/` and so on), and offers to add them to `.gitignore` instead of committing them
3. asks for a commit message (blank means `regular backup`, `s` skips that repo)
4. commits with `git add -A` and pushes to `origin`, using `-u` if the branch has no upstream yet

If something goes wrong in one repo (detached HEAD, failed commit, rejected push), it says so and moves on to the next.

```bash
./gitadder.sh ~/dev
```

## Setup

```bash
chmod +x gitchecker.sh gitadder.sh
```

Optional: add aliases to your `.bashrc` so you can run them from anywhere.

```bash
alias gcheck='~/dev/only-vibes/repo-babysitter/gitchecker.sh ~/dev'
alias gback='~/dev/only-vibes/repo-babysitter/gitadder.sh ~/dev'
```

## Things to know

- Both scripts only look at repos **one level down** from the path you give them, not nested ones.
- `gitadder.sh` uses `git add -A`, so it commits everything that isn't ignored. The junk warning catches the common cases, but have a `.gitignore` in place first.
- It never force pushes and never pulls. If the remote is ahead of you, the push fails and you sort it out by hand.
