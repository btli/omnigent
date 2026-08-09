# Personal staging nightly (fork-only)

`.github/workflows/personal-staging.yml` runs nightly (05:37 UTC, plus
`workflow_dispatch`) on the `btli/omnigent` fork only. It:

1. Composes fork branch `staging` = upstream `omnigent-ai/omnigent` main +
   every open btli PR, merged sequentially ascending by PR number
   (`stage.py`). A conflicting PR is skipped — its conflict paths land in
   `merge-report.json` — and the run continues. All PRs conflicting is a
   reported outcome, not a failure.
2. Pins an immutable `nightly-YYYYMMDD` branch + tag at the staging commit
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

**This never touches branch `testing`** — that remains the manually composed
homelab deploy branch driven by `just test-branch`. Nothing here flips any
existing behavior.

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
uv run --frozen --extra dev python -m pytest .github/scripts/personal-staging/
```
