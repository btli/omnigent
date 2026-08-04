default:
    @just --list

export FASTLANE_SKIP_UPDATE_CHECK := "1"

# iOS device override (default: iPhone 17 Pro)
DEVICE := env("OMNIGENT_IOS_SIMULATOR", "iPhone 17 Pro")

# --- uv Python env ---

_check-uv:
    uv run --no-sync ruff --version
    uv run --no-sync pyrefly --version
    uv run --no-sync pre-commit --version

_ensure-uv:
    uv sync --extra all --extra dev

# --- iOS Ruby dependencies ---

_check-ios:
    cd web/ios && bundle check

_ensure-ios:
    cd web/ios && (bundle check || bundle install)

# --- omnidev Rust dev tool ---

_install-omnidev:
    cargo install --path dev/omnidev --locked --force

_check-omnidev:
    command -v omnidev >/dev/null 2>&1

_ensure-omnidev:
    command -v omnidev >/dev/null 2>&1 || just _install-omnidev

# --- Aggregate setup checks / installs ---

[group('setup')]
check: _check-uv _check-ios _check-omnidev

[group('setup')]
ensure: _ensure-uv _ensure-ios _ensure-omnidev

# --- Local dev ---

[group('dev')]
dev: _ensure-omnidev
    omnidev

[group('dev')]
dev-mobile: _ensure-omnidev
    omnidev --vite-host 0.0.0.0 --trust-lan-origins

# --- Mobile builds ---

[group('mobile')]
run-ios: _ensure-ios
    cd web/ios && bundle exec fastlane simulator device:"{{ DEVICE }}"

[group('mobile')]
run-android:
    cd web/android && ./gradlew installDebug runDebug

[group('mobile')]
android-reverse:
    cd web/android && ./gradlew reverseProxy

# --- Electron desktop app ---

_ensure-web:
    cd web && test -d node_modules || pnpm install

_ensure-electron:
    cd web/electron && test -d node_modules || pnpm install

[group('electron')]
electron-dev: _ensure-web _ensure-electron
    pnpm --filter web/electron run dev

[group('electron')]
electron-build: _ensure-web _ensure-electron
    pnpm --filter web/electron run build

# --- Lint ---

[group('lint')]
lint: _ensure-uv
    uv run pre-commit run

[group('lint')]
lint-all: _ensure-uv
    uv run pre-commit run --all-files

[group('lint')]
typecheck-python: _ensure-uv
    uv run --no-sync pyrefly check

[group('lint')]
lint-ts:
    pnpm install --frozen-lockfile --filter web --filter omnigent-vscode
    pnpm --filter web run lint
    pnpm --filter web run type-check
    pnpm --filter omnigent-vscode run type-check

# --- Lockfile maintenance ---

[group('lint')]
normalize-locks: _ensure-uv
    uv run scripts/normalize_uv_lock_registry.py uv.lock || true

# ─── homelab test env (test.omnigent.bryanli.net) ── NOT upstream ─────────────
# Stages a worktree to server0 NFS and bounces the omnigent-test pod (k3s-infra
# k8s/omnigent-test). Spec: homelab docs/superpowers/specs/2026-08-04-*.md

TEST_STAGE := "bli@server0.joyful.house:/srv/k3s/omnigent-test/src/"
TEST_KUBECTL := "ssh bli@host.k3s.joyful.house kubectl -n omnigent-test"
TEST_BASE := env("OMNIGENT_TEST_BASE", "master")

# Deploy the CURRENT worktree (whatever state it's in) to the test env.
[group('test-env')]
test-sync:
    rsync -a --delete --exclude .venv --exclude node_modules \
      --exclude .git --exclude dev/omnidev/target \
      ./ {{ TEST_STAGE }}
    {{ TEST_KUBECTL }} rollout restart deploy/omnigent-test
    {{ TEST_KUBECTL }} rollout status deploy/omnigent-test --timeout=30m
    @echo "→ https://test.omnigent.bryanli.net"

# Build a fresh worktree from upstream {{ TEST_BASE }} + the given PR numbers
# (upstream GitHub PRs), then deploy it. Aborts loudly on merge conflicts.
[group('test-env')]
test-up +prs:
    #!/usr/bin/env bash
    set -euo pipefail
    ids="$(echo "{{ prs }}" | tr ' ' '-')"
    wt="$(git rev-parse --show-toplevel)/../omnigent-worktrees/test-${ids}"
    git fetch upstream {{ TEST_BASE }}
    rm -rf "$wt" && git worktree prune
    git worktree add --detach "$wt" "upstream/{{ TEST_BASE }}"
    cd "$wt"
    for pr in {{ prs }}; do
      echo "── merging upstream PR #$pr"
      git fetch upstream "pull/${pr}/head"
      git merge --no-edit FETCH_HEAD \
        || { echo "CONFLICT merging PR #$pr — resolve in $wt"; exit 1; }
    done
    rsync -a --delete --exclude .venv --exclude node_modules \
      --exclude .git --exclude dev/omnidev/target \
      ./ {{ TEST_STAGE }}
    {{ TEST_KUBECTL }} rollout restart deploy/omnigent-test
    {{ TEST_KUBECTL }} rollout status deploy/omnigent-test --timeout=30m
    echo "→ https://test.omnigent.bryanli.net"

# Stop the test env (staged tree stays; test-sync or a restart revives it).
[group('test-env')]
test-down:
    {{ TEST_KUBECTL }} scale deploy/omnigent-test --replicas=0
# ─── end homelab test env ─────────────────────────────────────────────────────
