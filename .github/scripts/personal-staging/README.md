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

3. Builds a debug APK and an unsigned release AAB from that commit and
   publishes them on a `nightly-YYYYMMDD` prerelease together with
   `merge-report.json` and `SHA256SUMS`, then re-points the floating
   `nightly-latest` prerelease.

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

Without it, each nightly APK is signed by a runner-ephemeral debug keystore
and in-place upgrades fail across runs (uninstall first). To make nightlies
share one signature, generate a keystore once and store it as fork secrets:

```sh
keytool -genkeypair -v -keystore debug.keystore -alias omnigent-debug \
  -keyalg RSA -keysize 2048 -validity 10000 -storepass CHANGE_ME \
  -keypass CHANGE_ME -dname "CN=omnigent staging"

gh secret set -R btli/omnigent OMNIGENT_DEBUG_KEYSTORE_B64 --body "$(base64 -i debug.keystore)"
gh secret set -R btli/omnigent OMNIGENT_DEBUG_KEYSTORE_PASSWORD --body CHANGE_ME
gh secret set -R btli/omnigent OMNIGENT_DEBUG_KEY_ALIAS --body omnigent-debug
gh secret set -R btli/omnigent OMNIGENT_DEBUG_KEY_PASSWORD --body CHANGE_ME
```

## Manual dispatch

```sh
gh workflow run personal-staging.yml -R btli/omnigent
```

## Tests

Offline (local temp git repos, no network, no gh):

```sh
uv run --frozen --extra dev python -m pytest .github/scripts/personal-staging/
```
