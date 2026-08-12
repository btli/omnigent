"use strict";

/**
 * Authenticate before committing any server-switch state or navigation.
 *
 * @param {{
 *   authenticate: () => Promise<boolean>,
 *   beforeLoad?: () => void | Promise<void>,
 *   load: () => Promise<void>,
 * }} params
 * @returns {Promise<boolean>}
 */
async function loadServerAfterAuth({ authenticate, beforeLoad, load }) {
  if (!(await authenticate())) return false;
  await beforeLoad?.();
  await load();
  return true;
}

/**
 * A cancelled cold-start login must land on setup instead of about:blank.
 *
 * @param {{ loadServer: () => Promise<boolean>, loadSetup: () => Promise<void> }} params
 * @returns {Promise<boolean>}
 */
async function loadInitialDestination({ loadServer, loadSetup }) {
  const loaded = await loadServer();
  if (!loaded) await loadSetup();
  return loaded;
}

/**
 * Persist an explicit deep-link origin consent after the open attempt. This is
 * intentionally independent of login success: it records trust in the origin,
 * not a completed authentication or a new launch default.
 *
 * @param {{ open: () => Promise<unknown>, remember: () => void | Promise<void> }} params
 * @returns {Promise<unknown>}
 */
async function openServerAfterConsent({ open, remember }) {
  const result = await open();
  await remember();
  return result;
}

module.exports = { loadServerAfterAuth, loadInitialDestination, openServerAfterConsent };
