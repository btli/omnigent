# Android `omnigent://` deep-link handling — design

Register the Android shell (`web/android`) as a handler for
`omnigent://<host>[:port]/c/<session_id>` links and handle them with the same
user-visible semantics as the iOS and desktop shells, using idiomatic Android
mechanisms. Behavior (URL shape, scheme inference, consent rules) stays
consistent across shells; the implementation follows Android platform patterns
rather than mirroring iOS structure.

## Link contract (shared with iOS/desktop)

- Shape: `omnigent://<host>[:port]/c/<id>` — nothing else is accepted.
- The link carries no `http`/`https`; the scheme is inferred: `http` for
  loopback hosts (`localhost`, `127.0.0.1`, `::1`), `https` otherwise.
- The Databricks workspace mount (`/ml/omnigents`) is never in the link; it is
  server-determined.
- Custom schemes are unverified on Android exactly as on iOS: any co-installed
  app may also declare `omnigent` and receive the link (host + conversation id
  are metadata-disclosed; the id carries no secret). Treat the scheme as
  untrusted input.

## Components

### 1. Manifest registration

A second `<intent-filter>` on `MainActivity` (which already uses
`launchMode="singleTop"`, so warm taps arrive via `onNewIntent`):

- `android.intent.action.VIEW`
- categories `DEFAULT` + `BROWSABLE` (required for links tapped in a browser)
- `<data android:scheme="omnigent" />`

### 2. `DeepLink.kt` — pure parser

A small value type + `parse(uri: Uri): DeepLink?` in `ai.omnigent.android`,
producing `(origin, path)`:

- Accepts only scheme `omnigent` (case-insensitive), a non-empty host, and a
  path of exactly `/c/<id>` (one optional trailing slash tolerated).
- The id segment is validated against a **denylist**, not a grammar (the SPA's
  router stays the authority on id format): rejects `?`, `#`, `/`, `.`, `%`,
  and control characters (U+0000–U+001F, U+007F). `Uri.getPath()` is
  percent-decoded, so smuggled encoded separators (`%3F`, `%23`, `%2F`, `%2E`,
  `%00`) reappear as literals and are caught; a malformed escape leaves a stray
  `%`, also caught.
- Origin is built as `scheme://host[:port]` with no trailing slash, `http` for
  loopback and `https` otherwise, re-bracketing IPv6 hosts.
- Anything unparseable returns null — an unrecognized link must never crash or
  mis-navigate.

### 3. Handling in `MainActivity`

Both `onCreate` and `onNewIntent` check for `ACTION_VIEW` with a data URI and
route through one handler:

- **Same origin as pinned** → set `pendingNavigatePath = link.path` and flush
  through the existing pending-path replay (the notification-activation
  mechanism); the SPA navigates in place with no reload.
- **Known server** — a `ServerStore` recent whose `originOf(url)` matches the
  link origin (this also covers stored URLs that carry a workspace mount) →
  `store.connect(storedUrl)` + `reloadWithNewServer(...)`, with the path left
  pending so `onPageReady` flushes it.
- **Unknown server** → AppCompat `AlertDialog` consent before any network
  request or persistence: "This link will connect Omnigent to `<host>` and
  open a conversation." Open → connect to the inferred origin, reload, navigate;
  Cancel → no-op (or fall through to `ConnectActivity` when no server was ever
  configured). Consent is required because pinning a new origin grants it the
  native bridge and notifications.
- **Cold start with no server configured + deep link** → the consent flow runs
  instead of the unconditional `ConnectActivity` redirect.

### 4. Strings

Consent dialog title/body/buttons in `res/values/strings.xml`, matching the
iOS prompt copy.

## Error handling

- Unparseable/rejected links are ignored (the app opens normally); debug builds
  may log the rejection.
- A deep link arriving while the WebView is parked off-origin (mid-login) stays
  pending and flushes on the next pinned-origin `onPageReady` — the existing
  `flushPendingActivation` behavior.

## Known gap (documented, out of scope)

Android has no `WorkspaceURLExpander`. A consent-approved **unknown** Databricks
workspace host connects to the bare origin without probing the `/ml/omnigents`
mount, so such links only fully work for servers the user has already connected
to (their stored URL keeps the mount). Called out in the Android README as a
follow-up.

## Testing

`DeepLinkTest.kt` under `app/src/test` (Robolectric, since the parser uses
`android.net.Uri`), porting the iOS `DeepLinkTests.swift` cases:

- Valid: loopback (http inferred), remote host (https inferred), explicit
  ports, IPv6 loopback, trailing slash on the id.
- Rejected: wrong scheme, missing host, non-`/c/` paths, empty/nested ids,
  literal and percent-encoded `?` `#` `/` `.`, `%zz` malformed escapes, control
  characters, path traversal (`..`).

Manual verification: `adb shell am start -a android.intent.action.VIEW -d
"omnigent://<host>/c/<id>"` against cold start, warm same-origin, known-server
switch, and unknown-server consent (accept and cancel).
