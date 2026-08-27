#!/usr/bin/env bash

set -euo pipefail

: "${SOURCE_REPO:?SOURCE_REPO must name the canonical repository}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must name the workflow repository}"

# Canonical runs already get the right tags from actions/checkout.
if [ "$GITHUB_REPOSITORY" = "$SOURCE_REPO" ]; then
  exit 0
fi

# Fork-local tags may be incomplete or diverge from the canonical tag set.
git for-each-ref --format='delete %(refname)' refs/tags | git update-ref --stdin
if ! git fetch --force --no-tags "https://github.com/${SOURCE_REPO}.git" \
  '+refs/tags/*:refs/tags/*'; then
  echo "Unable to fetch release tags from canonical repository $SOURCE_REPO" >&2
  exit 1
fi
