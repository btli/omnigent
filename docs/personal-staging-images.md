# Personal staging images

`.github/workflows/personal-staging-images.yml` publishes the server and host
images from `btli/omnigent` to GHCR. It runs for `nightly-*` tag pushes and for
manual dispatches that provide an existing tag or ref through the `ref` input.
The job is deliberately guarded to run only in the fork repository.

Both images are multi-architecture manifests for `linux/amd64` and
`linux/arm64`:

- `ghcr.io/btli/omnigent-server`
- `ghcr.io/btli/omnigent-host`

Each publish adds three tags to both images:

- `sha-<short>` — the short commit pin.
- The triggering `nightly-*` tag, such as `nightly-20260809`.
- `staging-nightly` — the floating staging tag.

The server build uses the Dockerfile's default `runtime` stage, as in the
upstream image workflow. The host build uses the explicit `host` target. Both
builds use QEMU, Buildx, and the GitHub Actions layer cache.

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
against it:

```bash
TAG=nightly-20260809
gh workflow run personal-staging-images.yml -R btli/omnigent -f ref="$TAG"
RUN_ID="$(gh run list -R btli/omnigent --workflow personal-staging-images.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$RUN_ID" -R btli/omnigent
docker buildx imagetools inspect "ghcr.io/btli/omnigent-server:$TAG"
docker buildx imagetools inspect "ghcr.io/btli/omnigent-host:$TAG"
```

Replace `nightly-20260809` with a tag that exists in the fork. Confirm the run
is successful and that each inspect command shows both `linux/amd64` and
`linux/arm64` manifests.

Harbor naming and auto-pin behavior at `registry.joyful.house` are out of
scope. This workflow never logs in to or pushes to Harbor; date tags must never
be pushed there.
