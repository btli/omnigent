const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  loadServerAfterAuth,
  loadInitialDestination,
  openServerAfterConsent,
} = require("../src/server_load");

describe("transactional server loading", () => {
  it("does not mutate settings, manifest, or page when authentication is cancelled", async () => {
    const events = [];

    const loaded = await loadServerAfterAuth({
      authenticate: async () => {
        events.push("authenticate");
        return false;
      },
      beforeLoad: () => events.push("commit-settings-and-manifest"),
      load: async () => events.push("load-new-server"),
    });

    assert.equal(loaded, false);
    assert.deepEqual(events, ["authenticate"]);
  });

  it("commits server state only after authentication and before navigation", async () => {
    const events = [];

    const loaded = await loadServerAfterAuth({
      authenticate: async () => {
        events.push("authenticate");
        return true;
      },
      beforeLoad: () => events.push("commit-settings-and-manifest"),
      load: async () => events.push("load-new-server"),
    });

    assert.equal(loaded, true);
    assert.deepEqual(events, ["authenticate", "commit-settings-and-manifest", "load-new-server"]);
  });

  it("loads setup after a cancelled cold-start login", async () => {
    const events = [];

    const loaded = await loadInitialDestination({
      loadServer: async () => {
        events.push("authenticate-saved-server");
        return false;
      },
      loadSetup: async () => events.push("load-setup"),
    });

    assert.equal(loaded, false);
    assert.deepEqual(events, ["authenticate-saved-server", "load-setup"]);
  });

  it("remembers explicit deep-link consent even when login is cancelled", async () => {
    const events = [];

    const loaded = await openServerAfterConsent({
      open: async () => {
        events.push("open-and-authenticate");
        return false;
      },
      remember: () => events.push("remember-origin-consent"),
    });

    assert.equal(loaded, false);
    assert.deepEqual(events, ["open-and-authenticate", "remember-origin-consent"]);
  });
});
