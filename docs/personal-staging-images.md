# Personal staging images

`.github/workflows/personal-staging-images.yml` publishes the server and host
images from `btli/omnigent` to GHCR. It runs for `nightly-*` tag pushes and for
manual dispatches that provide an existing `nightly-*` tag name through the
`ref` input. Branch refs are rejected.
The job is deliberately guarded to run only in the fork repository.

Both images are multi-architecture manifests for `linux/amd64` and
`linux/arm64`:

- `ghcr.io/btli/omnigent-server`
- `ghcr.io/btli/omnigent-host`

Each successful publish exposes three consumer tags on both images:

- `sha-<short>` — a short commit label.
- The triggering `nightly-*` tag, such as `nightly-20260809`.
- `staging-nightly` — the floating staging tag.

The server build uses the Dockerfile's `runtime` target, followed by the
explicit `host` target. The builds run sequentially so the shared Buildx
builder can reuse layers. Both builds use QEMU and registry-backed caches at
`ghcr.io/btli/omnigent-server:buildcache` and
`ghcr.io/btli/omnigent-host:buildcache`. These `buildcache` tags are internal
BuildKit cache refs, not deployment image pins.

The server and host builds publish only their `sha-<short>` and date labels.
After both builds succeed, the workflow applies `staging-nightly` from each
build's pushed manifest digest. A failed build therefore leaves the previous
floating tag unchanged. Rerunning a build may repoint the named `sha-*` and
date tags; only an image reference with an explicit `@sha256:...` digest is
immutable.

## First package setup

The first publish creates the GHCR packages as **private**. In GitHub package
settings, flip both packages to **public** once so homelab nodes can pull them
without credentials. The workflow uses only the job's `GITHUB_TOKEN` to log in
and publish; it does not change package visibility.

## Pin by digest

Inspect the manifest list for a date tag:

The commands below require public packages or an authenticated GHCR session.
If the packages remain private, authenticate before inspecting:

```bash
docker login ghcr.io
docker buildx imagetools inspect ghcr.io/btli/omnigent-server:nightly-20260809
docker buildx imagetools inspect ghcr.io/btli/omnigent-host:nightly-20260809
```

Use the returned top-level `Digest` to pin a consumer, for example:

```text
ghcr.io/btli/omnigent-server@sha256:<manifest-list-digest>
ghcr.io/btli/omnigent-host@sha256:<manifest-list-digest>
```

The digest is the immutable multi-architecture manifest-list digest; keep the
architecture-specific digests shown by `imagetools inspect` when a deployment
needs to verify a platform image directly.

## Manual verification after merge

Use an existing `nightly-*` tag from `btli/omnigent` and dispatch the workflow
against that tag. The workflow is dispatched from the default branch, with the
tag passed through the required `ref` input. Snapshot existing run IDs first so
the polling loop can select the new matching run rather than a concurrent run:

```bash
set -euo pipefail

TAG=nightly-20260809
REPO=btli/omnigent
WORKFLOW=personal-staging-images.yml
ACTOR="$(gh api user --jq '.login')"
RUN_NAME="Publish personal staging images (${TAG}) by @${ACTOR}"
list_run_ids() {
  gh api "repos/${REPO}/actions/runs?event=workflow_dispatch&per_page=100" \
    --jq "[.workflow_runs[] | select(.display_title == \"${RUN_NAME}\" and .actor.login == \"${ACTOR}\") | .id] | .[]" | sort
}

BEFORE_RUN_IDS="$(list_run_ids)"
gh workflow run "$WORKFLOW" -R "$REPO" -f ref="$TAG"

RUN_ID=""
for attempt in $(seq 1 30); do
  AFTER_RUN_IDS="$(list_run_ids)"
  RUN_ID="$(comm -13 \
    <(printf '%s\n' "$BEFORE_RUN_IDS" | sed '/^$/d') \
    <(printf '%s\n' "$AFTER_RUN_IDS" | sed '/^$/d') | tail -n 1)"
  if [ -n "$RUN_ID" ]; then
    break
  fi
  sleep 2
done
test -n "$RUN_ID"
gh run watch "$RUN_ID" -R "$REPO" --exit-status

manifest_digest() {
  local output digest
  output="$(docker buildx imagetools inspect "$1")"
  grep -q 'linux/amd64' <<<"$output"
  grep -q 'linux/arm64' <<<"$output"
  digest="$(awk '/^Digest:/{print $2; exit}' <<<"$output")"
  test -n "$digest"
  printf '%s\n' "$digest"
}

TAG_SHA="$(gh api "repos/${REPO}/commits/${TAG}" --jq '.sha')"
SHORT_SHA="${TAG_SHA:0:12}"
for image in ghcr.io/btli/omnigent-server ghcr.io/btli/omnigent-host; do
  date_digest="$(manifest_digest "${image}:${TAG}")"
  sha_digest="$(manifest_digest "${image}:sha-${SHORT_SHA}")"
  staging_digest="$(manifest_digest "${image}:staging-nightly")"
  test "$date_digest" = "$sha_digest"
  test "$date_digest" = "$staging_digest"
  printf '%s: %s\n' "$image" "$date_digest"
done
```

Replace `nightly-20260809` with a tag that exists in the fork. Confirm the run
is successful and that both images' date tag, `sha-*` tag, and
`staging-nightly` tag resolve to the same two-platform manifest digest. The
`buildcache` tags are expected to exist on GHCR after the builds.

Harbor naming and auto-pin behavior at `registry.joyful.house` are out of
scope. This workflow never logs in to or pushes to Harbor; date tags must never
be pushed there.
