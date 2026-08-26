"use strict";

const { setTimeout: delay } = require("node:timers/promises");
const {
  isDatabricksWorkspaceHost,
  isLoopbackServer,
  joinServerUrl,
  workspaceIdentityKey,
} = require("./url");

const AUTH_PROBE_TIMEOUT_MS = 10000;
const OIDC_LOGIN_TIMEOUT_MS = 5 * 60 * 1000;
const OIDC_POLL_INTERVAL_MS = 2000;
const OIDC_REQUEST_TIMEOUT_MS = 10000;
const SESSION_COOKIE_READ_TIMEOUT_MS = 2000;
const SESSION_COOKIE_OWNERSHIP_ERROR = "SESSION_COOKIE_OWNERSHIP_UNKNOWN";
const MAX_PATH_DECODE_PASSES = 32;
const TRANSIENT_AUTH_STATUSES = new Set([429, 502, 503, 504]);
const cookieMutationQueues = new WeakMap();
const activeCookieWorkspaceOwners = new WeakMap();
const pendingCookieWorkspaceOwners = new WeakMap();
const observedCookieSessions = new WeakSet();

// Keep API routes under workspace mounts, matching the CLI.
function serverRoute(serverUrl, routePath) {
  if (oidcServerUrlError(serverUrl)) throw new Error("Invalid server URL.");
  return joinServerUrl(serverUrl, routePath);
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
  {
    timeoutMs = AUTH_PROBE_TIMEOUT_MS,
    signal,
    cookieWorkspacePrepared = false,
    cookieReadTimeoutMs = SESSION_COOKIE_READ_TIMEOUT_MS,
  } = {},
) {
  const runProbe = async () => {
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
  };
  if (cookieWorkspacePrepared) return runProbe();

  return runWithSessionCookieWorkspace(electronSession, serverUrl, runProbe, {
    cookieReadTimeoutMs,
  });
}

function requestSignal(signal, timeoutMs = OIDC_REQUEST_TIMEOUT_MS) {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;
}

function isUserAbort(signal) {
  return signal?.aborted === true;
}

