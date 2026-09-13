# Personal staging ring

The hourly and nightly staging workflows compose from published fork main.
Neither workflow syncs or pushes main; scheduled production owns that operation.

```text
published fork main (base_sha)
  -> fresh upstream/main (upstream_sha, entry zero)
  -> open btli PRs + extras.txt, ascending PR number
  -> homelab overlay last
  -> staging; nightly also pins nightly-YYYYMMDD[-rerunN]
```

Reports carry `base_sha`, `upstream_sha`, and a separate `entry_zero` record.
The entry-zero subject is `staging: merge upstream <sha12>`. Its `minted` field
is false when published main already contains upstream. Verified rerere seeds
can resolve entry-zero conflicts; unresolved conflicts fail without publishing.
The PR stream and rescue target remain upstream-based. No upstream PR ref is
rewritten; a staging rescue can update only the contributor's fork branch.

The composer requires a strict descendant of the base and rechecks remote main
before publication. The three privileged composition jobs share a non-cancelling
`personal-ring-compose` concurrency group. Human pushes to main can still make
an existing staging pin fall behind until the next composition.

To verify a completed run, download its `merge-report` artifact, then run in a
disposable clone of the fork (replace `PIN` with the report's `tag`, or use
`origin/staging` for an hourly run):

```sh
git fetch origin main staging --tags
git merge-base --is-ancestor origin/main PIN
git rev-list --left-right --count origin/main...PIN
git log --first-parent --reverse --format='%s' origin/main..PIN
```

Expect ancestry exit 0, counts `0 N` with `N > 0`, and entry zero before PR
merges when upstream was ahead. Check `base_sha` against the main SHA observed
at publication and verify the homelab merge is last. A later human main push
invalidates comparison with today's main; compare with the recorded base then.

See the [composer README](../../../../.github/scripts/personal-staging/README.md)
for CLI flags, pin semantics and conflict-resolution maintenance.
