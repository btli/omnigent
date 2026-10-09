# Personal rings: build once in staging, soak, promote to production

Status: approved 2026-10-09. Fork-only (`btli/omnigent`).

## Goal

Build the release once, in the staging nightly. Soak that exact build on live
hardware, then promote the same commit and the same artifacts to production.
Production stops composing and building on its own.

## Decisions

| Topic | Decision |
|---|---|
| Promotion trigger | The hardware posts a commit status; a passing verdict promotes. A manual `workflow_dispatch` promote is always available. |
| Soak environment | Its own database: separate machines or a separate namespace on the same ones. Migrations run against soak data first. The production migration gate stays at promote time. |
| Overlapping nightlies | Latest wins. Any soak-passed nightly newer than the current production source can be promoted. |
| Verdict channel | Commit status `soak/homelab` on the candidate sha, from a credential limited to `statuses: write`. |
| Artifacts | Reused byte-for-byte. Staging builds the `.dev` APK, the desktop app and the images. Promote copies them. |
| Production cadence | Event-driven. The hardware watches `production-latest` (or the `production-nightly` image channel) and deploys on change. |
| Approach | The staging nightly builds everything; a small `personal-promote.yml` plus a tested `promote.py plan\|pin\|verify-assets` promotes. |

## Flow

1. **Staging nightly** (`personal-staging.yml`, 10:00 UTC):
   - `sync-main` advances fork `main` to upstream. On conflict it alerts, leaves
     `main` untouched and the nightly continues on the old base, with upstream
     as entry zero (rerere seeds apply there).
   - It composes fork `main` + upstream + open btli PRs + `extras.txt` and pins
     `nightly-YYYYMMDD[-rerunN]`, as today.
   - **verify** builds the web bundle; a nightly that fails gets no artifacts
     and is never a candidate.
   - **android-build** builds the `.dev` debug APK (`ai.omnigent.android.dev`,
     version name = nightly tag) and, separately, the unsigned release AAB
     (store id). **android-sign** re-signs the APK, as today.
   - **desktop-build** builds the ad-hoc-signed arm64 macOS app (dmg + zip).
   - **publish** uploads the APK, AAB, desktop files, merge report and
     `SHA256SUMS` to the nightly release and `nightly-latest`, then dispatches
     `personal-staging-images.yml` with the nightly tag.
2. **Images** (`personal-staging-images.yml`): for a `nightly-*` tag, after
   both images verify and the `staging-nightly` channel moves, it uploads
   `build-complete.json` to the nightly release, last:
   ```json
   {"schema": 1, "tag": "nightly-20261010", "sha": "<40-hex>",
    "assets": {"omnigent-staging-debug.apk": "<sha256>", ...},
    "images": {"server": "sha256:...", "host": "sha256:..."}}
   ```
   `assets` comes from the release's `SHA256SUMS`. Its presence means the nightly
   is complete.
3. **Soak** (hardware, out of scope here): deploys the nightly to the soak
   namespace and database, then posts a status on the nightly sha:
   `context=soak/homelab`, `state=success|failure`, `target_url=<soak run>`.
4. **Promote** (`personal-promote.yml` → `promote.py plan|pin|verify-assets`), described below.
5. **Production hardware** deploys whenever `production-latest` moves.

## Promote rules (`promote.py plan|pin|verify-assets`)

Every rule must pass, or nothing is published.

1. **Trigger filter.** `status` events act only on `context == soak/homelab`.
   `failure`/`error` writes a summary and an `ha-notify` alert, then stops.
   `pending` is ignored.
2. **Trusted verdict.** It re-reads the sha's statuses from the REST API. The
   newest `soak/homelab` status must be `success`, created by the login in repo
   variable `SOAK_STATUS_LOGIN`. If that variable is unset, status-triggered
   promotion is disabled. A manual dispatch skips this rule; you are the verdict.
3. **Complete nightly.** The sha is the target of a `nightly-YYYYMMDD[-rerunN]`
   tag whose release carries `build-complete.json`. Its `sha` and `tag` must
   match, and every listed asset must be present on the release.
4. **Forward-only.** The candidate's `(date, rerun)` must be newer than the source
   nightly of the current production pin, which is read from that release's
   `source.json`. A legacy composed pin has no `source.json`; then the candidate
   date must be on or after the pin's date. A candidate whose sha production
   already points at is a no-op success. `allow_older=true` (manual dispatch
   only) permits rollback, and the rollback is recorded in `source.json`.