function oidcServerUrlError(serverUrl) {
  if (typeof serverUrl !== "string" || serverUrl.includes("\\") || serverUrl.includes("#")) {
    return "invalid_server_url";
  }
  let parsed;
  try {
    parsed = new URL(serverUrl);
  } catch {
    return "invalid_server_url";
  }
  const organizationOnly =
    isDatabricksWorkspaceHost(parsed.hostname) &&
    [...parsed.searchParams.keys()].every((key) => key === "o");
  if (serverUrl.includes("?") && !parsed.search) return "invalid_server_url";
  if (parsed.search && !organizationOnly) return "invalid_server_url";
  if (
    (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
    parsed.username ||
    parsed.password ||
    parsed.hash
  ) {
    return "invalid_server_url";
  }
  const serialized = parsed.toString();
  const rootWithoutSlash = parsed.pathname === "/" ? serialized.slice(0, -1) : null;
  if (serverUrl !== serialized && serverUrl !== rootWithoutSlash) {
    return "invalid_server_url";
  }
  if (parsed.pathname.includes("//")) return "invalid_server_url";
  for (const segment of parsed.pathname.split("/")) {
    let decoded = segment;
    let stabilized = false;
    for (let pass = 0; pass < MAX_PATH_DECODE_PASSES; pass += 1) {
      let next;
      try {
        next = decodeURIComponent(decoded);
      } catch {
        return "invalid_server_url";
      }
      if (next === "." || next === ".." || next.includes("/") || next.includes("\\")) {
        return "invalid_server_url";
      }
      if (next === decoded) {
        stabilized = true;
        break;
      }
      decoded = next;
    }
    if (!stabilized) return "invalid_server_url";
  }
  if (parsed.protocol === "http:" && !isLoopbackServer(serverUrl)) {
    return "insecure_transport";
  }
  return null;
}

// Match Python urllib.parse.urlencode's quote_plus encoding.
function encodeTicket(ticket) {
  return encodeURIComponent(ticket)
    .replace(/%20/g, "+")
    .replace(/%7E/gi, "~")
    .replace(/[!'()*]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
}

function canonicalTicketLoginUrl(serverUrl, loginPath, ticket) {
  if (typeof loginPath !== "string" || typeof ticket !== "string") return null;
  const expectedPath = `/auth/login?ticket=${encodeTicket(ticket)}`;
  if (loginPath !== expectedPath) return null;
  const loginUrl = serverRoute(serverUrl, loginPath);
  let parsed;
  let expected;
  try {
    parsed = new URL(loginUrl);
    expected = new URL(serverRoute(serverUrl, "/auth/login"));
  } catch {
    return null;
  }
  if (parsed.origin !== expected.origin || parsed.pathname !== expected.pathname) {
    return null;
  }
  return loginUrl;
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
  const serverUrlError = oidcServerUrlError(serverUrl);
  if (serverUrlError) return { ok: false, reason: serverUrlError };

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
    loginUrl = canonicalTicketLoginUrl(serverUrl, loginPath, ticket);
    if (!ticket || !loginUrl) {
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

function priorSessionCookie(cookies, serverUrl, details) {
  const hostname = new URL(serverUrl).hostname;
  return cookies.find(
    (cookie) =>
      cookie &&
      cookie.name === details.name &&
      typeof cookie.value === "string" &&
      cookie.path === details.path &&
      cookie.secure === details.secure &&
      cookie.httpOnly === true &&
      (!cookie.domain || cookie.domain === hostname),
  );
}

function sessionCookieRestoreDetails(serverUrl, cookie) {
  const details = {
    url: serverUrl,
    name: cookie.name,
    value: cookie.value,
    path: cookie.path,
    secure: cookie.secure,
    httpOnly: cookie.httpOnly,
    sameSite: cookie.sameSite,
  };
  if (typeof cookie.expirationDate === "number") {
    details.expirationDate = cookie.expirationDate;
  }
  return details;
}

function serializeCookieMutation(electronSession, serverUrl, details, mutation) {
  let queues = cookieMutationQueues.get(electronSession);
  if (!queues) {
    queues = new Map();
    cookieMutationQueues.set(electronSession, queues);
  }
  const key = `${new URL(serverUrl).hostname}\0${details.name}\0${details.path}`;
  const previous = queues.get(key) ?? Promise.resolve();
  const current = previous.catch(() => {}).then(mutation);
  queues.set(key, current);
  return current.finally(() => {
    if (queues.get(key) === current) queues.delete(key);
  });
}

function sessionCookieScope(serverUrl, details) {
  return `${new URL(serverUrl).hostname}\0${details.name}\0${details.path}`;
}

function deletePendingCookieWorkspaceOwner(electronSession, scope) {
  const pendingOwners = pendingCookieWorkspaceOwners.get(electronSession);
  if (!pendingOwners) return;
  for (const [key, pending] of pendingOwners) {
    if (pending.scope === scope) pendingOwners.delete(key);
  }
}

class SessionCookieOwnershipError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "SessionCookieOwnershipError";
    this.code = SESSION_COOKIE_OWNERSHIP_ERROR;
  }
}

function isSessionCookieOwnershipError(error) {
  return error?.code === SESSION_COOKIE_OWNERSHIP_ERROR;
}

function boundedCookieOperation(operation, timeoutMs, timeoutError) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    const timer = setTimeout(() => finish(reject, timeoutError()), timeoutMs);
    Promise.resolve()
      .then(operation)
      .then(
        (result) => finish(resolve, result),
        (error) => finish(reject, error),
      );
  });
}

function readSessionCookies(electronSession, serverUrl, details, timeoutMs) {
  return boundedCookieOperation(
    () => electronSession.cookies.get({ url: serverUrl, name: details.name }),
    timeoutMs,
    () => new SessionCookieOwnershipError("Timed out while reading the session cookie."),
  );
}

async function clearSessionCookieWorkspace(
  electronSession,
  serverUrl,
  details,
  timeoutMs = SESSION_COOKIE_READ_TIMEOUT_MS,
) {
  const scope = sessionCookieScope(serverUrl, details);
  activeCookieWorkspaceOwners.get(electronSession)?.delete(scope);
  deletePendingCookieWorkspaceOwner(electronSession, scope);
  try {
    await boundedCookieOperation(
      () => electronSession.cookies.remove(serverUrl, details.name),
      timeoutMs,
      () => new SessionCookieOwnershipError("Timed out while clearing the session cookie."),
    );
  } catch (cause) {
    throw new SessionCookieOwnershipError("Could not clear an unowned session cookie.", {
      cause,
    });
  }
}

