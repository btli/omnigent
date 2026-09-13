# Fork staging pipeline (NOT upstream)

`staging` is a **derived build artifact** based on the fork's published
`origin/main`, plus our upstream PRs and the `homelab` overlay. It is rebuilt,
never rebased, and never used as a base for PR branches.

```
origin/main ──┐
PR tips (pinned in staging-manifest.txt, merged as-is) ──┼──▶ staging
homelab overlay (merged last) ──┘
```

This follows Design D: both deployment rings base on published fork main, and
only the scheduled production nightly advances `origin/main`, once daily.
The automated staging composer then merges fresh `upstream/main` as entry zero
before its PR entries, retaining upstream freshness while keeping fork-main
ancestry. Production has no entry zero. This manual manifest builder uses the
published `origin/main` directly and keeps its pinned-PR workflow unchanged.

Rules that keep upstream approvals intact:

- **PR branches are frozen once under review.** Never rebase or force-push
  them during a sync; any push to a PR branch dismisses maintainer approvals.
  Only update a PR when upstream mergeability actually demands it (and then by
  merging `upstream/main` into it, right before the maintainer merges).
- **Conflicts against newer upstream are absorbed in staging's merge
  commits**, not in the PR branches. `git rerere` records each resolution and
  replays it on every later rebuild, so re-syncing is cheap.
- **`homelab` holds everything fork-only** (overlay code, these scripts). It
  has no upstream PRs, so it can be rewritten freely.

## Syncing

```sh
just sync-staging            # rebuild on latest origin/main, no push
just sync-staging --push     # rebuild and force-with-lease push origin staging
```

The script fetches `origin/main` before resolving its default base. Pass
`--base <ref>` only for an intentional alternate-base composition.

The rebuild happens in a separate worktree (`../omnigent-worktrees/
staging-rebuild`); your checkout is untouched. On a conflict rerere can't
resolve, the script stops with instructions — resolve in the worktree, commit,
rerun; already-resolved merges replay instantly.

## Maintaining the manifest

`staging-manifest.txt` pins each upstream PR's reviewed head sha.

- PR merged upstream → delete its line (content arrives after fork main advances).
- You pushed a new revision to a PR → update its pinned sha.
- New PR opened → append a line (`<pr> <head-sha> <label>`).

The script warns about merged/closed/moved PRs when `gh` is available.
