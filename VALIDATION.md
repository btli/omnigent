# Touch validation rig

This worktree combines the six `pollux/touch-p0-*` branches for live Android validation. It is throwaway integration infrastructure; do not push it or open a pull request from it.

## Prerequisites

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
export ANDROID_HOME="$HOME/Library/Android/sdk"
node --version   # v22.x
java -version   # 21.x
```

## Refresh the integration merge

Commit or discard deliberate local edits first. Already-merged tips report `Already up to date`; updated tips make new merge commits.

```sh
git fetch origin main
git fetch fork pollux/touch-p0-capability pollux/touch-p0-resize-panel pollux/touch-p0-resize-sidebar pollux/touch-p0-resize-inline pollux/touch-p0-resize-comments pollux/touch-p0-resize-column
git merge --no-edit origin/main
for branch in pollux/touch-p0-capability pollux/touch-p0-resize-panel pollux/touch-p0-resize-sidebar pollux/touch-p0-resize-inline pollux/touch-p0-resize-comments pollux/touch-p0-resize-column; do
  git merge --no-edit "$branch"
done
git status --short --branch
git ls-remote --heads fork 'refs/heads/pollux/touch-p0-*'
```

If a merge conflicts, stop and record the branch and files. Do not resolve a semantic production-code conflict merely to make the rig green.

## Web tests

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation/web
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
pnpm install --frozen-lockfile
NODE_OPTIONS="--no-experimental-webstorage" pnpm exec vitest run
# Fallback if pnpm exec misbehaves:
NODE_OPTIONS="--no-experimental-webstorage" ./node_modules/.bin/vitest run
```

Baseline on 2026-08-14: 289 files (286 passed, 2 failed, 1 skipped), 5,813 tests (5,805 passed, 4 failed, 3 expected failures, 1 skipped). The three `NewChatDialog.test.tsx` failures that cannot find `This machine` reproduce on `origin/main`. The `useResizableInlinePanel.test.tsx` persistence failure does not exist on main and belongs to the integrated touch branches.

## Rebuild, boot, install, and serve live code

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
cp /Users/bryan.li/Projects/omnigent/web/android/local.properties web/android/local.properties
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
export ANDROID_HOME="$HOME/Library/Android/sdk"
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
cd web/android
./gradlew assembleDebug
if ! adb devices | rg -q '^emulator-.*device'; then
  "$ANDROID_HOME/emulator/emulator" -avd RemoteDevTest -no-snapshot-load &
  adb wait-for-device
  until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 1; done
fi
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

In terminal 1, run the backend and Vite. Install `omnidev` once if needed:

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
command -v omnidev >/dev/null || cargo install --path dev/omnidev --locked
omnidev --vite-host 0.0.0.0 --trust-lan-origins
```

Wait for `server running` and `vite running`. In terminal 2:

```sh
adb reverse tcp:8000 tcp:5173
adb shell am force-stop ai.omnigent.android
adb shell am start -n ai.omnigent.android/.MainActivity
adb reverse --list
```

On first install (or after `adb shell pm clear ai.omnigent.android`), enter `http://localhost:8000` and tap **Connect**. The host badge should say `localhost:8000`, and the new-chat UI should load. `adb install -r` preserves this choice in later fix rounds.

## Per-PR touch checks

Use landscape/tablet width for desktop handles (`adb shell settings put system user_rotation 1`). Create a chat session so workspace surfaces are available. For every drag, verify the handle has a forgiving touch target, content tracks the finger, text is not selected, the final size persists after reload, and adjacent content keeps its minimum width.

- `touch-p0-resize-panel`: open Files, a file viewer, execution logs, or the right Shells panel; drag its left edge both ways.
- `touch-p0-resize-sidebar`: open the conversations sidebar and drag its right edge. Viewport shrink must clamp temporarily and widening must restore the saved width.
- `touch-p0-resize-inline`: expand the inline Workspace panel and drag its left edge across embedded/file content. The full-window overlay must prevent losing the gesture over an iframe.
- `touch-p0-resize-comments`: open Comments and drag its left edge at `md`+ width. Below `md` it becomes a bottom surface and must not retain a desktop drag.
- `touch-p0-resize-column`: open Shells with a split terminal and drag the divider between terminal columns.

For each of the five handles:

1. Start with finger 1, place/move finger 2, then finish with finger 1. Finger 2 must not steal, jump, or end the drag.
2. Start a drag and cancel by rotating, crossing `md`, backgrounding, or using the emulator cancel gesture. The overlay/cursor must clear, cancelled size must not persist, and the next drag must work.
3. Release while crossing iframe/content boundaries. Release must persist and must not leave the page dragging.

For `touch-p0-capability`, attach Chrome DevTools (`chrome://inspect/#devices`) and inspect `window.__omnigentIsMobileViewport()`. Test the exact `md` boundary. At the tested density of 420, 2,016 physical px equals 768 CSS px:

```sh
adb shell wm density
adb shell wm size 2015x1080  # mobile true; sidebar drawer
adb shell wm size 2016x1080  # mobile false; docked/resizable sidebar
adb shell wm size reset
```

If density differs, calculate the boundary width as `768 * density / 160`; confirm `window.innerWidth` in DevTools.

Android back versus breakpoint signal:

1. Below `md`, open the sidebar drawer and run `adb shell input keyevent 4`. The drawer closes while the app/session remains.
2. Repeat with each visible modal/drawer; back dismisses the topmost overlay first.
3. At `md`+, open the docked sidebar and press back. It must not be treated as a mobile drawer merely because the device is touch-capable; normal WebView history/native back applies.
4. Compare `window.__omnigentIsMobileViewport()` with `window.innerWidth < 768`. Native back must follow the function when present; the inline-width check is only an older-build fallback.

## Record a demo

Start recording, perform the interaction, then stop with Ctrl-C or let the limit expire:

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
mkdir -p validation-artifacts
adb shell screenrecord --time-limit 30 /sdcard/touch-demo.mp4
adb pull /sdcard/touch-demo.mp4 validation-artifacts/touch-demo.mp4
ffmpeg -i validation-artifacts/touch-demo.mp4 -f null -
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 validation-artifacts/touch-demo.mp4
```

For an adb-driven smoke gesture, run `adb shell input swipe START_X START_Y END_X END_Y 900` while `screenrecord` is active.
