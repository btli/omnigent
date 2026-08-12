const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const preloadSource = readFileSync(path.join(__dirname, "../src/webauthn.js"), "utf8");

function makePage(timerLimit) {
  const calls = [];
  let activeConditional = false;
  let timerCalls = 0;
  let consoleWarnCalls = 0;
  const PublicKeyCredential = function PublicKeyCredential() {};
  PublicKeyCredential.isConditionalMediationAvailable = () => Promise.resolve(true);
  PublicKeyCredential.getClientCapabilities = () =>
    Promise.resolve({ conditionalGet: true, conditionalCreate: true, hybrid: true });

  const credentialsPrototype = {
    get(options) {
      calls.push({ receiver: this, options });
      if (options?.mediation !== "conditional") {
        if (activeConditional) return Promise.reject(new Error("modal request was poisoned"));
        return Promise.resolve("modal");
      }
      activeConditional = true;
      return new Promise((resolve, reject) => {
        options.signal.addEventListener(
          "abort",
          () => {
            activeConditional = false;
            reject(new DOMException("aborted", "AbortError"));
          },
          { once: true },
        );
      });
    },
  };
  const credentials = Object.create(credentialsPrototype);
  function makeSignal() {
    let aborted = false;
    let reason;
    const listeners = new Set();
    return {
      get aborted() {
        return aborted;
      },
      get reason() {
        return reason;
      },
      addEventListener(type, listener, options) {
        if (type !== "abort") return;
        if (aborted) {
          listener.call(this, { type: "abort" });
          return;
        }
        listeners.add({ listener, once: options?.once === true });
      },
      removeEventListener(type, listener) {
        if (type !== "abort") return;
        for (const entry of listeners) {
          if (entry.listener === listener) listeners.delete(entry);
        }
      },
      abort(nextReason) {
        if (aborted) return;
        aborted = true;
        reason = nextReason;
        for (const entry of [...listeners]) {
          entry.listener.call(this, { type: "abort" });
          if (entry.once) listeners.delete(entry);
        }
      },
    };
  }

  const page = {
    Object,
    PublicKeyCredential,
    navigator: { credentials },
    AbortSignal: {
      timeout(milliseconds) {
        const signal = makeSignal();
        page.setTimeout(
          () => signal.abort(new DOMException("timed out", "TimeoutError")),
          milliseconds,
        );
        return signal;
      },
      any(signals) {
        const combined = makeSignal();
        for (const signal of signals) {
          if (signal.aborted) {
            combined.abort(signal.reason);
            break;
          }
          signal.addEventListener("abort", () => combined.abort(signal.reason), { once: true });
        }
        return combined;
      },
    },
    DOMException,
    console: { warn: () => consoleWarnCalls++ },
    setTimeout: (callback, milliseconds) => {
      timerCalls += 1;
      return setTimeout(callback, Math.min(milliseconds, timerLimit ?? milliseconds));
    },
    clearTimeout,
  };
  page.window = page;
  return {
    page,
    credentials,
    credentialsPrototype,
    calls,
    get consoleWarnCalls() {
      return consoleWarnCalls;
    },
    get timerCalls() {
      return timerCalls;
    },
  };
}

function executeSerialized(script, page) {
  const context = vm.createContext({ ...page, window: page });
  const serialized = new vm.Script(
    `new Function("return (" + ${JSON.stringify(script.func.toString())} + ")")()`,
  ).runInContext(context);
  return serialized(...(script.args ?? []));
}

function loadPreload(page) {
  const injections = [];
  const contextBridge = {
    executeInMainWorld(script) {
      injections.push(script);
      executeSerialized(script, page);
    },
  };
  const preloadContext = vm.createContext({
    process: { type: "renderer" },
    require(request) {
      assert.equal(request, "electron");
      return { contextBridge };
    },
  });
  new vm.Script(preloadSource, { filename: "webauthn.js" }).runInContext(preloadContext);
  assert.equal(injections.length, 1);
  return injections[0];
}

describe("conditional WebAuthn mediation guard", () => {
  it("reports conditional mediation and creation as unavailable", async () => {
    const { page } = makePage();
    loadPreload(page);

    assert.equal(await page.PublicKeyCredential.isConditionalMediationAvailable(), false);
    const capabilities = await page.PublicKeyCredential.getClientCapabilities();
    assert.equal(capabilities.conditionalGet, false);
    assert.equal(capabilities.conditionalCreate, false);
    assert.equal(capabilities.hybrid, true);
  });

  it("passes non-conditional requests through unchanged", async () => {
    const { page, credentials, calls } = makePage();
    const originalOptions = { mediation: "required" };
    const originalPromise = Promise.resolve("modal");
    Object.getPrototypeOf(credentials).get = function () {
      calls.push({ receiver: this, args: arguments });
      return originalPromise;
    };
    const nativeGet = credentials.get;
    loadPreload(page);

    assert.notEqual(page.navigator.credentials.get, nativeGet);
    assert.equal(page.navigator.credentials.get.call(credentials), originalPromise);
    assert.equal(calls.at(-1).receiver, credentials);
    assert.equal(calls.at(-1).args.length, 0);
    assert.equal(
      page.navigator.credentials.get.call(credentials, originalOptions),
      originalPromise,
    );
    assert.equal(calls.at(-1).args.length, 1);
    assert.equal(calls.at(-1).args[0], originalOptions);
  });

  it("uses native-like descriptors and bounds a conditional request", async () => {
    const fixture = makePage(10);
    const { page, credentials } = fixture;
    loadPreload(page);

    const instanceDescriptor = Object.getOwnPropertyDescriptor(credentials, "get");
    assert.equal(instanceDescriptor.configurable, true);
    assert.equal(instanceDescriptor.writable, true);
    assert.equal(instanceDescriptor.enumerable, false);

    const error = await credentials
      .get({ mediation: "conditional" })
      .catch((requestError) => requestError);
    assert.equal(error.name, "TimeoutError");
    assert.match(error.message, /Use another sign-in method/);
    assert.doesNotMatch(error.message, /Omnigent|desktop app/);
    assert.equal(fixture.consoleWarnCalls, 0);
    assert.equal(await credentials.get({ mediation: "required" }), "modal");
  });

  it("composes an RP abort signal and preserves synchronous native throws", async () => {
    const { page, calls } = makePage(20);
    loadPreload(page);
    const controller = new AbortController();
    const request = page.navigator.credentials.get({
      mediation: "conditional",
      signal: controller.signal,
    });
    assert.notEqual(calls.at(-1).options.signal, controller.signal);
    controller.abort();
    await assert.rejects(request, { name: "AbortError" });

    const throwing = makePage(20);
    throwing.credentialsPrototype.get = () => {
      throw new TypeError("invalid request");
    };
    loadPreload(throwing.page);
    assert.throws(() => throwing.page.navigator.credentials.get({ mediation: "conditional" }), {
      name: "TypeError",
    });
  });

  it("does not add page globals while installing the compatibility shim", () => {
    const { page } = makePage();
    const before = Reflect.ownKeys(page);
    loadPreload(page);
    const beforeSet = new Set(before);
    const after = Reflect.ownKeys(page);
    assert.deepEqual(
      after.filter((key) => !beforeSet.has(key)),
      [],
    );
  });
});