async function readSessionCookiesFailClosed(
  electronSession,
  serverUrl,
  details,
  cookieReadTimeoutMs,
) {
  try {
    const cookies = await readSessionCookies(
      electronSession,
      serverUrl,
      details,
      cookieReadTimeoutMs,
    );
    if (!Array.isArray(cookies)) throw new Error("The cookie store returned an invalid result.");
    return cookies;
  } catch (cause) {
    let cleanupError;
    try {
      await clearSessionCookieWorkspace(electronSession, serverUrl, details, cookieReadTimeoutMs);
    } catch (error) {
      cleanupError = error;
    }
    const ownershipError = new SessionCookieOwnershipError(
      "Could not determine session cookie ownership.",
      { cause },
    );
    if (cleanupError) ownershipError.cleanupError = cleanupError;
    throw ownershipError;
  }
}

function inspectSessionCookies(cookies, serverUrl, details) {
  const namedCookies = cookies.filter((cookie) => cookie?.name === details.name);
  if (namedCookies.length === 0) return { present: false, cookie: null };
  const cookie = priorSessionCookie(namedCookies, serverUrl, details);
  return {
    present: true,
    cookie: namedCookies.length === 1 ? (cookie ?? null) : null,
  };
}

async function prepareSessionCookieWorkspace(
  electronSession,
  serverUrl,
  details,
  cookieReadTimeoutMs = SESSION_COOKIE_READ_TIMEOUT_MS,
) {
  const identity = workspaceIdentityKey(serverUrl);
  if (!identity) throw new Error("The server URL is invalid.");
  const cookies = await readSessionCookiesFailClosed(
    electronSession,
    serverUrl,
    details,
    cookieReadTimeoutMs,
  );
  const inspected = inspectSessionCookies(cookies, serverUrl, details);
  const currentCookie = inspected.cookie;
  const owners = activeCookieWorkspaceOwners.get(electronSession);
  const scope = sessionCookieScope(serverUrl, details);
  const owner = owners?.get(scope);
  if (!inspected.present) {
    owners?.delete(scope);
    return null;
  }
  if (currentCookie && owner?.identity === identity && owner.cookieValue === currentCookie.value) {
    return currentCookie.value;
  }

  // A persisted or unfamiliar cookie without matching live ownership is unsafe.
  await clearSessionCookieWorkspace(electronSession, serverUrl, details, cookieReadTimeoutMs);
  return null;
}

function rememberSessionCookieWorkspace(electronSession, serverUrl, details, cookieValue) {
  let owners = activeCookieWorkspaceOwners.get(electronSession);
  if (!owners) {
    owners = new Map();
    activeCookieWorkspaceOwners.set(electronSession, owners);
  }
  const scope = sessionCookieScope(serverUrl, details);
  owners.set(scope, {
    identity: workspaceIdentityKey(serverUrl),
    cookieValue,
  });
  deletePendingCookieWorkspaceOwner(electronSession, scope);
}

function changedCookieScope(cookie) {
  if (
    !cookie ||
    typeof cookie.domain !== "string" ||
    typeof cookie.name !== "string" ||
    typeof cookie.path !== "string"
  ) {
    return null;
  }
  const hostname = cookie.domain.replace(/^\./, "").toLowerCase();
  return `${hostname}\0${cookie.name}\0${cookie.path}`;
}

function observePendingCookieWorkspaceOwners(electronSession) {
  if (observedCookieSessions.has(electronSession)) return;
  if (typeof electronSession.cookies.on !== "function") return;
  observedCookieSessions.add(electronSession);
  electronSession.cookies.on("changed", (_event, cookie, _cause, removed) => {
    if (removed) return;
    const scope = changedCookieScope(cookie);
    if (!scope) return;
    const pendingOwners = pendingCookieWorkspaceOwners.get(electronSession);
    const pendingEntry = [...(pendingOwners?.entries() ?? [])].find(
      ([, candidate]) => candidate.scope === scope,
    );
    if (!pendingEntry) return;
    const [pendingKey, pending] = pendingEntry;
    if (Date.now() > pending.expiresAt) {
      pendingOwners.delete(pendingKey);
      return;
    }
    let currentIdentity = null;
    try {
      currentIdentity = pending.currentWorkspaceIdentity?.();
    } catch {
      // An unavailable pin cannot establish cookie ownership.
    }
    if (!pending.identity || currentIdentity !== pending.identity) {
      pendingOwners.delete(pendingKey);
      void clearSessionCookieWorkspace(electronSession, pending.serverUrl, pending.details).catch(
        () => {},
      );
      return;
    }
    const inspected = inspectSessionCookies([cookie], pending.serverUrl, pending.details);
    if (!inspected.cookie) {
      void clearSessionCookieWorkspace(electronSession, pending.serverUrl, pending.details).catch(
        () => {},
      );
      return;
    }
    rememberSessionCookieWorkspace(
      electronSession,
      pending.serverUrl,
      pending.details,
      inspected.cookie.value,
    );
  });
}

