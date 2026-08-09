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

Each publish adds three tags to both images:

- `sha-<short>` — the short commit pin.
- The triggering `nightly-*` tag, such as `nightly-20260809`.
- `staging-nightly` — the floating staging tag.

The server build uses the Dockerfile's `runtime` target, followed by the
explicit `host` target. The builds run sequentially so the shared Buildx
builder can reuse layers. Both builds use QEMU and registry-backed caches at
`ghcr.io/btli/omnigent-server:buildcache` and
`ghcr.io/btli/omnigent-host:buildcache`. These `buildcache` tags are internal
BuildKit cache refs, not deployment image pins.

The server and host builds publish only their immutable `sha-<short>` and date
tags. After both builds succeed, the workflow applies `staging-nightly` from
each build's pushed manifest digest. A failed build therefore leaves the
previous floating tag unchanged.

## First package setup

The first publish creates the GHCR packages as **private**. In GitHub package
settings, flip both packages to **public** once so homelab nodes can pull them
without credentials. The workflow uses only the job's `GITHUB_TOKEN` to log in
and publish; it does not change package visibility.

## Pin by digest

Inspect the manifest list for a date tag:

```bash
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
against that tag. The `--ref` flag makes the workflow run itself use the same
tag as the required `ref` input, while the timestamp filter avoids watching an
older dispatch run:

```bash
set -euo pipefail

TAG=nightly-20260809
REPO=btli/omnigent
WORKFLOW=personal-staging-images.yml
DISPATCHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run "$WORKFLOW" -R "$REPO" --ref "$TAG" -f ref="$TAG"

RUN_ID=""
for attempt in 1 2 3 4 5; do
  RUN_ID="$(gh run list -R "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch --limit 20 --json databaseId,createdAt --jq "[.[] | select(.createdAt >= \"${DISPATCHED_AT}\")] | sort_by([.createdAt, .databaseId]) | .[-1].databaseId // empty")"
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

HEAD_SHA="$(gh run view "$RUN_ID" -R "$REPO" --json headSha --jq '.headSha')"
SHORT_SHA="${HEAD_SHA:0:7}"
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