5. **Migration safety.**
   - `assert_migration_graph(candidate)` must pass.
   - `assert_migration_history(candidate, prev_production_pin)` must pass: shipped
     migrations stay append-only and approval cannot override that.
   - The gate uses `migration_touched(candidate, nightly upstream_sha,
     prev_production_pin)`. If the candidate touches migrations and
     `approve_migration` isn't exactly the candidate sha, the run **stops
     green**: it alerts, writes the approval hint and publishes nothing.
6. **Serialized.** `concurrency: personal-promote`, never cancelled. Rules 3–5
   are evaluated inside the run, after the lock is acquired.

`promote.py plan` prints a JSON plan
`{"action": "promote|noop|blocked|refused", ...}`. The workflow carries out the
plan. Every step is idempotent, so a rerun completes a partial promotion.

## Publish order

`production-latest` is what the hardware watches, so it moves last.

1. **Draft.** Allocate `production-YYYYMMDD[-rerunN]` (by promote date, through
   `pin_name`). Create the draft release, or reuse an existing draft for the same
   sha. Download the nightly's assets, check each sha256 against
   `build-complete.json`, and upload them under production names
   (`omnigent-staging-*` → `omnigent-production-*`). Add `SHA256SUMS`, the merge
   report, and `source.json` (`source_tag`, `sha`, `soak_target_url`, `trigger`,
   `approve_migration`, `allow_older`).
2. **Images.** `imagetools create` the server and host images by digest to
   `production-YYYYMMDD` and `sha-<short>`.
3. **Pin.** Push the lightweight `production-YYYYMMDD` tag to the candidate sha
   with an empty-lease atomic push. An existing tag at the same sha is reused,
   and the same sha never gets a second tag.
4. **Switch.** Publish the release (not a draft, prerelease). Move the
   `production-nightly` image channel. Point `production-latest` (tag, then
   release) at the candidate and clobber-upload the assets.

**On failure:**
- Before step 3, delete the draft and leave production untouched.
- At or after step 3, the rerun resumes: it finds the tag or draft for the sha
  and continues.
- Every failure alerts through `ha-notify`.

## Pin retention

`prune_pins.py` must never delete the nightly named in the current
`production-latest` `source.json`, or that nightly's release. Production keeps
its own copies of the assets, so this protects provenance, not downloads.

## Hardware contract

- Candidate: `nightly-*` tags. Images: `staging-nightly`, or the digests in
  `build-complete.json`.
- Verdict: the commit-status API with a token from `SOAK_STATUS_LOGIN`'s account
  (or GitHub App), limited to `statuses: write` on `btli/omnigent`.
- Production: follow `production-latest` / `production-nightly`. This replaces
  the 11:10 UTC `production-*` resolver.

## Rollout

1. **PR 1, staging builds everything.** Add verify, the `.dev` APK, the desktop
   build, the `sync-main` step with fallback, and `build-complete.json` from the
   image workflow. Production keeps composing unchanged.
2. **PR 2, promote (manual dispatch only).** Add `promote.py plan|pin|verify-assets` with unit
   tests and `personal-promote.yml` with inputs `nightly`, `approve_migration`,
   `allow_older` and `dry_run`. Prove it with a dry run, then a real promote of
   one nightly.
3. **PR 3, cutover** (left open until the hardware posts statuses):
   - Add the `status` trigger, gated on `SOAK_STATUS_LOGIN`.
   - Remove `personal-production.yml`'s compose and build jobs and its cron.
   - Remove `extras-production.txt` and the PRODUCTION compose ring.
   - Add the retention guard and update the docs.

## Out of scope

The soak harness itself, the hourly staging refresh, `homelab` sync, and
dev-rebase.

## Testing

- `test_stage.py`-style unit tests for every promote rule: untrusted status
  author, unset `SOAK_STATUS_LOGIN`, failure and pending verdicts, a missing or
  mismatched `build-complete.json`, an older or equal candidate, a legacy pin
  without `source.json`, `allow_older`, a migration touched with and without the
  exact approval, history rewrite refused, and resuming on an existing tag or
  draft.
- `actionlint` on every changed workflow.
- Live proof: a staging nightly with all artifacts plus `build-complete.json`,
  then a `dry_run` promote, then one real promote.
