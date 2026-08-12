"use strict";

const MODAL_WEBAUTHN_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * The main window only needs the escape affordance on first-party sign-in
 * documents. OIDC itself is intercepted before this page loads; `/login`
 * remains the accounts-mode fallback. OAuth children are scoped separately.
 *
 * @param {string} pageUrl
 * @param {string | null | undefined} serverUrl
 * @returns {boolean}
 */
function isWebAuthnEscapePage(pageUrl, serverUrl) {
  if (!serverUrl) return false;
  try {
    const page = new URL(pageUrl);
    const server = new URL(serverUrl);
    const basePath = server.pathname.replace(/\/+$/, "");
    return (
      page.origin === server.origin &&
      (page.pathname === `${basePath}/login` || page.pathname === `${basePath}/auth/login`)
    );
  } catch {
    return false;
  }
}

/**
 * Build the page-world guard. It adds no globals or bridge strings: the only
 * main-process signal is the Promise returned by executeJavaScript itself.
 * Conditional mediation and allow-listed USB/security-key ceremonies are
 * untouched. Discoverable modal requests keep their original Promise too: the
 * timer only surfaces a browser escape and never ends a slow hardware-key use.
 *
 * @param {number} timeoutMs
 * @returns {string}
 */
function webAuthnTimeoutScript(timeoutMs) {
  const delay = Math.max(1, Number(timeoutMs) || MODAL_WEBAUTHN_TIMEOUT_MS);
  return `(() => {
    const credentials = navigator.credentials;
    if (!credentials || typeof credentials.get !== "function") return Promise.resolve(null);
    const originalGet = credentials.get.bind(credentials);
    let reportTimeout;
    const timeoutReport = new Promise((resolve) => { reportTimeout = resolve; });
    try {
      Object.defineProperty(credentials, "get", {
        configurable: true,
        writable: true,
        value: function (options) {
          const allowCredentials = options && options.publicKey
            ? options.publicKey.allowCredentials
            : null;
          const hasAllowList = Array.isArray(allowCredentials) && allowCredentials.length > 0;
          const isModalPublicKey = Boolean(
            options && options.publicKey && options.mediation !== "conditional" && !hasAllowList
          );
          if (!isModalPublicKey) return originalGet(options);
          const request = originalGet(options);
          const timer = setTimeout(() => reportTimeout({ timedOut: true }), ${delay});
          Promise.resolve(request).then(
            () => clearTimeout(timer),
            () => clearTimeout(timer)
          );
          return request;
        },
      });
    } catch {
      return Promise.resolve(null);
    }
    return timeoutReport;
  })()`;
}

/**
 * Arm the guard after every main-frame load. A navigation tears down the old
 * page-world Promise; its rejection is intentionally ignored and the next page
 * gets a fresh guard.
 *
 * @param {Electron.WebContents} webContents
 * @param {{ timeoutMs?: number, shouldInject?: () => boolean, onTimeout: () => void }} opts
 */
function registerWebAuthnTimeout(
  webContents,
  { timeoutMs = MODAL_WEBAUTHN_TIMEOUT_MS, shouldInject = () => true, onTimeout },
) {
  webContents.on("did-finish-load", () => {
    if (!shouldInject()) return;
    void webContents
      .executeJavaScript(webAuthnTimeoutScript(timeoutMs), true)
      .then((result) => {
        if (result?.timedOut === true) onTimeout();
      })
      .catch(() => {
        // Navigation/destroyed context, or a page where injection is unavailable.
      });
  });
}

module.exports = {
  MODAL_WEBAUTHN_TIMEOUT_MS,
  isWebAuthnEscapePage,
  webAuthnTimeoutScript,
  registerWebAuthnTimeout,
};