function allowPendingCookieWorkspaceOwner(
  electronSession,
  serverUrl,
  details,
  currentWorkspaceIdentity,
) {
  let pendingOwners = pendingCookieWorkspaceOwners.get(electronSession);
  if (!pendingOwners) {
    pendingOwners = new Map();
    pendingCookieWorkspaceOwners.set(electronSession, pendingOwners);
  }
  const scope = sessionCookieScope(serverUrl, details);
  const identity = workspaceIdentityKey(serverUrl);
  deletePendingCookieWorkspaceOwner(electronSession, scope);
  pendingOwners.set(`${scope}\0${identity}`, {
    scope,
    identity,
    serverUrl,
    details,
    currentWorkspaceIdentity,
    expiresAt: Date.now() + OIDC_LOGIN_TIMEOUT_MS,
  });
  observePendingCookieWorkspaceOwners(electronSession);
}

async function reverifySessionCookieWorkspace(
  electronSession,
  serverUrl,
  details,
  expectedCookieValue,
  cookieReadTimeoutMs,
) {
  const cookies = await readSessionCookiesFailClosed(
    electronSession,
    serverUrl,
    details,
    cookieReadTimeoutMs,
  );
  const inspected = inspectSessionCookies(cookies, serverUrl, details);
  if (!inspected.present && expectedCookieValue === null) return;

  const identity = workspaceIdentityKey(serverUrl);
  const owner = activeCookieWorkspaceOwners
    .get(electronSession)
    ?.get(sessionCookieScope(serverUrl, details));
  if (
    inspected.cookie &&
    inspected.cookie.value === expectedCookieValue &&
    owner?.identity === identity &&
    owner.cookieValue === expectedCookieValue
  ) {
    return;
  }
  if (inspected.present) {
    await clearSessionCookieWorkspace(electronSession, serverUrl, details, cookieReadTimeoutMs);
  } else {
    activeCookieWorkspaceOwners
      .get(electronSession)
      ?.delete(sessionCookieScope(serverUrl, details));
  }
}

async function recordCurrentSessionCookieWorkspace(
  electronSession,
  serverUrl,
  details,
  cookieReadTimeoutMs,
) {
  const cookies = await readSessionCookiesFailClosed(
    electronSession,
    serverUrl,
    details,
    cookieReadTimeoutMs,
  );
  const inspected = inspectSessionCookies(cookies, serverUrl, details);
  if (!inspected.present) {
    activeCookieWorkspaceOwners
      .get(electronSession)
      ?.delete(sessionCookieScope(serverUrl, details));
    return false;
  }
  if (!inspected.cookie) {
    await clearSessionCookieWorkspace(electronSession, serverUrl, details, cookieReadTimeoutMs);
    throw new SessionCookieOwnershipError("Chromium established an unfamiliar session cookie.");
  }
  rememberSessionCookieWorkspace(electronSession, serverUrl, details, inspected.cookie.value);
  return true;
}

async function runWithSessionCookieWorkspace(
  electronSession,
  serverUrl,
  operation,
  {
    cookieReadTimeoutMs = SESSION_COOKIE_READ_TIMEOUT_MS,
    recordCookieAfterOperation = false,
    currentWorkspaceIdentity,
  } = {},
) {
  const details = sessionCookieDetails(serverUrl, "");
  return serializeCookieMutation(electronSession, serverUrl, details, async () => {
    const expectedCookieValue = await prepareSessionCookieWorkspace(
      electronSession,
      serverUrl,
      details,
      cookieReadTimeoutMs,
    );
    await reverifySessionCookieWorkspace(
      electronSession,
      serverUrl,
      details,
      expectedCookieValue,
      cookieReadTimeoutMs,
    );
    const result = await operation();
    if (recordCookieAfterOperation) {
      const recorded = await recordCurrentSessionCookieWorkspace(
        electronSession,
        serverUrl,
        details,
        cookieReadTimeoutMs,
      );
      if (!recorded) {
        allowPendingCookieWorkspaceOwner(
          electronSession,
          serverUrl,
          details,
          currentWorkspaceIdentity,
        );
      }
    }
    return result;
  });
}

