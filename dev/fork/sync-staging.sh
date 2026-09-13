#!/usr/bin/env bash
# Rebuild the `staging` branch: upstream base + manifest PR tips + homelab.
#
# staging is a derived build artifact, never rebased and never a PR source.
# Each rebuild starts fresh from upstream/main and merges the pinned PR heads
# from staging-manifest.txt as-is, so upstream PR branches are never touched
# and maintainer approvals survive every sync. Conflicts are resolved once in
# the merge commits; git rerere replays those resolutions on later rebuilds.
#
# Usage: sync-staging.sh [--base <ref>] [--push] [--tag]
#   --base   base ref to build on (default: upstream/main)
#   --push   force-with-lease push the result to origin staging
#   --tag    tag the result staging-build/<utc timestamp>
set -euo pipefail

BASE="upstream/main"
PUSH=0
TAG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --tag) TAG=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

TOPLEVEL="$(git rev-parse --show-toplevel)"
MANIFEST="$TOPLEVEL/dev/fork/staging-manifest.txt"
WT="$TOPLEVEL/../omnigent-worktrees/staging-rebuild"
OVERLAY="homelab"

[ -f "$MANIFEST" ] || { echo "manifest not found: $MANIFEST" >&2; exit 1; }

# Snapshot the manifest before any checkout can change the working tree.
MANIFEST_SNAP="$(mktemp)"
trap 'rm -f "$MANIFEST_SNAP"' EXIT
cp "$MANIFEST" "$MANIFEST_SNAP"

git config rerere.enabled true
git config rerere.autoupdate true

echo "── fetching upstream"
git fetch upstream

# Ensure every pinned sha is present; PR heads are fetchable from upstream.
missing=0
while read -r num sha label; do
  case "$num" in \#*|"") continue ;; esac
  git rev-parse --verify -q "$sha^{commit}" >/dev/null && continue
  echo "── fetching PR #$num head ($label)"
  git fetch upstream "pull/$num/head" || true
  git rev-parse --verify -q "$sha^{commit}" >/dev/null || {
    echo "MISSING: pinned sha $sha for PR #$num ($label) — the PR may have"
    echo "been force-pushed. Update the manifest line or restore the commit."
    missing=1
  }
done < "$MANIFEST_SNAP"
[ "$missing" = 0 ] || exit 1

# Advisory drift check: warn when a PR merged upstream or moved past its pin.
if command -v gh >/dev/null 2>&1; then
  while read -r num sha label; do
    case "$num" in \#*|"") continue ;; esac
    info="$(gh api "repos/omnigent-ai/omnigent/pulls/$num" \
      --jq '.state + " " + (.merged|tostring) + " " + .head.sha' 2>/dev/null)" || continue
    read -r state merged head <<<"$info"
    if [ "$merged" = "true" ]; then
      echo "NOTE: PR #$num ($label) merged upstream — remove it from the manifest."
    elif [ "$state" = "closed" ]; then
      echo "NOTE: PR #$num ($label) is closed unmerged — remove or keep deliberately."
    elif [ "$head" != "$sha" ]; then
      echo "NOTE: PR #$num ($label) head moved to ${head:0:12} (pinned ${sha:0:12})."
    fi
  done < "$MANIFEST_SNAP"
fi

echo "── building in worktree $WT"
if [ -d "$WT" ]; then
  git -C "$WT" merge --abort 2>/dev/null || true
  git -C "$WT" reset --hard >/dev/null
  git -C "$WT" checkout --detach "$BASE" >/dev/null 2>&1
else
  git worktree add --detach "$WT" "$BASE" >/dev/null
fi
git -C "$WT" reset --hard "$BASE" >/dev/null

merge_one() {
  local ref="$1" msg="$2"
  git -C "$WT" merge --no-ff --rerere-autoupdate -m "$msg" "$ref" >/dev/null && return 0
  # A fatal before the merge started (flaky fs, not a conflict) leaves no
  # MERGE_HEAD; retry once rather than misreading it as resolved.
  if ! git -C "$WT" rev-parse -q --verify MERGE_HEAD >/dev/null; then
    echo "   (merge aborted before starting; retrying once)"
    git -C "$WT" merge --no-ff --rerere-autoupdate -m "$msg" "$ref" >/dev/null && return 0
    git -C "$WT" rev-parse -q --verify MERGE_HEAD >/dev/null || {
      echo "merge of $ref failed without starting; see $WT" >&2; exit 1; }
  fi
  # rerere may have staged every resolution; if nothing is left unmerged,
  # the merge only needs committing.
  if [ -z "$(git -C "$WT" diff --name-only --diff-filter=U)" ]; then
    echo "   (conflicts auto-resolved from rerere cache)"
    git -C "$WT" -c core.editor=true merge --continue >/dev/null
    return 0
  fi
  echo
  echo "CONFLICT merging $ref:"
  git -C "$WT" diff --name-only --diff-filter=U | sed 's/^/    /'
  echo "Resolve in $WT, then: git add -A && git commit  (rerere records the"
  echo "resolution), then rerun sync-staging — completed merges replay instantly."
  exit 1
}

while read -r num sha label; do
  case "$num" in \#*|"") continue ;; esac
  echo "── merge PR #$num ($label @ ${sha:0:12})"
  merge_one "$sha" "staging: merge PR #$num ($label @ ${sha:0:12})"
done < "$MANIFEST_SNAP"

overlay_sha="$(git rev-parse "$OVERLAY")"
echo "── merge branch $OVERLAY (@ ${overlay_sha:0:12})"
merge_one "$OVERLAY" "staging: merge branch $OVERLAY ($OVERLAY @ ${overlay_sha:0:12})"

new_sha="$(git -C "$WT" rev-parse HEAD)"
git branch -f staging "$new_sha"
echo "── staging rebuilt at ${new_sha:0:12} (base: $(git rev-parse --short "$BASE"))"
git -C "$WT" diff --stat "$BASE" HEAD | tail -1

if [ "$TAG" = 1 ]; then
  t="staging-build/$(date -u +%Y%m%d-%H%M)"
  git tag -f "$t" "$new_sha"
  echo "── tagged $t"
fi

if [ "$PUSH" = 1 ]; then
  echo "── pushing origin staging (force-with-lease)"
  git push --force-with-lease origin staging
else
  echo "── not pushed; run with --push (or: git push --force-with-lease origin staging)"
fi
