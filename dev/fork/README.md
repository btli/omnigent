# Fork staging pipeline (NOT upstream)

`staging` is a **derived build artifact** based on the fork's published
`origin/main`, plus our upstream PRs and the `homelab` overlay. It is rebuilt,
never rebased, and never used as a base for PR branches.

```
origin/main ──┐
PR tips (pinned in staging-manifest.txt, merged as-is) ──┼──▶ staging
homelab overlay (merged last) ──┘
```

Under Design D, the automated rings both base on published fork main. The
scheduled production nightly advances `origin/main` once daily; automated
staging merges fresh `upstream/main` as entry zero before its PR entries, while
production has no entry zero. This manual builder adds no entry zero, so its
upstream freshness defaults to that of `origin/main`. Use `--base upstream/main`
only for an intentional alternate-base build.

Rules that keep upstream approvals intact:

- **PR branches are frozen once under review.** Never rebase or force-push
  them during a sync; any push to a PR branch dismisses maintainer approvals.
  Only update a PR when upstream mergeability actually demands it (and then by
  merging `upstream/main` into it, right before the maintainer merges).
- **Conflicts against newer upstream are absorbed in staging's merge
  commits**, not in the PR branches. `git rerere` records each resolution and
  replays it on every later rebuild, so re-syncing is cheap.
- **`homelab` is an append-only merge chain** containing `origin/main`; the host
  fleet advances its shared checkout to `origin/homelab` with `--ff-only`.
  Never reset or force-push it. Revert a bad merge with
  `git revert -m 1 <merge-sha>` and push normally.

## Syncing

```sh
just sync-staging            # rebuild on latest origin/main, no push
just sync-staging --push     # rebuild and force-with-lease push origin staging
```

The script fetches `origin/main` before resolving its default base.

The rebuild happens in a separate worktree (`../omnigent-worktrees/
staging-rebuild`); your checkout is untouched. On a conflict rerere can't
resolve, the script stops with instructions — resolve in the worktree, commit,
rerun; already-resolved merges replay instantly.

## Maintaining the manifest

`staging-manifest.txt` pins each upstream PR's reviewed head sha.

- PR merged upstream → retain its pin until the selected base contains the
  merged result, then delete it. For squash merges, check by content or subject
  (for example, `git log --oneline origin/main | grep '(#<pr>)'`).
- You pushed a new revision to a PR → update its pinned sha.
- New PR opened → append a line (`<pr> <head-sha> <label>`).

The script warns about merged/closed/moved PRs when `gh` is available.
