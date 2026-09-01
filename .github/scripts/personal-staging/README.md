# Personal staging nightly (fork-only)

`.github/workflows/personal-staging.yml` runs nightly (10:00 UTC — cron
`0 10 * * *` — plus `workflow_dispatch`) on the `btli/omnigent` fork only.
It:

1. Composes fork branch `staging` = upstream `omnigent-ai/omnigent` main +
   every open btli PR plus the [`extras.txt`](#extras-manifest-extrastxt)
   pins, merged sequentially ascending by PR number (`stage.py`). A
   conflicting open PR gets one rebase rescue: its head is replayed onto
   upstream main (reproducible identity/dates, already-landed patches
   dropped), and a clean rescue is merged and pushed back to the PR's fork
   branch with a lease on the pre-rescue head (a branch that moved keeps
   its newer work). A PR whose rescue also conflicts is skipped — its
   conflict paths land in `merge-report.json` — and the run continues.
   Extras are never rebased (their refs are frozen pins), and the
   production ring never rescues. All PRs conflicting is a reported
   outcome, not a failure.
2. Pins an immutable, canonical `nightly-YYYYMMDD` tag at the staging commit
   (same-day rerun: no-op when nothing changed, else `-rerunN`), plus a
   deprecated same-name compatibility branch slated for removal in v0.12.0
   and a PEP 440 `vX.Y.Z.devYYYYMMDD` tag mirroring `nightly-release.yml`'s
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

`Personal Staging Hourly` (cron `17 * * * *`, plus `workflow_dispatch`)
keeps branch `staging` fresh between nightlies. The odd minute is
deliberate: `:00` would collide with the nightly's 10:00 UTC slot (both
push fork main) and GitHub delays or drops runs scheduled on that
congested minute. It runs the same composer
with `--staging-only`: compose upstream main + open btli PRs + extras
exactly as the nightly does, then push ONLY `refs/heads/staging`
(`--force-with-lease`). It mints no `nightly-*` pins and no dev tags, and
builds no APK/releases/images — those stay nightly-only.

Composition is byte-reproducible, so when the composed commit equals the
current remote `staging` sha the run is a no-op: the push is skipped and
the summary reports "unchanged". When it does push, the summary's one-line
result names which of upstream HEAD, the open PR set, or the extras
changed.

Its `sync-main` job soft-fails on a merge *conflict* (abort + `::warning::`
+ summary, exit 0) — unlike the nightly's, which hard-fails — because the
compose job stacks from upstream HEAD and never reads fork main. Only a
genuine content conflict is soft-failed: a merge that fails with no
unmerged paths (bad object, corrupt repo) still fails the job loudly. If
its `git push origin main` is rejected as a confirmed stale ref
(`! [rejected]` with `non-fast-forward` / `fetch first` / `stale info` —
typically a race with the nightly or a human push), it re-fetches and
retries once, then warns and exits 0. Auth, permissions, or transport
failures still fail the job. `main` is never force-pushed.

The workflow uses its own concurrency group (`personal-staging-hourly`,
`cancel-in-progress: true`) so stale hourly runs coalesce and can never
cancel a running nightly; if an hourly push loses a `--force-with-lease`
race with the nightly, the next hour retries.

## Personal production nightly (`personal-production.yml`)

`Personal Production Nightly` (cron `30 10 * * *`, plus `workflow_dispatch`)
runs the same composer with `--ring production`: fork branch `production` =
upstream main + every open **non-draft** btli PR plus numeric pins from
`extras-production.txt`, and no dev tag. Draft status gates the automatic
stream: `filter_drafts()` runs before the extras union, so a numeric production
extra bypasses it (both current pins are non-draft). The current bot-owned pins
also resolve mutable `refs/pull/N/head` refs. Each run mints an immutable,
canonical `production-YYYYMMDD` tag
pin (same rerun/no-op semantics as `nightly-*`) plus a deprecated same-name
compatibility branch slated for removal in v0.12.0, which homelab's
`build-omnigent-production.yml` resolves at 11:10 UTC to build and
digest-pin the prod server + host images. Compose + pin only: no
APK/releases/images. Its concurrency group (`personal-production`,
`cancel-in-progress: false`) is disjoint from the staging groups, so the
rings can never cancel each other.

### Migration gate

A candidate that touches `omnigent/db/migrations/versions/**` — on EITHER
`upstream/main..candidate` OR `previous-production-pin..candidate` (the
second leg catches a migration-bearing PR *removed* between compositions) —
is never auto-published. `stage.py` itself refuses the atomic push, so **no
refs move at all**: the run stays green (blocked is a success outcome), the
step summary shows a BLOCKED row with the candidate sha, and a best-effort
HMAC-signed alert goes to `hooks.bryanli.net/hooks/ha-notify` (repo secret
`HA_NOTIFY_HMAC`; absent secret or unreachable receiver only warns — CI
never holds an HA API token). To promote, take the CNPG backup checkpoint
per the homelab runbook, then re-dispatch with the exact candidate sha:

```sh
gh workflow run personal-production.yml -R btli/omnigent --ref main \
  -f approve_migration=<full-40-hex-candidate-sha>
```

The approval publishes exactly that sha: if the composition drifted in the
meantime (a PR or upstream moved), the rerun mints a different candidate,
the approval no longer matches, and the gate blocks again with the new sha.

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
  `vX.Y.Z.devYYYYMMDD` tag from the nightly. The same-name dated branch is a
  deprecated compatibility shim slated for removal in v0.12.0.
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
single `staging-seed-stale` issue when a PR changes from applied to skipped, or
when a newly introduced extra is skipped on its first run. Seed-assisted prior
merges are identified in the issue from their `rerere_paths`. The issue closes
automatically after a clean comparison. The monitor is deliberately separate
from `stage.py` and can never gate composition, pushes, tags, or builds.

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
