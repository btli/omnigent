"use strict";

const AUTH_PROBE_TIMEOUT_MS = 10000;
const OIDC_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
const OIDC_POLL_INTERVAL_MS = 2000;
const OIDC_REQUEST_TIMEOUT_MS = 10000;

/**
 * Join an API path onto a server URL that may include a workspace mount.
 * Mirrors the CLI's string-concatenation behavior.
 *
 * @param {string} serverUrl
 * @param {string} routePath
 * @returns {string}
 */
function serverRoute(serverUrl, routePath) {
  return serverUrl.replace(/\/+$/, "") + (routePath.startsWith("/") ? routePath : `/${routePath}`);
}

/**
 * Classify the auth posture reported by GET /v1/me.
 *
 * @param {number} status
 * @param {unknown} loginUrl
 * @returns {"authenticated" | "oidc" | "accounts" | "other"}
 */
function classifyAuthProbe(status, loginUrl) {
  if (status === 200) return "authenticated";
  if (status !== 401) return "other";
  if (loginUrl === "/auth/login") return "oidc";
  if (loginUrl === "/login") return "accounts";
  return "other";
}

/**
 * Probe the pinned server with Electron's Session fetch so existing Electron
 * cookies participate. Redirects stay manual: Databricks/workspace auth must
 * remain on its existing path rather than being followed and misclassified.
 *
 * @param {Electron.Session} electronSession
 * @param {string} serverUrl
 * @param {{ timeoutMs?: number }} [opts]
 * @returns {Promise<{ kind: ReturnType<typeof classifyAuthProbe>, status: number }>}
 */
async function probeServerAuth(
  electronSession,
  serverUrl,
  { timeoutMs = AUTH_PROBE_TIMEOUT_MS } = {},
) {
  const response = await electronSession.fetch(serverRoute(serverUrl, "/v1/me"), {
    method: "GET",
    redirect: "manual",
    cache: "no-store",
    credentials: "include",
    signal: AbortSignal.timeout(timeoutMs),
  });
  let loginUrl = null;
  if (response.status === 401) {
    try {
      const body = await response.json();
      loginUrl = body && typeof body === "object" ? body.login_url : null;
    } catch {
      // Non-JSON 401: unknown posture, so preserve the existing navigation.
    }
  }
  return { kind: classifyAuthProbe(response.status, loginUrl), status: response.status };
}

function requestSignal(signal, timeoutMs = OIDC_REQUEST_TIMEOUT_MS) {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;
}

