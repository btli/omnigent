# Personal production ring

To verify a completed run, use a disposable clone and replace `PIN` with the
report's immutable `production-YYYYMMDD[-rerunN]` tag:

```sh
git fetch origin main production --tags
git merge-base --is-ancestor origin/main PIN
git rev-list --left-right --count origin/main...PIN
git log --first-parent --reverse --format='%s' origin/main..PIN
git checkout --detach PIN
omni
```

Expect ancestry exit 0 and counts `0 N`; `N` may be zero when a zero-input
production run pins main. Check production merge subjects and confirm that the
update check reports main is not behind. Compare the report's `base_sha` with
main at publication; a later main push or blocked production run can leave the
immutable pin behind.

Git can elide the matching no-op main refspec from an atomic push. A
post-publication read records `base_matches_remote_main` and detects a main move
after advertisement. That detection fails the run, but ring or rescue refs may
already have moved; they are not rolled back, and the next compose reconciles
them from the new main.

If sync-main conflicts, check out fork main, fetch and merge upstream main,
resolve and commit, then use a normal `git push origin main`. Never force or
force-with-lease main. Use **Re-run jobs** on the failed scheduled run afterward;
an empty workflow dispatch skips sync-main. To roll back the design, revert the
change on main; `--base-ref upstream/main` cannot bypass the published-main
check.

For composition semantics, migration approval, concurrency, and detailed
recovery guidance, see the
[composer README](../../../../.github/scripts/personal-staging/README.md).