function navigateWithSessionCookieWorkspace(
  electronSession,
  serverUrl,
  navigate,
  { cookieReadTimeoutMs, recordCookieAfterNavigation = false, currentWorkspaceIdentity } = {},
) {
  const details = sessionCookieDetails(serverUrl, "");
  deletePendingCookieWorkspaceOwner(electronSession, sessionCookieScope(serverUrl, details));
  return runWithSessionCookieWorkspace(electronSession, serverUrl, navigate, {
    cookieReadTimeoutMs,
    recordCookieAfterOperation: recordCookieAfterNavigation,
    currentWorkspaceIdentity,
  });
}

async function rollbackSessionCookie(electronSession, serverUrl, details, priorCookie) {
  const current = await electronSession.cookies.get({ url: serverUrl, name: details.name });
  const installedCookie = priorSessionCookie(current, serverUrl, details);
  if (!installedCookie || installedCookie.value !== details.value) return;

  let removalError = null;
  try {
    await electronSession.cookies.remove(serverUrl, details.name);
  } catch (error) {
    removalError = error;
  }

  if (priorCookie) {
    try {
      await electronSession.cookies.set(sessionCookieRestoreDetails(serverUrl, priorCookie));
      return;
    } catch (restoreError) {
      const error = new Error("Could not restore the prior session cookie.", {
        cause: restoreError,
      });
      if (removalError) error.removalError = removalError;
      throw error;
    }
  }
  if (removalError) {
    throw new Error("Could not remove the unverified session cookie.", { cause: removalError });
  }
}

// Prove both Chromium and the server accepted the installed session.
async function installAndVerifySessionCookie(
  electronSession,
  serverUrl,
  token,
  { verificationAttempts = 3, retryDelayMs = 250, signal, assertCanCommit = () => {} } = {},
) {
  const serverUrlError = oidcServerUrlError(serverUrl);
  if (serverUrlError) {
    throw new Error(
      serverUrlError === "insecure_transport"
        ? "Remote session cookies require HTTPS."
        : "The server URL is invalid.",
    );
  }
  const details = sessionCookieDetails(serverUrl, token);
  return serializeCookieMutation(electronSession, serverUrl, details, async () => {
    deletePendingCookieWorkspaceOwner(electronSession, sessionCookieScope(serverUrl, details));
    await prepareSessionCookieWorkspace(electronSession, serverUrl, details);
    const existing = await electronSession.cookies.get({ url: serverUrl, name: details.name });
    const priorCookie = priorSessionCookie(existing, serverUrl, details);
    await electronSession.cookies.set(details);
    try {
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
        const probe = await probeServerAuth(electronSession, serverUrl, {
          signal,
          cookieWorkspacePrepared: true,
        });
        if (probe.kind === "authenticated") return;
        if (!TRANSIENT_AUTH_STATUSES.has(probe.status) || attempt >= verificationAttempts) {
          throw new Error("The server did not accept the installed session cookie.");
        }
        await delay(retryDelayMs, undefined, { signal });
        return verify(attempt + 1);
      };
      await verify(1);
      signal?.throwIfAborted();
      assertCanCommit();
      rememberSessionCookieWorkspace(electronSession, serverUrl, details, cookie.value);
    } catch (verificationError) {
      try {
        await rollbackSessionCookie(electronSession, serverUrl, details, priorCookie);
      } catch (cleanupError) {
        const error = new Error(
          "Session cookie verification failed and cleanup did not complete.",
          {
            cause: cleanupError,
          },
        );
        error.verificationError = verificationError;
        throw error;
      }
      throw verificationError;
    }
  });
}

module.exports = {
  AUTH_PROBE_TIMEOUT_MS,
  OIDC_LOGIN_TIMEOUT_MS,
  OIDC_POLL_INTERVAL_MS,
  OIDC_REQUEST_TIMEOUT_MS,
  SESSION_COOKIE_READ_TIMEOUT_MS,
  oidcServerUrlError,
  serverRoute,
  classifyAuthProbe,
  probeServerAuth,
  runOidcBrowserLogin,
  sessionCookieDetails,
  isSessionCookieOwnershipError,
  navigateWithSessionCookieWorkspace,
  installAndVerifySessionCookie,
};
