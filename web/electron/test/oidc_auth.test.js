const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  serverRoute,
  classifyAuthProbe,
  probeServerAuth,
  runOidcBrowserLogin,
  sessionCookieDetails,
  installAndVerifySessionCookie,
} = require("../src/oidc_auth");

function response(status, body = null) {
  return {
    status,
    json: async () => {
      if (body === null) throw new Error("not json");
      return body;
    },
  };
}

describe("OIDC provider detection", () => {
  it("joins /v1/me under a mounted server URL", () => {
    assert.equal(
      serverRoute("https://workspace.example/ml/omnigents/", "/v1/me"),
      "https://workspace.example/ml/omnigents/v1/me",
    );
  });

  it("gates only the OIDC login_url", () => {
    assert.equal(classifyAuthProbe(200, null), "authenticated");
    assert.equal(classifyAuthProbe(401, "/auth/login"), "oidc");
    assert.equal(classifyAuthProbe(401, "/login"), "accounts");
    assert.equal(classifyAuthProbe(401, null), "other");
    assert.equal(classifyAuthProbe(302, null), "other");
  });

  it("uses the Electron session and leaves redirects manual", async () => {
    const calls = [];
    const electronSession = {
      fetch: async (url, init) => {
        calls.push({ url, init });
        return response(401, { login_url: "/auth/login" });
      },
    };

    const result = await probeServerAuth(electronSession, "https://server.example/base");

    assert.deepEqual(result, { kind: "oidc", status: 401 });
    assert.equal(calls[0].url, "https://server.example/base/v1/me");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[0].init.credentials, "include");
  });
});

describe("OIDC browser ticket flow", () => {
  it("requests a ticket, opens the pinned-server URL, and polls to completion", async () => {
    const calls = [];
    const responses = [
      response(200, { ticket: "one-time", login_url: "/auth/login?ticket=one-time" }),
      response(202, { status: "pending" }),
      response(200, { token: "session-jwt", user_id: "user@example.com", expires_in: 60 }),
    ];
    const electronSession = {
      fetch: async (url, init) => {
        calls.push({ url, init });
        return responses.shift();
      },
    };
    const opened = [];

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example/base",
      async (url) => opened.push(url),
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, {
      ok: true,
      token: "session-jwt",
    });
    assert.deepEqual(opened, ["https://server.example/base/auth/login?ticket=one-time"]);
    assert.equal(calls[0].url, "https://server.example/base/auth/cli-login");
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(calls[1].url, "https://server.example/base/auth/cli-poll?ticket=one-time");
    assert.equal(calls[2].url, calls[1].url);
  });

  it("rejects a server-supplied external verification URL", async () => {
    const electronSession = {
      fetch: async () =>
        response(200, { ticket: "secret", login_url: "https://attacker.example/steal" }),
    };
    let opened = false;

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {
        opened = true;
      },
    );

    assert.deepEqual(result, { ok: false, reason: "failed" });
    assert.equal(opened, false);
  });

  it("cancels polling without redeeming the ticket", async () => {
    const controller = new AbortController();
    let fetches = 0;
    const electronSession = {
      fetch: async () => {
        fetches += 1;
        return response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" });
      },
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => controller.abort(),
      { signal: controller.signal, pollIntervalMs: 1 },
    );

    assert.deepEqual(result, { ok: false, reason: "cancelled" });
    assert.equal(fetches, 1);
  });

  it("surfaces an expired single-use ticket", async () => {
    const responses = [
      response(200, { ticket: "expired", login_url: "/auth/login?ticket=expired" }),
      response(410, { error: "Ticket expired" }),
    ];
    const electronSession = { fetch: async () => responses.shift() };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      { pollIntervalMs: 1, timeoutMs: 100 },
    );

    assert.deepEqual(result, { ok: false, reason: "expired" });
  });

  it("reports transient poll failures while continuing to completion", async () => {
    const responses = [
      response(200, { ticket: "secret", login_url: "/auth/login?ticket=secret" }),
      new Error("offline"),
      response(202, { status: "pending" }),
      response(200, { token: "session-jwt", user_id: "user@example.com", expires_in: 60 }),
    ];
    const electronSession = {
      fetch: async () => {
        const next = responses.shift();
        if (next instanceof Error) throw next;
        return next;
      },
    };
    let pollErrors = 0;

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      {
        pollIntervalMs: 1,
        timeoutMs: 100,
        onPollError: () => {
          pollErrors += 1;
        },
      },
    );

    assert.equal(result.ok, true);
    assert.equal(pollErrors, 1);
  });

  it("bounds a pending ticket without relying on a CLI subprocess timeout", async () => {
    let fetches = 0;
    const electronSession = {
      fetch: async () => {
        fetches += 1;
        return response(200, { ticket: "pending", login_url: "/auth/login?ticket=pending" });
      },
    };

    const result = await runOidcBrowserLogin(
      electronSession,
      "https://server.example",
      async () => {},
      { pollIntervalMs: 10, timeoutMs: 5 },
    );

    assert.deepEqual(result, { ok: false, reason: "timed_out" });
    assert.equal(fetches, 1);
  });
});

describe("OIDC session cookie installation", () => {
  it("builds a valid __Host- cookie on HTTPS with no Domain", () => {
    const details = sessionCookieDetails("https://server.example", "token");
    assert.deepEqual(details, {
      url: "https://server.example",
      name: "__Host-ap_session",
      value: "token",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
    });
    assert.equal(Object.hasOwn(details, "domain"), false);
  });

  it("uses the non-Host cookie on HTTP", () => {
    assert.equal(sessionCookieDetails("http://remote.example", "token").name, "ap_session");
    assert.equal(sessionCookieDetails("http://remote.example", "token").secure, false);
  });

  it("proves Chromium accepted the cookie and the server accepted the session", async () => {
    let stored = null;
    const electronSession = {
      cookies: {
        set: async (details) => {
          stored = { ...details };
        },
        get: async () => [stored],
      },
      fetch: async () => response(200),
    };

    const result = await installAndVerifySessionCookie(
      electronSession,
      "https://server.example",
      "session-jwt",
    );

    assert.equal(result, undefined);
    assert.equal(stored.value, "session-jwt");
  });

  it("fails loudly when Electron silently rejects the __Host- cookie", async () => {
    const electronSession = {
      cookies: {
        set: async () => {},
        get: async () => [],
      },
      fetch: async () => response(200),
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "session-jwt"),
      /rejected the session cookie/,
    );
  });

  it("fails when the server still reports an unauthenticated OIDC session", async () => {
    let stored = null;
    const electronSession = {
      cookies: {
        set: async (details) => {
          stored = { ...details };
        },
        get: async () => [stored],
      },
      fetch: async () => response(401, { login_url: "/auth/login" }),
    };

    await assert.rejects(
      installAndVerifySessionCookie(electronSession, "https://server.example", "session-jwt"),
      /did not accept/,
    );
  });
});
