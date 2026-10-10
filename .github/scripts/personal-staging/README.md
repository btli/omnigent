# Personal staging nightly (fork-only)

`.github/workflows/personal-staging.yml` runs nightly (10:00 UTC — cron
`0 10 * * *` — plus `workflow_dispatch`) on the `btli/omnigent` fork only.
It:

1. Composes fork branch `staging` from published fork main, merges fresh
   upstream `omnigent-ai/omnigent` main as entry zero, then adds
   every open btli PR plus the [`extras.txt`](#extras-manifest-extrastxt)
   pins, merged sequentially ascending by PR number (`stage.py`). A
   conflicting open PR gets one rebase rescue: its head is replayed onto
   upstream main (reproducible identity/dates, already-landed patches
   dropped), and a clean rescue is merged and pushed back to the PR's fork
   branch with a lease on the pre-rescue head (a branch that moved keeps
   its newer work). A PR whose rescue also conflicts is skipped — its
   conflict paths land in `merge-report.json` — and the run continues.
   Extras are never rebased (their refs are frozen pins). All PRs conflicting is a reported
   outcome. If upstream is already contained, staging may equal fork main.
2. Pins an immutable, canonical `nightly-YYYYMMDD` tag at the staging commit
   (same-day rerun: no-op when nothing changed, else `-rerunN`), plus a
   PEP 440 `vX.Y.Z.devYYYYMMDD` tag mirroring `nightly-release.yml`'s
   scheme, so `scripts/update_nightly.sh` resolves it:

   ```sh
   OMNIGENT_REPO=https://github.com/btli/omnigent bash scripts/update_nightly.sh
   ```

   The version is read from the *upstream* commit's `pyproject.toml`, so a
   merged PR can never control the tag. Unlike `nightly-release.yml`, no
   version-stamped commit or `uv lock` refresh is produced — the tag points
   at the raw staging commit, whose packages still claim `X.Y.Z.dev0`
   (risk-accepted for a personal ring). Note the dev tag floats within a
   UTC day: a same-day rerun repoints it silently, and `update_nightly.sh`
   consumers who already installed that day's cut see the same version
   string and won't pick up the repoint until the next day's tag.

3. Builds a debug APK and an unsigned release AAB from that commit and
   publishes them on a `nightly-YYYYMMDD` prerelease together with
   `merge-report.json` and `SHA256SUMS`, then re-points the floating
   `nightly-latest` prerelease.
4. Dispatches `personal-staging-images.yml` (main's trusted definition,
   dispatch-only) with the exact `nightly-YYYYMMDD[-rerunN]` tag; if that
   workflow isn't on main yet, the run warns in the summary instead of
   failing.

**This never touches branch `development`** — that remains the manually
composed homelab dev-env deploy branch driven by `just dev-branch` (the
[dev auto-rebase](#development-auto-rebase-personal-dev-rebaseyml) only
rebases it, never recomposes it). Nothing here flips any existing behavior.

## Hourly staging refresh (`personal-staging-hourly.yml`)

`Personal Staging Hourly` (cron `17 0-9,11-23 * * *`, plus `workflow_dispatch`)
keeps branch `staging` fresh between nightlies. The odd minute is
deliberate: GitHub delays or drops runs scheduled on the congested `:00` minute. It runs the same composer
with `--staging-only`: compose fork main + fresh upstream main + open btli PRs + extras
exactly as the nightly does, then push ONLY `refs/heads/staging`
(`--force-with-lease`). It mints no `nightly-*` pins and no dev tags, and
builds no APK/releases/images — those stay nightly-only.

Composition is byte-reproducible, so when the composed commit equals the
current remote `staging` sha the run is a no-op: the push is skipped and
the summary reports "unchanged". When it does push, the summary's one-line
result names which of upstream HEAD, the open PR set, or the extras
changed.

Only the staging nightly advances main: its integrate job runs `stage.py
sync-main` first and, if that fails, alerts and composes on the previous fork
main. The hourly refresh never syncs. Each privileged composition job has its own
non-cancelling concurrency group, so GitHub's single pending slot cannot evict a
different ring. Hourly runs are also non-cancelling and skip 10:17 UTC, leaving
the nightly window clear. A human main push invalidates an
in-flight composition:
every ring, pin, or rescue update is atomically coupled to an explicit lease and
no-op refspec for main, so the entire push fails if `base_sha` is stale.

## Bases and sync ownership

```text
upstream/main -- staging nightly sync-main --> published fork main
                                              | base_sha
                                              +-- fresh upstream (entry zero)
                                                  --> staging PRs --> staging pin
                                                      --> soak --> promote --> production pin
```

`stage --base-ref REF` defaults to freshly fetched fork main. An explicit ref
must resolve to that same published commit before anything can be published.
Every report, including hourly infrastructure/extra blocks and migration blocks,
carries `base_sha` and `upstream_sha`. `base_sha` anchors ancestry and the
first migration-gate diff (promote reads it from the nightly's
`merge-report.json`). `upstream_sha` remains the PR fetch/rescue and
dev-version baseline. The previous production pin is the second migration
baseline.

Staging reports also carry `entry_zero` (`source: upstream`, `pr: 0`, `oid`,
`minted`, and `rerere_paths` when used), separately from the existing PR lists.
A real entry-zero merge has subject `staging: merge upstream <sha12>` and uses
the upstream committer date and staging identity. The composition decoder includes
it under `upstream`. If upstream is already contained, `minted` is false and
no redundant commit is made. A conflict without a complete verified rerere
resolution fails closed.

```sh
python3 .github/scripts/personal-staging/stage.py sync-main \
  --workdir /path/to/disposable-clone --upstream-remote upstream --fork-remote origin
```

This command writes fork main: use it only in the staging nightly's sync path
or an intentional operator sync. It merges from fresh main, verifies fast-forward
ancestry, and pushes with an explicit old-value lease. It never rewrites main.
Only a confirmed stale-ref rejection gets one retry, rebuilt from fresh main;
a second race, merge conflict, hook, auth or transport failure fails the sync,
and the nightly then composes on the previous base. `stage` fetches main again
after sync. A candidate equal to its base is valid and pins main when there are
no new composition commits.

### Main-sync recovery

Rerere seeds apply to composition and staging entry zero, not `sync-main`. If
sync-main or entry zero conflicts, check out fork main in a disposable clone,
fetch and merge upstream main, resolve and commit, then use a normal
`git push origin main`. Never use `--force` or `--force-with-lease` to rewrite
main. After main contains the resolved merge, the next staging nightly (or a
dispatch of `personal-staging.yml`) composes on it.

Git may elide the no-op main refspec after advertising a matching main, leaving
a residual window during the atomic push. The composer therefore re-reads main
after every publication and records `base_matches_remote_main`. If main moved,
the run fails after publication; ring or rescue refs may already have moved and
are not rolled back. The next compose reconciles them from the new main.

If this base-on-main design must be rolled back, revert the change on main.
`--base-ref upstream/main` is not an escape hatch: an explicit base must equal
published fork main.

See the [staging ring README](../../../docs/rings/staging/README.md) and
[production ring README](../../../docs/rings/production/README.md) for verification.

## Personal production ring (promoted, not composed)

Production no longer composes or builds. `personal-production.yml` is a
retired stub: no cron, and a dispatch fails with a pointer to
`personal-promote.yml`. The PRODUCTION extras manifest
(`extras-production.txt`) is gone too; `stage.py` keeps its PRODUCTION ring
only so promotion can allocate `production-YYYYMMDD[-rerunN]` names through
`pin_name`. A production pin is now a soaked staging nightly's own commit,
with that nightly's artifacts and image digests copied byte-for-byte (see
[Soak and promote](#soak-and-promote-personal-promoteyml)).

`personal-staging-images.yml` still accepts both ring tag families. Server
and host artifacts contain native amd64 and arm64 images. Native GitHub
runners check the host binaries as uid 1000 and 1000660000 before moving
`staging-nightly`; promote then retags the same digests as
`production-nightly`. Clusters using a multi-architecture host image can
select Pi workers with
`sandbox.providers[].kubernetes.node_selector: {kubernetes.io/arch: arm64}`.
Set `pod_ready_timeout_s: 600` in that provider block to allow a cold image
download; the default is 90 seconds.

### Migration gate

Both rings validate the complete migration graph before publishing, including
hourly staging refreshes. Missing parents, duplicate revisions, cycles, and
multiple heads block publication. Staging also preserves every migration from
its previous branch tip. Fork main retains the migration files shipped by
either ring, even after their feature PRs close; the published mobile-push
join is now shared by both rings rather than carried as a staging branch pin.
Fresh databases and upgrades from historical production and staging revisions
are covered by `tests/db/test_ring_migration_history.py`.

Published migration files are immutable. Before considering backup approval,
the composer and `promote.py plan` reject any edit, deletion, or rename under the migrations path
relative to the previous production pin. This is a hard failure that approval
cannot bypass. Keep shipped ancestry intact and add a new revision to join new
heads; a merge revision's ID cannot be reused with different parents. For a
previously shipped defect, add a forward repair revision instead of editing
the old file.

Fork-only migrations already shipped to production stay on fork main even if
their originating feature PR is removed from the composition. The retained
scheduled-project and merge revisions preserve existing databases; the forward
repair handles databases stamped by the earlier merge graph without overwriting
existing project-order preferences.

Production's migration gate now runs at promote time: `promote.py plan`
diffs the candidate against the nightly's `base_sha` and the current
production pin, and a migration-touching candidate stays blocked (green run,
ha-notify alert, nothing published) until a dispatch passes
`approve_migration=<exact candidate sha>` after the CNPG backup checkpoint.
See [Soak and promote](#soak-and-promote-personal-promoteyml).

## Soak and promote (`personal-promote.yml`)

Build once in staging, soak that exact build, then promote the same commit
and the same bytes to production. Design:
`docs/superpowers/specs/2026-10-09-ring-soak-promote-design.md`.

Flow:

1. The staging nightly builds everything (APK, AAB, desktop app, images) and
   `personal-staging-images.yml` uploads `build-complete.json` last: the sha,
   every asset's sha256 and both image digests. Without it a nightly is
   never a candidate. A failed or cancelled nightly sends one ha-notify alert
   (an incomplete composition alerts from `composition-gate` instead).
2. The soak hardware deploys the nightly and posts a `soak/homelab` commit
   status on its sha.
3. `personal-promote.yml` runs on that status (see
   [Status-triggered promotion](#status-triggered-promotion)) or on a manual
   dispatch, and calls `promote.py plan`. The plan is JSON with an `action`:
   - `promote` publishes.
   - `noop` means `production-latest` and its release already show this
     build: the tag is at the sha and the release's `source.json` names it.
     A tag moved without its release plans `promote` again, which repairs it.
   - `blocked` means the migration gate needs approval. The run stays green
     and sends an ha-notify alert with the approval hint.
   - `ignored` means a status event carried no actionable verdict (status
     trigger only); the run stays green with a summary line. A `failure` or
     `error` verdict also sends an ha-notify alert
     (`soak <state> for <sha> (<nightly>) — <run url>`).
   - `refused` and `error` fail the run.

   The workflow only carries out the plan; it never checks out the nightly.

Rules (all must pass, or nothing is published):

- The nightly carries a sound `build-complete.json` naming its tag and sha,
  and every listed asset is on its release.
- Forward-only: the candidate must be newer than the source nightly in
  production's `source.json` (a legacy composed pin compares by date).
  Rollback needs `allow_older=true`.
- Migrations: the graph and shipped history checks must pass. The gate diffs
  the candidate against the nightly's `base_sha` (fork main, from its
  `merge-report.json`) and the current production pin. A candidate touching
  migrations needs `approve_migration=<exact candidate sha>`.
- One production tag per sha: a rerun or rollback reuses the existing tag.
  The promote job holds the `personal-promote` lock (never cancelled). It is
  job-level, so the status events the gate skips never take the group's
  single pending slot.

Publish order (`production-latest` moves last):

1. Draft: download the listed assets, check them with `promote.py
   verify-assets`, and rename `omnigent-staging-*` to
   `omnigent-production-*`. Add `source.json` and a fresh `SHA256SUMS`,
   then upload to a draft `production-YYYYMMDD[-rerunN]` release, or refresh
   an existing release for that tag.
2. Images: `imagetools create` the soaked digests as `:<production tag>` and
   `:sha-<short>`.
3. Pin: `promote.py pin` pushes the tag, or confirms it is already at the sha.
4. Switch: publish the release, move the `production-nightly` image channel,
   then point `production-latest` (tag, then release) at the sha.

If a run fails or is cancelled before the pin, it deletes the draft it
created. From the pin onward a rerun resumes. Every failure or cancellation
alerts via ha-notify.

Dispatch (always from `main`):

```sh
gh workflow run personal-promote.yml -R btli/omnigent --ref main \
  -f nightly=nightly-YYYYMMDD -f dry_run=true     # print the plan only
gh workflow run personal-promote.yml -R btli/omnigent --ref main \
  -f nightly=nightly-YYYYMMDD                     # promote
# after the CNPG backup, for a migration-touching candidate:
#   -f approve_migration=<full-40-hex-candidate-sha>
# rollback to an older soaked nightly:
#   -f allow_older=true
```

Hardware contract:

- Candidates are `nightly-*` tags. Soak images are `staging-nightly`, or the
  digests in `build-complete.json`.
- Verdict: commit status `soak/homelab` on the candidate sha, posted with a
  token from repo variable `SOAK_STATUS_LOGIN`'s account (or a GitHub App),
  limited to `statuses: write`.
- Production follows `production-latest` / the `production-nightly` image
  channel. Nothing composes production any more: `personal-production.yml`
  is a retired stub (see
  [Personal production ring](#personal-production-ring-promoted-not-composed)).

### Status-triggered promotion

`personal-promote.yml` listens to every commit status, but both jobs run only
for a `soak/homelab` status that is not `pending`, and only while repo
variable `SOAK_STATUS_LOGIN` is set. Leave it unset to keep promotion
manual. The run executes main's definition and checks out main; the plan
gets only the event sha and the login (through `env:`) and runs with
`--trigger status`, which refuses the dispatch-only `approve_migration` and
`allow_older`. `promote.py` re-reads the sha's statuses and trusts only the
newest `soak/homelab` status, and only when `SOAK_STATUS_LOGIN` created it:

- `success` from that login: promote, subject to every rule above. A
  migration-touching candidate stays blocked until a manual dispatch passes
  `approve_migration`.
- `failure` or `error`: nothing is published, the run stays green and an
  ha-notify alert names the sha and nightly.
- A newest status from another login, or a sha that is not a `nightly-*`
  target, is `refused` (red run, failure alert).

```sh
gh variable set SOAK_STATUS_LOGIN -R btli/omnigent --body <soak-account-login>
# what the soak hardware posts (with its statuses:write token):
gh api repos/btli/omnigent/statuses/<nightly-sha> -f context=soak/homelab \
  -f state=success -f target_url=<soak run url>
```

### Pin retention guard

`prune_pins.py` never deletes the nightly named by `production-latest`'s
`source.json` (`source_tag`), or that production tag, however old: they are
production's provenance. A missing `production-latest` release or
`source.json` protects nothing; any other failure reading it fails the
retention run before anything is planned or deleted.

## Development auto-rebase (`personal-dev-rebase.yml`)

`Personal Dev Rebase` (cron `45 10 * * *`, plus `workflow_dispatch`) keeps
branch `development` (the live dev.omni deploy branch) based on upstream
main while preserving its experiment commits. **Never-clobber semantics**:
every outcome other than a clean rebase leaves the branch bit-for-bit
untouched and exits green —

- already based on upstream main → no-op;
- rebase conflict → `git rebase --abort`, `::warning::` + ha-notify with
  the conflicting paths, no push (resolve manually or via `just dev-sync`);
- upstream main advanced mid-rebase → nothing pushed, next run retries;
- `--force-with-lease` rejected (a concurrent `just dev-sync` or human
  push won the race) → nothing overwritten, next run retries;
- branch `development` doesn't exist yet (pre-cutover) → green no-op.

Only genuine infrastructure failures (auth, transport, a broken probe) go
red — and those also ha-notify.

## `staging` is ephemeral — do not track it

`staging` is **rebuilt from scratch and force-pushed, now up to 24× a
day**. Its history is rewritten every time upstream or a PR moves: commit
shas are not stable, and a commit that was on the branch an hour ago may
be gone. Nothing should track the branch tip.

- **Pin instead:** for anything reproducible — homelab deploys, container
  builds, bisecting — use the canonical `nightly-YYYYMMDD` tag or the
  `vX.Y.Z.devYYYYMMDD` tag from the nightly.
- **Existing clone:** `git pull` on `staging` will refuse or conflict
  after a rewrite. Recover with:

  ```sh
  git fetch origin && git reset --hard origin/staging
  ```

  (discards local work on the branch — keep none there).

## Extras manifest (`extras.txt`)

`stage.py` always reads `.github/scripts/personal-staging/extras.txt`
(missing file == no extras), so extras land in BOTH the hourly and the
nightly composition. Format: one PR number per line; blank lines and `#`
comments allowed; anything else fails the run loudly.

Extras are PR numbers that must stay baked into `staging` even though
they are no longer open (typically closed-without-merge) — GitHub keeps
`refs/pull/N/head` fetchable after close. The merge stream is the union
of open PRs and extras, deduped by PR number (the open entry wins),
sorted ascending — the same ordering rule as always. **Remove an entry
once the change lands upstream.**

When `omni-resolve-agent[bot]` closes a contributor PR and opens an upstream
successor, both miss the automatic stream: the original is closed and the
bot-authored successor fails the `btli` author filter. Pin the successor in each
intended ring and record its reviewed head SHA because this **open**, bot-owned
ref can move. That SHA is informational: the only check today is manually
comparing it with the applied `oid` in the run report; automating this is
planned. The `stage.py` comment describing extras as frozen pins applies only to
closed PR pull refs. A force-push that changes conflicting content breaks the
recorded rerere match: the merge aborts, the pin appears under **Skipped PRs**,
and the nightly stays green. If the conflict text stays byte-identical, the
recorded resolution can still replay and land the changed head; if the
merge is now clean, it lands silently. Neither silent branch is
detected today, and a conflict skip is only a symptom, not proof
the head moved. Re-record a mismatched resolution per
[Conflict resolutions](#conflict-resolutions-rr-cache), or remove the pin.

If a pinned successor closes unmerged as `Superseded by #M`, move the pin to M,
refresh the reviewed-head comment, and re-record the rr-cache resolution if the
new head conflicts.

Remove a bot successor's line as soon as it merges upstream. Leaving it behind
reports `minted: false` only after a merge-commit landing, when the pinned head
is already an ancestor. After a squash merge, the head is not an ancestor; the
stale extra can mint and reapply landed content, or conflict and silently skip
on a still-green nightly.

An extra that can't be resolved gets one of two distinct outcomes, because
a deleted ref and an unreachable server are different problems:

- **Confirmed gone** (`ls-remote` says the ref no longer exists): skipped
  loudly with reason `extra unfetchable (likely deleted; remove from
  extras.txt)` — distinct from a conflict skip — and the run continues.
  This is the only reason that invites editing the manifest.
- **Could not reach upstream** (the fetch keeps failing after retries, and
  the existence probe itself errors): reason `extra fetch failed (cannot
  reach upstream; pin kept, staging not advanced)`. The hourly run does
  **not** push — `staging` keeps its previous content rather than silently
  losing a required pin — and emits a `::warning::`. The nightly fails the
  job instead, before any ref moves, since it publishes releases from that
  composition. **Do not delete the pin on this reason**; it means the
  fetch failed, not that the PR is gone.

## Conflict resolutions (`rr-cache/`)

### Stale-seed detector

After each nightly and hourly composition, a best-effort monitor compares the
new merge report with the latest successful run's report. It opens or updates a
workflow-specific `staging-seed-stale-*` issue when a PR changes from applied
to skipped, or when a newly introduced extra is skipped on its first run.
Persistently skipped extras keep their ring's issue open until the current
report is clear. Seed-assisted prior merges are identified in the issue from
their `rerere_paths`. The monitor is deliberately separate from `stage.py` and
can never gate composition, pushes, tags, or builds.

A PR whose merge conflicts with an earlier train member is normally
skipped. `.github/scripts/personal-staging/rr-cache/` holds committed
resolutions in git's own rr-cache layout (one `<40-hex>/` directory with
a `preimage`/`postimage` pair per recorded conflict). `stage.py` seeds
them into the compose workspace before merging (`--rr-cache` overrides
the directory; a missing directory means no resolutions), so a merge
whose conflicts are **all** covered lands instead of skipping. Coverage
is verified positively — `git rerere remaining` empty, every conflict
two-sided (rerere never handles delete/rename conflicts and stays silent
about them), no markers left in the worktree — anything less skips
exactly as before. Applied entries record the covered paths as
`rerere_paths` in `merge-report.json` and in the release notes.

The seed is a composition input like `extras.txt`: identical seeds and
heads reproduce identical staging bytes, and both the hourly/nightly
staging and the production ring consume it. To record a new resolution:
in a clone with `rerere.enabled=true`, merge the PR head onto the current
composition point, resolve, commit, then copy the new
`.git/rr-cache/<hash>/` directories here. **Remove entries once the
conflicting pair no longer coexists** (one side landed or was retired);
a stale entry whose conflict text no longer matches is inert.

## Stable download URL

The floating prerelease keeps asset names fixed, so the newest nightly APK is
always:

```
https://github.com/btli/omnigent/releases/download/nightly-latest/omnigent-staging-debug.apk
```

## Debug keystore secret (optional, recommended)

The `android-sign` job always re-signs the APK (the untrusted build job's
own signature never ships). Without a shared keystore it mints a fresh one
per run, so in-place upgrades fail across nightlies (uninstall first).

To make nightlies share one signature, the four keystore secrets live in a
**`staging-signing` GitHub Environment with a main-only deployment-branch
rule — not repo-level secrets**. A `workflow_dispatch` of a non-main ref
executes that ref's own copy of the workflow, so nothing written in this
file can stop a malicious ref from reading repo-level secrets; the
Environment's branch rule is enforced server-side regardless of what the
dispatched workflow file says.

One-time setup — create the Environment, restrict it to `main`, then store
the secrets there (passwords go via prompts/stdin, never argv):

```sh
gh api -X PUT repos/btli/omnigent/environments/staging-signing \
  --input - <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api -X POST repos/btli/omnigent/environments/staging-signing/deployment-branch-policies \
  -f name=main -f type=branch

# keytool prompts for the store/key passwords interactively
keytool -genkeypair -v -keystore debug.keystore -alias omnigent-debug \
  -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=omnigent staging"

base64 -i debug.keystore | gh secret set -R btli/omnigent --env staging-signing OMNIGENT_DEBUG_KEYSTORE_B64
gh secret set -R btli/omnigent --env staging-signing OMNIGENT_DEBUG_KEYSTORE_PASSWORD  # paste at the prompt
gh secret set -R btli/omnigent --env staging-signing OMNIGENT_DEBUG_KEY_ALIAS --body omnigent-debug
gh secret set -R btli/omnigent --env staging-signing OMNIGENT_DEBUG_KEY_PASSWORD       # paste at the prompt
```

The keystore only ever reaches the `android-sign` job, which never checks
out or executes merged PR code (the build job is secretless and its gradle
cache access is read-only; if staging runs ever wrote gradle caches before
that lockdown, purge them once from the repo's Actions cache UI).

Each run's `android-sign` step summary prints the signing certificate's
SHA-256 fingerprint — compare a device's installed cert against it
(`apksigner verify --print-certs`) when a leak is suspected.

**If the keystore leaks:** regenerate it with the keytool command above,
replace all four secrets, and uninstall/reinstall the app once on each
device — the next nightly's signature won't match the leaked one.

## Manual dispatch

Always dispatch `main` — the privileged jobs (integrate, android-sign,
publish) carry a `github.ref == 'refs/heads/main'` guard and skip on any
other ref:

```sh
gh workflow run personal-staging.yml -R btli/omnigent --ref main
```

Risk-accepted residual: the ref guard is accident prevention, not a
security boundary — a dispatched ref runs its own workflow copy, and the
integrate job's `GITHUB_TOKEN` write permission comes from that file, so a
repo writer dispatching a hostile non-main ref could still push refs.
Acceptable for a single-writer fork; the keystore (the only custom secret)
is protected for real by the `staging-signing` Environment above.

## Tests

Offline (local temp git repos, no network, no gh):

```sh
uv run --frozen --group dev python -m pytest .github/scripts/personal-staging/
```
