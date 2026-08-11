"use strict";

function installConditionalMediationGuard() {
  const pageGlobal = typeof window === "object" ? window : globalThis;
  const publicKeyCredential = pageGlobal.PublicKeyCredential;
  if (typeof publicKeyCredential !== "function") return;
  const credentials = pageGlobal.navigator?.credentials;
  const originalGet = credentials?.get;

  const replaceProperty = (target, property, value) => {
    try {
      Object.defineProperty(target, property, {
        configurable: true,
        enumerable: false,
        writable: true,
        value,
      });
      return true;
    } catch {
      return false;
    }
  };

  replaceProperty(publicKeyCredential, "isConditionalMediationAvailable", () =>
    Promise.resolve(false),
  );

  const originalCapabilities = publicKeyCredential.getClientCapabilities;
  if (typeof originalCapabilities === "function") {
    replaceProperty(publicKeyCredential, "getClientCapabilities", function (...args) {
      return Promise.resolve(originalCapabilities.apply(this, args)).then((capabilities) => {
        if (!capabilities || typeof capabilities !== "object") return capabilities;
        const result = { ...capabilities, conditionalGet: false };
        if (Object.hasOwn(capabilities, "conditionalCreate")) {
          result.conditionalCreate = false;
        }
        return result;
      });
    });
  }

  if (!credentials || typeof originalGet !== "function") return;
  const guardedGet = function (options) {
    if (options?.mediation !== "conditional" || options.signal?.aborted) {
      return Reflect.apply(originalGet, this, arguments);
    }

    const timeoutSignal = pageGlobal.AbortSignal.timeout(60_000);
    const signal = options.signal
      ? pageGlobal.AbortSignal.any([options.signal, timeoutSignal])
      : timeoutSignal;
    const nativeResult = Reflect.apply(originalGet, this, [{ ...options, signal }]);
    return nativeResult.catch((error) => {
      if (timeoutSignal.aborted) {
        throw new pageGlobal.DOMException(
          "The sign-in request did not finish within 60 seconds. Use another sign-in method on this page.",
          "TimeoutError",
        );
      }
      throw error;
    });
  };

  replaceProperty(credentials, "get", guardedGet);
}

if (typeof process === "object" && process.type === "renderer") {
  const { contextBridge } = require("electron");

  contextBridge.executeInMainWorld({ func: installConditionalMediationGuard });
} else if (typeof module === "object" && module.exports) {
  module.exports = { installConditionalMediationGuard };
}