function waitForPoll(delayMs, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function isUserAbort(signal) {
  return signal?.aborted === true;
}

/**
 * Run the server's browser-login ticket protocol directly from Electron's main
 * process. The ticket stays in memory and the system browser is the only place
 * the verification URL is rendered.
 *
 * @param {Electron.Session} electronSession
 * @param {string} serverUrl
 * @param {(url: string) => Promise<void>} openExternal
 * @param {{
 *   signal?: AbortSignal,
 *   timeoutMs?: number,
 *   pollIntervalMs?: number,
 *   onPollError?: () => void,
 * }} [opts]
 * @returns {Promise<
 *   | { ok: true, token: string, userId: string | null, expiresIn: number }
 *   | { ok: false, reason: "cancelled" | "timed_out" | "expired" | "failed" }
 * >}
 */
async function runOidcBrowserLogin(
  electronSession,
  serverUrl,
  openExternal,
  {
    signal,
    timeoutMs = OIDC_LOGIN_TIMEOUT_MS,
    pollIntervalMs = OIDC_POLL_INTERVAL_MS,
    onPollError,
  } = {},
) {
  let ticket;
  let loginUrl;
  try {
    const response = await electronSession.fetch(serverRoute(serverUrl, "/auth/cli-login"), {
      method: "POST",
      body: "",
      redirect: "manual",
      cache: "no-store",
      signal: requestSignal(signal),
    });
    if (response.status !== 200) return { ok: false, reason: "failed" };
    const body = await response.json();
    ticket = body && typeof body.ticket === "string" ? body.ticket : "";
    const loginPath = body && typeof body.login_url === "string" ? body.login_url : "";
    if (!ticket || !loginPath.startsWith("/") || loginPath.startsWith("//")) {
      return { ok: false, reason: "failed" };
    }
    loginUrl = serverRoute(serverUrl, loginPath);
    if (new URL(loginUrl).origin !== new URL(serverUrl).origin) {
      return { ok: false, reason: "failed" };
    }
    await openExternal(loginUrl);
  } catch {
    return { ok: false, reason: isUserAbort(signal) ? "cancelled" : "failed" };
  }

  const pollUrl = new URL(serverRoute(serverUrl, "/auth/cli-poll"));
  pollUrl.searchParams.set("ticket", ticket);
  const deadline = Date.now() + timeoutMs;
  const pollForCompletion = async () => {
    if (Date.now() >= deadline) {
      return { ok: false, reason: isUserAbort(signal) ? "cancelled" : "timed_out" };
    }
    try {
      await waitForPoll(pollIntervalMs, signal);
    } catch {
      return { ok: false, reason: "cancelled" };
    }
    if (Date.now() >= deadline) {
      return { ok: false, reason: isUserAbort(signal) ? "cancelled" : "timed_out" };
    }

    let response;
    try {
      response = await electronSession.fetch(pollUrl.toString(), {
        method: "GET",
        redirect: "manual",
        cache: "no-store",
        signal: requestSignal(signal),
      });
    } catch {
      if (isUserAbort(signal)) return { ok: false, reason: "cancelled" };
      try {
        onPollError?.();
      } catch {
        // Progress reporting must never terminate an otherwise recoverable poll.
      }
      return pollForCompletion();
    }
    if (response.status === 202) return pollForCompletion();
    if (response.status === 410) return { ok: false, reason: "expired" };
    if (response.status !== 200) return { ok: false, reason: "failed" };

    try {
      const body = await response.json();
      if (!body || typeof body.token !== "string" || body.token === "") {
        return { ok: false, reason: "failed" };
      }
      return {
        ok: true,
        token: body.token,
        userId: typeof body.user_id === "string" ? body.user_id : null,
        expiresIn:
          typeof body.expires_in === "number" && body.expires_in > 0 ? body.expires_in : 8 * 3600,
      };
    } catch {
      return { ok: false, reason: "failed" };
    }
  };
  return pollForCompletion();
}

/**
 * Build the server's session-cookie shape. __Host- cookies require Secure,
 * Path=/, and no Domain attribute.
 *
 * @param {string} serverUrl
 * @param {string} token
 * @returns {Electron.CookiesSetDetails}
 */
function sessionCookieDetails(serverUrl, token) {
  const isHttps = new URL(serverUrl).protocol === "https:";
  return {
    url: serverUrl,
    name: isHttps ? "__Host-ap_session" : "ap_session",
    value: token,
    httpOnly: true,
    secure: isHttps,
    sameSite: "lax",
    path: "/",
  };
}

/**
 * Install a cached CLI token into Electron, prove Chromium accepted the cookie,
 * then prove the server accepts the resulting session.
 *
 * @param {Electron.Session} electronSession
 * @param {string} serverUrl
 * @param {string} token
 * @returns {Promise<{ cookieName: string }>}
 */
async function installAndVerifySessionCookie(electronSession, serverUrl, token) {
  const details = sessionCookieDetails(serverUrl, token);
  await electronSession.cookies.set(details);
  const accepted = await electronSession.cookies.get({ url: serverUrl, name: details.name });
  const cookie = accepted.find(
    (candidate) =>
      candidate.name === details.name &&
      candidate.value === token &&
      candidate.path === "/" &&
      candidate.httpOnly === true &&
      candidate.secure === details.secure,
  );
  if (!cookie) {
    throw new Error("Electron rejected the session cookie.");
  }

  const probe = await probeServerAuth(electronSession, serverUrl);
  if (probe.kind !== "authenticated") {
    throw new Error("The server did not accept the installed session cookie.");
  }
  return { cookieName: details.name };
}

module.exports = {
  AUTH_PROBE_TIMEOUT_MS,
  OIDC_LOGIN_TIMEOUT_MS,
  OIDC_POLL_INTERVAL_MS,
  OIDC_REQUEST_TIMEOUT_MS,
  serverRoute,
  classifyAuthProbe,
  probeServerAuth,
  runOidcBrowserLogin,
  sessionCookieDetails,
  installAndVerifySessionCookie,
};
