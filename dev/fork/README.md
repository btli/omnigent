# Fork staging pipeline (NOT upstream)

`staging` is a **derived build artifact**: upstream `main` + our open upstream
PRs + the `homelab` overlay. It is rebuilt, never rebased, and never used as a
base for PR branches.

```
upstream/main ──┐
PR tips (pinned in staging-manifest.txt, merged as-is) ──┼──▶ staging
homelab overlay (merged last) ──┘
```

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
just sync-staging            # rebuild on latest upstream/main, no push
just sync-staging --push     # rebuild and force-with-lease push origin staging
```

The rebuild happens in a separate worktree (`../omnigent-worktrees/
staging-rebuild`); your checkout is untouched. On a conflict rerere can't
resolve, the script stops with instructions — resolve in the worktree, commit,
rerun; already-resolved merges replay instantly.

## Maintaining the manifest

`staging-manifest.txt` pins each upstream PR's reviewed head sha.

- PR merged upstream → delete its line (content arrives via `upstream/main`).
- You pushed a new revision to a PR → update its pinned sha.
- New PR opened → append a line (`<pr> <head-sha> <label>`).

The script warns about merged/closed/moved PRs when `gh` is available.
