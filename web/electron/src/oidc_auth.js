"use strict";

const { setTimeout: delay } = require("node:timers/promises");

const AUTH_PROBE_TIMEOUT_MS = 10000;
const OIDC_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
const OIDC_POLL_INTERVAL_MS = 2000;
const OIDC_REQUEST_TIMEOUT_MS = 10000;
const TRANSIENT_AUTH_STATUSES = new Set([429, 502, 503, 504]);

// Keep API routes under workspace mounts, matching the CLI.
function serverRoute(serverUrl, routePath) {
  return serverUrl.replace(/\/+$/, "") + (routePath.startsWith("/") ? routePath : `/${routePath}`);
}

/** @returns {"authenticated" | "oidc" | "accounts" | "other"} */
function classifyAuthProbe(status, loginUrl) {
  if (status === 200) return "authenticated";
  if (status !== 401) return "other";
  if (loginUrl === "/auth/login") return "oidc";
  if (loginUrl === "/login") return "accounts";
  return "other";
}

// Keep redirects manual so workspace auth stays on its existing path.
async function probeServerAuth(
  electronSession,
  serverUrl,
  { timeoutMs = AUTH_PROBE_TIMEOUT_MS, signal } = {},
) {
  const response = await electronSession.fetch(serverRoute(serverUrl, "/v1/me"), {
    method: "GET",
    redirect: "manual",
    cache: "no-store",
    credentials: "include",
    signal: requestSignal(signal, timeoutMs),
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

function isUserAbort(signal) {
  return signal?.aborted === true;
}

// The ticket stays in memory; only the system browser renders its URL.
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
  const deadline = Date.now() + timeoutMs;
  let ticket;
  let loginUrl;
  try {
    const createTicket = async () => {
      const response = await electronSession.fetch(serverRoute(serverUrl, "/auth/cli-login"), {
        method: "POST",
        body: "",
        redirect: "manual",
        cache: "no-store",
        signal: requestSignal(signal),
      });
      if (!TRANSIENT_AUTH_STATUSES.has(response.status)) return response;
      try {
        onPollError?.(response.status);
      } catch {
        // Progress reporting cannot terminate authentication.
      }
      if (Date.now() >= deadline) return null;
      await delay(pollIntervalMs, undefined, { signal });
      return Date.now() < deadline ? createTicket() : null;
    };
    const response = await createTicket();
    if (!response) return { ok: false, reason: "timed_out" };
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
  const pollForCompletion = async () => {
    if (Date.now() >= deadline) {
      return { ok: false, reason: isUserAbort(signal) ? "cancelled" : "timed_out" };
    }
    try {
      await delay(pollIntervalMs, undefined, { signal });
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
    if (TRANSIENT_AUTH_STATUSES.has(response.status)) {
      try {
        onPollError?.(response.status);
      } catch {
        // Progress reporting must never terminate an otherwise recoverable poll.
      }
      return pollForCompletion();
    }
    if (response.status === 410) return { ok: false, reason: "expired" };
    if (response.status !== 200) return { ok: false, reason: "failed" };

    try {
      const body = await response.json();
      if (!body || typeof body.token !== "string" || body.token === "") {
        return { ok: false, reason: "failed" };
      }
      return { ok: true, token: body.token };
    } catch {
      return { ok: false, reason: "failed" };
    }
  };
  return pollForCompletion();
}

// __Host- cookies require Secure, Path=/, and no Domain attribute.
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

// Prove both Chromium and the server accepted the installed session.
async function installAndVerifySessionCookie(
  electronSession,
  serverUrl,
  token,
  { verificationAttempts = 3, retryDelayMs = 250, signal } = {},
) {
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

  const verify = async (attempt) => {
    const probe = await probeServerAuth(electronSession, serverUrl, { signal });
    if (probe.kind === "authenticated") return;
    if (!TRANSIENT_AUTH_STATUSES.has(probe.status) || attempt >= verificationAttempts) {
      throw new Error("The server did not accept the installed session cookie.");
    }
    await delay(retryDelayMs, undefined, { signal });
    return verify(attempt + 1);
  };
  await verify(1);
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
