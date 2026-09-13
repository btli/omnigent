# Personal staging ring

To verify a completed run, download its `merge-report` artifact, then run in a
disposable clone of the fork. Replace `PIN` with the report's `tag`, or use
`origin/staging` for an hourly run:

```sh
git fetch origin main staging --tags
git merge-base --is-ancestor origin/main PIN
git rev-list --left-right --count origin/main...PIN
git log --first-parent --reverse --format='%s' origin/main..PIN
```

Expect ancestry exit 0 and counts `0 N`; `N` may be zero when upstream and all
inputs are already contained in main. Check `base_sha` against main at
publication and verify that entry zero precedes PR merges and the homelab merge
is last. A later main push requires comparison with the recorded base.

For composition semantics, concurrency, conflict recovery, and rollback, see
the [composer README](../../../../.github/scripts/personal-staging/README.md).
