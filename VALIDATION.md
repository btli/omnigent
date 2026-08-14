# Touch validation rig

This worktree combines the six `pollux/touch-p0-*` branches for live Android and iOS validation across phone, unfolded foldable, and tablet layouts. It is throwaway integration infrastructure; do not push it or open a pull request from it.

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

If the worktree package links are missing, run `pnpm install --frozen-lockfile` from the repository root first, then invoke `./node_modules/.bin` tools directly.

Baseline on 2026-08-14: 289 files (286 passed, 2 failed, 1 skipped), 5,813 tests (5,805 passed, 4 failed, 3 expected failures, 1 skipped). The three `NewChatDialog.test.tsx` failures that cannot find `This machine` reproduce on `origin/main`. The `useResizableInlinePanel.test.tsx` persistence failure does not exist on main and belongs to the integrated touch branches.

## Build once and serve live code

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
cp /Users/bryan.li/Projects/omnigent/web/android/local.properties web/android/local.properties
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
export ANDROID_HOME="$HOME/Library/Android/sdk"
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
cd web/android
./gradlew assembleDebug
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

On first install, enter `http://localhost:8000` and tap **Connect**. The host badge should say `localhost:8000`, and the new-chat UI should load. `adb install -r` preserves this choice in later fix rounds.

If `omnidev` leaves the backend running but Vite exits, restart only Vite:

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation/web
OMNIGENT_URL=http://127.0.0.1:6767 ./node_modules/.bin/vite --host 127.0.0.1 --port 5173 --strictPort
```

## Android phone

Use the existing Pixel-class `RemoteDevTest` AVD (1080x2400, 420 dpi):

```sh
export ANDROID_HOME="$HOME/Library/Android/sdk"
"$ANDROID_HOME/emulator/emulator" -avd RemoteDevTest -no-snapshot-load &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 1; done
adb install -r web/android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8000 tcp:5173
adb shell am start -n ai.omnigent.android/.MainActivity
adb shell wm size
adb shell wm density
```

Record to `validation-artifacts/android-phone.mp4`:

```sh
adb shell screenrecord --time-limit 60 /sdcard/android-phone.mp4
# Perform the phone checks while recording; Ctrl-C may stop early.
adb pull /sdcard/android-phone.mp4 validation-artifacts/android-phone.mp4
ffmpeg -v error -i validation-artifacts/android-phone.mp4 -f null -
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 validation-artifacts/android-phone.mp4
```

Expected layout: below `md`; sidebar and side panels are drawers/mobile surfaces. The checked-in smoke proof is `validation-artifacts/touch-validation-smoke.mp4` (8.334 seconds).

## Android foldable, unfolded

The SDK has no Pixel 10/9 Pro Fold profile. Create the closest installed `7.6in Foldable` profile and override its unfolded viewport to 2076x2152 at 420 dpi:

```sh
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
export ANDROID_HOME="$HOME/Library/Android/sdk"
if ! "$ANDROID_HOME/emulator/emulator" -list-avds | rg -qx TouchValidationFoldable; then
  printf 'no\n' | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd \
    -n TouchValidationFoldable \
    -k 'system-images;android-34;google_apis;arm64-v8a' \
    -d '7.6in Foldable'
fi
"$ANDROID_HOME/emulator/emulator" -avd TouchValidationFoldable -no-snapshot-load &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 1; done
adb shell cmd device_state state 3 || true
adb shell wm size 2076x2152
adb shell wm density 420
adb install -r web/android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8000 tcp:5173
adb shell am start -n ai.omnigent.android/.MainActivity
```

Record at half-size (the stock encoder is more reliable below 1920 pixels) to `validation-artifacts/android-foldable-unfolded.mp4`:

```sh
adb shell screenrecord --size 1038x1076 --bit-rate 4000000 --time-limit 60 /sdcard/android-foldable-unfolded.mp4
adb pull /sdcard/android-foldable-unfolded.mp4 validation-artifacts/android-foldable-unfolded.mp4
ffmpeg -v error -i validation-artifacts/android-foldable-unfolded.mp4 -f null -
ffprobe -v error -show_entries stream=width,height,codec_name -show_entries format=duration,size -of default=noprint_wrappers=1 validation-artifacts/android-foldable-unfolded.mp4
```

Expected layout: `window.innerWidth` is at least 768 CSS px, desktop-style rails are docked/resizable, and `(pointer: coarse)`/touch remains true. This is the key capability-PR regime. The checked-in proof is 10.503 seconds.

## Android tablet

Create and boot the SDK's native Pixel Tablet profile (2560x1600 landscape, 320 dpi):

```sh
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
export ANDROID_HOME="$HOME/Library/Android/sdk"
if ! "$ANDROID_HOME/emulator/emulator" -list-avds | rg -qx TouchValidationTablet; then
  printf 'no\n' | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd \
    -n TouchValidationTablet \
    -k 'system-images;android-34;google_apis;arm64-v8a' \
    -d pixel_tablet
