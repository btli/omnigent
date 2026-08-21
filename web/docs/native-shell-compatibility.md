# Native shell compatibility

The React picker at the bottom of the left conversations sidebar is the only
normal server picker on Electron, iOS, and Android. Mobile shells never add a
top-center picker or recovery overlay.

## Protocol

Mobile bridge protocol version `1` requires the mounted web app to call
`nativeWebReady(1)` and send `nativeHeartbeat(1)` periodically. The shell waits
20 seconds for readiness and 15 seconds between heartbeats. A missing or
different version, a renderer crash, or a liveness timeout leaves the WebView
and opens the existing full-screen connect view with an error and the current
server prefilled.

The watchdog is suspended while the app is inactive, locked, backgrounded, or
showing external authentication, and whenever the main frame is away from the
pinned server origin for embedded IdP/MFA. Returning to the foreground and the
pinned origin starts a fresh 20-second readiness window before heartbeats are
required again.

Picker reads are native round trips. Each `getServerPicker()` request reads the
current managed configuration and persisted recents, and switching is checked
again against that same native state. Managed duplicates use a canonical URL
key (including mount path and query, excluding default ports and fragments).

## Version matrix

| Shell                                            | Server-served web       | Result                                                                                                   |
| ------------------------------------------------ | ----------------------- | -------------------------------------------------------------------------------------------------------- |
| Protocol-1 mobile shell                          | Protocol-1 web          | Sidebar picker and heartbeat operate normally.                                                           |
| Protocol-1 mobile shell                          | Cached pre-protocol web | Readiness times out into full-screen connect/failure.                                                    |
| Protocol-1 mobile shell                          | Future/incompatible web | Version mismatch opens full-screen connect/failure.                                                      |
| Pre-protocol mobile shell                        | Protocol-1 web          | The web hides any legacy picker and replaces the app surface with a full-screen update-required outcome. |
| Android without the origin-scoped message bridge | Any web                 | MainActivity opens full-screen connect/failure before loading the server UI.                             |
| Plain browser                                    | Any web                 | Native bridge remains null-gated and no server picker is mounted.                                        |

No matrix entry uses the retired native picker. If an old shell lacks a native
full-screen recovery capability, protocol-1 web renders its own full-screen
update-required page and explicitly keeps the legacy switcher hidden.

## Reload decision

Reload is not an automatic recovery action. It can loop forever on a blank,
hung, crashed, or incompatible bundle and provides no route to another server.
All those cases go to full-screen connect/failure; pressing **Connect** retries
the prefilled server explicitly.
