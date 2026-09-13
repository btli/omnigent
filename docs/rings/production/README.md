# Personal production ring

The scheduled production compose job is the sole automatic main-sync owner.
With empty migration approval it runs `stage.py sync-main`, then the composer
fetches fork main again. Manual and approval dispatches use published main.

```text
upstream/main -> sync-main (schedule, empty approval) -> published fork main
                                                      | base_sha
                                                      -> eligible PRs + production extras
                                                      -> migration gate -> production pin
```

Sync merges without rewriting main, checks ancestry, and uses an explicit
old-value lease. A stale-ref rejection permits one fresh-main retry; conflicts,
other push failures, or a second race fail closed. The three privileged ring
jobs share the non-cancelling `personal-ring-compose` concurrency group.

Production has no entry-zero merge or homelab overlay. Open non-draft btli PRs
and `extras-production.txt` numeric pins merge in ascending order. Reports carry
`base_sha` (published fork main) and `upstream_sha` (fresh upstream), including
blocked reports. Identity validation checks minted first-parent merges over
`base_sha..candidate`; equality or missing ancestry prevents publication.

The migration gate compares both `upstream_sha..candidate` and the previous
production pin against the candidate. A blocked run leaves ring refs unchanged,
although scheduled sync may already have advanced main. After the runbook's
verified CNPG backup checkpoint, approve the exact reported candidate SHA via
`approve_migration`. Drift produces a new candidate and requires new approval.

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

Expect ancestry exit 0, counts `0 N` with `N > 0`, production PR merge subjects
only, and no main-behind notice after the update-check cache refreshes. Compare
the report's base with main at publication: human main pushes or a later blocked
production run can leave the deployed immutable pin behind until recomposition.
Unchanged candidates reuse pins; changed same-day candidates allocate `-rerunN`.
Same-name dated compatibility branches remain deprecated for removal in v0.12.0.

See the [composer README](../../../../.github/scripts/personal-staging/README.md)
for migration approval, CLI flags and publication details.