fi
"$ANDROID_HOME/emulator/emulator" -avd TouchValidationTablet -no-snapshot-load &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 1; done
adb install -r web/android/app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8000 tcp:5173
adb shell am start -n ai.omnigent.android/.MainActivity
adb shell wm size
adb shell wm density
```

Record to `validation-artifacts/android-tablet.mp4`:

```sh
adb shell screenrecord --size 1280x800 --bit-rate 4000000 --time-limit 60 /sdcard/android-tablet.mp4
adb pull /sdcard/android-tablet.mp4 validation-artifacts/android-tablet.mp4
ffmpeg -v error -i validation-artifacts/android-tablet.mp4 -f null -
ffprobe -v error -show_entries stream=width,height,codec_name -show_entries format=duration,size -of default=noprint_wrappers=1 validation-artifacts/android-tablet.mp4
```

Expected layout: `window.innerWidth` is at least 768 CSS px with docked desktop rails and coarse touch input. The checked-in proof is 9.906 seconds.

## iOS phone

The shell currently declares `TARGETED_DEVICE_FAMILY = 1`, so it supports iPhone only; do not add an iPad run until the app target opts into family 2. Xcode 26+ and an iOS 26 runtime are required.

On this machine, first accept the Xcode license in a human terminal (requires administrator authority):

```sh
sudo DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -license
```

Then create/boot an iPhone, build without signing, install, and launch:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
IOS_RUNTIME=$(xcrun simctl list runtimes -j | jq -r '.runtimes[] | select(.platform == "iOS" and .isAvailable) | .identifier' | tail -1)
IOS_TYPE=$(xcrun simctl list devicetypes -j | jq -r '.devicetypes[] | select(.name == "iPhone 17 Pro") | .identifier')
IOS_UDID=$(xcrun simctl list devices -j | jq -r '.devices[][] | select(.name == "TouchValidationiPhone") | .udid' | head -1)
if [ -z "$IOS_UDID" ]; then
  IOS_UDID=$(xcrun simctl create TouchValidationiPhone "$IOS_TYPE" "$IOS_RUNTIME")
fi
xcrun simctl boot "$IOS_UDID" 2>/dev/null || true
open -a Simulator
xcrun simctl bootstatus "$IOS_UDID" -b
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation/web/ios
xcodebuild -project Omnigent.xcodeproj -scheme Omnigent -configuration Debug \
  -destination "platform=iOS Simulator,id=$IOS_UDID" \
  -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
xcrun simctl install "$IOS_UDID" build/Build/Products/Debug-iphonesimulator/Omnigent.app
xcrun simctl launch "$IOS_UDID" ai.omnigent.ios
```

Connect the native shell to `http://localhost:5173`; the iOS Simulator shares the Mac loopback, so no `adb reverse` equivalent is needed. Confirm the live new-chat UI loads.

Record to `validation-artifacts/ios-phone.mov` in one terminal, perform the gesture in Simulator, and press Ctrl-C:

```sh
cd /Users/bryan.li/Projects/omnigent/.worktrees/touch-validation
xcrun simctl io "$IOS_UDID" recordVideo --codec=h264 validation-artifacts/ios-phone.mov
ffmpeg -v error -i validation-artifacts/ios-phone.mov -f null -
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 validation-artifacts/ios-phone.mov
```

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

## Demo-recording checklist

Make one recording per form factor. A complete demo may use several sessions or clips, but the final artifact for that form factor must visibly establish every applicable item below.

### Android phone checklist

- Show the below-`md` layout: sidebar and side surfaces behave as drawers, not docked rails.
- Touch-drag the sidebar edge, right panel seam, inline workspace rail, comments gutter, and terminals column wherever the mobile layout exposes them; explicitly show that desktop-only seams are absent when the corresponding surface is a drawer.
- Beside every exposed seam, scroll the adjacent content without accidentally resizing it. Include scrolling the transcript on the transcript/right-panel scrollbar seam.
- On an exposed seam, begin with finger 1, add/move finger 2, and show that finger 2 is ignored.
- Cancel an active drag (rotate/background/breakpoint transition), then show the next drag succeeds.
- Press Android back with a drawer open and show that the drawer closes without leaving the app.

### Android foldable-unfolded checklist

- Show the 2076x2152 unfolded viewport and the breakpoint-correct `md`+ layout with docked desktop rails despite coarse touch input.
- Touch-drag all five resize seams: sidebar right edge, right panel left edge, inline workspace rail, comments gutter, and terminals column divider.
- Immediately beside each seam, scroll its adjacent content without resizing. Include the transcript scrollbar/right-panel seam explicitly.
- Start each representative drag with finger 1, add/move finger 2, and show that the active pointer remains finger 1.
- Cross iframe/content boundaries during inline/right-panel drags and release cleanly.
- Cancel a drag by crossing below `md`, return to unfolded `md`+, and show saved width restoration plus a successful next drag.

### Android tablet checklist

- Show the native 2560x1600 landscape viewport and breakpoint-correct `md`+ docked layout under coarse touch input.
- Touch-drag all five seams: sidebar, right panel, inline rail, comments gutter, and terminals column.
- Adjacent-scroll beside every seam without resizing, including transcript scrolling at the transcript scrollbar seam.
- Demonstrate second-finger-ignored and cancellation recovery on at least one seam; repeat on any seam whose behavior differs.
- Rotate portrait/landscape and verify temporary clamping/restoration rather than overwriting the preferred width.

### iOS phone checklist

- Show the below-`md` iPhone layout and live web UI inside the native shell.
- Touch-drag every seam the phone layout exposes; show desktop-only seams remain absent for drawer-style surfaces.
- Adjacent-scroll beside each exposed seam without resizing, including the transcript scrollbar seam.
- Demonstrate second-finger-ignored, cancellation recovery, and breakpoint-correct layout after rotation.
- Exercise iOS navigation/back behavior with an open drawer/modal and confirm the topmost surface dismisses first.

For adb-driven smoke motion, run `adb shell input swipe START_X START_Y END_X END_Y 900` while `screenrecord` is active. This proves the transport/recording pipeline only; it does not replace the multi-touch human checks above.
